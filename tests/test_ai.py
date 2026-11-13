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
