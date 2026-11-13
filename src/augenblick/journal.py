"""The journal: what was changed in which photo, by whom, and how to take it back.

Unlike the index it cannot be rebuilt from the photos, so it lives in its own file.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS entries (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    user TEXT NOT NULL,
    batch TEXT NOT NULL,             -- changes made in one go share this
    action TEXT NOT NULL,            -- meta | rotate | move | faces
    area TEXT NOT NULL,
    path TEXT NOT NULL,
    before TEXT NOT NULL,            -- what a person saw before (JSON)
    after TEXT NOT NULL,             -- … and after
    tags TEXT NOT NULL,              -- the exact tag values before the change (JSON), for taking it back
    sha_after TEXT NOT NULL,         -- SHA-256 of the file after the change
    undone REAL,
    undone_by TEXT
);
CREATE INDEX IF NOT EXISTS entries_path ON entries (path);
"""


class Journal:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        return db

    @staticmethod
    def _entry(row: sqlite3.Row) -> dict:
        entry = dict(row)
        for field in ("before", "after", "tags"):
            entry[field] = json.loads(entry[field])
        return entry

    def add(self, *, user: str, batch: str, action: str, path: str, before: dict, after: dict, tags: dict,
            sha_after: str) -> int:
        with self._lock, self._connect() as db:
            cursor = db.execute(
                "INSERT INTO entries (ts, user, batch, action, area, path, before, after, tags, sha_after) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (time.time(), user, batch, action, path.split("/", 1)[0], path, json.dumps(before, ensure_ascii=False),
                 json.dumps(after, ensure_ascii=False), json.dumps(tags, ensure_ascii=False), sha_after))
            return cursor.lastrowid

    def get(self, entry_id: int) -> dict | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM entries WHERE id = ?", (entry_id,)).fetchone()
        return self._entry(row) if row else None

    def list(self, areas: list[str], limit: int = 200, before: int | None = None) -> list[dict]:
        if not areas:
            return []
        sql = f"SELECT * FROM entries WHERE area IN ({', '.join('?' * len(areas))})"
        params: list = list(areas)
        if before:
            sql += " AND id < ?"
            params.append(before)
        with self._connect() as db:
            rows = db.execute(sql + " ORDER BY id DESC LIMIT ?", params + [limit]).fetchall()
        return [self._entry(row) for row in rows]

    def repath(self, old: str, new: str) -> None:
        """A file moved: its earlier entries follow, so they can still be taken back."""
        with self._lock, self._connect() as db:
            db.execute("UPDATE entries SET path = ?, area = ? WHERE path = ?", (new, new.split("/", 1)[0], old))

    def mark_undone(self, entry_id: int, user: str) -> None:
        with self._lock, self._connect() as db:
            db.execute("UPDATE entries SET undone = ?, undone_by = ? WHERE id = ?", (time.time(), user, entry_id))
