"""Memories: journeys and looks back, found in the photos – nothing is written for them."""

import dataclasses
from datetime import date, timedelta

import pytest

from augenblick.ai import AIError, Completion, parse_memory
from augenblick.app import create_app
from augenblick.memories import homes, journeys, looks_back, pick
from conftest import PASSWORD, browser, make_jpeg

HOME, SEA = (49.86, 7.80), (54.18, 7.89)


def located(day: str, where, town: str, n: int = 1, land: str = "Deutschland", state: str = "") -> list[dict]:
    return [{"taken": f"{day}T{10 + i % 8:02d}:{i % 60:02d}:00", "lat": where[0], "lon": where[1], "city": town,
             "state": state, "country": land} for i in range(n)]


def year_at_home(year: int) -> list[dict]:
    return [row for month in range(1, 13) for row in located(f"{year}-{month:02d}-05", HOME, "Rotbach", 2)]


def test_home_is_where_the_most_days_are():
    rows = sorted(year_at_home(2019) + located("2019-07-12", SEA, "Helgoland", 500) + located("2011-08-01", SEA, "Helgoland", 30),
                  key=lambda r: r["taken"])
    found = homes(rows)
    assert abs(found[2019][0] - HOME[0]) < 0.01          # 500 pictures on one day do not make a home
    assert found[2011] == found[2019]                    # a year known from one day takes the nearest year that knows


def test_journeys_are_stretches_of_days_away():
    rows = year_at_home(2019)
    for day in ("2019-07-12", "2019-07-13", "2019-07-15", "2019-07-16"):       # the 14th without a picture: still one journey
        rows += located(day, SEA, "Helgoland", 6, state="Schleswig-Holstein")
    rows += located("2019-09-01", SEA, "Helgoland", 30)                         # one day only: an outing, not a journey
    rows += located("2019-10-01", SEA, "Helgoland", 3) + located("2019-10-02", SEA, "Helgoland", 3)     # too few pictures
    found = journeys(sorted(rows, key=lambda r: r["taken"]))
    assert [(j["id"], j["title"], j["sub"], j["days"]) for j in found] == \
        [("reise-2019-07-12", "Helgoland", "12.–16. Juli 2019", 5)]
    assert (found[0]["since"], found[0]["until"]) == ("2019-07-12T00:00:00", "2019-07-17T00:00:00")
    assert journeys([]) == []


def test_a_journey_through_several_places_is_named_by_the_land():
    rows = year_at_home(2015)
    for at, town in enumerate(("Skagen", "Aarhus", "Ribe", "Odense", "Nyborg")):
        rows += located(f"2015-08-0{at + 1}", (57.7 - at / 2, 10.5), town, 5, land="Dänemark")
    assert journeys(sorted(rows, key=lambda r: r["taken"]))[0]["title"] == "Dänemark"


def test_a_journey_is_named_by_where_one_stayed():
    rows = year_at_home(2024)
    for day in ("03", "04", "05", "06"):                                # four days in Jever, a few pictures each
        rows += located(f"2024-05-{day}", (53.57, 7.90), "Jever", 3, state="Niedersachsen")
    rows += located("2024-05-05", (53.53, 8.11), "Wilhelmshaven", 40, state="Niedersachsen")     # one day, many pictures
    rows += located("2024-05-06", (53.70, 8.02), "Schillig", 9, state="Niedersachsen")
    assert journeys(sorted(rows, key=lambda r: r["taken"]))[0]["title"] == "Jever und Wilhelmshaven"


def test_looks_back_take_the_days_around_today():
    found = looks_back(date(2026, 11, 13), [2026, 2025, 2019, 2019])
    assert [(b["id"], b["title"], b["sub"]) for b in found] == [
        ("woche-2025", "Diese Woche vor einem Jahr", "10.–16. November 2025"),
        ("woche-2019", "Diese Woche vor 7 Jahren", "10.–16. November 2019")]
    assert looks_back(date(2024, 2, 29), [2023])[0]["sub"] == "25. Februar – 3. März 2023"


def test_the_pick_spreads_and_drops_bursts():
    rows = [{"key": f"a{i}", "kind": "image", "taken": f"2019-07-12T10:00:{i:02d}", "rating": 0, "people": "", "width": 4, "height": 3}
            for i in range(20)]                                         # a burst: twenty in twenty seconds
    rows += [{"key": "stern", "kind": "image", "taken": "2019-07-13T09:00:00", "rating": 5, "people": "", "width": 3, "height": 4},
             {"key": "leute", "kind": "image", "taken": "2019-07-13T12:00:00", "rating": 0, "people": "\nGreta\n", "width": 4, "height": 3},
             {"key": "film", "kind": "video", "taken": "2019-07-14T12:00:00", "rating": 5, "people": "", "width": 4, "height": 3}]
    chosen = pick(rows)
    assert chosen == ["a0", "stern", "leute"]                           # one of the burst, no film, in the order of time


def test_what_the_ai_says_is_tidied():
    assert parse_memory('Gern: {"title": " Helgoland  im Wind ", "text": "Vier Tage.", "cover": 2}', ["k1", "k2"]) == \
        {"title": "Helgoland im Wind", "text": "Vier Tage.", "cover": "k2"}
    assert parse_memory('{"title": "X", "cover": 9}', ["k1"])["cover"] == ""
    with pytest.raises(AIError):
        parse_memory("keine Ahnung", ["k1"])


@pytest.fixture
def rw(settings):
    settings = dataclasses.replace(settings, readonly=False)
    for month in range(1, 13):                                          # at home all through the year
        make_jpeg(settings.photos / f"Familie/2019/heim{month:02d}.jpg", color=(month, 1, 1), gps=HOME,
                  taken=f"2019:{month:02d}:05 12:00:00")
    for day in (12, 13, 14):
        for n in range(4):
            make_jpeg(settings.photos / f"Familie/2019/see{day}_{n}.jpg", color=(day, n, 9), gps=SEA,
                      taken=f"2019:07:{day} {10 + n}:00:00")
    make_jpeg(settings.photos / "Familie/2019/ohne_ort.jpg", color=(7, 7, 7), taken="2019:07:13 20:00:00")     # belongs to it all the same
    back = date.today().replace(year=date.today().year - 2) if date.today().strftime("%m%d") != "0229" else date.today() - timedelta(days=730)
    for n in range(5):
        make_jpeg(settings.photos / f"Greta/alt{n}.jpg", color=(n, 50, 50), taken=back.strftime(f"%Y:%m:%d 1{n}:00:00"))
    app = create_app(settings, background=False)
    app.state.index.scan()
    client = browser(app)
    assert client.post("/api/setup", json={"username": "oliver", "password": PASSWORD}).status_code == 201
    client.ai = app.state.ai
    return client


def test_memories_are_listed_and_open_as_a_stretch_of_days(rw):
    found = rw.get("/api/memories").json()
    assert [(m["kind"], m["count"]) for m in found] == [("back", 5), ("journey", 13)]
    journey = found[1]
    assert journey["id"] == "reise-2019-07-12" and journey["sub"] == "12.–14. Juli 2019" and journey["cover"] in journey["picks"]
    span = {"since": journey["since"], "until": journey["until"]}
    assert sum(m["count"] for m in rw.get("/api/timeline", params=span).json()) == 13
    picked = rw.get("/api/photos", params={"month": "2019-07", **span, "keys": ",".join(journey["picks"])}).json()
    assert sorted(p["key"] for p in picked) == sorted(journey["picks"]) and len(picked) == 12
    assert rw.get("/api/memories", params={"areas": "Greta"}).json()[0]["kind"] == "back"
    assert rw.get("/api/photos", params={"month": "2019-07", "keys": ""}).json()        # no keys given: no filter


def test_the_ai_names_a_memory_only_when_switched_on(rw, monkeypatch):
    assert rw.post("/api/memories/reise-2019-07-12/ai").status_code == 422
    assert rw.put("/api/ai", json={"enabled": True, "memories": True}).status_code == 200
    assert rw.get("/api/ai/prompts").json()["memories"] is True
    seen = {}

    def answer(system, prompt, max_tokens=0, images=None, **more):
        seen.update(prompt=prompt, images=len(images))
        return Completion(text='{"title": "Wind auf Helgoland", "text": "Drei Tage am Meer.", "cover": 3}', model="test",
                          input_tokens=1, output_tokens=1, cost=0.01, seconds=0.1)
    monkeypatch.setattr(rw.ai, "complete", answer)
    named = rw.post("/api/memories/reise-2019-07-12/ai").json()
    assert named["title"] == "Wind auf Helgoland" and named["by_ai"] and named["text"] == "Drei Tage am Meer."
    assert named["cover"] == named["picks"][2] and seen["images"] == 8 and "12.–14. Juli 2019" in seen["prompt"]
    assert rw.get("/api/memories").json()[1]["title"] == "Wind auf Helgoland"
    back = rw.delete("/api/memories/reise-2019-07-12/ai").json()
    assert not back["by_ai"] and back["title"] == back["found"]
    assert rw.post("/api/memories/gibtesnicht/ai").status_code == 404
