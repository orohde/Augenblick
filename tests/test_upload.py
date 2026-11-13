"""The door for the phone: uploads over WebDAV land in one area, and the key opens nothing else."""
import base64
import dataclasses

import pytest

from augenblick.app import create_app
from augenblick import upload
from conftest import PASSWORD, browser, make_jpeg


@pytest.fixture
def door(settings):
    settings = dataclasses.replace(settings, readonly=False)
    app = create_app(settings, background=False)
    app.state.index.scan()
    admin = browser(app)
    assert admin.post("/api/setup", json={"username": "oliver", "password": PASSWORD}).status_code == 201
    assert admin.post("/api/areas", json={"name": "Eingang"}).status_code == 201
    assert admin.put("/api/upload", json={"area": "Nirgends"}).status_code == 404
    assert admin.put("/api/upload", json={"area": "Eingang"}).json()["area"] == "Eingang"
    made = admin.post("/api/upload/key").json()
    assert made["has_key"] and len(made["key"]) > 30 and "key" not in admin.get("/api/upload").json()
    phone = browser(app)
    phone.headers["Authorization"] = "Basic " + base64.b64encode(f"upload:{made['key']}".encode()).decode()
    phone.photos, phone.admin, phone.key = settings.photos, admin, made["key"]
    return phone


def picture(tmp_path, color=(5, 6, 7)):
    return make_jpeg(tmp_path / "quelle.jpg", color=color, taken="2026:11:12 10:15:00").read_bytes()


def test_upload_lands_in_the_area_and_is_read_in(door, tmp_path):
    data = picture(tmp_path)
    assert door.request("OPTIONS", "/dav/").headers["DAV"] == "1"
    listing = door.request("PROPFIND", "/dav/", headers={"Depth": "1"})
    assert listing.status_code == 207 and "<D:href>/dav/</D:href>" in listing.text and "quelle" not in listing.text
    assert door.request("MKCOL", "/dav/iPhone%20Oliver").status_code == 201
    assert door.request("MKCOL", "/dav/iPhone%20Oliver").status_code == 405
    assert door.put("/dav/iPhone%20Oliver/IMG_0001.jpg", content=data).status_code == 201
    assert (door.photos / "Eingang/iPhone Oliver/IMG_0001.jpg").read_bytes() == data
    listing = door.request("PROPFIND", "/dav/iPhone%20Oliver/", headers={"Depth": "1"}).text
    assert "/dav/iPhone%20Oliver/IMG_0001.jpg" in listing and f"<D:getcontentlength>{len(data)}</D:getcontentlength>" in listing
    assert door.head("/dav/iPhone%20Oliver/IMG_0001.jpg").headers["content-length"] == str(len(data))
    assert door.head("/dav/fehlt.jpg").status_code == 404
    # the same file again changes nothing; another picture of the same name gets a number
    assert door.put("/dav/iPhone%20Oliver/IMG_0001.jpg", content=data).status_code == 204
    other = door.put("/dav/iPhone%20Oliver/IMG_0001.jpg", content=picture(tmp_path, (9, 9, 9)))
    assert other.status_code == 201 and other.headers["location"].endswith("/IMG_0001-2.jpg")
    assert sorted(p.name for p in (door.photos / "Eingang/iPhone Oliver").iterdir()) == ["IMG_0001-2.jpg", "IMG_0001.jpg"]
    door.app.state.index.scan()
    assert {a["name"]: a["count"] for a in door.admin.get("/api/areas").json()}["Eingang"] == 2
    # where the phone uploads to is the inbox – the only one
    assert [a["name"] for a in door.admin.get("/api/areas").json() if a["inbox"]] == ["Eingang"]
    door.admin.put("/api/upload", json={"area": ""})
    assert not any(a["inbox"] for a in door.admin.get("/api/areas").json())
    door.admin.put("/api/upload", json={"area": "Eingang"})
    state = door.admin.get("/api/upload").json()
    assert state["count"] == 2 and state["last"] > 0


def test_the_door_leads_nowhere_else(door, tmp_path):
    data = picture(tmp_path)
    for path in ("/dav/../Familie/x.jpg", "/dav/%2e%2e/Familie/x.jpg", "/dav/.augenblick/x.jpg", "/dav/a/..%2f..%2fFamilie/x.jpg"):
        assert door.put(path, content=data).status_code in (403, 404, 405, 409), path
    assert not list((door.photos / "Familie").rglob("x.jpg"))
    assert door.put("/dav/skript.sh", content=b"#!/bin/sh").status_code == 415
    assert door.put("/dav/gibtsnicht/a.jpg", content=data).status_code == 409
    assert door.put("/dav/a.jpg", content=data).status_code == 201
    # no reading back, no deleting, no moving
    assert door.get("/dav/a.jpg").status_code == 403
    assert door.delete("/dav/a.jpg").status_code == 403
    assert door.request("MOVE", "/dav/a.jpg", headers={"Destination": "/dav/b.jpg"}).status_code == 403
    assert (door.photos / "Eingang/a.jpg").is_file()
    # the upload key opens nothing but this door
    assert door.get("/api/areas").status_code == 401
    assert door.get("/api/timeline", headers={"Authorization": f"Bearer {door.key}"}).status_code == 401
    assert door.post("/api/upload/key").status_code == 401


def test_without_the_key_nothing(door, tmp_path, monkeypatch):
    data = picture(tmp_path)
    stranger = browser(door.app)
    assert stranger.request("OPTIONS", "/dav/").status_code == 200
    assert stranger.request("PROPFIND", "/dav/").status_code == 401
    wrong = {"Authorization": "Basic " + base64.b64encode(b"upload:falsch").decode()}
    assert stranger.put("/dav/a.jpg", content=data, headers=wrong).status_code == 401
    assert not (door.photos / "Eingang/a.jpg").exists()
    # the API key of the administrator does not fit either
    api = door.admin.post("/api/security/api-token").json()["api_token"]
    assert stranger.put("/dav/a.jpg", content=data, headers={"Authorization": f"Bearer {api}"}).status_code == 401
    # too many wrong keys from one address: a pause, even for the right key
    monkeypatch.setattr(upload, "TRIES", 3)
    late = browser(create_late(door))
    for _ in range(3):
        assert late.request("PROPFIND", "/dav/", headers=wrong).status_code == 401
    assert late.request("PROPFIND", "/dav/", headers={"Authorization": door.headers["Authorization"]}).status_code == 429
    # a new key shuts the old one out; a deleted key shuts the door
    door.admin.post("/api/upload/key")
    assert door.request("PROPFIND", "/dav/").status_code == 401
    assert door.admin.delete("/api/upload/key").json()["has_key"] is False


def create_late(door):
    """The same installation started anew – so that the counter of wrong keys starts at nothing."""
    return create_app(door.app.state.settings, background=False)


def test_no_upload_without_an_area_or_when_read_only(door, tmp_path):
    data = picture(tmp_path)
    door.admin.put("/api/upload", json={"area": ""})
    assert door.put("/dav/a.jpg", content=data).status_code == 503
    locked = create_app(dataclasses.replace(door.app.state.settings, readonly=True), background=False)
    locked.state.inbox.save(area="Eingang")
    phone = browser(locked)
    assert phone.put("/dav/a.jpg", content=data, headers={"Authorization": door.headers["Authorization"]}).status_code == 403
    assert not (door.photos / "Eingang/a.jpg").exists()


def test_a_file_in_pieces_is_sent_back(door, tmp_path):
    """Some apps send large files in pieces. A piece taken for the file would be a ruined film."""
    data = picture(tmp_path)
    sent = door.put("/dav/film.mov", content=data[:100], headers={"Content-Range": f"bytes 0-99/{len(data)}"})
    assert sent.status_code == 400 and "pieces" in sent.text
    assert [p.name for p in (door.photos / "Eingang").iterdir()] == []
    assert door.put("/dav/film.mov", content=data).status_code == 201


def test_the_phones_raw_files_come_in(door, tmp_path):
    assert door.put("/dav/IMG_0002.DNG", content=picture(tmp_path)).status_code == 201
    assert door.put("/dav/notiz.xmp", content=b"<x/>").status_code == 415


def test_every_phone_its_own_key(door, tmp_path):
    """One key per phone or person: one can be taken away without the others noticing, and one can lead elsewhere."""
    admin, data = door.admin, picture(tmp_path)
    first = admin.get("/api/upload").json()["keys"]
    assert len(first) == 1 and first[0]["name"] == ""                    # the key made before there were several
    made = admin.post("/api/upload/keys", json={"name": "  Gretas   iPhone ", "area": "Greta"})
    assert made.status_code == 201 and len(made.json()["key"]) > 30
    hers = next(k for k in made.json()["keys"] if k["id"] == made.json()["key_id"])
    assert (hers["name"], hers["area"]) == ("Gretas iPhone", "Greta") and "hash" not in hers
    assert admin.post("/api/upload/keys", json={"name": "x", "area": "Nirgends"}).status_code == 404

    greta = browser(door.app)
    greta.headers["Authorization"] = "Basic " + base64.b64encode(f"greta:{made.json()['key']}".encode()).decode()
    assert greta.put("/dav/IMG_1.jpg", content=data).status_code == 201
    assert door.put("/dav/IMG_1.jpg", content=data).status_code == 201
    assert (door.photos / "Greta/IMG_1.jpg").is_file() and (door.photos / "Eingang/IMG_1.jpg").is_file()
    assert "IMG_1" in greta.request("PROPFIND", "/dav/").text and "2023" in greta.request("PROPFIND", "/dav/").text
    counted = {k["name"]: k["count"] for k in admin.get("/api/upload").json()["keys"]}
    assert counted == {"": 1, "Gretas iPhone": 1}
    assert {a["name"] for a in admin.get("/api/areas").json() if a["inbox"]} == {"Eingang", "Greta"}

    assert admin.patch(f"/api/upload/keys/{hers['id']}", json={"name": "Greta", "area": ""}).status_code == 200
    assert greta.put("/dav/IMG_2.jpg", content=data).status_code == 201 and (door.photos / "Eingang/IMG_2.jpg").is_file()

    renewed = admin.post(f"/api/upload/keys/{hers['id']}/renew").json()
    assert greta.put("/dav/IMG_3.jpg", content=data).status_code == 401      # the old key is dead at once
    greta.headers["Authorization"] = "Bearer " + renewed["key"]
    assert greta.put("/dav/IMG_3.jpg", content=data).status_code == 201
    assert admin.delete(f"/api/upload/keys/{hers['id']}").status_code == 200
    assert greta.put("/dav/IMG_4.jpg", content=data).status_code == 401
    assert door.put("/dav/IMG_4.jpg", content=data).status_code == 201      # the other phone never noticed
    assert admin.patch("/api/upload/keys/gibtesnicht", json={"name": "x"}).status_code == 404


def test_the_one_key_from_before_goes_on_working(settings):
    import hashlib
    import json
    settings = dataclasses.replace(settings, readonly=False)
    settings.data.mkdir(parents=True, exist_ok=True)
    (settings.data / "upload.json").write_text(json.dumps(
        {"area": "Familie", "hash": hashlib.sha256(b"alter-schluessel").hexdigest(), "made": 5.0, "last": 6.0, "count": 86}))
    app = create_app(settings, background=False)
    phone = browser(app)
    phone.headers["Authorization"] = "Bearer alter-schluessel"
    assert phone.request("PROPFIND", "/dav/").status_code == 207
    admin = browser(app)
    assert admin.post("/api/setup", json={"username": "oliver", "password": PASSWORD}).status_code == 201
    assert admin.get("/api/upload").json()["keys"] == [{"id": "erster", "name": "", "area": "", "made": 5.0, "last": 6.0, "count": 86}]
