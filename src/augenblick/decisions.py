"""What somebody decided, kept beside the photos.

Names, albums and dates stand in the photos themselves. A few decisions have no place there:
"this is nobody to be named", "the AI says this is no face", "this face stands for the person",
"these two look alike but are different pictures". They live in the data folder – and that folder
may be lost while the photos survive.

So every area carries a small file, `<area>/.augenblick/entscheidungen.json`, written in plain
words. It is a copy: the app works from its own stores and writes the file anew whenever a
decision or a path changes. It is read only where a store is empty – after the data folder was
lost –, kind by kind.

A decision hangs on the picture itself (the hash of its data) and a spot in it, never on a path.
Moving or renaming a file therefore changes nothing; the path in the file is there for the
reader and follows by itself.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from datetime import datetime
from pathlib import Path

from .index import Index
from .media import turn_box
from .trash import Prefs

log = logging.getLogger("augenblick.decisions")

FOLDER = ".augenblick"
NAME = "entscheidungen.json"
NOTE = ("Entscheidungen aus Augenblick!, die nicht im Bild selbst stehen. Diese Datei ist eine Abschrift: Augenblick! "
        "schreibt sie bei jeder Änderung neu und liest sie nur, wenn sein eigener Datenordner verloren ging. "
        "'bild' ist die Kennung des Bildinhalts – daran hängt die Entscheidung, nicht am Dateinamen; 'datei' zeigt nur, "
        "wo das Bild zuletzt lag. 'rahmen' ist die Stelle im Bild: Mitte x, Mitte y, Breite, Höhe als Anteil des Bildes.")


def _when(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def _ts(text) -> float:
    try:
        return datetime.strptime(str(text), "%Y-%m-%d %H:%M:%S").timestamp()
    except ValueError:
        return time.time()


def _box(row) -> list[float]:
    return [round(row[side], 5) for side in ("x", "y", "w", "h")]


class Decisions:
    def __init__(self, root: Path, index: Index, prefs: Prefs, ignored_path: Path, apart_path: Path):
        self.root = root
        self.index = index
        self.prefs = prefs
        self.ignored_path = ignored_path        # holds the faces waved away and what the AI said
        self.apart_path = apart_path
        self._seen = None

    @staticmethod
    def _rows(path: Path, sql: str) -> list[sqlite3.Row]:
        if not path.is_file():
            return []
        db = sqlite3.connect(path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            return db.execute(sql).fetchall()
        except sqlite3.OperationalError:        # a store from before this table was thought of
            return []
        finally:
            db.close()

    def _collect(self) -> dict:
        return {"ignored": self._rows(self.ignored_path, "SELECT hash, x, y, w, h, user, ts FROM ignored ORDER BY ts, hash"),
                "judged": self._rows(self.ignored_path, "SELECT hash, x, y, w, h, face, ts FROM judged ORDER BY ts, hash"),
                "apart": self._rows(self.apart_path, "SELECT a, b, user, ts FROM apart ORDER BY ts, a, b"),
                "covers": self.prefs.load()["covers"]}

    def _signature(self, found: dict):
        return (self.index.changes, len(found["ignored"]), len(found["judged"]), len(found["apart"]),
                max([row["ts"] for kind in ("ignored", "judged", "apart") for row in found[kind]], default=0),
                json.dumps(found["covers"], sort_keys=True))

    # ------------------------------------------------------------ a picture turned

    def turned(self, old: str, new: str, n: int, path: str) -> int:
        """A picture was turned without loss: its data – and with it its hash – is another now.
        What was decided about it goes along, every spot turned the way the picture was (through
        orientation n). Where another file still holds the old picture, that one keeps its own."""
        if old == new:
            return 0
        still = bool(self.index._all("SELECT 1 FROM photos WHERE hash = ? AND path != ? LIMIT 1", (old, path)))
        moved = 0
        if self.ignored_path.is_file():
            db = sqlite3.connect(self.ignored_path, timeout=30)
            try:
                with db:
                    for table, more in (("ignored", "user"), ("judged", "face")):
                        try:
                            rows = db.execute(f"SELECT x, y, w, h, {more}, ts FROM {table} WHERE hash = ? ORDER BY rowid", (old,)).fetchall()
                        except sqlite3.OperationalError:
                            continue
                        for x, y, w, h, extra, ts in rows:
                            db.execute(f"INSERT INTO {table} VALUES (?, ?, ?, ?, ?, ?, ?)", (new, *turn_box((x, y, w, h), n), extra, ts))
                        if not still:
                            db.execute(f"DELETE FROM {table} WHERE hash = ?", (old,))
                        moved += len(rows)
            finally:
                db.close()
        if self.apart_path.is_file():
            db = sqlite3.connect(self.apart_path, timeout=30)
            try:
                with db:
                    rows = db.execute("SELECT a, b, user, ts FROM apart WHERE a = ? OR b = ?", (old, old)).fetchall()
                    for a, b, user, ts in rows:
                        one, other = sorted((new, b if a == old else a))
                        db.execute("INSERT OR REPLACE INTO apart VALUES (?, ?, ?, ?)", (one, other, user, ts))
                    if not still:
                        db.execute("DELETE FROM apart WHERE a = ? OR b = ?", (old, old))
                    moved += len(rows)
            finally:
                db.close()
        covers = dict(self.prefs.load()["covers"])
        mine = [name for name, cover in covers.items() if cover.get("hash") == old]
        if mine and not still:
            for name in mine:
                covers[name] = {"hash": new, "box": list(turn_box(tuple(covers[name]["box"]), n))}
            self.prefs.save(covers=covers)
            moved += len(mine)
        return moved

    # ------------------------------------------------------------ writing the copies

    def write(self, force: bool = False) -> int:
        """Bring the file of every area up to date; returns how many were written. Cheap when nothing changed."""
        found = self._collect()
        signature = self._signature(found)
        if signature == self._seen and not force:
            return 0
        where: dict[str, tuple[str, str]] = {}          # picture hash -> (area, path inside the area)
        for row in self.index._all("SELECT hash, area, path FROM photos ORDER BY path"):
            where.setdefault(row["hash"], (row["area"], row["path"].split("/", 1)[-1]))
        areas: dict[str, dict] = {}

        def of(digest: str) -> tuple[dict, str] | None:
            if digest not in where:
                return None                              # the picture is gone; so is what was decided about it
            area, path = where[digest]
            return areas.setdefault(area, {"ignorierte_gesichter": [], "ki_kein_gesicht": [], "ki_gesicht": [],
                                           "schluesselbilder": {}, "keine_duplikate": []}), path

        for row in found["ignored"]:
            if (hit := of(row["hash"])):
                hit[0]["ignorierte_gesichter"].append({"bild": row["hash"], "datei": hit[1], "rahmen": _box(row),
                                                       "von": row["user"], "am": _when(row["ts"])})
        latest: dict[tuple, sqlite3.Row] = {}
        for row in found["judged"]:                      # in the order they were said: the last word counts
            latest[(row["hash"], *_box(row))] = row
        for row in latest.values():
            if (hit := of(row["hash"])):
                hit[0]["ki_gesicht" if row["face"] else "ki_kein_gesicht"].append(
                    {"bild": row["hash"], "datei": hit[1], "rahmen": _box(row), "am": _when(row["ts"])})
        for name, cover in sorted(found["covers"].items()):
            if (hit := of(cover.get("hash", ""))):
                hit[0]["schluesselbilder"][name] = {"bild": cover["hash"], "datei": hit[1],
                                                    "rahmen": [round(n, 5) for n in cover["box"]]}
        for row in found["apart"]:
            one, other = of(row["a"]), of(row["b"])
            if one and other:
                one[0]["keine_duplikate"].append({"bilder": [row["a"], row["b"]], "dateien": [one[1], other[1]],
                                                  "von": row["user"], "am": _when(row["ts"])})
        written = 0
        names = {area["name"] for area in self.index.areas()}
        for name in sorted(names):
            target = self.root / name / FOLDER / NAME
            content = areas.get(name)
            if content is None or not any(content.values()):
                if target.is_file():                     # nothing decided here any more
                    target.unlink()
                    written += 1
                continue
            try:
                old = json.loads(target.read_text(encoding="utf-8"))
                old.pop("stand", None)
                old.pop("hinweis", None)
            except (OSError, ValueError):
                old = None
            if old == content:
                continue
            target.parent.mkdir(exist_ok=True)
            temp = target.with_suffix(".tmp")
            temp.write_text(json.dumps({"hinweis": NOTE, "stand": _when(time.time()), **content},
                                       ensure_ascii=False, indent=1), encoding="utf-8")
            temp.replace(target)
            written += 1
        self._seen = signature
        return written

    # ------------------------------------------------------------ reading them back

    def restore(self) -> dict:
        """Take back from the files what the stores no longer know – only where a store is empty,
        so that a file left behind never overrules what was decided since."""
        files = []
        if self.root.is_dir():
            for folder in sorted(self.root.iterdir()):
                source = folder / FOLDER / NAME
                if folder.is_dir() and not folder.name.startswith((".", "@")) and source.is_file():
                    try:
                        files.append(json.loads(source.read_text(encoding="utf-8")))
                    except (OSError, ValueError):
                        log.warning("cannot read %s", source)
        if not files:
            return {}
        found = self._collect()
        done = {"ignored": 0, "judged": 0, "apart": 0, "covers": 0}
        faces = sqlite3.connect(self.ignored_path, timeout=30)
        try:
            with faces:
                for data in files:
                    if not found["ignored"]:
                        for entry in data.get("ignorierte_gesichter", []):
                            faces.execute("INSERT INTO ignored VALUES (?, ?, ?, ?, ?, ?, ?)",
                                          (entry["bild"], *entry["rahmen"], entry.get("von", ""), _ts(entry.get("am"))))
                            done["ignored"] += 1
                    if not found["judged"]:
                        for key, verdict in (("ki_kein_gesicht", 0), ("ki_gesicht", 1)):
                            for entry in data.get(key, []):
                                faces.execute("INSERT INTO judged VALUES (?, ?, ?, ?, ?, ?, ?)",
                                              (entry["bild"], *entry["rahmen"], verdict, _ts(entry.get("am"))))
                                done["judged"] += 1
        finally:
            faces.close()
        if not found["apart"]:
            apart = sqlite3.connect(self.apart_path, timeout=30)
            try:
                with apart:
                    for data in files:
                        for entry in data.get("keine_duplikate", []):
                            a, b = sorted(entry["bilder"])
                            apart.execute("INSERT OR REPLACE INTO apart VALUES (?, ?, ?, ?)",
                                          (a, b, entry.get("von", ""), _ts(entry.get("am"))))
                            done["apart"] += 1
            finally:
                apart.close()
        if not found["covers"]:
            covers = {name: {"hash": cover["bild"], "box": cover["rahmen"]}
                      for data in files for name, cover in data.get("schluesselbilder", {}).items()}
            if covers:
                self.prefs.save(covers=covers)
                done["covers"] = len(covers)
        return {kind: count for kind, count in done.items() if count}
