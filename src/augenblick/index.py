"""The index: a SQLite file that mirrors the photo folders. It can be thrown away at any time."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

from .media import examine, kind_of

log = logging.getLogger("augenblick.index")

SCHEMA_VERSION = 1
SCHEMA = """
CREATE TABLE photos (
    id INTEGER PRIMARY KEY,
    key TEXT NOT NULL UNIQUE,        -- short public id, from the picture hash
    hash TEXT NOT NULL,              -- SHA-256 of the picture data (JPEG) or the file
    area TEXT NOT NULL,              -- first folder level
    folder TEXT NOT NULL,            -- folder below the photo root, with the area
    path TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    ext TEXT NOT NULL,
    kind TEXT NOT NULL,              -- image | video
    size INTEGER NOT NULL,
    mtime REAL NOT NULL,
    taken TEXT NOT NULL,             -- local time, YYYY-MM-DDTHH:MM:SS
    taken_src TEXT NOT NULL,         -- exif | name | file
    month TEXT NOT NULL,             -- YYYY-MM
    width INTEGER, height INTEGER, orientation INTEGER,
    lat REAL, lon REAL,
    make TEXT, model TEXT, description TEXT, keywords TEXT, rating INTEGER,
    error TEXT,
    target TEXT NOT NULL DEFAULT '',     -- where the sorting rules want it; '' = no rule has a say
    misfit INTEGER NOT NULL DEFAULT 0,   -- 1 = it is not there
    albums TEXT NOT NULL DEFAULT '',     -- album paths, one per line, with a line break before and after
    people TEXT NOT NULL DEFAULT '',     -- names on the marked faces, kept the same way
    regions TEXT NOT NULL DEFAULT ''     -- the marked regions as they stand in the file (JSON)
);
CREATE INDEX photos_taken ON photos (taken DESC, id);
CREATE INDEX photos_month ON photos (month);
CREATE INDEX photos_folder ON photos (folder);
CREATE INDEX photos_hash ON photos (hash);
CREATE TABLE meta (name TEXT PRIMARY KEY, value TEXT);
"""
COLUMNS = ("key", "hash", "area", "folder", "path", "name", "ext", "kind", "size", "mtime", "taken", "taken_src",
           "month", "width", "height", "orientation", "lat", "lon", "make", "model", "description", "keywords",
           "rating", "error", "target", "misfit", "albums", "people", "regions", "city", "state", "country",
           "live", "seconds", "source", "recipe", "creator")
ADDED = {"target": "TEXT NOT NULL DEFAULT ''", "misfit": "INTEGER NOT NULL DEFAULT 0",
         "albums": "TEXT NOT NULL DEFAULT ''", "people": "TEXT NOT NULL DEFAULT ''",
         "regions": "TEXT NOT NULL DEFAULT ''", "city": "TEXT NOT NULL DEFAULT ''",
         "state": "TEXT NOT NULL DEFAULT ''", "country": "TEXT NOT NULL DEFAULT ''",
         "live": "TEXT NOT NULL DEFAULT ''",        # what a Live Photo and its short film have in common
         "seconds": "REAL NOT NULL DEFAULT 0",      # how long a film runs
         "source": "TEXT NOT NULL DEFAULT ''",      # a picture worked on here: the hash of the one it was made from
         "recipe": "TEXT NOT NULL DEFAULT ''",      # … and what was done to it (JSON)
         "creator": "TEXT NOT NULL DEFAULT ''"}     # who took the picture, as the file says it
# columns newer than schema 1; what these hold was not read before, so the files that can have it are looked at once more
REREAD = {"people": "1", "live": "kind = 'video' OR make = 'Apple'", "source": "0", "recipe": "0", "seconds": "0",
          "creator": "kind = 'image'"}
# The short film of a Live Photo is part of the photo while the two lie in one folder: it is not shown on its own.
# The same holds for the original of a picture worked on here: it stands behind the picture made from it.
# what is read from a file changed for these: they are looked at once more, each kind of change once
AGAIN = {"heif-regions": "ext IN ('heic', 'heif', 'avif') AND regions != ''"}
SHOWN = ("NOT (kind = 'video' AND live != '' AND EXISTS (SELECT 1 FROM photos still WHERE still.live = photos.live "
         "AND still.kind = 'image' AND still.folder = photos.folder)) "
         "AND NOT EXISTS (SELECT 1 FROM photos made WHERE made.source = photos.hash AND made.folder = photos.folder)")


# Albums nobody has to keep: what belongs in them follows from the pictures themselves.
AUTO = {
    "videos": ("Videos", "kind = 'video'"),
    "live": ("Live Photos", "kind = 'image' AND live != '' AND EXISTS (SELECT 1 FROM photos film WHERE "
                            "film.live = photos.live AND film.kind = 'video' AND film.folder = photos.folder)"),
    "panoramen": ("Panoramen", "kind = 'image' AND (make != '' OR model != '') AND width > 0 AND height > 0 "
                               "AND (width >= 2 * height OR height >= 2 * width) "
                               "AND max(width, height) >= 1500"),      # a small strip is a cut-out, not a wide view
    "favoriten": ("Favoriten", "rating >= 4"),
    "raw": ("RAW", "ext = 'dng'"),
    "heute": ("An diesem Tag", "substr(taken, 6, 5) = ? AND substr(taken, 1, 4) < ?"),
}


def _auto(name: str) -> tuple[str, list]:
    clause = AUTO[name][1]
    today = date.today()
    return clause, [today.strftime("%m-%d"), today.strftime("%Y")] if name == "heute" else []


def _entry(row) -> dict:
    """A row as the rest of the programme wants it."""
    data = dict(row)
    data.pop("id", None)
    data["keywords"] = [k for k in (data["keywords"] or "").split("\n") if k]
    data["albums"] = [a for a in (data.get("albums") or "").split("\n") if a]
    data["people"] = [p for p in (data.get("people") or "").split("\n") if p]
    data["regions"] = json.loads(data.get("regions") or "[]")
    data["recipe"] = json.loads(data.get("recipe") or "{}")
    return data


def _like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class Index:
    def __init__(self, db_path: Path, root: Path, thumbs: Path):
        self.db_path = db_path
        self.root = root
        self.thumbs = thumbs
        self._write = threading.Lock()
        self._scan = threading.Lock()
        self.progress = {"running": False, "done": 0, "total": 0, "last": 0.0, "seconds": 0.0}
        self.planner = None            # set by the app: says where a photo belongs
        self.geo = None                # set by the app: names the town at a pair of coordinates
        self.no_places: frozenset[str] = frozenset()    # areas whose pictures need no place (screenshots)
        self._dirs: tuple[float, int, list[str]] | None = None     # the folders on disk, kept for a moment
        self.no_dates: frozenset[str] = frozenset()     # areas whose pictures need no date from the camera
        self.changes = 0               # rises with every change, for those who keep something worked out from it
        self._open()

    # ------------------------------------------------------------ plumbing

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.db_path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        return db

    def _open(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        db = self._connect()
        try:
            has_meta = db.execute("SELECT 1 FROM sqlite_master WHERE name='meta'").fetchone()
            version = None
            if has_meta:
                row = db.execute("SELECT value FROM meta WHERE name='schema'").fetchone()
                version = row["value"] if row else None
            if version != str(SCHEMA_VERSION):
                for table in ("photos", "meta"):
                    db.execute(f"DROP TABLE IF EXISTS {table}")
                db.executescript(SCHEMA)
                db.execute("INSERT INTO meta VALUES ('schema', ?)", (str(SCHEMA_VERSION),))
                db.commit()
                if version is not None:
                    log.info("index layout changed, rebuilding from the photos")
            have = {row["name"] for row in db.execute("PRAGMA table_info(photos)")}
            for column, kind in ADDED.items():      # added without throwing the index away
                if column not in have:
                    db.execute(f"ALTER TABLE photos ADD COLUMN {column} {kind}")
                    if column in REREAD:            # the entries stay usable; the next scan reads the files again
                        db.execute(f"UPDATE photos SET mtime = -1 WHERE {REREAD[column]}")
            for name, which in AGAIN.items():
                if not db.execute("SELECT 1 FROM meta WHERE name = ?", ("again-" + name,)).fetchone():
                    db.execute(f"UPDATE photos SET mtime = -1 WHERE {which}")
                    db.execute("INSERT INTO meta VALUES (?, '1')", ("again-" + name,))
            db.execute("CREATE INDEX IF NOT EXISTS photos_misfit ON photos (misfit)")
            db.execute("CREATE INDEX IF NOT EXISTS photos_live ON photos (live)")
            db.execute("CREATE INDEX IF NOT EXISTS photos_source ON photos (source)")
            db.commit()
        finally:
            db.close()

    # ------------------------------------------------------------ scanning

    def _walk(self) -> dict[str, tuple[int, float]]:
        found: dict[str, tuple[int, float]] = {}
        root = str(self.root)
        for folder, dirs, files in os.walk(root):
            dirs[:] = sorted(d for d in dirs if not d.startswith((".", "@")))
            rel_folder = os.path.relpath(folder, root)
            if rel_folder == ".":
                continue        # files directly in the root belong to no area
            for name in files:
                if name.startswith("."):
                    continue
                if kind_of(name.rsplit(".", 1)[-1].lower() if "." in name else "") is None:
                    continue
                try:
                    stat = os.stat(os.path.join(folder, name))
                except OSError:
                    continue
                found[f"{rel_folder}/{name}".replace(os.sep, "/")] = (stat.st_size, stat.st_mtime)
        return found

    def _key(self, db: sqlite3.Connection, digest: str, path: str) -> str:
        key = digest[:16]
        clash = db.execute("SELECT path FROM photos WHERE key=?", (key,)).fetchone()
        if clash and clash["path"] != path:   # the same picture a second time
            key = f"{key}-{hashlib.sha1(path.encode()).hexdigest()[:6]}"
        return key

    def _save(self, db: sqlite3.Connection, record: dict) -> None:
        path = record["path"]
        folder = path.rsplit("/", 1)[0]
        db.execute("DELETE FROM photos WHERE path=?", (path,))
        values = dict(record, key=self._key(db, record["hash"], path), area=folder.split("/", 1)[0], folder=folder,
                      month=record["taken"][:7], keywords="\n".join(record.get("keywords") or []))
        values["target"], values["misfit"] = self._placed(dict(values, folder=folder))
        albums = record.get("albums") or []
        values["albums"] = "\n" + "\n".join(albums) + "\n" if albums else ""
        people = record.get("people") or []
        values["people"] = "\n" + "\n".join(people) + "\n" if people else ""
        values["regions"] = json.dumps(record["regions"], ensure_ascii=False) if record.get("regions") else ""
        values["recipe"] = json.dumps(record["recipe"]) if record.get("recipe") else ""
        values["city"], values["state"], values["country"] = (
            self.geo.lookup(record.get("lat"), record.get("lon"))
            if self.geo and values["area"] not in self.no_places else ("", "", ""))
        self.changes += 1
        db.execute(f"INSERT INTO photos ({', '.join(COLUMNS)}) VALUES ({', '.join('?' * len(COLUMNS))})",
                   [values.get(c) for c in COLUMNS])

    def _placed(self, photo: dict) -> tuple[str, int]:
        if not self.planner:
            return "", 0
        target = self.planner.target(photo)
        return target, 0 if self.planner.fits(photo["path"], target) else 1

    def replan(self) -> int:
        """The rules changed: work out again for every photo where it belongs. Reads no file."""
        db = self._connect()
        try:
            with self._write:
                rows = db.execute("SELECT id, path, folder, name, taken, taken_src, model FROM photos").fetchall()
                db.executemany("UPDATE photos SET target = ?, misfit = ? WHERE id = ?",
                               [(*self._placed(dict(row)), row["id"]) for row in rows])
                db.commit()
            return db.execute("SELECT COUNT(*) n FROM photos WHERE misfit = 1").fetchone()["n"]
        finally:
            db.close()

    def relocate(self) -> int:
        """The list of towns is new or another one: name the place of every photo again. Reads no file."""
        if self.geo is None:
            return 0
        db = self._connect()
        try:
            stamp = self.geo.stamp + "".join(f"|{area}" for area in sorted(self.no_places))
            row = db.execute("SELECT value FROM meta WHERE name = 'geo'").fetchone()
            if (row["value"] if row else "") == stamp:
                return 0
            rows = db.execute("SELECT id, area, lat, lon FROM photos WHERE lat IS NOT NULL").fetchall()
            named = [(*(("", "", "") if row["area"] in self.no_places else self.geo.lookup(row["lat"], row["lon"])),
                      row["id"]) for row in rows]
            with self._write:
                db.execute("UPDATE photos SET city = '', state = '', country = '' WHERE lat IS NULL")
                db.executemany("UPDATE photos SET city = ?, state = ?, country = ? WHERE id = ?", named)
                db.execute("INSERT OR REPLACE INTO meta VALUES ('geo', ?)", (stamp,))
                db.commit()
                self.changes += 1
            if named:
                log.info("place names worked out for %d photos", len(named))
            return len(named)
        finally:
            db.close()

    def moved(self, old: str, new: str) -> dict:
        """A file was renamed: the entry follows, without reading the file again."""
        folder, _, name = new.rpartition("/")
        db = self._connect()
        try:
            with self._write:
                row = db.execute("SELECT taken, taken_src, model, lat, lon FROM photos WHERE path = ?", (old,)).fetchone()
                target, misfit = self._placed(dict(row, path=new, folder=folder, name=name)) if row else ("", 0)
                area = folder.split("/", 1)[0]          # it may have gone to another area, with other wishes
                named = (self.geo.lookup(row["lat"], row["lon"])
                         if row and self.geo and area not in self.no_places else ("", "", ""))
                db.execute("UPDATE photos SET path = ?, area = ?, folder = ?, name = ?, target = ?, misfit = ?, "
                           "city = ?, state = ?, country = ? WHERE path = ?",
                           (new, area, folder, name, target, misfit, *named, old))
                db.commit()
                self.changes += 1
        finally:
            db.close()
        return self.by_path(new)

    def misfits(self, areas: list[str], limit: int, **filters) -> list[dict]:
        where, params = self._where(areas, **dict(filters, misfit=True))
        rows = self._all(f"SELECT * FROM photos WHERE {where} ORDER BY path LIMIT ?", params + [limit])
        return [_entry(row) for row in rows]

    def count_where(self, areas: list[str], **filters) -> int:
        where, params = self._where(areas, **filters)
        return self._all(f"SELECT COUNT(*) n FROM photos WHERE {where}", params)[0]["n"]

    def scan(self, workers: int = 2) -> dict:
        """Bring the index in line with the folders: new and changed files in, vanished ones out."""
        if not self._scan.acquire(blocking=False):
            return {"busy": True}
        started = time.time()
        try:
            if not self.root.is_dir():
                log.warning("photo folder %s does not exist", self.root)
                return {"added": 0, "removed": 0}
            self.relocate()
            found = self._walk()
            db = self._connect()
            try:
                known = {r["path"]: (r["size"], r["mtime"]) for r in db.execute("SELECT path, size, mtime FROM photos")}
                gone = [p for p in known if p not in found]
                todo = [p for p, state in found.items() if known.get(p) != state]
                self.progress.update(running=True, done=0, total=len(todo))
                if gone:
                    with self._write:
                        db.executemany("DELETE FROM photos WHERE path=?", [(p,) for p in gone])
                        db.commit()
                        self.changes += 1
                if todo:
                    log.info("reading %d new or changed files", len(todo))
                with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
                    pending = 0
                    for record in pool.map(self._examine, todo):
                        self.progress["done"] += 1
                        if record is None:
                            continue
                        with self._write:
                            self._save(db, record)
                            pending += 1
                            if pending >= 200:
                                db.commit()
                                pending = 0
                    with self._write:
                        db.commit()
            finally:
                db.close()
            return {"added": len(todo), "removed": len(gone)}
        finally:
            self.progress.update(running=False, last=time.time(), seconds=round(time.time() - started, 1))
            self._scan.release()

    def _examine(self, rel: str) -> dict | None:
        try:
            return examine(self.root, rel, self.thumbs)
        except OSError as exc:
            log.warning("cannot read %s: %s", rel, exc)
            return None

    # ------------------------------------------------------------ queries

    def _where(self, areas: list[str], *, folder: str = "", recursive: bool = True, q: str = "",
               camera: str = "", creator: str = "", no_place: bool = False, no_date: bool = False, kind: str = "",
               misfit: bool = False, album: str = "", person: str = "", country: str = "",
               city: str = "", films: bool = False, auto: str = "", since: str = "", until: str = "",
               keys: list[str] | None = None) -> tuple[str, list]:
        if not areas:
            return "0", []
        clauses = [f"area IN ({', '.join('?' * len(areas))})"]
        if not films:
            clauses.append(SHOWN)
        params: list = list(areas)
        if folder:
            if recursive:
                clauses.append("(folder = ? OR folder LIKE ? ESCAPE '\\')")
                params += [folder, _like(folder) + "/%"]
            else:
                clauses.append("folder = ?")
                params.append(folder)
        for word in q.split()[:8]:
            clauses.append("(name LIKE ? ESCAPE '\\' OR folder LIKE ? ESCAPE '\\' OR description LIKE ? ESCAPE '\\'"
                           " OR keywords LIKE ? ESCAPE '\\' OR city LIKE ? ESCAPE '\\' OR state LIKE ? ESCAPE '\\'"
                           " OR country LIKE ? ESCAPE '\\' OR creator LIKE ? ESCAPE '\\')")
            params += [f"%{_like(word)}%"] * 8
        if camera:
            clauses.append("model = ?")
            params.append(camera)
        if creator:
            clauses.append("creator = ?")
            params.append(creator)
        if no_place:        # "without a place" is a to-do list: areas that need none are not on it
            clauses.append("lat IS NULL")
            if self.no_places:
                clauses.append(f"area NOT IN ({', '.join('?' * len(self.no_places))})")
                params += sorted(self.no_places)
        if country:
            clauses.append("country = ?")
            params.append(country)
        if city:
            clauses.append("city = ?")
            params.append(city)
        if no_date:
            clauses.append("taken_src != 'exif'")
            if self.no_dates:
                clauses.append(f"area NOT IN ({', '.join('?' * len(self.no_dates))})")
                params += sorted(self.no_dates)
        if misfit:
            clauses.append("misfit = 1")
        if album:        # the album itself or one below it
            clauses.append("(albums LIKE ? ESCAPE '\\' OR albums LIKE ? ESCAPE '\\')")
            params += [f"%\n{_like(album)}\n%", f"%\n{_like(album)}/%"]
        if person:
            clauses.append("people LIKE ? ESCAPE '\\'")
            params.append(f"%\n{_like(person)}\n%")
        if kind in ("image", "video"):
            clauses.append("kind = ?")
            params.append(kind)
        if since:
            clauses.append("taken >= ?")
            params.append(since)
        if until:
            clauses.append("taken < ?")
            params.append(until)
        if keys is not None:
            clauses.append(f"key IN ({', '.join('?' * len(keys))})" if keys else "0")
            params += keys
        if auto in AUTO:
            clause, values = _auto(auto)
            clauses.append(f"({clause})")
            params += values
        return " AND ".join(clauses), params

    def _all(self, sql: str, params=()) -> list[sqlite3.Row]:
        db = self._connect()
        try:
            return db.execute(sql, params).fetchall()
        finally:
            db.close()

    def areas(self) -> list[dict]:
        counts = {r["area"]: r["n"] for r in self._all(f"SELECT area, COUNT(*) n FROM photos WHERE {SHOWN} GROUP BY area")}
        names = set(counts)
        if self.root.is_dir():
            names |= {p.name for p in self.root.iterdir() if p.is_dir() and not p.name.startswith((".", "@"))}
        return [{"name": name, "count": counts.get(name, 0)} for name in sorted(names, key=str.lower)]

    def folders(self, fresh: bool = False) -> list[str]:
        """Every folder below the areas as it lies on disk – also the ones without a photo yet.
        Looked up at most every half minute, and anew after a change."""
        kept = self._dirs
        if kept and not fresh and kept[1] == self.changes and time.monotonic() - kept[0] < 30:
            return kept[2]
        found: list[str] = []

        def walk(folder: str, rel: str, depth: int) -> None:
            try:
                entries = [e for e in os.scandir(folder) if not e.name.startswith((".", "@")) and e.is_dir(follow_symlinks=False)]
            except OSError:
                return
            for entry in entries:
                path = f"{rel}/{entry.name}" if rel else entry.name
                found.append(path)
                if depth < 12:
                    walk(entry.path, path, depth + 1)

        if self.root.is_dir():
            walk(str(self.root), "", 0)
        self._dirs = (time.monotonic(), self.changes, found)
        return found

    def tree(self, areas: list[str]) -> list[dict]:
        """Every folder with its own count and the count including subfolders."""
        where, params = self._where(areas)
        own = {r["folder"]: r["n"] for r in
               self._all(f"SELECT folder, COUNT(*) n FROM photos WHERE {where} GROUP BY folder", params)}
        total: dict[str, int] = {}
        for folder, count in own.items():
            parts = folder.split("/")
            for depth in range(1, len(parts) + 1):
                parent = "/".join(parts[:depth])
                total[parent] = total.get(parent, 0) + count
        for area in areas:
            total.setdefault(area, 0)
        allowed = set(areas)
        for folder in self.folders():           # folders that hold no photo yet are shown all the same
            if folder.split("/", 1)[0] in allowed:
                total.setdefault(folder, 0)
        return [{"path": path, "name": path.rsplit("/", 1)[-1], "depth": path.count("/"),
                 "count": own.get(path, 0), "total": total[path]}
                for path in sorted(total, key=lambda p: [s.lower() for s in p.split("/")])]

    def months(self, areas: list[str], **filters) -> list[dict]:
        where, params = self._where(areas, **filters)
        return [{"month": r["month"], "count": r["n"]} for r in self._all(
            f"SELECT month, COUNT(*) n FROM photos WHERE {where} GROUP BY month ORDER BY month DESC", params)]

    def month(self, areas: list[str], month: str, **filters) -> list[dict]:
        where, params = self._where(areas, **filters)
        rows = self._all(
            f"SELECT key, kind, taken, width, height, orientation, seconds FROM photos WHERE {where} AND month = ? "
            "ORDER BY taken DESC, id", params + [month])
        return [{"key": r["key"], "kind": r["kind"], "taken": r["taken"], "w": r["width"], "h": r["height"],
                 "o": r["orientation"] or 1, **({"s": r["seconds"]} if r["kind"] == "video" else {})} for r in rows]

    def places(self, areas: list[str], **filters) -> list[list]:
        """Every photo that knows where it was taken: [key, lat, lon, month, orientation], newest first."""
        where, params = self._where(areas, **filters)
        return [[r["key"], r["lat"], r["lon"], r["month"], r["orientation"] or 1] for r in self._all(
            f"SELECT key, lat, lon, month, orientation FROM photos WHERE {where} AND lat IS NOT NULL "
            "ORDER BY taken DESC, id", params)]

    def common_keywords(self, areas: list[str], limit: int = 80) -> list[str]:
        """The keywords used most often – so that new ones follow the ones already there."""
        where, params = self._where(areas)
        counts: dict[str, int] = {}
        for row in self._all(f"SELECT keywords FROM photos WHERE {where} AND keywords != ''", params):
            for word in row["keywords"].split("\n"):
                counts[word] = counts.get(word, 0) + 1
        return sorted(counts, key=lambda w: (-counts[w], w.lower()))[:limit]

    def place_names(self, areas: list[str]) -> list[dict]:
        """The countries with their towns and how many photos were taken there, the busiest country first."""
        where, params = self._where(areas)
        found: dict[str, dict] = {}
        for row in self._all(f"SELECT country, city, COUNT(*) n FROM photos WHERE {where} AND country != '' "
                             "GROUP BY country, city", params):
            entry = found.setdefault(row["country"], {"country": row["country"], "count": 0, "cities": []})
            entry["count"] += row["n"]
            entry["cities"].append({"city": row["city"], "count": row["n"]})
        for entry in found.values():
            entry["cities"].sort(key=lambda c: c["city"].lower())
        return sorted(found.values(), key=lambda e: (-e["count"], e["country"].lower()))

    def cameras(self, areas: list[str]) -> list[dict]:
        where, params = self._where(areas)
        return [{"model": r["model"], "count": r["n"]} for r in self._all(
            f"SELECT model, COUNT(*) n FROM photos WHERE {where} AND model != '' GROUP BY model ORDER BY n DESC",
            params)]

    def creators(self, areas: list[str]) -> list[dict]:
        where, params = self._where(areas)
        return [{"creator": r["creator"], "count": r["n"]} for r in self._all(
            f"SELECT creator, COUNT(*) n FROM photos WHERE {where} AND creator != '' GROUP BY creator ORDER BY n DESC",
            params)]

    def photo(self, key: str, areas: list[str]) -> dict | None:
        where, params = self._where(areas, films=True)
        rows = self._all(f"SELECT * FROM photos WHERE {where} AND key = ?", params + [key])
        return _entry(rows[0]) if rows else None

    def film_of(self, photo: dict, own: bool = False) -> dict | None:
        """The short film of a Live Photo: the film in the same folder that carries the same identifier.
        Of several, the one with the photo's own name. With `own`, only if the film is this photo's to
        take along: where two pictures share one film (the same photo as HEIC and as JPEG), it stays
        with the one that has its name, else with the first."""
        if photo.get("kind") != "image" or not photo.get("live"):
            return None
        folder = photo["path"].rpartition("/")[0]
        rows = self._all("SELECT * FROM photos WHERE live = ? AND folder = ? ORDER BY path", (photo["live"], folder))
        stem = lambda row: row["name"].rpartition(".")[0].casefold()        # noqa: E731
        films = sorted((row for row in rows if row["kind"] == "video"), key=lambda row: stem(row) != stem(photo))
        if not films:
            return None
        if own:
            stills = sorted((row for row in rows if row["kind"] == "image"), key=lambda row: stem(row) != stem(films[0]))
            if stills and stills[0]["path"] != photo["path"]:
                return None
        return _entry(films[0])

    def by_path(self, path: str) -> dict | None:
        rows = self._all("SELECT * FROM photos WHERE path = ?", (path,))
        return _entry(rows[0]) if rows else None

    def original_of(self, photo: dict) -> dict | None:
        """The picture this one was made from, while it lies in the same folder."""
        if not photo.get("source"):
            return None
        folder = photo["path"].rpartition("/")[0]
        rows = self._all("SELECT * FROM photos WHERE hash = ? AND folder = ? AND path != ? ORDER BY path",
                         (photo["source"], folder, photo["path"]))
        return _entry(rows[0]) if rows else None

    def company(self, photo: dict) -> list[tuple[dict, str]]:
        """What goes wherever this photo goes: (the file's entry, what is added to the photo's name
        to name it). The original of a picture worked on here, and the short film of a Live Photo."""
        found: list[tuple[dict, str]] = []
        original = self.original_of(photo)
        if original:
            found.append((original, "_original"))
        film = self.film_of(photo, own=True)
        if film:
            found.append((film, ""))
        return found

    def auto_albums(self, areas: list[str]) -> list[dict]:
        """The albums that fill themselves, with how much is in each – the empty ones left out."""
        found = []
        for name, (title, _) in AUTO.items():
            count = self.count_where(areas, auto=name)
            if count:
                found.append({"id": name, "name": title, "count": count})
        return found

    def album_tree(self, areas: list[str]) -> list[dict]:
        """Every album with its own count and the count including the albums below it."""
        where, params = self._where(areas)
        own: dict[str, int] = {}
        total: dict[str, int] = {}
        lies: dict[str, dict[str, int]] = {}        # an album is a mark in the picture – this says where its pictures lie
        for row in self._all(f"SELECT albums, folder FROM photos WHERE {where} AND albums != ''", params):
            seen = set()
            for album in row["albums"].strip("\n").split("\n"):
                own[album] = own.get(album, 0) + 1
                parts = album.split("/")
                seen.update("/".join(parts[:depth]) for depth in range(1, len(parts) + 1))
            for album in seen:          # a photo in two sub-albums counts once for the parent
                total[album] = total.get(album, 0) + 1
                folders = lies.setdefault(album, {})
                folders[row["folder"]] = folders.get(row["folder"], 0) + 1
        return [{"path": path, "name": path.rsplit("/", 1)[-1], "depth": path.count("/"),
                 "count": own.get(path, 0), "total": total[path], "folders": len(lies[path]),
                 "areas": sorted({folder.split("/", 1)[0] for folder in lies[path]}, key=str.lower),
                 "where": sorted(lies[path].items(), key=lambda item: (-item[1], item[0].lower()))[:8]}
                for path in sorted(total, key=lambda p: [s.lower() for s in p.split("/")])]

    def people(self, areas: list[str]) -> list[dict]:
        """Everyone named on a face, with the number of photos."""
        where, params = self._where(areas)
        counts: dict[str, int] = {}
        for row in self._all(f"SELECT people FROM photos WHERE {where} AND people != ''", params):
            for name in row["people"].strip("\n").split("\n"):
                counts[name] = counts.get(name, 0) + 1
        return [{"name": name, "count": counts[name]} for name in sorted(counts, key=str.lower)]

    def pictures(self, areas: list[str] | None = None) -> list[dict]:
        """Every readable picture, as short rows – what the face search works through."""
        where, params = self._where(areas) if areas is not None else ("1", [])
        rows = self._all(f"SELECT key, hash, area, path, orientation, regions FROM photos WHERE {where} "
                         "AND kind = 'image' AND error = '' ORDER BY taken DESC, id", params)
        return [dict(row, regions=json.loads(row["regions"] or "[]")) for row in rows]

    def files(self, areas: list[str]) -> list[dict]:
        """Every readable picture file with what tells two copies of a picture apart."""
        where, params = self._where(areas)
        rows = self._all("SELECT key, hash, area, path, name, size, width, height, taken, taken_src, orientation, rating, "
                         f"keywords, albums, people FROM photos WHERE {where} AND kind = 'image' AND error = '' "
                         "ORDER BY path", params)
        return [dict(row, **{field: [item for item in (row[field] or "").split("\n") if item]
                             for field in ("keywords", "albums", "people")}) for row in rows]

    def by_name(self, areas: list[str]) -> list[dict]:
        """Every readable picture with what tells it from others of the same name – for finding
        the pictures another programme speaks of."""
        where, params = self._where(areas)
        rows = self._all(f"SELECT path, name, taken, people FROM photos WHERE {where} AND kind = 'image' "
                         "AND error = '' ORDER BY path", params)
        return [dict(row, people=[name for name in row["people"].split("\n") if name]) for row in rows]

    def with_person(self, areas: list[str], name: str) -> list[dict]:
        """The photos somebody is named on, as full entries."""
        where, params = self._where(areas, person=name)
        return [_entry(row) for row in self._all(f"SELECT * FROM photos WHERE {where} ORDER BY path", params)]

    def with_hash(self, digest: str, areas: list[str]) -> list[dict]:
        """The photos that hold this very picture – usually one."""
        where, params = self._where(areas)
        return [_entry(row) for row in self._all(f"SELECT * FROM photos WHERE {where} AND hash = ? ORDER BY path",
                                                 params + [digest])]

    def in_folder(self, folder: str, limit: int = 100000) -> list[dict]:
        """The pictures in a folder and below, as full entries."""
        rows = self._all("SELECT * FROM photos WHERE (folder = ? OR folder LIKE ? ESCAPE '\\') AND kind = 'image' "
                         "ORDER BY path LIMIT ?", (folder, _like(folder) + "/%", limit))
        return [_entry(row) for row in rows]

    def refresh(self, path: str) -> dict:
        """Read one file again right now – after it was changed – and hand back its new entry."""
        record = examine(self.root, path, self.thumbs)
        db = self._connect()
        try:
            with self._write:
                self._save(db, record)
                db.commit()
        finally:
            db.close()
        return self.by_path(path)

    def forget(self, path: str) -> None:
        db = self._connect()
        try:
            with self._write:
                db.execute("DELETE FROM photos WHERE path = ?", (path,))
                db.commit()
                self.changes += 1
        finally:
            db.close()

    def count(self) -> int:
        return self._all("SELECT COUNT(*) n FROM photos")[0]["n"]
