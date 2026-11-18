"""Place names for coordinates: the nearest town from the GeoNames list, looked up on this machine.

The list is fetched once, when the image is built, and boiled down to a small SQLite file. At run time
nothing goes outside. The names are worked out from the coordinates and live in the index only – no
photo is written for them.

This file runs on its own (python geo.py fetch <folder> [language]) and needs nothing but Python.
"""

from __future__ import annotations

import io
import math
import sqlite3
import sys
import tempfile
import threading
import time
import urllib.request
import zipfile
from functools import lru_cache
from pathlib import Path

SOURCE = "https://download.geonames.org/export/dump/"     # GeoNames, CC BY 4.0
FILE = "places.db"
REACH_KM = 25.0         # a town further away than this is not "where the photo was taken"
BAND = 0.25             # degrees of latitude per band – a little more than the reach, so three bands cover it
NOT_TOWNS = {"PPLX", "PPLH", "PPLQ", "PPLW", "PPLCH"}     # quarters of a town, and places nobody lives in any more
EARTH_KM = 6371.0088


def _band(lat: float) -> int:
    return int(math.floor((lat + 90.0) / BAND))


def _spread(people: int) -> float:
    """Roughly how far a town of this size reaches from its centre, in km – so that the middle of a big
    city is named after the city and not after the small neighbour whose centre happens to be nearer."""
    return 0.6 * math.sqrt(max(0, people) / (3000 * math.pi))


def _distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    a, b = math.radians(lat1), math.radians(lat2)
    h = math.sin((b - a) / 2) ** 2 + math.cos(a) * math.cos(b) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 2 * EARTH_KM * math.asin(min(1.0, math.sqrt(h)))



def distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Kilometres between two points on the earth's surface."""
    return _distance(lat1, lon1, lat2, lon2)


class Geo:
    """Reads the boiled-down list. Without the file every question is answered with nothing."""

    def __init__(self, folder: Path | None):
        self.path = folder / FILE if folder else None
        self.stamp = ""
        self._db: sqlite3.Connection | None = None
        self._lock = threading.Lock()
        if self.path and self.path.is_file():
            try:
                self._db = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, check_same_thread=False)
                row = self._db.execute("SELECT value FROM meta WHERE name = 'stamp'").fetchone()
                self.stamp = row[0] if row else ""
            except sqlite3.Error:
                self._db, self.stamp = None, ""
        self._cached = lru_cache(maxsize=50000)(self._nearest)

    @property
    def ready(self) -> bool:
        return self._db is not None

    def lookup(self, lat: float | None, lon: float | None) -> tuple[str, str, str]:
        """(town, state, country) of the nearest town within reach, or three empty strings."""
        if self._db is None or lat is None or lon is None or abs(lat) > 90 or abs(lon) > 180:
            return "", "", ""
        return self._cached(round(lat, 4), round(lon, 4))       # 4 places: about ten metres

    def _nearest(self, lat: float, lon: float) -> tuple[str, str, str]:
        band = _band(lat)
        width = math.degrees(REACH_KM / EARTH_KM) / max(0.01, math.cos(math.radians(lat)))
        sql = "SELECT lat, lon, city, state, country, spread FROM places WHERE band IN (?, ?, ?)"
        params: list = [band - 1, band, band + 1]
        if lon - width >= -180 and lon + width <= 180:           # at the date line: take the whole band
            sql += " AND lon BETWEEN ? AND ?"
            params += [lon - width, lon + width]
        with self._lock:
            rows = self._db.execute(sql, params).fetchall()
        best, best_km = None, 0.0
        for row in rows:        # a big town reaches further than its centre: its spread counts in its favour
            km = _distance(lat, lon, row[0], row[1])
            if km <= REACH_KM and (best is None or km - row[5] < best_km):
                best, best_km = row, km - row[5]
        return (best[2], best[3], best[4]) if best else ("", "", "")


# ------------------------------------------------------------------ building the list


def _download(name: str, target: Path) -> None:
    request = urllib.request.Request(SOURCE + name, headers={"User-Agent": "augenblick-build"})
    with urllib.request.urlopen(request, timeout=300) as answer, open(target, "wb") as out:
        while chunk := answer.read(1 << 20):
            out.write(chunk)


def _lines(path: Path, member: str | None = None):
    """The lines of a text file, or of one file inside a zip, without unpacking it to disk."""
    if member is None:
        with open(path, encoding="utf-8") as handle:
            yield from handle
        return
    with zipfile.ZipFile(path) as archive, archive.open(member) as raw:
        yield from io.TextIOWrapper(raw, encoding="utf-8")


def _names_in(path: Path, language: str, wanted: set[int], short: set[int]) -> dict[int, str]:
    """The name each of these places goes by in the language – the preferred one, never an old or casual one.
    For those in `short` (the countries) the everyday name wins over the official one."""
    best: dict[int, tuple[int, str]] = {}
    for line in _lines(path, "alternateNamesV2.txt"):
        part = line.rstrip("\n").split("\t")
        if len(part) < 8 or part[2] != language or part[6] == "1" or part[7] == "1":
            continue
        place = int(part[1])
        if place not in wanted:
            continue
        if place in short and len(part[3]) <= 3:      # "USA", "UK": an abbreviation, not a name
            continue
        if place in short:
            rank = (4 if part[5] == "1" else 0) + (2 if part[4] == "1" else 0)
        else:
            rank = (2 if part[4] == "1" else 0) + (0 if part[5] == "1" else 1)
        if place not in best or rank > best[place][0] or (
                place in short and rank == best[place][0] and len(part[3]) < len(best[place][1])):
            best[place] = (rank, part[3])
    return {place: name for place, (_, name) in best.items()}


def build(target: Path, language: str = "", source: Path | None = None) -> int:
    """Fetch the GeoNames files (or take them from `source`) and write places.db. Returns the number of towns."""
    target.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        work = source or Path(tmp)
        files = ["cities500.zip", "admin1CodesASCII.txt", "countryInfo.txt"] + (["alternateNamesV2.zip"] if language else [])
        if source is None:
            for name in files:
                print(f"fetching {name}", flush=True)
                _download(name, work / name)

        countries: dict[str, tuple[int, str]] = {}      # DE -> (id, name)
        for line in _lines(work / "countryInfo.txt"):
            part = line.rstrip("\n").split("\t")
            if line.startswith("#") or len(part) < 17:
                continue
            countries[part[0]] = (int(part[16] or 0), part[4])
        states: dict[str, tuple[int, str]] = {}         # DE.08 -> (id, name)
        for line in _lines(work / "admin1CodesASCII.txt"):
            part = line.rstrip("\n").split("\t")
            if len(part) >= 4:
                states[part[0]] = (int(part[3] or 0), part[1])
        towns = []
        for line in _lines(work / "cities500.zip", "cities500.txt"):
            part = line.rstrip("\n").split("\t")
            if len(part) >= 11 and part[7] not in NOT_TOWNS:
                towns.append((int(part[0]), part[1], float(part[4]), float(part[5]), part[8], part[10],
                              int(part[14] or 0) if len(part) > 14 else 0))
        if len(towns) < 1000 and source is None:
            raise RuntimeError(f"the town list is far too short ({len(towns)} entries)")

        local: dict[int, str] = {}
        if language and (work / "alternateNamesV2.zip").is_file():
            print(f"reading the names in '{language}'", flush=True)
            wanted = {t[0] for t in towns} | {s[0] for s in states.values()} | {c[0] for c in countries.values()}
            local = _names_in(work / "alternateNamesV2.zip", language, wanted, {c[0] for c in countries.values()})

        fresh = target / (FILE + ".new")
        fresh.unlink(missing_ok=True)
        db = sqlite3.connect(fresh)
        db.executescript("CREATE TABLE places (band INTEGER NOT NULL, lat REAL NOT NULL, lon REAL NOT NULL, "
                         "city TEXT NOT NULL, state TEXT NOT NULL, country TEXT NOT NULL, spread REAL NOT NULL);"
                         "CREATE TABLE meta (name TEXT PRIMARY KEY, value TEXT);")
        rows = []
        for place, name, lat, lon, country, admin1, people in towns:
            state_id, state = states.get(f"{country}.{admin1}", (0, ""))
            country_id, land = countries.get(country, (0, country))
            rows.append((_band(lat), lat, lon, local.get(place, name), local.get(state_id, state),
                         local.get(country_id, land), round(_spread(people), 2)))
        db.executemany("INSERT INTO places VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
        db.execute("CREATE INDEX places_band ON places (band, lon)")
        db.execute("INSERT INTO meta VALUES ('stamp', ?)", (f"{time.strftime('%Y-%m-%d')} {language or '-'} {len(rows)}",))
        db.execute("INSERT INTO meta VALUES ('source', 'GeoNames (geonames.org), CC BY 4.0')")
        db.commit()
        db.execute("VACUUM")
        db.close()
        fresh.replace(target / FILE)
        return len(rows)


def main() -> None:
    if len(sys.argv) in (3, 4) and sys.argv[1] == "fetch":
        count = build(Path(sys.argv[2]), sys.argv[3] if len(sys.argv) == 4 else "")
        print(f"{count} towns written to {Path(sys.argv[2]) / FILE}")
    else:
        print("usage: python -m augenblick.geo fetch <folder> [language, e.g. de]", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
