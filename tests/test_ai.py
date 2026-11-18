import base64
import dataclasses
import io
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from PIL import Image

from augenblick.app import create_app
from conftest import PASSWORD, browser


class FakeModel(BaseHTTPRequestHandler):
    """Speaks just enough of the OpenAI chat API to stand in for Ollama."""
    seen: list = []
    answer = '<think>hm</think>{"description": "Ein Strand mit Meer", "keywords": ["Strand", "Meer", "Strand"], "date": null, "note": ""}'

    def log_message(self, *args):
        pass

    def _send(self, data):
        body = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._send({"data": [{"id": "sehmodell"}, {"id": "nomic-embed-text"}]})

    def do_POST(self):
        FakeModel.seen.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
        self._send({"model": "sehmodell", "choices": [{"message": {"content": FakeModel.answer}}],
                    "usage": {"prompt_tokens": 900, "completion_tokens": 40}})


@pytest.fixture
def model():
    server = HTTPServer(("127.0.0.1", 0), FakeModel)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    FakeModel.seen = []
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


@pytest.fixture
def greta_of():
    def make(client):
        client.post("/api/users", json={"name": "greta", "password": PASSWORD, "areas": {"Familie": "write"}})
        guest = browser(client.app)
        guest.post("/api/login", json={"username": "greta", "password": PASSWORD})
        return guest
    return make


@pytest.fixture
def rw(settings):
    app = create_app(dataclasses.replace(settings, readonly=False), background=False)
    app.state.index.scan()
    client = browser(app)
    assert client.post("/api/setup", json={"username": "oliver", "password": PASSWORD}).status_code == 201
    return client


def test_ai_settings(rw, model):
    state = rw.get("/api/ai").json()
    assert state["enabled"] is False and state["builtin"] is True and state["prompts"][0]["name"] == "Beschreiben"
    assert rw.put("/api/ai", json={"provider": "anthropic", "api_key": "falsch"}).status_code == 400
    state = rw.put("/api/ai", json={"enabled": True, "provider": "ollama", "base_url": model, "model": "sehmodell",
                                    "api_key": "geheim-12345678"}).json()
    assert state["base_url"] == model + "/v1" and state["key_set"] is True and "api_key" not in state
    assert state["key_hint"] == "…5678"
    assert [m["id"] for m in rw.get("/api/ai/models").json()["models"]] == ["sehmodell"]
    state = rw.put("/api/ai", json={"prompts": [{"name": "Kurz", "text": "Ein Satz."}, {"name": "Lang", "text": "Viel."}],
                                    "default_prompt": "Lang"}).json()
    assert state["builtin"] is False and state["default_prompt"] == "Lang"
    assert rw.get("/api/ai/prompts").json() == {"enabled": True, "prompts": ["Kurz", "Lang"], "default": "Lang",
                                                "excluded_areas": [], "memories": False, "inbox_auto": False}
    assert rw.put("/api/ai", json={"prompts": [{"name": "A", "text": "x"}, {"name": "a", "text": "y"}]}).status_code == 400


def test_ai_looks_at_the_photo_and_only_proposes(rw, model):
    key = rw.get("/api/photos?month=2024-07").json()[0]["key"]
    assert rw.post(f"/api/photos/{key}/ai", json={}).status_code == 422          # still off
    rw.put("/api/ai", json={"enabled": True, "provider": "ollama", "base_url": model, "model": "sehmodell"})
    rw.patch(f"/api/photos/{key}", json={"keywords": ["Urlaub"]})
    before = rw.get(f"/api/photos/{key}").json()
    answer = rw.post(f"/api/photos/{key}/ai", json={})
    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert body["description"] == "Ein Strand mit Meer" and body["keywords"] == ["Strand", "Meer"] and body["date"] is None
    assert body["model"] == "sehmodell"
    sent = FakeModel.seen[-1]["messages"]
    picture, text = sent[1]["content"]
    data = base64.b64decode(picture["image_url"]["url"].split(",", 1)[1])
    assert Image.open(io.BytesIO(data)).format == "JPEG"
    assert "20240714_102030_ab12.jpg" in text["text"] and "Aufnahmedatum: 2024-07-14T10:20:30" in text["text"]
    assert "schon vorkommen: Urlaub" in text["text"] and "Auftrag:" in text["text"]
    assert rw.get(f"/api/photos/{key}").json() == before                           # nothing written
    assert rw.get("/api/ai").json()["month_usage"]["calls"] == 1
    once = rw.post(f"/api/photos/{key}/ai", json={"instructions": "Nur das Jahr schätzen."})
    assert once.status_code == 200 and "Nur das Jahr schätzen." in FakeModel.seen[-1]["messages"][1]["content"][1]["text"]
    assert rw.post(f"/api/photos/{key}/ai", json={"prompt": "gibtsnicht"}).status_code == 404


def test_ai_date_guess_and_bad_answers(rw, model, monkeypatch):
    rw.put("/api/ai", json={"enabled": True, "provider": "ollama", "base_url": model, "model": "sehmodell"})
    key = rw.get("/api/photos?month=1981-05").json()[0]["key"]
    monkeypatch.setattr(FakeModel, "answer", '{"description": "", "keywords": [], "date": "1975", "note": "Schlaghosen"}')
    body = rw.post(f"/api/photos/{key}/ai", json={}).json()
    assert body["date"] == {"taken": "1975-01-01T12:00:00", "precision": "year"} and body["note"] == "Schlaghosen"
    assert "keines im Bild" in FakeModel.seen[-1]["messages"][1]["content"][1]["text"]
    monkeypatch.setattr(FakeModel, "answer", "Das ist ein schönes Bild.")
    assert rw.post(f"/api/photos/{key}/ai", json={}).status_code == 422


def test_the_ai_names_a_place_and_doubts_one_far_off(rw, model, monkeypatch):
    rw.put("/api/ai", json={"enabled": True, "provider": "ollama", "base_url": model, "model": "sehmodell"})
    stettin = '{"description": "Hafen", "keywords": [], "date": null, "note": "Hakenterrasse", ' \
              '"place": {"name": "Stettin, Polen", "lat": 53.4285, "lon": 14.5528}}'
    monkeypatch.setattr(FakeModel, "answer", stettin)
    nowhere = rw.get("/api/photos?month=1981-05").json()[0]["key"]          # no coordinates in the picture
    body = rw.post(f"/api/photos/{nowhere}/ai", json={}).json()
    assert body["place"] == {"name": "Stettin, Polen", "lat": 53.4285, "lon": 14.5528}
    assert "\"place\"" in FakeModel.seen[-1]["messages"][0]["content"]
    assert rw.get(f"/api/photos/{nowhere}").json()["lat"] is None               # still only a proposal

    there = rw.get("/api/photos?month=2024-07").json()[0]["key"]           # taken at 49.5, 7.75
    place = rw.post(f"/api/photos/{there}/ai", json={}).json()["place"]
    assert place["far"] is True and 600 < place["km"] < 700
    monkeypatch.setattr(FakeModel, "answer", stettin.replace("53.4285", "49.6").replace("14.5528", "7.8"))
    assert rw.post(f"/api/photos/{there}/ai", json={}).json()["place"]["far"] is False

    for wrong in ('"place": {"name": "x", "lat": "hier", "lon": 7}', '"place": {"lat": 95, "lon": 7}',
                  '"place": {"lat": 0, "lon": 0}', '"place": "Stettin"'):
        monkeypatch.setattr(FakeModel, "answer", '{"description": "", "keywords": [], "date": null, ' + wrong + '}')
        assert rw.post(f"/api/photos/{nowhere}/ai", json={}).json()["place"] is None, wrong


def test_fixed_places_are_kept_and_told_to_the_ai(rw, model, greta_of):
    assert rw.get("/api/spots").json() == []
    saved = rw.put("/api/spots", json={"spots": [{"name": " Zuhause ", "lat": 49.86, "lon": 7.8},
                                                 {"name": "Eltern", "lat": 49.5, "lon": 8.1}]})
    assert saved.json() == [{"name": "Zuhause", "lat": 49.86, "lon": 7.8}, {"name": "Eltern", "lat": 49.5, "lon": 8.1}]
    assert rw.put("/api/spots", json={"spots": [{"name": "A", "lat": 1, "lon": 1}, {"name": "a", "lat": 2, "lon": 2}]}).status_code == 422
    assert rw.put("/api/spots", json={"spots": [{"name": "A", "lat": 91, "lon": 1}]}).status_code == 422
    assert len(rw.get("/api/spots").json()) == 2                              # a refused list changes nothing
    guest = greta_of(rw)
    assert guest.get("/api/spots").status_code == 200 and guest.put("/api/spots", json={"spots": []}).status_code == 403
    rw.put("/api/ai", json={"enabled": True, "provider": "ollama", "base_url": model, "model": "sehmodell"})
    key = rw.get("/api/photos?month=1981-05").json()[0]["key"]
    rw.post(f"/api/photos/{key}/ai", json={})
    sent = FakeModel.seen[-1]["messages"]
    assert "Zuhause: 49.86000, 7.80000; Eltern: 49.50000, 8.10000" in sent[1]["content"][1]["text"]
    assert "festen Orte" in sent[0]["content"]


def test_ai_respects_areas_and_rights(rw, model):
    rw.put("/api/ai", json={"enabled": True, "provider": "ollama", "base_url": model, "model": "sehmodell",
                            "excluded_areas": ["Greta"]})
    key = rw.get("/api/photos?month=2023-12").json()[0]["key"]
    assert rw.post(f"/api/photos/{key}/ai", json={}).status_code == 403
    rw.post("/api/users", json={"name": "gast", "password": PASSWORD, "areas": {"Familie": "read"}})
    guest = browser(rw.app)
    guest.post("/api/login", json={"username": "gast", "password": PASSWORD})
    key = guest.get("/api/photos?month=2024-07").json()[0]["key"]
    assert guest.post(f"/api/photos/{key}/ai", json={}).status_code == 403
    assert guest.get("/api/ai").status_code == 403 and not FakeModel.seen


def test_the_ai_is_told_who_is_in_the_picture():
    from augenblick.ai import photo_prompt

    photo = {"name": "a.jpg", "folder": "2020", "taken": "2020-05-01T10:00:00", "taken_src": "exif",
             "people": ["Greta", "Paul"]}
    assert "Greta, Paul" in photo_prompt(photo, "Beschreibe.", [])
    assert "Personen im Bild" not in photo_prompt({**photo, "people": []}, "Beschreibe.", [])


def test_a_stored_copy_of_the_old_wording_follows_the_built_in_one():
    from augenblick.ai import AIConfig, prompts_of
    from augenblick.lang import PHOTO_TASK, PHOTO_TASK_BEFORE

    old = next(text for text in PHOTO_TASK_BEFORE if text.startswith("Beschreibe das Foto"))
    config = AIConfig(prompts=[{"name": "Beschreiben", "text": old}, {"name": "Eigenes", "text": "Zähle die Hunde."}],
                      default_prompt="Beschreiben")
    offered, default = prompts_of(config, "de")
    assert default == "Beschreiben"
    assert offered[0]["text"] == PHOTO_TASK["de"][1] and "Meer" in offered[0]["text"]
    assert offered[1]["text"] == "Zähle die Hunde."


def test_the_ai_describes_what_is_there_and_it_can_be_taken_back(rw, model):
    import time

    rw.put("/api/ai", json={"enabled": True, "provider": "ollama", "base_url": model, "model": "sehmodell"})
    index = rw.app.state.index
    photos = [index.by_path(row["path"]) for row in index._all(
        "SELECT path FROM photos WHERE kind = 'image' AND error = '' AND keywords = '' AND ext != 'dng' ORDER BY path")]
    kept = photos[0]
    assert rw.post("/api/edit", json={"keys": [kept["key"]], "change": {"keywords": ["Eigenes"]}}).json()[0]["ok"]
    stock = rw.get("/api/ai/stock").json()
    assert stock["waiting"] == len(photos) - 1 and stock["cost"] is None and stock["model"] == "sehmodell"
    assert rw.post("/api/ai/stock", json={}).json()["total"] == len(photos) - 1
    for _ in range(600):
        if not rw.get("/api/job").json()["running"]:
            break
        time.sleep(0.1)
    job = rw.get("/api/job").json()
    assert job["kind"] == "aistock" and job["failed"] == 0 and job["done"] == len(photos) - 1
    assert index.by_path(kept["path"])["keywords"] == ["Eigenes"]              # what a person wrote stays
    for before in photos[1:]:
        now = index.by_path(before["path"])
        assert now["keywords"] == ["Strand", "Meer"]
        assert now["description"] == (before["description"] or "Ein Strand mit Meer")      # only where there was none
    other = index.by_path(photos[1]["path"])
    assert rw.get("/api/ai/stock").json()["waiting"] == 0
    entry = next(e for e in rw.get("/api/journal").json() if e["path"] == other["path"])
    assert entry["user"] == "KI"
    assert rw.post(f"/api/journal/{entry['id']}/undo").status_code == 200
    assert rw.get(f"/api/photos/{other['key']}").json()["keywords"] == []


def test_a_run_over_the_stock_goes_on_after_a_restart(rw, model, settings):
    import json, time

    def wait():
        for _ in range(600):
            if not rw.get("/api/job").json()["running"]:
                return
            time.sleep(0.1)

    note = settings.data / "aistock.json"
    resume = rw.app.state.resume_stock
    assert resume() is False                                    # nothing was under way
    note.write_text("kaputt")
    assert resume() is False and not note.exists()              # a note nobody can read is thrown away
    note.write_text(json.dumps({"user": "oliver", "lang": "de", "model": "", "areas": []}))
    assert resume() is False and not note.exists()              # the AI is switched off: the run is over

    rw.put("/api/ai", json={"enabled": True, "provider": "ollama", "base_url": model, "model": "sehmodell"})
    waiting = rw.get("/api/ai/stock").json()["waiting"]
    assert waiting > 1
    # a trial run of one picture leaves no note; it ends by itself
    assert rw.post("/api/ai/stock", json={"limit": 1}).json()["total"] == 1
    assert not note.exists()
    wait()
    assert rw.get("/api/ai/stock").json()["waiting"] == waiting - 1

    # the program was stopped in the middle of a run: the note is still there at the next start
    note.write_text(json.dumps({"user": "oliver", "lang": "de", "model": "", "areas": []}))
    assert resume() is True
    wait()
    job = rw.get("/api/job").json()
    assert job["kind"] == "aistock" and job["failed"] == 0 and job["done"] == waiting - 1
    assert rw.get("/api/ai/stock").json()["waiting"] == 0
    assert not note.exists()                                    # it came to its end: nothing to pick up again
    assert resume() is False
