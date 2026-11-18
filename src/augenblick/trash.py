"""The trash: deleting moves a photo into a hidden folder of its area, from where it can come back.

The folder lies inside the area, so moving is a rename on the same disk – nothing is copied,
and the photo never leaves the place its owner chose for it. The scanner skips hidden folders.
What is in the trash is written down in the journal file, which cannot be rebuilt.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path

from .index import Index

log = logging.getLogger("augenblick.trash")

FOLDER = ".augenblick-papierkorb"
SCHEMA = """
CREATE TABLE IF NOT EXISTS trash (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    user TEXT NOT NULL,
    area TEXT NOT NULL,
    path TEXT NOT NULL,              -- where the photo was
    stored TEXT NOT NULL,            -- where it is now, below the photo root
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    size INTEGER NOT NULL,
    taken TEXT NOT NULL,
    hash TEXT NOT NULL,              -- finds the preview that is still in the cache
    orientation INTEGER NOT NULL
);
"""


class TrashError(Exception):
    pass


class Prefs:
    """The few settings that belong to the installation, not to a person or a browser."""

    DEFAULTS = {"trash_days": 30, "sort_rules": [], "sort_modes": {}, "faces": [], "covers": {},
                "areas": {},      # per area what it does without: {"Bildschirmfotos": {"places": false, "dates": false}}
                "spots": [],      # places one picks again and again: [{"name": "Zuhause", "lat": 49.86, "lon": 7.8}]
                "mcp": False}     # the MCP endpoint for AI assistants; off until an administrator turns it on

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()

    def load(self) -> dict:
        try:
            stored = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            stored = {}
        return {**self.DEFAULTS, **{k: v for k, v in stored.items() if k in self.DEFAULTS}}

    def save(self, **changes) -> dict:
        with self._lock:
            merged = {**self.load(), **changes}
            temp = self.path.with_suffix(".tmp")
            temp.write_text(json.dumps(merged, indent=1), encoding="utf-8")
            temp.replace(self.path)
            return merged


class Trash:
    def __init__(self, db_path: Path, root: Path, index: Index, lock: threading.Lock):
        self.db_path = db_path
        self.root = root
        self.index = index
        self._lock = lock          # shared with the editor: one change to the photos at a time
        with self._connect() as db:
            db.executescript(SCHEMA)
            have = {row["name"] for row in db.execute("PRAGMA table_info(trash)")}
            # what went into the trash with a photo: the short film of a Live Photo (film: where it was,
            # film_stored: where it is), and since then anything, as a list of [where it was, where it is]
            for column in ("film", "film_stored", "extras"):
                if column not in have:
                    db.execute(f"ALTER TABLE trash ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.db_path, timeout=30)
        db.row_factory = sqlite3.Row
        return db

    def _inside(self, relative: str) -> Path:
        root = self.root.resolve()
        path = (root / relative).resolve()
        if root not in path.parents:
            raise TrashError("file not found")
        return path

    # ------------------------------------------------------------ in

    def put(self, photo: dict, user: str, alone: bool = False) -> int:
        """Into the trash, with what belongs to the photo – unless it is to go `alone`."""
        with self._lock:
            src = self._inside(photo["path"])
            if not src.is_file():
                raise TrashError("file not found")
            folder = self.root / photo["area"] / FOLDER
            try:
                folder.mkdir(exist_ok=True)
            except OSError as exc:
                raise TrashError("file is not writable") from exc
            # the short film of a Live Photo and the original of a picture worked on go with it
            mates = [] if alone else [mate for mate, _ in self.index.company(photo)
                                      if self._inside(mate["path"]).is_file()]
            with self._connect() as db:
                entry = db.execute(
                    "INSERT INTO trash (ts, user, area, path, stored, name, kind, size, taken, hash, orientation) "
                    "VALUES (?, ?, ?, ?, '', ?, ?, ?, ?, ?, ?)",
                    (time.time(), user, photo["area"], photo["path"], photo["name"], photo["kind"], photo["size"],
                     photo["taken"], photo["hash"], photo["orientation"] or 1)).lastrowid
                stored = f"{photo['area']}/{FOLDER}/{entry}-{photo['name']}"
                extras = [[mate["path"], f"{photo['area']}/{FOLDER}/{entry}-{mate['name']}"] for mate in mates]
                done: list[list[str]] = []
                try:
                    os.rename(src, self.root / stored)
                    try:
                        for was, kept in extras:
                            os.rename(self.root / was, self.root / kept)
                            done.append([was, kept])
                    except OSError:
                        for was, kept in done:                      # all or nothing
                            os.rename(self.root / kept, self.root / was)
                        os.rename(self.root / stored, src)
                        raise
                except OSError as exc:
                    db.rollback()
                    raise TrashError("file is not writable") from exc
                db.execute("UPDATE trash SET stored = ?, extras = ? WHERE id = ?",
                           (stored, json.dumps(extras) if extras else "", entry))
            self.index.forget(photo["path"])
            for was, _ in extras:
                self.index.forget(was)
            return entry

    @staticmethod
    def extras(entry: dict) -> list[list[str]]:
        """What lies in the trash with this photo: [where it was, where it is]."""
        found = json.loads(entry.get("extras") or "[]")
        if entry.get("film_stored"):
            found.append([entry["film"], entry["film_stored"]])
        return found

    # ------------------------------------------------------------ looking

    def list(self, areas: list[str]) -> list[dict]:
        if not areas:
            return []
        with self._connect() as db:
            rows = db.execute(f"SELECT * FROM trash WHERE area IN ({', '.join('?' * len(areas))}) ORDER BY id DESC",
                              areas).fetchall()
        return [dict(row) for row in rows]

    def get(self, entry: int) -> dict | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM trash WHERE id = ?", (entry,)).fetchone()
        return dict(row) if row else None

    def _drop(self, entry: int) -> None:
        with self._connect() as db:
            db.execute("DELETE FROM trash WHERE id = ?", (entry,))

    # ------------------------------------------------------------ out

    def restore(self, entry: dict) -> dict:
        """Back to where it was. If something else has that name by now, it gets a number."""
        with self._lock:
            stored = self._inside(entry["stored"])
            if not stored.is_file():
                self._drop(entry["id"])
                raise TrashError("file not found")
            target = self._inside(entry["path"])
            mates = [(self._inside(kept), self._inside(was)) for was, kept in self.extras(entry)]
            mates = [(kept, home) for kept, home in mates if kept.is_file()]
            n = 1
            numbered = lambda path: path.with_name(f"{path.stem} ({n}){path.suffix}") if n > 1 else path   # noqa: E731
            while numbered(target).exists() or any(numbered(home).exists() for _, home in mates):
                n += 1
            target, mates = numbered(target), [(kept, numbered(home)) for kept, home in mates]
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                os.rename(stored, target)
                for kept, home in mates:
                    home.parent.mkdir(parents=True, exist_ok=True)
                    os.rename(kept, home)
            except OSError as exc:
                raise TrashError("file is not writable") from exc
            self._drop(entry["id"])
            for _, home in mates:               # the original first: the picture made from it then finds it
                self.index.refresh(home.relative_to(self.root.resolve()).as_posix())
            return self.index.refresh(target.relative_to(self.root.resolve()).as_posix())

    def destroy(self, entry: dict) -> None:
        """Gone for good."""
        with self._lock:
            try:
                self._inside(entry["stored"]).unlink(missing_ok=True)
                for _, kept in self.extras(entry):
                    self._inside(kept).unlink(missing_ok=True)
            except OSError as exc:
                raise TrashError("file is not writable") from exc
            self._drop(entry["id"])

    def purge(self, days: int) -> int:
        """Remove what has been in the trash longer than the given days. 0 keeps everything."""
        if days <= 0:
            return 0
        with self._connect() as db:
            old = [dict(r) for r in db.execute("SELECT * FROM trash WHERE ts < ?", (time.time() - days * 86400,))]
        done = 0
        for entry in old:
            try:
                self.destroy(entry)
                done += 1
            except TrashError:
                log.warning("cannot remove %s from the trash", entry["stored"])
        return done
