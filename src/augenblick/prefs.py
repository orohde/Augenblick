"""Per-person view settings that follow the account, not the browser.

Kept in ``prefs.json`` in the data folder, one entry per user name: the language of the
interface and the albums pinned to the start page. (Taken over from Ordnung!, which also keeps the expanded folders of its tree here.)
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

LANGUAGES = ("de", "en", "fr", "it")
MAX_PATHS = 5000
MAX_PATH_LEN = 1000
MAX_PINS = 100


class PrefsStore:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()

    def _load(self) -> dict[str, dict]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        return {name: dict(p) for name, p in raw.get("users", {}).items()}

    def _save(self, users: dict[str, dict]) -> None:
        data = {"version": 1, "users": dict(sorted(users.items()))}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.path)

    def get(self, user: str) -> dict:
        with self._lock:
            p = self._load().get(user, {})
        return {"tree_open": list(p.get("tree_open", [])), "language": p.get("language", ""),
                "pins": list(p.get("pins", []))}

    def set_pins(self, user: str, albums: list[str]) -> list[str]:
        """The albums pinned to the start page, in their order."""
        clean = list(dict.fromkeys(str(a).strip("/") for a in albums if a and len(str(a)) <= MAX_PATH_LEN))[:MAX_PINS]
        with self._lock:
            users = self._load()
            users.setdefault(user, {})["pins"] = clean
            self._save(users)
        return clean

    def set_language(self, user: str, language: str) -> str:
        if language not in LANGUAGES:
            raise ValueError("unknown language")
        with self._lock:
            users = self._load()
            users.setdefault(user, {})["language"] = language
            self._save(users)
        return language

    def set_tree_open(self, user: str, paths: list[str]) -> list[str]:
        clean = sorted({str(p) for p in paths if p and len(str(p)) <= MAX_PATH_LEN})[:MAX_PATHS]
        with self._lock:
            users = self._load()
            users.setdefault(user, {})["tree_open"] = clean
            self._save(users)
        return clean

    def forget(self, user: str) -> None:
        with self._lock:
            users = self._load()
            if users.pop(user, None) is not None:
                self._save(users)
