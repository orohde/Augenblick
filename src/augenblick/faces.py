"""Faces: finding them, telling them apart, grouping the ones without a name.

Who is on a photo stands in the photo itself, as a named region – the editor writes it there
and the index reads it back. What is kept here can be worked out again at any time: where the
faces are and one vector per face that says how alike two faces look. So it lives in a file of
its own that may be thrown away. Nothing in this module ever writes to a photo.

The models come from the OpenCV Zoo and run on the processor, without a graphics card and
without any service outside: YuNet finds the faces (MIT licence), SFace compares them
(Apache 2.0).
"""

from __future__ import annotations

import hashlib
import io
import logging
import os
import shutil
import sqlite3
import sys
import threading
import time
import urllib.request
from pathlib import Path

from PIL import Image, ImageOps

from .edit import INVERSE, SAME_FACE, _overlap
from .index import Index
from .media import cache_path, image_hash, is_face, store, turn_box

log = logging.getLogger("augenblick.faces")

_ZOO = "https://github.com/opencv/opencv_zoo/raw/48b10fa505aeb51fbd127fc60a5c660040e823b7/models/"
MODELS = {      # file name -> where it comes from, and the SHA-256 it must have
    "face_detection_yunet_2023mar.onnx": (
        _ZOO + "face_detection_yunet/face_detection_yunet_2023mar.onnx",
        "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4"),
    "face_recognition_sface_2021dec.onnx": (
        _ZOO + "face_recognition_sface/face_recognition_sface_2021dec.onnx",
        "0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79"),
}
ENGINE = "yunet-2023mar+sface-2021dec/1"   # what the stored faces were found with; a change finds them anew

EDGE = 1600             # the picture is looked at with its longer side scaled down to this
MIN_SCORE = 0.75        # how sure the finder must be that this is a face
MIN_FACE = 32           # px at that size: smaller faces are found, but too coarse to tell apart
FACE_THUMB = 160        # px, the small cut-out of a face
ALIKE = 0.6             # cosine similarity from which unnamed faces fall into one group
KNOWN = 0.5             # … from which an unnamed face joins the bundle of a named one
MAYBE = 0.4             # … from which a face that stands alone is laid beside a named one, as a guess to look at
TWIN = 0.95             # … and from which it is the same face in a second copy of the picture
SUGGEST = 0.45          # … and from which a named face lends its name as a proposal
VECTOR = 128            # numbers per face


def fetch_models(target: Path) -> None:
    """Download the two model files and check them against the hashes above."""
    target.mkdir(parents=True, exist_ok=True)
    for name, (url, digest) in MODELS.items():
        path = target / name
        if path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == digest:
            continue
        with urllib.request.urlopen(url, timeout=120) as answer:
            data = answer.read()
        if hashlib.sha256(data).hexdigest() != digest:
            raise RuntimeError(f"{name}: the download is not the expected file")
        temp = path.with_suffix(".tmp")
        temp.write_bytes(data)
        temp.replace(path)


class Engine:
    """Finds the faces in one picture and gives each a vector. Loaded on first use."""

    def __init__(self, models: Path):
        self.models = models
        self._lock = threading.Lock()      # the networks are not made for several threads at once
        self._nets = None

    @property
    def missing(self) -> str:
        """Why faces cannot be found here – empty if they can."""
        try:
            import cv2  # noqa: F401
            import numpy  # noqa: F401
        except ImportError:
            return "opencv is not installed"
        if not all((self.models / name).is_file() for name in MODELS):
            return "the face models are missing"
        return ""

    def _load(self):
        if self._nets is None:
            import cv2
            cv2.setNumThreads(1)           # one core at most: this runs beside everything else
            finder = cv2.FaceDetectorYN.create(str(self.models / "face_detection_yunet_2023mar.onnx"), "",
                                               (320, 320), MIN_SCORE, 0.3, 5000)
            teller = cv2.FaceRecognizerSF.create(str(self.models / "face_recognition_sface_2021dec.onnx"), "")
            self._nets = (finder, teller)
        return self._nets

    def find(self, data: bytes) -> list[dict]:
        """The faces in a picture file: the box in the stored picture (centre, width, height as
        fractions – the way regions are written), how sure the finder is, the vector (None for
        faces too small to compare) and a small cut-out."""
        import numpy

        image = Image.open(io.BytesIO(data))
        orientation = 1
        try:
            found = image.getexif().get(0x0112)
            orientation = found if isinstance(found, int) and 1 <= found <= 8 else 1
        except Exception:
            pass
        if image.format == "JPEG":
            image.draft("RGB", (EDGE, EDGE))
        image = ImageOps.exif_transpose(image)      # faces are looked for the way a person sees the picture
        if image.mode != "RGB":
            image = image.convert("RGB")
        image.thumbnail((EDGE, EDGE), Image.BILINEAR)
        width, height = image.size
        pixels = numpy.ascontiguousarray(numpy.asarray(image)[:, :, ::-1])      # OpenCV counts blue first
        faces: list[dict] = []
        with self._lock:
            finder, teller = self._load()
            finder.setInputSize((width, height))
            _, rows = finder.detect(pixels)
            for row in rows if rows is not None else []:
                left, top, wide, high = (float(value) for value in row[:4])
                left, top = max(0.0, left), max(0.0, top)
                wide, high = min(wide, width - left), min(high, height - top)
                if wide < 4 or high < 4:
                    continue
                vector = None
                if min(wide, high) >= MIN_FACE:
                    vector = teller.feature(teller.alignCrop(pixels, row)).flatten().astype("float32")
                    length = float(numpy.linalg.norm(vector))
                    vector = (vector / length).tobytes() if length > 0 else None
                seen = ((left + wide / 2) / width, (top + high / 2) / height, wide / width, high / height)
                x, y, w, h = turn_box(seen, INVERSE[orientation])
                faces.append({"x": x, "y": y, "w": w, "h": h, "score": float(row[14]), "vector": vector,
                              "thumb": _cut_out(image, left, top, wide, high)})
        return faces


def _cut_out(image: Image.Image, left: float, top: float, wide: float, high: float) -> bytes:
    """A square around the face with some room, as a small JPEG."""
    half = max(wide, high) * 0.75
    cx, cy = left + wide / 2, top + high / 2
    box = (max(0, int(cx - half)), max(0, int(cy - half)),
           min(image.width, int(cx + half)), min(image.height, int(cy + half)))
    face = image.crop(box)
    face.thumbnail((FACE_THUMB, FACE_THUMB), Image.LANCZOS)
    out = io.BytesIO()
    face.save(out, "JPEG", quality=80)
    return out.getvalue()


SCHEMA = """
CREATE TABLE IF NOT EXISTS pictures (
    hash TEXT PRIMARY KEY,           -- the picture hash the index uses
    engine TEXT NOT NULL,            -- what looked at it
    found INTEGER NOT NULL,
    error TEXT NOT NULL DEFAULT '',
    ts REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS faces (
    id INTEGER PRIMARY KEY,
    hash TEXT NOT NULL,
    x REAL NOT NULL, y REAL NOT NULL, w REAL NOT NULL, h REAL NOT NULL,     -- in the stored picture
    score REAL NOT NULL,
    vector BLOB                      -- 128 float32, length 1; NULL = too small to compare
);
CREATE INDEX IF NOT EXISTS faces_hash ON faces (hash);
"""

IGNORED = """
CREATE TABLE IF NOT EXISTS ignored (
    hash TEXT NOT NULL,
    x REAL NOT NULL, y REAL NOT NULL, w REAL NOT NULL, h REAL NOT NULL,     -- in the stored picture
    user TEXT NOT NULL DEFAULT '',
    ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ignored_hash ON ignored (hash);
CREATE TABLE IF NOT EXISTS judged (          -- what somebody (an AI, asked and paid for) said: is this a face at all?
    hash TEXT NOT NULL,
    x REAL NOT NULL, y REAL NOT NULL, w REAL NOT NULL, h REAL NOT NULL,
    face INTEGER NOT NULL,
    ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS judged_hash ON judged (hash);
"""


class People:
    """What is known about faces: the store that can be thrown away, the search through the
    photos in the background, and the questions the pages ask."""

    def __init__(self, db_path: Path, thumbs: Path, root: Path, index: Index, engine, load: float = 0.5,
                 ignored_path: Path | None = None):
        self.db_path = db_path
        self.ignored_path = ignored_path or db_path.with_name("faces-ignored.db")
        self.thumbs = thumbs
        self.root = root
        self.index = index
        self.engine = engine
        self.load = min(1.0, max(0.05, load))      # share of one core the search may take
        self._write = threading.Lock()
        self._pass = threading.Lock()
        self._think = threading.Lock()
        self._worked: tuple | None = None           # (what it was worked out from, the result)
        self._changes = 0
        self.progress = {"running": False, "done": 0, "total": 0, "last": 0.0}
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript(SCHEMA)
        with self._connect(self.ignored_path) as db:
            db.executescript(IGNORED)

    def _connect(self, path: Path | None = None) -> sqlite3.Connection:
        db = sqlite3.connect(path or self.db_path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        return db

    def _all(self, sql: str, params=(), path: Path | None = None) -> list[sqlite3.Row]:
        db = self._connect(path)
        try:
            return db.execute(sql, params).fetchall()
        finally:
            db.close()

    # ------------------------------------------------------------ searching

    def pending(self, areas: list[str] | None = None) -> list[dict]:
        """The pictures nobody has looked at yet with the present models – in these areas, or everywhere."""
        done = {row["hash"] for row in self._all("SELECT hash FROM pictures WHERE engine = ?", (ENGINE,))}
        todo, seen = [], set()
        for row in self.index.pictures(areas):
            if row["hash"] not in done and row["hash"] not in seen:
                seen.add(row["hash"])
                todo.append(row)
        return todo

    def search(self, stop: threading.Event | None = None, wanted=lambda: None) -> dict:
        """Look at every picture not yet looked at. Reads only; can be broken off and taken up again
        at any time, also after a restart – what is done is done. `wanted` says in which areas to
        look (None: everywhere); when its answer changes, the search stops to be started anew."""
        if self.engine.missing:
            return {"missing": self.engine.missing}
        if not self._pass.acquire(blocking=False):
            return {"busy": True}
        looked = 0
        try:
            areas = wanted()
            todo = self.pending(areas)
            self.progress.update(running=bool(todo), done=0, total=len(todo))
            for row in todo:
                if (stop is not None and stop.is_set()) or wanted() != areas:
                    break
                started = time.monotonic()
                self.look(row["path"])
                looked += 1
                self.progress["done"] += 1
                if stop is not None:        # leave the machine to the others for a while
                    stop.wait(min(5.0, (time.monotonic() - started) * (1 / self.load - 1)))
            return {"looked": looked, "left": len(todo) - looked}
        finally:
            self.progress.update(running=False, last=time.time())
            self._pass.release()

    def look(self, path: str) -> int:
        """Find the faces in one photo and remember them under the hash of its picture."""
        digest, faces, error = "", [], ""
        try:
            data = (self.root / path).read_bytes()
            ext = path.rsplit(".", 1)[-1].lower()
            digest = image_hash(data, ext)
            faces = self.engine.find(data)
        except OSError as exc:
            log.warning("cannot read %s: %s", path, exc)
            return 0
        except Exception as exc:            # a picture the finder chokes on is not asked again and again
            error = f"{type(exc).__name__}: {exc}"[:300]
            if not digest:
                return 0
        for face in faces:
            if face.get("thumb"):
                store(self._thumb(digest, face), face["thumb"])
        db = self._connect()
        try:
            with self._write:
                db.execute("DELETE FROM faces WHERE hash = ?", (digest,))
                db.execute("INSERT OR REPLACE INTO pictures VALUES (?, ?, ?, ?, ?)",
                           (digest, ENGINE, len(faces), error, time.time()))
                db.executemany("INSERT INTO faces (hash, x, y, w, h, score, vector) VALUES (?, ?, ?, ?, ?, ?, ?)",
                               [(digest, f["x"], f["y"], f["w"], f["h"], f["score"], f.get("vector")) for f in faces])
                db.commit()
                self._changes += 1
        finally:
            db.close()
        return len(faces)

    def follow(self, old: str, new: str, still: bool = False) -> int:
        """The same picture under another hash: words were written into a file whose hash covers all
        of it (a PNG). The faces found in it are still the faces in it – they go along instead of
        being lost until the search comes by again. `still`: another file keeps the old hash."""
        db = self._connect()
        try:
            with self._write:
                seen = db.execute("SELECT engine, found, error, ts FROM pictures WHERE hash = ?", (old,)).fetchone()
                if not seen or db.execute("SELECT 1 FROM pictures WHERE hash = ?", (new,)).fetchone():
                    return 0
                rows = db.execute("SELECT x, y, w, h FROM faces WHERE hash = ?", (old,)).fetchall()
                if still:
                    db.execute("INSERT INTO pictures VALUES (?, ?, ?, ?, ?)", (new, *seen))
                    db.execute("INSERT INTO faces (hash, x, y, w, h, score, vector) SELECT ?, x, y, w, h, score, vector "
                               "FROM faces WHERE hash = ? ORDER BY id", (new, old))
                else:               # the faces keep their numbers: what is on the screen stays true
                    db.execute("UPDATE pictures SET hash = ? WHERE hash = ?", (new, old))
                    db.execute("UPDATE faces SET hash = ? WHERE hash = ?", (new, old))
                db.commit()
                self._changes += 1
        finally:
            db.close()
        for face in rows:           # the small cut-outs are named after the hash
            source = self._thumb(old, face)
            if source.exists():
                target = self._thumb(new, face)
                target.parent.mkdir(parents=True, exist_ok=True)
                (shutil.copyfile if still else os.replace)(source, target)
        return len(rows)

    def tidy(self, areas: list[str] | None = None) -> int:
        """Forget the faces of pictures that are no longer there – and of those in areas where
        faces are not to be looked for (any more)."""
        here = {row["hash"] for row in self.index.pictures()}
        if not here:
            return 0
        known = [row["hash"] for row in self._all("SELECT hash FROM pictures")]
        gone = [digest for digest in known if digest not in here]
        if len(gone) > max(20, len(known) // 2):
            # half the library gone at once is more likely a folder not mounted than photos deleted
            log.warning("faces: %d of %d pictures are not in the index, keeping what is known", len(gone), len(known))
            return 0
        if areas is not None:
            wanted = {row["hash"] for row in self.index.pictures(areas)}
            gone += [digest for digest in known if digest in here and digest not in wanted]
        if gone:
            db = self._connect()
            try:
                for digest in gone:         # the small cut-outs go as well
                    for face in db.execute("SELECT x, y FROM faces WHERE hash = ?", (digest,)).fetchall():
                        self._thumb(digest, face).unlink(missing_ok=True)
                with self._write:
                    db.executemany("DELETE FROM faces WHERE hash = ?", [(digest,) for digest in gone])
                    db.executemany("DELETE FROM pictures WHERE hash = ?", [(digest,) for digest in gone])
                    db.commit()
                    self._changes += 1
            finally:
                db.close()
        return len(gone)

    def state(self) -> dict:
        row = self._all("SELECT COUNT(*) pictures, COALESCE(SUM(found), 0) faces FROM pictures WHERE engine = ?",
                        (ENGINE,))[0]
        ignored = self._all("SELECT COUNT(*) n FROM ignored", path=self.ignored_path)[0]["n"]
        return {"missing": self.engine.missing, "pictures": row["pictures"], "faces": row["faces"],
                "ignored": ignored, "search": dict(self.progress)}

    # ------------------------------------------------------------ waved away

    def _ignored(self, digest: str | None = None) -> dict[str, list[tuple]]:
        """The boxes somebody said are nobody to be named, by picture."""
        rows = self._all("SELECT hash, x, y, w, h FROM ignored" + (" WHERE hash = ?" if digest else ""),
                         (digest,) if digest else (), self.ignored_path)
        found: dict[str, list[tuple]] = {}
        for row in rows:
            found.setdefault(row["hash"], []).append((row["x"], row["y"], row["w"], row["h"]))
        return found

    def ignore(self, face: dict, user: str = "", ignored: bool = True) -> None:
        """Wave a found face away: it is no longer shown as waiting for a name – or take that back.
        Nothing is written to the photo; the decision is kept beside the store that can be thrown away."""
        box = (face["x"], face["y"], face["w"], face["h"])
        db = self._connect(self.ignored_path)
        try:
            with self._write:
                for row in db.execute("SELECT rowid, x, y, w, h FROM ignored WHERE hash = ?", (face["hash"],)).fetchall():
                    if _overlap(box, (row["x"], row["y"], row["w"], row["h"])) >= SAME_FACE:
                        db.execute("DELETE FROM ignored WHERE rowid = ?", (row["rowid"],))
                if ignored:
                    db.execute("INSERT INTO ignored VALUES (?, ?, ?, ?, ?, ?, ?)", (face["hash"], *box, user, time.time()))
                db.commit()
                self._changes += 1
        finally:
            db.close()

    def restore_all(self) -> int:
        """Bring every face waved away back among those waiting for a name."""
        db = self._connect(self.ignored_path)
        try:
            with self._write:
                count = db.execute("DELETE FROM ignored").rowcount
                db.commit()
                self._changes += 1
                return count
        finally:
            db.close()

    # ------------------------------------------------------------ one face

    def _thumb(self, digest: str, face) -> Path:
        """Named after where the face is, so it is found again when the store was thrown away."""
        spot = f"{round(face['x'] * 9999):04d}{round(face['y'] * 9999):04d}"
        return cache_path(self.thumbs, digest, spot)

    def face(self, face_id: int) -> dict | None:
        rows = self._all("SELECT id, hash, x, y, w, h, score FROM faces WHERE id = ?", (face_id,))
        return dict(rows[0]) if rows else None

    def thumb(self, face: dict, photo: dict) -> Path:
        """The small cut-out of a face; made again from the photo if it is gone."""
        target = self._thumb(face["hash"], face)
        if not target.exists():
            image = Image.open(io.BytesIO((self.root / photo["path"]).read_bytes()))
            orientation = photo["orientation"] or 1
            if image.format == "JPEG":
                image.draft("RGB", (EDGE, EDGE))
            image = ImageOps.exif_transpose(image).convert("RGB")
            x, y, w, h = turn_box((face["x"], face["y"], face["w"], face["h"]), orientation)
            store(target, _cut_out(image, (x - w / 2) * image.width, (y - h / 2) * image.height,
                                   w * image.width, h * image.height))
        return target

    # ------------------------------------------------------------ who is who

    def _everything(self, areas: list[str], stale: float = 0) -> dict:
        """Every found face in these areas, with the name the file gives it – worked out once and
        kept until a photo or a face changes. While the search is adding faces, an answer up to
        `stale` seconds old will do for those who only want proposals."""
        import numpy

        def kept():
            stamp = (self._changes, self.index.changes, tuple(areas))
            worked = self._worked
            if worked and (worked[0] == stamp or (worked[0][2] == stamp[2] and time.monotonic() - worked[2] < stale)):
                return worked[1]
            return None

        found = kept()
        if found is not None:
            return found
        with self._think:                   # two who ask at once: the second takes what the first worked out
            found = kept()
            return found if found is not None else self._work_out(areas)

    def _work_out(self, areas: list[str]) -> dict:
        import numpy

        stamp = (self._changes, self.index.changes, tuple(areas))
        photos: dict[str, dict] = {}
        for row in self.index.pictures(areas):
            kept = photos.get(row["hash"])
            if kept is None or (row["regions"] and not kept["regions"]):
                photos[row["hash"]] = row
        faces, vectors = [], []
        ignored = self._ignored()
        for row in self._all("SELECT id, hash, x, y, w, h, score, vector FROM faces ORDER BY id"):
            photo = photos.get(row["hash"])
            if photo is None:
                continue
            box = (row["x"], row["y"], row["w"], row["h"])
            # the region that lies on this face – where two do (the camera's own mark beside a named one),
            # the named one counts, then the one that fits best
            name, region, fit = "", None, (False, 0.0)
            for there in photo["regions"]:
                over = _overlap(box, (there["x"], there["y"], there["w"], there["h"])) if is_face(there["type"]) else 0.0
                if over >= SAME_FACE and (bool(there["name"]), over) > fit:
                    name, region, fit = there["name"], there, (bool(there["name"]), over)
            # waved away – also where the camera itself marked the face without a name (Apple does)
            if not name and any(_overlap(box, there) >= SAME_FACE for there in ignored.get(row["hash"], ())):
                continue
            faces.append({"id": row["id"], "hash": row["hash"], "key": photo["key"], "box": box, "name": name,
                          "marked": region is not None, "score": row["score"],
                          "at": len(vectors) if row["vector"] else None})
            if row["vector"]:
                vectors.append(numpy.frombuffer(row["vector"], dtype="float32"))
        matrix = numpy.stack(vectors) if vectors else numpy.zeros((0, VECTOR), dtype="float32")
        named = [face for face in faces if face["name"] and face["at"] is not None]
        known = matrix[[face["at"] for face in named]] if named else numpy.zeros((0, VECTOR), dtype="float32")
        result = {"photos": photos, "faces": faces, "matrix": matrix, "named": named, "known": known}
        self._worked = (stamp, result, time.monotonic())
        return result

    @staticmethod
    def _present(world: dict, digest: str) -> frozenset[str]:
        """Who is already named in this picture – nobody is in a picture twice."""
        photo = world["photos"].get(digest)
        return frozenset(there["name"] for there in photo["regions"] if there["name"] and is_face(there["type"])) if photo else frozenset()

    @staticmethod
    def _propose(world: dict, rows, least: float = 0, taken: list | None = None) -> list[tuple[str, float]]:
        """For each vector the name of the most alike named face – if it is alike enough. `taken` holds,
        row by row, the names that are out of the question: those already given to another face of the
        same picture. Such a face gets the next best name, or none."""
        if not len(rows) or not len(world["named"]):
            return [("", 0.0)] * len(rows)
        alike = rows @ world["known"].T
        if taken and any(taken):
            import numpy
            columns = world.get("columns")
            if columns is None:
                lists: dict[str, list[int]] = {}
                for at, face in enumerate(world["named"]):
                    lists.setdefault(face["name"], []).append(at)
                columns = world["columns"] = {name: numpy.array(ats) for name, ats in lists.items()}
            for row, names in enumerate(taken):
                for name in names:
                    if name in columns:
                        alike[row, columns[name]] = -1.0
        best = alike.argmax(axis=1)
        return [(world["named"][at]["name"], float(alike[i, at])) if alike[i, at] >= (least or SUGGEST) else ("", 0.0)
                for i, at in enumerate(best)]

    def on_photo(self, photo: dict, areas: list[str]) -> list[dict]:
        """The faces of one photo as they are shown: the regions marked in the file and the faces
        found beside them, with boxes in the upright picture. `areas` are those whose found faces
        may be shown and lend their names; a photo outside them shows only what its file says."""
        import numpy

        orientation = photo["orientation"] or 1
        shown = []
        found = [dict(row, box=(row["x"], row["y"], row["w"], row["h"])) for row in self._all(
            "SELECT id, x, y, w, h, vector FROM faces WHERE hash = ? ORDER BY id", (photo["hash"],))
        ] if photo["area"] in areas else []
        ignored = self._ignored(photo["hash"]).get(photo["hash"], [])
        marked = set()
        nameless: list[tuple[dict, dict]] = []
        for region in photo["regions"]:
            if not is_face(region["type"]):
                continue
            there = (region["x"], region["y"], region["w"], region["h"])
            same = [face["id"] for face in found if _overlap(face["box"], there) >= SAME_FACE]
            marked.update(same)
            x, y, w, h = turn_box(there, orientation)
            shown.append({"x": x, "y": y, "w": w, "h": h, "name": region["name"], "marked": True,
                          "face": same[0] if same else None, "proposal": "", "ignored": False})
            if same and not region["name"]:     # marked by the camera, named by nobody: a guess helps here too
                nameless.append((shown[-1], next(face for face in found if face["id"] == same[0])))
        loose, away = [], []
        for face in found:
            if face["id"] not in marked:
                waved = any(_overlap(face["box"], there) >= SAME_FACE for there in ignored)
                (away if waved else loose).append(face)
        nameless = [(entry, face) for entry, face in nameless if face["vector"]
                    and not any(_overlap(face["box"], there) >= SAME_FACE for there in ignored)]
        asked = [face for face in loose if face["vector"]] + [face for _, face in nameless]
        vectors = [numpy.frombuffer(face["vector"], dtype="float32") for face in asked]
        here = frozenset(region["name"] for region in photo["regions"] if region["name"] and is_face(region["type"]))
        proposals = iter(self._propose(self._everything(areas, stale=60), numpy.stack(vectors),
                                       taken=[here] * len(vectors)) if vectors else [])
        guesses = {face["id"]: proposal[0] for face, proposal in zip(asked, proposals)}
        proposals = iter([(guesses[face["id"]], 0.0) for face in loose if face["vector"]])
        for entry, face in nameless:
            entry["proposal"] = guesses[face["id"]]
        for face in loose:
            x, y, w, h = turn_box(face["box"], orientation)
            proposal = next(proposals)[0] if face["vector"] else ""
            shown.append({"x": x, "y": y, "w": w, "h": h, "name": "", "marked": False, "face": face["id"],
                          "proposal": proposal, "ignored": False})
        for face in away:
            x, y, w, h = turn_box(face["box"], orientation)
            shown.append({"x": x, "y": y, "w": w, "h": h, "name": "", "marked": False, "face": face["id"],
                          "proposal": "", "ignored": True})
        return shown

    def groups(self, areas: list[str], limit: int = 40, offset: int = 0, shown: int = 60, stale: float = 0) -> dict:
        """The faces without a name, bundled by likeness – the big bundles first. A bundle that looks
        like somebody already named carries that name as a proposal. Nothing is written here."""
        import numpy

        world = self._everything(areas, stale)
        if "groups" not in world:
            loose = [face for face in world["faces"] if not face["name"] and face["at"] is not None]
            loose.sort(key=lambda face: (-face["score"] * face["box"][2] * face["box"][3], face["id"]))

            def shown_as(face: dict) -> dict:           # with the box as it lies in the upright picture
                turn = world["photos"][face["hash"]]["orientation"] or 1
                return {"id": face["id"], "key": face["key"], "o": turn,
                        "box": [round(n, 4) for n in turn_box(face["box"], turn)]}

            rows = world["matrix"][[face["at"] for face in loose]] if loose else world["matrix"][:0]
            # first whoever looks like somebody already named: one bundle per name, the surest first
            whose: dict[str, list[tuple[float, int]]] = {}
            rest = []
            for start in range(0, len(loose), 2000):
                taken = [self._present(world, face["hash"]) for face in loose[start:start + 2000]]
                for at, (name, alike) in enumerate(self._propose(world, rows[start:start + 2000], KNOWN, taken), start):
                    whose.setdefault(name, []).append((-alike, at)) if name else rest.append(at)
            bundles = []
            for name, group in whose.items():
                group.sort()
                bundles.append({"id": loose[group[0][1]]["id"], "count": len(group), "proposal": name, "known": True,
                                "faces": [shown_as(loose[at]) for _, at in group]})
            # then the strangers among themselves
            centres = numpy.zeros((len(rest), VECTOR), dtype="float32")
            sums = numpy.zeros((len(rest), VECTOR), dtype="float32")
            members: list[list[int]] = []
            for at in rest:                             # the clearest faces first, they make good centres
                vector = rows[at]
                if members:
                    alike = centres[:len(members)] @ vector
                    best = int(alike.argmax())
                    if alike[best] >= ALIKE:
                        members[best].append(at)
                        sums[best] += vector
                        centres[best] = sums[best] / numpy.linalg.norm(sums[best])
                        continue
                sums[len(members)] = centres[len(members)] = vector
                members.append([at])
            # a bundle of strangers gets a name proposed only on the same strength as a single face –
            # things taken for faces (wheels, masks) flock together and must not borrow a name
            # … and never the name of somebody who already stands in one of the bundle's pictures
            proposals = self._propose(world, centres[:len(members)], KNOWN, [
                frozenset().union(*(self._present(world, loose[at]["hash"]) for at in group)) for group in members])
            bundles += [{"id": loose[group[0]]["id"], "count": len(group), "proposal": proposals[number][0],
                         "known": False, "faces": [shown_as(loose[at]) for at in group]}
                        for number, group in enumerate(members) if len(group) > 1]
            # what is left stands alone – thousands of bundles of one face are no help to anybody. They are
            # laid beside the named person they look most like, even if only a little ("maybe"), the likeliest
            # first; and what looks like nobody goes into one last bundle, the clearest faces first.
            alone = [group[0] for group in members if len(group) == 1]
            maybe: dict[str, list[tuple[float, int]]] = {}
            rest = []
            for start in range(0, len(alone), 2000):
                part = alone[start:start + 2000]
                taken = [self._present(world, loose[at]["hash"]) for at in part]
                for at, (name, alike) in zip(part, self._propose(world, rows[part], MAYBE, taken)):
                    maybe.setdefault(name, []).append((-alike, at)) if name else rest.append(at)
            for name, group in maybe.items():
                group.sort()
                bundles.append({"id": loose[group[0][1]]["id"], "count": len(group), "proposal": name, "known": False,
                                "maybe": True, "faces": [shown_as(loose[at]) for _, at in group]})
            bundles.sort(key=lambda bundle: (-bundle["count"], bundle["id"]))
            if rest:                                    # always the last one
                bundles.append({"id": loose[rest[0]]["id"], "count": len(rest), "proposal": "", "known": False,
                                "rest": True, "faces": [shown_as(loose[at]) for at in rest]})
            world["groups"] = bundles
        bundles = world["groups"]
        return {"groups": [dict(bundle, faces=bundle["faces"][:shown]) for bundle in bundles[offset:offset + limit]],
                "total": len(bundles), "faces": sum(bundle["count"] for bundle in bundles),
                "small": sum(1 for face in world["faces"] if not face["name"] and face["at"] is None)}

    def twins(self, areas: list[str], stale: float = 0) -> list[dict]:
        """Faces without a name that are a named face over again: as good as the same numbers, at the
        same place in the picture – what a second copy of a photo looks like. Nothing is written here."""
        world = self._everything(areas, stale)
        if "twins" not in world:
            loose = [face for face in world["faces"] if not face["name"] and face["at"] is not None]
            found = []
            for start in range(0, len(loose) if len(world["named"]) else 0, 2000):
                part = loose[start:start + 2000]
                alike = world["matrix"][[face["at"] for face in part]] @ world["known"].T
                for face, row in zip(part, alike):
                    for at in row.argsort()[::-1][:5]:
                        if row[at] < TWIN:
                            break
                        other = world["named"][at]
                        if _overlap(face["box"], other["box"]) >= SAME_FACE:
                            turn = world["photos"][face["hash"]]["orientation"] or 1
                            found.append({"id": face["id"], "key": face["key"], "o": turn, "name": other["name"],
                                          "box": [round(n, 4) for n in turn_box(face["box"], turn)],
                                          "like": other["key"]})
                            break
            found.sort(key=lambda face: (face["name"].casefold(), face["id"]))
            world["twins"] = found
        return world["twins"]

    def doubles(self, areas: list[str], stale: float = 0) -> list[dict]:
        """Faces whose name stands twice in their picture – one of the two is somebody else.
        Side by side, picture by picture. Nothing is written here."""
        world = self._everything(areas, stale)
        if "doubles" not in world:
            twice: dict[str, set[str]] = {}
            for digest, photo in world["photos"].items():
                seen: dict[str, int] = {}
                for there in photo["regions"]:
                    if there["name"] and is_face(there["type"]):
                        seen[there["name"]] = seen.get(there["name"], 0) + 1
                names = {name for name, count in seen.items() if count > 1}
                if names:
                    twice[digest] = names
            found = []
            for face in world["faces"]:
                if face["name"] and face["name"] in twice.get(face["hash"], ()):
                    turn = world["photos"][face["hash"]]["orientation"] or 1
                    found.append({"id": face["id"], "key": face["key"], "o": turn, "name": face["name"],
                                  "box": [round(n, 4) for n in turn_box(face["box"], turn)]})
            found.sort(key=lambda face: (face["name"].casefold(), face["key"], face["box"][0]))
            world["doubles"] = found
        return world["doubles"]

    # ------------------------------------------------------------ is it a face at all?

    def _judged(self) -> dict[str, list[tuple]]:
        found: dict[str, list[tuple]] = {}
        for row in self._all("SELECT hash, x, y, w, h, face FROM judged ORDER BY ts", path=self.ignored_path):
            found.setdefault(row["hash"], []).append(((row["x"], row["y"], row["w"], row["h"]), bool(row["face"])))
        return found

    def verdicts(self, areas: list[str], stale: float = 0) -> tuple[list[dict], list[dict]]:
        """The faces without a name that wait among the strangers: those nobody has judged yet, and
        those said to be no face. (A later verdict on the same face counts.)"""
        world = self._everything(areas, stale)
        self.groups(areas, limit=0, stale=stale)
        judged = self._judged()
        by_id = {face["id"]: face for face in world["faces"]}
        open_, none = [], []
        for bundle in world["groups"]:
            if bundle["known"]:
                continue                    # looks like somebody named: hardly a tyre
            for shown in bundle["faces"]:
                face = by_id[shown["id"]]
                said = [verdict for there, verdict in judged.get(face["hash"], ()) if _overlap(face["box"], there) >= SAME_FACE]
                if not said:
                    open_.append(dict(shown, hash=face["hash"], stored=face["box"]))
                elif not said[-1]:
                    none.append(shown)
        return open_, none

    def judge(self, faces: list[dict], verdicts: list[bool]) -> None:
        """Keep what was said about these faces (each with `hash` and its box `stored`)."""
        db = self._connect(self.ignored_path)
        try:
            with db:
                for face, verdict in zip(faces, verdicts):
                    db.execute("INSERT INTO judged VALUES (?, ?, ?, ?, ?, ?, ?)",
                               (face["hash"], *face["stored"], int(bool(verdict)), time.time()))
        finally:
            db.close()
        self._changes += 1

    def waved_away(self, areas: list[str]) -> list[dict]:
        """The faces somebody waved away, the latest first – so that a slip can be undone."""
        photos = self._everything(areas)["photos"]
        when: dict[str, list[tuple]] = {}
        for row in self._all("SELECT hash, x, y, w, h, ts FROM ignored", path=self.ignored_path):
            when.setdefault(row["hash"], []).append(((row["x"], row["y"], row["w"], row["h"]), row["ts"]))
        found = []
        for row in self._all("SELECT id, hash, x, y, w, h FROM faces ORDER BY id"):
            photo = photos.get(row["hash"])
            if photo is None or row["hash"] not in when:
                continue
            box = (row["x"], row["y"], row["w"], row["h"])
            if any(region["name"] and is_face(region["type"])
                   and _overlap(box, (region["x"], region["y"], region["w"], region["h"])) >= SAME_FACE
                   for region in photo["regions"]):
                continue                    # named since: the name counts
            for there, ts in when[row["hash"]]:
                if _overlap(box, there) >= SAME_FACE:
                    turn = photo["orientation"] or 1
                    found.append({"id": row["id"], "key": photo["key"], "o": turn, "ts": ts,
                                  "box": [round(n, 4) for n in turn_box(box, turn)]})
                    break
        found.sort(key=lambda face: (-face["ts"], face["id"]))
        return found

    def covers(self, areas: list[str], chosen: dict) -> dict[str, int]:
        """The face that stands for each person in the list: the one chosen, else the clearest."""
        best: dict[str, tuple[float, int]] = {}
        for face in self._everything(areas)["faces"]:
            if not face["name"]:
                continue
            worth = face["score"] * face["box"][2] * face["box"][3]
            pick = chosen.get(face["name"])
            if pick and pick.get("hash") == face["hash"] and _overlap(face["box"], tuple(pick["box"])) >= SAME_FACE:
                worth = float("inf")
            if face["name"] not in best or worth > best[face["name"]][0]:
                best[face["name"]] = (worth, face["id"])
        return {name: face_id for name, (_, face_id) in best.items()}

    def bundle(self, areas: list[str], group: int, limit: int = 120, stale: float = 0) -> dict | None:
        """One bundle with its first `limit` faces, asked for by its own number or by one of its faces."""
        self.groups(areas, limit=0, stale=stale)
        bundles = self._everything(areas, stale)["groups"]
        for bundle in bundles:
            if bundle["id"] == group:
                return dict(bundle, faces=bundle["faces"][:limit])
        for bundle in bundles:              # … or the bundle this face is in
            if any(face["id"] == group for face in bundle["faces"]):
                return dict(bundle, faces=bundle["faces"][:limit])
        return None

    def members(self, areas: list[str], group: int) -> list[int]:
        """All faces of one bundle, also those the page did not show."""
        self.groups(areas, limit=0)
        for bundle in self._everything(areas)["groups"]:
            if bundle["id"] == group:
                return [face["id"] for face in bundle["faces"]]
        return []


def main() -> None:
    """python -m augenblick.faces fetch <folder>: put the model files there (used when the image is built)."""
    if len(sys.argv) == 3 and sys.argv[1] == "fetch":
        fetch_models(Path(sys.argv[2]))
        print(f"models are in {sys.argv[2]}")
    else:
        print("usage: python -m augenblick.faces fetch <folder>", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
