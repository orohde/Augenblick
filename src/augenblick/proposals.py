"""What the AI proposed for the pictures in the inbox.

A proposal – description, keywords, perhaps a date – is kept beside the picture until someone takes
it over or throws it away; nothing of it is written into the file before that. It belongs to the
place the picture lies in (its path), so turning or changing the picture in the inbox keeps it;
once the picture has left the inbox, the proposal goes too.

When asked to, the AI looks at what arrives by itself (as in Ordnung!: switched on in the AI
settings, off unless chosen). Pictures may also be put in line by hand, a few chosen ones at a time.
A picture the AI could not answer for is not asked about again by itself.

The proposals live in ``proposals.db`` in the data folder; deleting it loses nothing but them.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS proposals (
    path    TEXT PRIMARY KEY,
    data    TEXT NOT NULL,
    ts      REAL NOT NULL,
    failed  INTEGER NOT NULL DEFAULT 0
)
"""


class Proposals:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self._wanted: list[str] = []        # paths put in line by hand, oldest first
        self.progress = {"running": False, "done": 0, "left": 0, "error": ""}
        with self._connect() as db:
            db.execute(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        return db

    # ------------------------------------------------------------ the store

    def get(self, path: str) -> dict | None:
        """The proposal for the picture at this place; None where there is none or the AI failed."""
        with self._connect() as db:
            row = db.execute("SELECT data, ts FROM proposals WHERE path = ? AND failed = 0", (path,)).fetchone()
        return {**json.loads(row["data"]), "ts": row["ts"]} if row else None

    def known(self) -> set[str]:
        """Every place that has an answer – or that the AI could not answer for."""
        with self._connect() as db:
            return {row["path"] for row in db.execute("SELECT path FROM proposals")}

    def put(self, path: str, data: dict) -> None:
        keep = {k: data.get(k) for k in ("description", "keywords", "date", "note", "model", "cost")}
        with self._lock, self._connect() as db:
            db.execute("INSERT OR REPLACE INTO proposals (path, data, ts, failed) VALUES (?, ?, ?, 0)",
                       (path, json.dumps(keep, ensure_ascii=False), time.time()))

    def fail(self, path: str, error: str) -> None:
        with self._lock, self._connect() as db:
            db.execute("INSERT OR REPLACE INTO proposals (path, data, ts, failed) VALUES (?, ?, ?, 1)",
                       (path, json.dumps({"error": error[:200]}), time.time()))

    def repath(self, old: str, new: str) -> None:
        """The picture moved (its folder did): what was proposed for it stays with it."""
        with self._lock, self._connect() as db:
            db.execute("UPDATE proposals SET path = ? WHERE path = ?", (new, old))

    def drop(self, path: str) -> None:
        with self._lock, self._connect() as db:
            db.execute("DELETE FROM proposals WHERE path = ?", (path,))

    def tidy(self, inbox_paths: set[str]) -> int:
        """Forget the proposals of pictures that are no longer in the inbox."""
        with self._lock, self._connect() as db:
            gone = [row["path"] for row in db.execute("SELECT path FROM proposals") if row["path"] not in inbox_paths]
            db.executemany("DELETE FROM proposals WHERE path = ?", [(p,) for p in gone])
        with self._lock:
            self._wanted = [p for p in self._wanted if p in inbox_paths]
        return len(gone)

    # ------------------------------------------------------------ the line

    def want(self, paths: list[str]) -> int:
        """Put pictures in line for the AI – asked for by hand, so again even where it failed before."""
        with self._lock:
            new = [p for p in paths if p not in self._wanted]
            self._wanted.extend(new)
        with self._lock, self._connect() as db:
            db.executemany("DELETE FROM proposals WHERE path = ? AND failed = 1", [(p,) for p in new])
        return len(new)

    def wanted(self) -> list[str]:
        with self._lock:
            return list(self._wanted)

    def served(self, path: str) -> None:
        with self._lock:
            if path in self._wanted:
                self._wanted.remove(path)

    def clear_line(self) -> None:
        with self._lock:
            self._wanted.clear()
