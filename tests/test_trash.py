import dataclasses
import time

import pytest

from augenblick.app import create_app
from augenblick.trash import FOLDER
from conftest import PASSWORD, browser


@pytest.fixture
def rw(settings):
    settings = dataclasses.replace(settings, readonly=False)
    app = create_app(settings, background=False)
    app.state.index.scan()
    client = browser(app)
    assert client.post("/api/setup", json={"username": "oliver", "password": PASSWORD}).status_code == 201
    client.photos = settings.photos
    return client


def beach(client):
    return client.get("/api/photos?month=2024-07").json()[0]["key"]


def test_delete_moves_into_the_trash_and_back(rw):
    key = beach(rw)
    original = rw.photos / "Familie/2024/Urlaub/20240714_102030_ab12.jpg"
    data = original.read_bytes()
    assert rw.get(f"/api/photos/{key}").json()["can_delete"] is True
    assert rw.delete(f"/api/photos/{key}").json()["ok"] is True
    assert not original.exists() and rw.get(f"/api/photos/{key}").status_code == 404
    assert rw.get("/api/timeline?folder=Familie/2024/Urlaub").json() == []
    kept = list((rw.photos / "Familie" / FOLDER).iterdir())
    assert len(kept) == 1 and kept[0].read_bytes() == data
    rw.app.state.index.scan()                              # the scanner leaves the trash alone
    assert rw.get("/api/timeline?folder=Familie/2024/Urlaub").json() == []
    listing = rw.get("/api/trash").json()
    entry = listing["entries"][0]
    assert listing["days"] == 30 and entry["name"] == original.name and entry["path"].endswith(original.name)
    assert abs(entry["until"] - entry["ts"] - 30 * 86400) < 1 and "stored" not in entry
    assert rw.get(f"/api/trash/{entry['id']}/thumb").status_code == 200
    back = rw.post(f"/api/trash/{entry['id']}/restore").json()
    assert back["key"] == key and original.read_bytes() == data
    assert rw.get("/api/trash").json()["entries"] == [] and not list((rw.photos / "Familie" / FOLDER).iterdir())


def test_restore_does_not_overwrite(rw):
    key = beach(rw)
    original = rw.photos / "Familie/2024/Urlaub/20240714_102030_ab12.jpg"
    data = original.read_bytes()
    rw.delete(f"/api/photos/{key}")
    original.write_bytes(b"etwas anderes")
    entry = rw.get("/api/trash").json()["entries"][0]["id"]
    back = rw.post(f"/api/trash/{entry}/restore").json()
    assert back["name"] == "20240714_102030_ab12 (2).jpg" and original.read_bytes() == b"etwas anderes"
    assert (original.parent / back["name"]).read_bytes() == data


def test_gone_for_good_and_after_time(rw):
    results = rw.post("/api/delete", json={"keys": [beach(rw), "gibtsnicht"]}).json()
    assert [r["ok"] for r in results] == [True, False]
    rw.delete("/api/photos/" + rw.get("/api/photos?month=2024-03").json()[0]["key"])
    first, second = [e["id"] for e in rw.get("/api/trash").json()["entries"]]
    assert rw.delete(f"/api/trash/{first}").json() == {"ok": True}
    assert len(list((rw.photos / "Familie" / FOLDER).iterdir())) == 1
    trash = rw.app.state.trash
    assert trash.purge(30) == 0 and trash.purge(0) == 0
    with trash._connect() as db:
        db.execute("UPDATE trash SET ts = ?", (time.time() - 31 * 86400,))
    assert rw.put("/api/trash/settings", json={"days": 60}).json() == {"days": 60}
    assert trash.purge(rw.app.state.prefs.load()["trash_days"]) == 0
    assert rw.get("/api/trash").json()["days"] == 60
    assert trash.purge(30) == 1 and rw.get("/api/trash").json()["entries"] == []
    assert not list((rw.photos / "Familie" / FOLDER).iterdir())


def test_empty_and_rights(rw):
    rw.post("/api/users", json={"name": "gast", "password": PASSWORD, "areas": {"Familie": "read"}})
    guest = browser(rw.app)
    assert guest.post("/api/login", json={"username": "gast", "password": PASSWORD}).status_code == 200
    key = beach(rw)
    assert guest.delete(f"/api/photos/{key}").status_code == 403
    assert guest.get(f"/api/photos/{key}").json()["can_delete"] is False
    rw.delete(f"/api/photos/{key}")
    entry = guest.get("/api/trash").json()["entries"][0]["id"]
    assert guest.post(f"/api/trash/{entry}/restore").status_code == 403
    assert guest.delete(f"/api/trash/{entry}").status_code == 403
    assert guest.post("/api/trash/empty").json() == {"removed": 0}
    assert guest.put("/api/trash/settings", json={"days": 1}).status_code == 403
    assert rw.post("/api/trash/empty").json() == {"removed": 1}
    assert rw.get("/api/trash").json()["entries"] == []


def test_readonly_cannot_delete(admin):
    key = admin.get("/api/photos?month=2024-07").json()[0]["key"]
    assert admin.delete(f"/api/photos/{key}").status_code == 403
    assert admin.post("/api/trash/empty").status_code == 403
