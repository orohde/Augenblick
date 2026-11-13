"""Memories: journeys and looks back, worked out from what the photos say about themselves.

Nothing is written into a photo and nothing has to be kept: every memory is a stretch of time,
found anew from the index. Only what the AI was asked to add (a title, a sentence, the picture
that stands for it) is kept, in the data folder, and can be thrown away.
"""

from __future__ import annotations

import json
import math
import threading
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

from .index import Index
from .lang import span, word

AWAY_KM = 40            # this far from home is "away"
GAP_DAYS = 2            # days without a picture from away that do not end a journey
MIN_DAYS, MIN_PHOTOS = 2, 10
SOLID_DAYS = 8          # pictures on this many days in one town make it home for that year
WEEK = 3                # a look back takes this many days before and after today
MIN_BACK = 5
PICKS = 12


def _km(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 12742 * math.asin(math.sqrt(h))


def homes(rows: list[dict]) -> dict[int, tuple[float, float]]:
    """Where home was in each year: the town with pictures on the most days – a holiday with a
    thousand pictures is still only two weeks. Years without a place take the nearest year's."""
    days: dict[int, dict[tuple, set]] = defaultdict(lambda: defaultdict(set))
    spots: dict[tuple, list] = defaultdict(lambda: [0.0, 0.0, 0])
    for row in rows:
        town = (int(row["taken"][:4]), row["city"], row["country"])
        days[town[0]][town].add(row["taken"][:10])
        spot = spots[town]
        spot[0] += row["lat"]; spot[1] += row["lon"]; spot[2] += 1      # noqa: E702
    found, solid = {}, {}
    for year, towns in days.items():
        town = max(towns, key=lambda t: (len(towns[t]), t[1]))
        lat, lon, n = spots[town]
        found[year] = (lat / n, lon / n)
        if len(towns[town]) >= SOLID_DAYS:
            solid[year] = found[year]
    if solid:           # a year with pictures from a few days only says little: it takes the nearest year that knows
        for year in found:
            if year not in solid:
                found[year] = solid[min(solid, key=lambda known: abs(known - year))]
    return found


def _where(run: list[dict], lang: str = "de") -> str:
    """What to call the place of a journey. The town one stayed in – pictures on the most days – and,
    if it does not hold most of the pictures, the town with the most pictures beside it. Only a tour
    through many places is called by its land or its part of the land."""
    towns = Counter(row["city"] for row in run if row["city"])
    stays: dict[str, set] = defaultdict(set)
    for row in run:
        if row["city"]:
            stays[row["city"]].add(row["taken"][:10])
    if towns:
        main = max(towns, key=lambda town: (len(stays[town]), towns[town], town))
        if towns[main] >= 0.6 * len(run):
            return main
        rest = [town for town in towns if town != main]
        if rest:
            other = max(rest, key=lambda town: (towns[town], len(stays[town]), town))
            if towns[main] + towns[other] >= 0.5 * len(run):
                return f"{main} {word(lang, 'and')} {other}"
    lands = Counter(row["country"] for row in run if row["country"])
    states = Counter(row["state"] for row in run if row["state"])
    if len(lands) > 1 and lands.most_common(1)[0][1] < 0.8 * len(run):
        return f" {word(lang, 'and')} ".join(name for name, _ in lands.most_common(2))
    if states and states.most_common(1)[0][1] >= 0.6 * len(run):
        return states.most_common(1)[0][0]
    return lands.most_common(1)[0][0] if lands else (max(towns, key=towns.get) if towns else word(lang, "away"))


def journeys(rows: list[dict], lang: str = "de") -> list[dict]:
    """Stretches of days away from home, from the pictures that know where they were taken
    (rows: taken, lat, lon, city, state, country – in the order of time)."""
    home = homes(rows)
    if not home:
        return []
    away = [row for row in rows
            if _km((row["lat"], row["lon"]), home[int(row["taken"][:4])]) > AWAY_KM]
    found, run = [], []

    def close() -> None:
        days = sorted({row["taken"][:10] for row in run})
        if len(days) < MIN_DAYS or len(run) < MIN_PHOTOS:
            return
        first, last = date.fromisoformat(days[0]), date.fromisoformat(days[-1])
        where = _where(run, lang)
        found.append({"id": f"reise-{days[0]}", "kind": "journey", "title": where, "sub": span(first, last, lang),
                      "since": f"{days[0]}T00:00:00", "until": f"{(last + timedelta(days=1)).isoformat()}T00:00:00",
                      "days": (last - first).days + 1})

    for row in away:
        if run and (date.fromisoformat(row["taken"][:10]) - date.fromisoformat(run[-1]["taken"][:10])).days > GAP_DAYS + 1:
            close()
            run = []
        run.append(row)
    if run:
        close()
    return found


def looks_back(today: date, years: list[int], lang: str = "de") -> list[dict]:
    """The days around today in each earlier year – whether there is anything in them is found out later."""
    found = []
    for year in sorted({y for y in years if y < today.year}, reverse=True):
        try:
            middle = today.replace(year=year)
        except ValueError:                  # the 29th of February
            middle = today.replace(year=year, day=28)
        first, last = middle - timedelta(days=WEEK), middle + timedelta(days=WEEK)
        ago = today.year - year
        found.append({"id": f"woche-{year}", "kind": "back",
                      "title": word(lang, "back1") if ago == 1 else word(lang, "back", n=ago),
                      "sub": span(first, last, lang), "since": f"{first.isoformat()}T00:00:00",
                      "until": f"{(last + timedelta(days=1)).isoformat()}T00:00:00", "days": 2 * WEEK + 1})
    return found


def pick(rows: list[dict], limit: int = PICKS) -> list[str]:
    """A few pictures that stand for the whole: those with stars and with people named on them first,
    spread over the days, and of a burst of near-alike shots only one (rows: key, taken, rating,
    people, width, height, kind)."""
    def worth(row: dict) -> tuple:
        return (row["rating"] or 0, 1 if row["people"] else 0, 1 if (row["width"] or 0) >= (row["height"] or 0) else 0)

    stills = [row for row in rows if row["kind"] == "image"]
    by_day: dict[str, list[dict]] = defaultdict(list)
    for row in stills:
        by_day[row["taken"][:10]].append(row)
    for day in by_day.values():
        day.sort(key=worth, reverse=True)
    chosen: list[dict] = []
    while len(chosen) < limit and any(by_day.values()):     # one from every day in turn, the best first
        for day in sorted(by_day):
            while by_day[day]:
                row = by_day[day].pop(0)
                moment = datetime.fromisoformat(row["taken"])
                if all(abs((moment - datetime.fromisoformat(c["taken"])).total_seconds()) > 120 for c in chosen):
                    chosen.append(row)
                    break
            if len(chosen) >= limit:
                break
    chosen.sort(key=lambda row: row["taken"])
    return [row["key"] for row in chosen]


class Memories:
    def __init__(self, index: Index, path: Path):
        self.index = index
        self.path = path                    # what the AI added: {memory id: {title, text, cover, model}}
        self._lock = threading.Lock()
        self._kept: tuple | None = None

    # ------------------------------------------------------------ what the AI added

    def notes(self) -> dict:
        try:
            stored = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return stored if isinstance(stored, dict) else {}

    def note(self, memory: str, value: dict | None) -> None:
        with self._lock:
            stored = self.notes()
            if value is None:
                stored.pop(memory, None)
            else:
                stored[memory] = value
            temp = self.path.with_suffix(".tmp")
            temp.write_text(json.dumps(stored, ensure_ascii=False, indent=1), encoding="utf-8")
            temp.replace(self.path)
            self._kept = None

    # ------------------------------------------------------------ finding

    def _rows(self, areas: list[str], since: str, until: str) -> list[dict]:
        where, params = self.index._where(areas, since=since, until=until)
        return [dict(row) for row in self.index._all(
            f"SELECT key, kind, taken, rating, people, width, height FROM photos WHERE {where} ORDER BY taken", params)]

    def list(self, areas: list[str], today: date | None = None, lang: str = "de") -> list[dict]:
        """Every memory the photos of these areas hold: the looks back first, then the journeys, newest first."""
        today = today or date.today()
        stamp = (self.index.changes, today, tuple(areas), lang)
        if self._kept and self._kept[0] == stamp:
            return self._kept[1]
        where, params = self.index._where(areas)
        located = [dict(row) for row in self.index._all(
            f"SELECT taken, lat, lon, city, state, country FROM photos WHERE {where} AND lat IS NOT NULL "
            "AND taken_src = 'exif' ORDER BY taken", params)]
        years = [int(row["y"]) for row in self.index._all(
            f"SELECT DISTINCT substr(taken, 1, 4) y FROM photos WHERE {where} AND taken_src != 'file'", params)]
        notes = self.notes()
        found = []
        for memory in looks_back(today, years, lang) + sorted(journeys(located, lang), key=lambda m: m["since"], reverse=True):
            rows = self._rows(areas, memory["since"], memory["until"])
            if len(rows) < (MIN_BACK if memory["kind"] == "back" else MIN_PHOTOS):
                continue
            picks = pick(rows)
            if not picks:
                continue
            note = notes.get(memory["id"]) or {}
            keys = {row["key"] for row in rows}
            found.append({**memory, "count": len(rows), "picks": picks,
                          "cover": note["cover"] if note.get("cover") in keys else picks[len(picks) // 2],
                          "named": note.get("title") or "", "text": note.get("text") or ""})
        self._kept = (stamp, found)
        return found

    def get(self, memory: str, areas: list[str], lang: str = "de") -> dict | None:
        return next((m for m in self.list(areas, lang=lang) if m["id"] == memory), None)
