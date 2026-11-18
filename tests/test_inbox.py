"""The inbox: the AI proposes for what arrives, the person takes it over and hands the picture on."""

import dataclasses
import threading

import pytest

from augenblick.app import create_app
from conftest import PASSWORD, browser, make_jpeg
from test_ai import FakeModel, model  # noqa: F401 – the stand-in model is a fixture


@pytest.fixture
def box(settings):
    make_jpeg(settings.photos / "Eingang/IMG_4711.jpg", color=(30, 140, 220), taken="2026:11:12 12:22:00")
    make_jpeg(settings.photos / "Eingang/IMG_4712.jpg", color=(220, 140, 30), taken="2026:11:12 12:30:00")
    app = create_app(dataclasses.replace(settings, readonly=False), background=False)
    app.state.index.scan()
    client = browser(app)
    assert client.post("/api/setup", json={"username": "oliver", "password": PASSWORD}).status_code == 201
    assert client.put("/api/upload", json={"area": "Eingang"}).status_code == 200
    client.app_state = app.state
    return client


def keys_of(client):
    return {p["path"].rsplit("/", 1)[1]: p["key"] for p in client.app_state.index.pictures(["Eingang"])}


def switch_on(client, url, **more):
    client.put("/api/ai", json={"enabled": True, "provider": "ollama", "base_url": url, "model": "sehmodell", **more})


def test_nothing_happens_by_itself_unless_switched_on(box, model):
    switch_on(box, model)
    assert box.app_state.propose_round(threading.Event()) == 0 and FakeModel.seen == []
    assert box.get("/api/inbox/proposals").json()["auto"] is False
    switch_on(box, model, inbox_auto=True)
    assert box.get("/api/ai/prompts").json()["inbox_auto"] is True
    assert box.app_state.propose_round(threading.Event()) == 2
    shown = box.get(f"/api/photos/{keys_of(box)['IMG_4711.jpg']}").json()
    assert shown["inbox"] is True
    assert shown["proposal"]["keywords"] == ["Strand", "Meer"] and shown["proposal"]["description"] == "Ein Strand mit Meer"
    assert shown["keywords"] == []                          # nothing written yet
    assert box.app_state.propose_round(threading.Event()) == 0          # asked once, not again


def test_chosen_pictures_are_put_in_line_and_thrown_away_ones_stay_away(box, model):
    keys = keys_of(box)
    assert box.post("/api/inbox/proposals", json={"keys": [keys["IMG_4711.jpg"]]}).status_code == 422     # AI off
    switch_on(box, model)
    queued = box.post("/api/inbox/proposals", json={"keys": list(keys.values())}).json()
    assert queued == {"queued": 2, "skipped": 0}
    assert box.app_state.propose_round(threading.Event()) == 2
    assert box.delete(f"/api/photos/{keys['IMG_4712.jpg']}/proposal").json() == {"ok": True}
    assert box.get(f"/api/photos/{keys['IMG_4712.jpg']}").json()["proposal"] is None
    switch_on(box, model, inbox_auto=True)
    assert box.app_state.propose_round(threading.Event()) == 0          # thrown away: not asked again by itself


def test_accepting_writes_the_proposal_and_hands_the_picture_on(box, model):
    switch_on(box, model, inbox_auto=True)
    box.app_state.propose_round(threading.Event())
    keys = keys_of(box)
    assert box.post("/api/inbox/accept", json={"keys": [keys["IMG_4711.jpg"]], "area": "Eingang"}).status_code == 403
    done = box.post("/api/inbox/accept", json={"keys": list(keys.values()), "area": "Familie"}).json()
    assert all(r["ok"] for r in done), done
    for result in done:
        photo = result["photo"]
        assert photo["area"] == "Familie" and photo["keywords"] == ["Strand", "Meer"]
        assert photo["description"] == "Ein Strand mit Meer"
    assert keys_of(box) == {}
    assert box.app_state.proposals.known() == set()


def test_one_picture_takes_the_persons_own_choice(box, model):
    switch_on(box, model, inbox_auto=True)
    box.app_state.propose_round(threading.Event())
    key = keys_of(box)["IMG_4711.jpg"]
    box.patch(f"/api/photos/{key}", json={"keywords": ["Urlaub"]})
    key = keys_of(box)["IMG_4711.jpg"]
    assert box.get(f"/api/photos/{key}").json()["proposal"]["keywords"] == ["Strand", "Meer"]   # kept over the change
    done = box.post("/api/inbox/accept", json={"keys": [key], "area": "Familie", "keywords": ["Meer"],
                                               "description": ""}).json()
    photo = done[0]["photo"]
    assert done[0]["ok"] and photo["keywords"] == ["Urlaub", "Meer"] and photo["description"] == ""


def test_one_picture_takes_an_estimated_place_only_where_it_has_none(box, model):
    switch_on(box, model)
    keys = keys_of(box)
    done = box.post("/api/inbox/accept", json={"keys": [keys["IMG_4711.jpg"]], "area": "Familie",
                                               "lat": 53.4285, "lon": 14.5528}).json()
    assert done[0]["ok"] and (done[0]["photo"]["lat"], done[0]["photo"]["lon"]) == (53.4285, 14.5528)
    box.patch(f"/api/photos/{keys['IMG_4712.jpg']}", json={"lat": 49.5, "lon": 7.75})
    key = keys_of(box)["IMG_4712.jpg"]
    done = box.post("/api/inbox/accept", json={"keys": [key], "area": "Familie", "lat": 53.4, "lon": 14.5}).json()
    assert done[0]["ok"] and done[0]["photo"]["lat"] == 49.5                  # the picture's own place stays


def test_without_taking_over_the_picture_only_moves(box):
    key = keys_of(box)["IMG_4712.jpg"]
    done = box.post("/api/inbox/accept", json={"keys": [key], "area": "Familie", "take": False}).json()
    assert done[0]["ok"] and done[0]["photo"]["area"] == "Familie" and done[0]["photo"]["keywords"] == []


def test_faces_are_looked_for_in_the_inbox_once_they_are_on(box):
    assert box.app_state.face_areas() == []                 # off everywhere: the inbox too
    box.app_state.prefs.save(faces=["Familie"])
    assert sorted(box.app_state.face_areas()) == ["Eingang", "Familie"]
