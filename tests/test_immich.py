"""People named in Immich are taken over once: asked for there, found here, written by the editor."""
import base64
import dataclasses
import hashlib
import shutil

import pytest

from augenblick.app import create_app
from augenblick.immich import Immich, ImmichError
from conftest import who, PASSWORD, browser
from test_faces import BLOCK, block_picture, face, key_of, mark, rw, stored, wait_for_job  # noqa: F401

needs_tools = pytest.mark.skipif(not shutil.which("exiftool"), reason="needs exiftool")
SOURCE = {"url": "http://immich.test:2283", "key": "geheim-zum-testen"}


def box(image, x1, y1, x2, y2, name, hidden=False):
    person = {"id": "p-" + name, "name": name, "isHidden": hidden} if name is not None else None
    return {"imageWidth": image[0], "imageHeight": image[1], "boundingBoxX1": x1, "boundingBoxY1": y1,
            "boundingBoxX2": x2, "boundingBoxY2": y2, "person": person}


class FakeImmich:
    """Answers the three questions the import asks, from what a test puts in – and notes every call."""

    def __init__(self, people, assets, faces):
        self.people, self.assets, self.faces, self.calls = people, assets, faces, []

    def __call__(self, client, method, path, body=None):
        self.calls.append((method, path))
        assert client.base == "http://immich.test:2283/api" and client.key == SOURCE["key"]
        assert method in ("GET", "POST") and (method == "GET" or path == "/search/metadata")    # nothing is changed there
        if path.startswith("/people"):
            return {"people": [{"id": pid, "name": name, "isHidden": hidden} for pid, name, hidden in self.people],
                    "hasNextPage": False}
        if path == "/search/metadata":
            items = self.assets.get(body["personIds"][0], [])
            if "cursor" in body:        # a newer server: the second page comes by cursor
                return {"assets": {"items": items[1:], "nextCursor": None, "nextPage": None}}
            return {"assets": {"items": items[:1], "nextCursor": "weiter" if len(items) > 1 else None, "nextPage": None}}
        if path.startswith("/faces?id="):
            return self.faces[path.split("=", 1)[1]]
        raise AssertionError(path)


def asset(asset_id, path, **more):
    return {"id": asset_id, "type": "IMAGE", "isTrashed": False, "originalPath": path,
            "originalFileName": path.rsplit("/", 1)[-1], "checksum": "", "localDateTime": "", **more}


@pytest.fixture
def immich(rw, monkeypatch):  # noqa: F811
    photos = rw.photos
    sha = base64.b64encode(hashlib.sha1((photos / "Familie/p/krumm.jpg").read_bytes()).digest()).decode()
    fake = FakeImmich(
        people=[("oma", "Oma", False), ("opa", " Opa ", False), ("leer", "", False), ("weg", "Versteckt", True)],
        assets={
            "oma": [asset("a1", "/mnt/fotos/Familie/p/block.jpg"),                      # found by its path
                    asset("a2", "upload/library/ab/cd/krumm.jpg", checksum=sha),        # … by the very same file
                    asset("a3", "upload/x/liegt.jpg", localDateTime="2021-05-06T07:08:09.000Z"),   # … by name and time
                    asset("a4", "upload/x/gibtsnicht.jpg"),
                    asset("a5", "/mnt/fotos/Familie/p/block.jpg", type="VIDEO")],
            "opa": [asset("a1", "/mnt/fotos/Familie/p/block.jpg")],
        },
        faces={
            "a1": [box((640, 480), 80, 80, 240, 240, "Oma"), box((640, 480), 400, 240, 560, 400, " Opa "),
                   box((640, 480), 0, 0, 50, 50, None), box((640, 480), 300, 0, 350, 50, "Versteckt")],
            "a2": [box((720, 500), 90, 83, 270, 250, "Oma")],
            "a3": [box((480, 640), 240, 80, 400, 240, "Oma")],       # liegt.jpg is shown upright: 48 × 64
        })
    monkeypatch.setattr(Immich, "call", lambda self, method, path, body=None: fake(self, method, path, body))
    rw.fake = fake
    return rw


def test_address_and_key_are_checked():
    for url in ("", "immich.local", "ftp://x"):
        with pytest.raises(ImmichError):
            Immich(url, "k")
    with pytest.raises(ImmichError):
        Immich("http://x", " ")
    assert Immich("https://fotos.example/api/", "k").base == "https://fotos.example/api"


@needs_tools
def test_preview_then_import(immich):
    before = {p: p.read_bytes() for p in immich.photos.rglob("*.jpg")}
    assert immich.post("/api/immich/preview", json={"url": "nirgends", "key": "x"}).status_code == 400
    preview = immich.post("/api/immich/preview", json=SOURCE).json()
    assert preview["people"] == [{"name": "Oma", "immich": 4, "found": 3, "named": 0, "todo": 3},
                                 {"name": "Opa", "immich": 1, "found": 1, "named": 0, "todo": 1}]
    assert preview["missed"] == ["upload/x/gibtsnicht.jpg"] and preview["photos"] == 3
    assert {p: p.read_bytes() for p in immich.photos.rglob("*.jpg")} == before          # a preview writes nothing
    assert immich.post("/api/immich/import", json={"token": "0" * 12}).status_code == 409

    assert immich.post("/api/immich/import", json={"token": preview["token"]}).json() == {"total": 3}
    job = wait_for_job(immich)
    assert (job["kind"], job["done"], job["failed"]) == ("immich", 3, 0), job["errors"]
    assert who(immich) == [{"name": "Oma", "count": 3}, {"name": "Opa", "count": 1}]
    # the boxes lie where Immich saw the faces – also in the picture that is stored lying
    regions = stored(immich.photos / "Familie/p/block.jpg")["RegionList"]
    assert [(r["Name"], r["Type"]) for r in regions] == [("Oma", "Face"), ("Opa", "Face")]
    assert [round(regions[0]["Area"][k], 4) for k in "XYWH"] == [0.25, 0.3333, 0.25, 0.3333]
    shown = immich.get(f"/api/photos/{key_of(immich, 'liegt.jpg')}/faces").json()
    assert [round(shown[0][k], 4) for k in "xywh"] == [0.6667, 0.25, 0.3333, 0.25] and shown[0]["name"] == "Oma"
    # one batch in the journal, each photo can be taken back
    entries = immich.get("/api/journal").json()[:3]
    assert len({e["batch"] for e in entries}) == 1 and {e["action"] for e in entries} == {"faces"}
    assert immich.post(f"/api/journal/{entries[0]['id']}/undo").status_code == 200
    # the token is used up; a second preview offers only what is still missing
    assert immich.post("/api/immich/import", json={"token": preview["token"]}).status_code == 409
    again = immich.post("/api/immich/preview", json=SOURCE).json()
    assert sum(row["todo"] for row in again["people"]) >= 1 and again["photos"] == 1
    assert not any(method not in ("GET", "POST") for method, _ in immich.fake.calls)


@needs_tools
def test_only_chosen_people_and_no_name_is_overwritten(immich):
    mark(immich.photos / "Familie/p/krumm.jpg", face(BLOCK, "Tante Erna"), size=(72, 50))
    immich.app.state.index.scan()
    # the file is no longer the one Immich read, so it is found by its name and the moment it was taken
    immich.fake.assets["oma"][1]["localDateTime"] = "2021-05-06T07:08:09.000Z"
    preview = immich.post("/api/immich/preview", json=SOURCE).json()
    assert immich.post("/api/immich/import", json={"token": preview["token"], "people": ["Oma"]}).json() == {"total": 3}
    assert wait_for_job(immich)["failed"] == 0
    assert who(immich) == [{"name": "Oma", "count": 2}, {"name": "Tante Erna", "count": 1}]
    assert [r["Name"] for r in stored(immich.photos / "Familie/p/krumm.jpg")["RegionList"]] == ["Tante Erna"]


@needs_tools
def test_picture_turned_since_is_left_alone(immich):
    immich.fake.faces["a3"] = [box((640, 480), 80, 80, 240, 240, "Oma")]       # Immich saw it lying, here it stands
    preview = immich.post("/api/immich/preview", json=SOURCE).json()
    immich.post("/api/immich/import", json={"token": preview["token"]})
    job = wait_for_job(immich)
    assert job["failed"] == 1 and job["errors"][0]["path"].endswith("liegt.jpg")
    assert stored(immich.photos / "Familie/p/liegt.jpg") is None


def test_import_needs_an_administrator_and_write_access(immich):
    immich.post("/api/users", json={"name": "gast", "password": PASSWORD, "areas": {"Familie": "write"}})
    guest = browser(immich.app)
    assert guest.post("/api/login", json={"username": "gast", "password": PASSWORD}).status_code == 200
    assert guest.post("/api/immich/preview", json=SOURCE).status_code == 403
    locked = create_app(dataclasses.replace(immich.app.state.settings, readonly=True), background=False)
    reader = browser(locked)
    assert reader.post("/api/login", json={"username": "oliver", "password": PASSWORD}).status_code == 200
    assert reader.post("/api/immich/preview", json=SOURCE).status_code == 403
    assert immich.fake.calls == []
