"""Pictures that are there more than once.

Two files can hold the very same picture data – the index already knows that, they share a hash.
More often a picture was saved again: smaller, with another quality, by another programme. Those
copies differ in every byte and still show the same thing. To find them each picture gets a short
description of how it looks (which neighbouring spot is brighter, on a coarse grid); copies agree
in nearly all of it, different pictures in about half.

What is worked out here lies in a file of its own and may be thrown away. What somebody decided –
"these two are not the same" – lies in another one and is kept.
"""

from __future__ import annotations

import io
import sqlite3
import threading
import time
from pathlib import Path

from PIL import Image, ImageOps

from .index import Index
from .media import cache_path

GRID = 16               # the grid the picture is laid on: 16×16 comparisons across and as many down
BITS = 2 * GRID * GRID
COPY = 0.025            # share of the comparisons in which a copy (another size, another quality) may differ
ALIKE = 0.065           # … and two shots of the same thing: an HDR pair, two in a row. Beyond it: other pictures
LEVELS = {"copies": COPY, "alike": ALIKE}
TONE = 14               # … and how far their brightness may lie apart, spot by spot (0–255)
RATIO = 0.03            # … and their shape
DULL = 0.04             # a picture with hardly any structure (a blank page) says too little to be compared
ENGINE = "grid16/1"     # how the descriptions were made; a change makes them anew

SCHEMA = """
CREATE TABLE IF NOT EXISTS looks (
    hash TEXT PRIMARY KEY,           -- the picture hash the index uses
    engine TEXT NOT NULL,
    bits BLOB,                       -- NULL = could not be read
    tone BLOB,
    ratio REAL NOT NULL DEFAULT 0
);
"""
APART = """
CREATE TABLE IF NOT EXISTS apart (
    a TEXT NOT NULL, b TEXT NOT NULL,   -- two picture hashes, a < b
    user TEXT NOT NULL DEFAULT '',
    ts REAL NOT NULL,
    PRIMARY KEY (a, b)
);
"""


def look(image: Image.Image) -> tuple[bytes, bytes, float]:
    """How an upright picture looks: the comparisons, a 4×4 patch of brightness, width by height."""
    import numpy

    ratio = image.width / max(1, image.height)
    grey = image.convert("L")
    across = numpy.asarray(grey.resize((GRID + 1, GRID), Image.LANCZOS), dtype="int16")
    down = numpy.asarray(grey.resize((GRID, GRID + 1), Image.LANCZOS), dtype="int16")
    bits = numpy.concatenate([(across[:, 1:] > across[:, :-1]).ravel(), (down[1:, :] > down[:-1, :]).ravel()])
    tone = numpy.asarray(grey.resize((4, 4), Image.BOX), dtype="uint8")
    return numpy.packbits(bits).tobytes(), tone.tobytes(), ratio


class Dupes:
    def __init__(self, db_path: Path, apart_path: Path, root: Path, thumbs: Path, index: Index):
        self.db_path = db_path
        self.apart_path = apart_path
        self.root = root
        self.thumbs = thumbs
        self.index = index
        self._lock = threading.Lock()
        self._changes = 0
        self._worked: tuple | None = None
        self._pairs_kept: tuple | None = None
        self.progress = {"running": False, "done": 0, "total": 0, "last": 0.0}
        for path, schema in ((db_path, SCHEMA), (apart_path, APART)):
            path.parent.mkdir(parents=True, exist_ok=True)
            with self._connect(path) as db:
                db.executescript(schema)
        with self._connect(db_path) as db:
            db.execute("DELETE FROM looks WHERE engine != ?", (ENGINE,))

    @staticmethod
    def _connect(path: Path) -> sqlite3.Connection:
        db = sqlite3.connect(path, timeout=30)
        db.row_factory = sqlite3.Row
        return db

    def _all(self, sql: str, params=(), path: Path | None = None) -> list[sqlite3.Row]:
        db = self._connect(path or self.db_path)
        try:
            return db.execute(sql, params).fetchall()
        finally:
            db.close()

    # ------------------------------------------------------------ looking at the pictures

    def pending(self, areas: list[str]) -> list[dict]:
        seen = {row["hash"] for row in self._all("SELECT hash FROM looks")}
        todo, taken = [], set()
        for row in self.index.pictures(areas):
            if row["hash"] not in seen and row["hash"] not in taken:
                taken.add(row["hash"])
                todo.append(row)
        return todo

    def _see(self, row: dict) -> tuple[bytes, bytes, float]:
        """The small picture kept for the overview will do, it is upright already; else the file itself."""
        small = cache_path(self.thumbs, row["hash"], row["orientation"] or 1)
        if small.is_file():
            with Image.open(small) as image:
                return look(image)
        with Image.open(io.BytesIO((self.root / row["path"]).read_bytes())) as image:
            if image.format == "JPEG":
                image.draft("RGB", (320, 320))
            return look(ImageOps.exif_transpose(image))

    def search(self, areas: list[str], stop=None) -> int:
        """Describe every picture not described yet. Only reads."""
        if not self._lock.acquire(blocking=False):
            return 0
        looked = 0
        try:
            todo = self.pending(areas)
            self.progress.update(running=bool(todo), done=0, total=len(todo))
            db = self._connect(self.db_path)
            try:
                for row in todo:
                    if stop is not None and stop():
                        break
                    try:
                        bits, tone, ratio = self._see(row)
                    except Exception:           # a picture that cannot be read is not looked at again
                        bits, tone, ratio = None, None, 0.0
                    db.execute("INSERT OR REPLACE INTO looks VALUES (?, ?, ?, ?, ?)", (row["hash"], ENGINE, bits, tone, ratio))
                    looked += 1
                    self.progress["done"] = looked
                    if looked % 200 == 0:
                        db.commit()
                db.commit()
            finally:
                db.close()
            self._changes += 1
            return looked
        finally:
            self.progress.update(running=False, last=time.time())
            self._lock.release()

    def state(self) -> dict:
        row = self._all("SELECT COUNT(*) n, COUNT(bits) ok FROM looks")[0]
        return {"pictures": row["n"], "unreadable": row["n"] - row["ok"], "search": dict(self.progress)}

    # ------------------------------------------------------------ what is there twice

    def _pairs(self, hashes: list[str], bits, tone, ratio) -> list[tuple[int, int, float]]:
        import numpy

        words = bits.view("uint64")                                  # 512 comparisons = 8 words a picture
        set_bits = numpy.bitwise_count(words).sum(axis=1) / BITS
        lively = (set_bits > DULL) & (set_bits < 1 - DULL)
        tones = tone.astype("int16")
        found = []
        for start in range(0, len(hashes), 256):
            block = words[start:start + 256]
            far = numpy.bitwise_count(block[:, None, :] ^ words[None, :, :]).sum(axis=2)
            rows, cols = numpy.nonzero(far <= ALIKE * BITS)
            for row, col in zip(rows + start, cols):
                if col <= row or not (lively[row] and lively[col]):
                    continue
                if abs(ratio[row] - ratio[col]) > RATIO * max(ratio[row], ratio[col]):
                    continue
                if numpy.abs(tones[row] - tones[col]).mean() > TONE:
                    continue
                found.append((int(row), int(col), float(far[row - start, col]) / BITS))
        return found

    def _near(self) -> list[tuple[str, str, float]]:
        """Every two described pictures that are alike, by hash (a < b) – the costly part, so it is
        kept until a description is added; which files there are does not matter to it."""
        import numpy

        if self._pairs_kept and self._pairs_kept[0] == self._changes:
            return self._pairs_kept[1]
        looks = self._all("SELECT hash, bits, tone, ratio FROM looks WHERE bits IS NOT NULL ORDER BY hash")
        found = []
        if looks:
            hashes = [row["hash"] for row in looks]
            bits = numpy.frombuffer(b"".join(row["bits"] for row in looks), dtype="uint8").reshape(len(looks), BITS // 8)
            tone = numpy.frombuffer(b"".join(row["tone"] for row in looks), dtype="uint8").reshape(len(looks), 16)
            ratio = numpy.array([row["ratio"] for row in looks])
            found = [(hashes[a], hashes[b], distance) for a, b, distance in self._pairs(hashes, bits, tone, ratio)]
        self._pairs_kept = (self._changes, found)
        return found

    def groups(self, areas: list[str], level: str = "copies") -> list[dict]:
        """The pictures that are there more than once, bundle by bundle, the files of each with what
        tells them apart. `level` says how alike they must be: "copies" – the same picture saved
        again – or "alike", which also takes shots of the same thing. Worked out once and kept
        until a photo or a description changes."""
        import numpy

        near = LEVELS.get(level, COPY)
        stamp = (self._changes, self.index.changes, tuple(areas), near)
        if self._worked and self._worked[0] == stamp:
            return self._worked[1]
        files: dict[str, list[dict]] = {}
        for row in self.index.files(areas):
            files.setdefault(row["hash"], []).append(row)
        apart = {(row["a"], row["b"]) for row in self._all("SELECT a, b FROM apart", path=self.apart_path)}
        hashes = sorted(files)
        place = {digest: at for at, digest in enumerate(hashes)}
        parent = list(range(len(hashes)))

        def top(at: int) -> int:
            while parent[at] != at:
                parent[at] = parent[parent[at]]
                at = parent[at]
            return at

        far: dict[tuple[str, str], float] = {}
        for a, b, distance in self._near():
            if distance > near or a not in place or b not in place or (a, b) in apart:
                continue
            far[(a, b)] = distance
            parent[top(place[a])] = top(place[b])
        bundles: dict[int, list[str]] = {}
        for at, digest in enumerate(hashes):
            bundles.setdefault(top(at), []).append(digest)
        widest: dict[int, float] = {}
        for (a, _), distance in far.items():
            root = top(place[a])
            widest[root] = max(widest.get(root, 0.0), distance)
        result = []
        for root, members in bundles.items():
            rows = [row for digest in members for row in files[digest]]
            if len(rows) < 2:
                continue
            rows.sort(key=lambda row: (-(row["width"] or 0) * (row["height"] or 0), -(row["size"] or 0),
                                       -(len(row["people"]) + len(row["albums"]) + len(row["keywords"])), row["path"]))
            exact = len(members) == 1
            # Sure enough to clear away unseen: the very same data, or every picture another size (saved
            # again, smaller). Two of one size may be two shots from a tripod – the coarse grid does not
            # see a blink –, so those somebody should look at.
            sizes = {(row["width"], row["height"]) for row in rows}
            sure = exact or len(sizes) == len(members)
            result.append({"id": min(members), "exact": exact, "distance": round(widest.get(root, 0.0), 4), "hashes": sorted(members),
                           "sure": sure, "files": rows})
        result.sort(key=lambda bundle: (bundle["files"][0]["taken"] or "", bundle["id"]), reverse=True)
        self._worked = (stamp, result)
        return result

    def keep_apart(self, hashes: list[str], user: str = "") -> int:
        """These pictures are not the same – never bundle them again."""
        hashes = sorted(set(hashes))
        with self._connect(self.apart_path) as db:
            for at, a in enumerate(hashes):
                for b in hashes[at + 1:]:
                    db.execute("INSERT OR REPLACE INTO apart VALUES (?, ?, ?, ?)", (a, b, user, time.time()))
        self._worked = None         # the bundles are made anew; which pictures are alike has not changed
        return len(hashes)
