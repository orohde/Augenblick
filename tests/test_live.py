"""Live Photos: the photo and its short film are one thing – shown once, moved, deleted and brought back together."""

import dataclasses
import io
import shutil
from pathlib import Path

import pytest
from PIL import Image

from augenblick.app import create_app
from augenblick.media import live_id, read_video
from augenblick.trash import FOLDER
from conftest import PASSWORD, browser

FILM = Path(__file__).parent / "data" / "klein.mov"
LIVE = "11111111-2222-3333-4444-555555555555"
RULE = {"rules": [{"scope": "Familie", "folder": "{Jahr}", "name": "{JJJJMMTT}_{HHMMSS}"}]}
needs_exiftool = pytest.mark.skipif(not shutil.which("exiftool"), reason="exiftool is not installed")


def apple_note(identifier: str) -> bytes:
    """The maker note of an iPhone with nothing in it but the identifier of a Live Photo."""
    text = identifier.encode() + b"\x00"
    entry = (0x11).to_bytes(2, "big") + (2).to_bytes(2, "big") + len(text).to_bytes(4, "big") + (32).to_bytes(4, "big")
    return b"Apple iOS\x00\x00\x01MM" + (1).to_bytes(2, "big") + entry + b"\x00\x00\x00\x00" + text


def live_jpeg(path: Path, identifier: str = LIVE, taken: str = "2024:07:14 10:20:30", color=(30, 90, 200)) -> Path:
    exif = Image.Exif()
    exif[0x010F] = "Apple"
    exif[0x8769] = {0x9003: taken, 0x927C: apple_note(identifier)}
    out = io.BytesIO()
    Image.new("RGB", (64, 48), color).save(out, "JPEG", exif=exif.tobytes(), quality=90)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(out.getvalue())
    return path


def test_the_identifier_is_read_from_the_maker_note():
    assert live_id(apple_note(LIVE)) == LIVE
    assert live_id(apple_note("kein/Kennzeichen")) == ""
    assert live_id(b"Nikon\x00\x02\x10\x00\x00MM") == ""
    assert live_id(b"Apple iOS\x00\x00\x01MM\x00\x09") == ""          # cut off
    assert live_id(None) == ""


@needs_exiftool
def test_a_film_says_when_how_long_and_whose(tmp_path):
    found = read_video(FILM)
    assert found["taken"] == "2024-07-14T10:20:29" and found["live"] == LIVE
    assert (found["width"], found["height"], found["seconds"]) == (64, 48, 1.0)
    assert found["model"] == "TestPhone"
    (tmp_path / "kaputt.mov").write_bytes(b"kein echter Film")
    assert read_video(tmp_path / "kaputt.mov") == {}


@pytest.fixture
def rw(settings):
    settings = dataclasses.replace(settings, readonly=False)
    live_jpeg(settings.photos / "Familie/Neu/IMG_1.jpg")
    shutil.copy(FILM, settings.photos / "Familie/Neu/IMG_1.mov")
    app = create_app(settings, background=False)
    app.state.index.scan()
    client = browser(app)
    assert client.post("/api/setup", json={"username": "oliver", "password": PASSWORD}).status_code == 201
    client.photos, client.index = settings.photos, app.state.index
    return client


def key(client, path):
    return client.index.by_path(path)["key"]


def names(client, folder):
    return sorted(p.name for p in (client.photos / folder).iterdir()) if (client.photos / folder).is_dir() else []


@needs_exiftool
def test_the_film_is_part_of_its_photo(rw):
    photo = rw.get(f"/api/photos/{key(rw, 'Familie/Neu/IMG_1.jpg')}").json()
    film = key(rw, "Familie/Neu/IMG_1.mov")
    assert photo["film"] == film and "live" not in photo
    shown = rw.get("/api/photos", params={"month": "2024-07", "folder": "Familie/Neu"}).json()
    assert [item["kind"] for item in shown] == ["image"]                # the film is not a tile of its own
    assert {a["name"]: a["count"] for a in rw.get("/api/areas").json()}["Familie"] == 5     # four from before and this one
    assert rw.get(f"/api/photos/{film}/file").status_code == 200       # but it can be played
    assert rw.get(f"/api/photos/{film}").json()["seconds"] == 1.0

    # a film whose photo lies elsewhere stands for itself
    shutil.copy(FILM, rw.photos / "Familie/Alt/allein.mov")
    rw.index.scan()
    alone = rw.get("/api/photos", params={"month": "2024-07", "folder": "Familie/Alt"}).json()
    assert [(item["kind"], item["s"]) for item in alone] == [("video", 1.0)]


@needs_exiftool
def test_sorting_takes_the_film_along_and_back(rw):
    assert rw.put("/api/sorting", json=RULE).status_code == 200
    moved = rw.post("/api/sort", json={"keys": [key(rw, "Familie/Neu/IMG_1.jpg")]}).json()
    assert moved[0]["ok"] and moved[0]["photo"]["path"] == "Familie/2024/20240714_102030.jpg"
    assert "20240714_102030.mov" in names(rw, "Familie/2024") and names(rw, "Familie/Neu") == []
    assert moved[0]["photo"]["film"] == key(rw, "Familie/2024/20240714_102030.mov")
    assert not [m for m in rw.get("/api/sort/preview").json()["moves"] if m["from"].endswith(".mov")]   # no misfit of its own

    entry = rw.get("/api/journal").json()[0]
    assert entry["before"]["with"] == ["Familie/Neu/IMG_1.mov"]
    assert rw.post(f"/api/journal/{entry['id']}/undo").status_code == 200
    assert names(rw, "Familie/Neu") == ["IMG_1.jpg", "IMG_1.mov"]
    assert rw.get(f"/api/photos/{key(rw, 'Familie/Neu/IMG_1.jpg')}").json()["film"]


@needs_exiftool
def test_a_taken_name_gives_both_the_same_number(rw):
    (rw.photos / "Familie/2024/20240714_102030.mov").write_bytes(b"ein anderer Film")
    assert rw.put("/api/sorting", json=RULE).status_code == 200
    moved = rw.post("/api/sort", json={"keys": [key(rw, "Familie/Neu/IMG_1.jpg")]}).json()
    assert moved[0]["photo"]["path"] == "Familie/2024/20240714_102030-2.jpg"
    assert (rw.photos / "Familie/2024/20240714_102030-2.mov").read_bytes() == FILM.read_bytes()
    assert (rw.photos / "Familie/2024/20240714_102030.mov").read_bytes() == b"ein anderer Film"


@needs_exiftool
def test_moving_to_another_folder_takes_the_film_along(rw):
    done = rw.post("/api/move", json={"keys": [key(rw, "Familie/Neu/IMG_1.jpg")], "folder": "Greta/2023"}).json()
    assert done[0]["ok"], done
    assert {"IMG_1.jpg", "IMG_1.mov"} <= set(names(rw, "Greta/2023")) and names(rw, "Familie/Neu") == []


@needs_exiftool
def test_the_trash_holds_both_and_gives_both_back(rw):
    entry = rw.delete(f"/api/photos/{key(rw, 'Familie/Neu/IMG_1.jpg')}").json()["entry"]
    assert names(rw, "Familie/Neu") == []
    assert names(rw, f"Familie/{FOLDER}") == [f"{entry}-IMG_1.jpg", f"{entry}-IMG_1.mov"]
    assert rw.index.by_path("Familie/Neu/IMG_1.mov") is None
    assert rw.get("/api/trash").json()["entries"][0]["with"] == 1

    assert rw.post(f"/api/trash/{entry}/restore").json()["film"]
    assert names(rw, "Familie/Neu") == ["IMG_1.jpg", "IMG_1.mov"]

    entry = rw.delete(f"/api/photos/{key(rw, 'Familie/Neu/IMG_1.jpg')}").json()["entry"]
    assert rw.delete(f"/api/trash/{entry}").status_code == 200
    assert names(rw, f"Familie/{FOLDER}") == []


@needs_exiftool
def test_a_shared_film_stays_with_the_photo_of_its_name(rw):
    """The phone app can send one Live Photo as HEIC and as JPEG: both show the film, one keeps it."""
    live_jpeg(rw.photos / "Familie/Neu/IMG_1_MP.jpg", color=(31, 91, 201))
    rw.index.scan()
    twin = rw.get(f"/api/photos/{key(rw, 'Familie/Neu/IMG_1_MP.jpg')}").json()
    assert twin["film"] == key(rw, "Familie/Neu/IMG_1.mov")
    rw.delete(f"/api/photos/{twin['key']}")
    assert names(rw, "Familie/Neu") == ["IMG_1.jpg", "IMG_1.mov"]         # the film did not go with the twin
