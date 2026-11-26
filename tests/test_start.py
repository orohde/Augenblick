"""The start page: what came in last, the albums pinned to it – and the list a slide show runs through."""

import dataclasses
import os
import time

import pytest

from augenblick.app import create_app
from conftest import PASSWORD, browser, make_jpeg


@pytest.fixture
def rw(settings):
    settings = dataclasses.replace(settings, readonly=False)
    app = create_app(settings, background=False)
    app.state.index.scan()
    client = browser(app)
    assert client.post("/api/setup", json={"username": "oliver", "password": PASSWORD}).status_code == 201
    client.app_ = app
    return client


def recent(client) -> list[str]:
    return [item["taken"][:10] for item in client.get("/api/start").json()["recent"]]


def test_the_first_scan_counts_as_taken(admin):
    # nothing is "new" just because the programme saw it for the first time: newest taken first
    assert recent(admin)[:3] == ["2024-07-14", "2024-03-05", "2024-03-02"]


def test_what_comes_in_later_is_first_however_old(rw, settings):
    make_jpeg(settings.photos / "Familie/Dias/1985.jpg", color=(1, 2, 3), taken="1985:08:01 12:00:00")
    rw.app_.state.index.scan()
    items = rw.get("/api/start").json()["recent"]
    assert items[0]["taken"] == "1985-08-01T12:00:00"
    assert abs(items[0]["added"] - time.time()) < 60
    assert {"key", "kind", "w", "h", "o", "month", "city", "country", "desc"} <= set(items[0])
    assert rw.get("/api/start", params={"limit": 2}).json()["recent"][0]["taken"].startswith("1985")
    # … and it fills the category of what is new
    new = rw.get("/api/photos", params={"month": "1985-08", "auto": "neu"}).json()
    assert len(new) == 1
    assert "neu" in {a["id"] for a in rw.get("/api/auto").json()}


def test_moving_or_changing_a_picture_keeps_it_as_old_as_it_was(rw, settings):
    index = rw.app_.state.index
    old = settings.photos / "Familie/2024/Urlaub/20240714_102030_ab12.jpg"
    before = {i["taken"]: i["added"] for i in rw.get("/api/start").json()["recent"]}["2024-07-14T10:20:30"]
    moved = settings.photos / "Familie/Woanders/strand.jpg"
    moved.parent.mkdir(parents=True)
    os.rename(old, moved)
    index.scan()
    # the picture itself changes (as a turn would): another hash at the same place
    make_jpeg(moved, color=(250, 250, 0), taken="2024:07:14 10:20:30")
    index.scan()
    after = {i["taken"]: i["added"] for i in rw.get("/api/start").json()["recent"]}["2024-07-14T10:20:30"]
    assert after == before
    assert rw.get("/api/start").json()["recent"][0]["taken"] == "2024-07-14T10:20:30"


def test_the_time_outlives_a_rebuilt_index(rw, settings):
    make_jpeg(settings.photos / "Familie/Dias/1985.jpg", color=(1, 2, 3), taken="1985:08:01 12:00:00")
    index = rw.app_.state.index
    index.scan()
    with index._connect() as db:
        db.execute("UPDATE meta SET value = 'alt' WHERE name = 'schema'")
    again = create_app(dataclasses.replace(settings, readonly=False), background=False)
    again.state.index.scan()
    assert again.state.index.recent(["Familie"])[0]["taken"].startswith("1985")


def test_pinned_albums(rw):
    key = rw.get("/api/photos", params={"month": "2024-07"}).json()[0]["key"]
    assert rw.patch(f"/api/photos/{key}", json={"albums": ["Urlaub/Strand"]}).status_code == 200
    assert rw.get("/api/start").json()["pins"] == []
    body = rw.put("/api/me/prefs", json={"pins": ["Urlaub/Strand", "Gibt es nicht", "Urlaub/Strand", "Urlaub"]}).json()
    assert body["pins"] == ["Urlaub/Strand", "Gibt es nicht", "Urlaub"]
    pins = rw.get("/api/start").json()["pins"]
    assert [(p["path"], p["name"], p["count"]) for p in pins] == [("Urlaub/Strand", "Strand", 1), ("Urlaub", "Urlaub", 1)]
    assert pins[0]["cover"]["key"] == key
    # an empty list takes them all off
    assert rw.put("/api/me/prefs", json={"pins": []}).json()["pins"] == []


def test_sequence_is_oldest_first_and_follows_the_choice(admin):
    every = admin.get("/api/sequence").json()
    assert [i["taken"][:10] for i in every if i["kind"] == "image"] == sorted(i["taken"][:10] for i in every if i["kind"] == "image")
    assert len(every) == sum(m["count"] for m in admin.get("/api/timeline").json())
    only = admin.get("/api/sequence", params={"folder": "Familie/2024/Urlaub"}).json()
    assert len(only) == 1 and only[0]["desc"] == "Am Strand"


def test_a_clock_running_ahead_does_not_stay_on_top(settings):
    make_jpeg(settings.photos / "Familie/Zukunft/bild.jpg", color=(9, 9, 9), taken="2099:01:01 00:00:00")
    app = create_app(settings, background=False)
    app.state.index.scan()
    first = app.state.index.recent(["Familie"])[0]
    assert first["taken"].startswith("2099") and first["added"] <= time.time()
    make_jpeg(settings.photos / "Familie/Neu/bild.jpg", color=(19, 9, 9), taken="2001:01:01 00:00:00")
    app.state.index.scan()
    assert app.state.index.recent(["Familie"])[0]["taken"].startswith("2001")


def test_questions_not_to_be_asked_again_follow_the_account(rw):
    assert rw.get("/api/me/prefs").json()["quiet"] == []
    body = rw.put("/api/me/prefs", json={"quiet": ["jpeg", "move", "jpeg", "<script>", ""]}).json()
    assert body["quiet"] == ["jpeg", "move"] and body["pins"] == []
    assert rw.get("/api/me/prefs").json()["quiet"] == ["jpeg", "move"]
    assert rw.put("/api/me/prefs", json={"quiet": []}).json()["quiet"] == []
