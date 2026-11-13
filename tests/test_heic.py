"""HEIC, the phone's own format: what the picture is, writing into it, turning it by its turn field."""
import dataclasses
import io
import shutil
import subprocess
from pathlib import Path

import pytest
from PIL import Image, ImageChops, ImageStat

from augenblick.app import create_app
from augenblick.media import heif_image_hash, heif_parts, heif_turn, image_hash
from conftest import PASSWORD, browser
from test_edit import key_of, tags

pytest.importorskip("pillow_heif")
pytestmark = pytest.mark.skipif(not shutil.which("exiftool"), reason="needs exiftool")
SAMPLE = Path(__file__).parent / "data" / "klein.heic"


def shown(path):
    with Image.open(path) as image:
        return image.convert("RGB")


def alike(a, b):
    return a.size == b.size and ImageStat.Stat(ImageChops.difference(a, b)).mean[0] < 3


@pytest.fixture
def rw(settings):
    settings = dataclasses.replace(settings, readonly=False)
    target = settings.photos / "Familie/telefon/IMG_0001.HEIC"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(SAMPLE, target)
    app = create_app(settings, background=False)
    app.state.index.scan()
    client = browser(app)
    assert client.post("/api/setup", json={"username": "oliver", "password": PASSWORD}).status_code == 201
    client.photos, client.path = settings.photos, target
    return client


def test_the_sample_is_understood():
    data = SAMPLE.read_bytes()
    parts = heif_parts(data)
    assert parts and parts["types"][parts["primary"]] in (b"hvc1", b"grid")
    at, quarter = heif_turn(data)
    assert quarter == 0 and data[at - 4:at] == b"irot"
    assert heif_image_hash(data) == image_hash(data, "heic") != image_hash(data, "png")
    assert heif_parts(b"\xff\xd8 not a heif file at all") is None and heif_turn(data[:200]) is None


def test_metadata_does_not_change_what_the_picture_is(rw):
    key = key_of(rw, "IMG_0001.HEIC")
    pixels = heif_image_hash(rw.path.read_bytes())
    assert rw.get(f"/api/photos/{key}").json()["can_turn"] is True
    body = rw.patch(f"/api/photos/{key}", json={
        "taken": "2026-11-13T08:26:33", "lat": 49.86, "lon": 7.87, "description": "Größe: 3 m",
        "keywords": ["Probe", "Öl & Essig"], "rating": 5}).json()
    assert body["key"] == key and body["taken_src"] == "exif"
    assert body["keywords"] == ["Probe", "Öl & Essig"] and body["rating"] == 5 and abs(body["lat"] - 49.86) < 1e-6
    assert heif_image_hash(rw.path.read_bytes()) == pixels          # exiftool added Exif and XMP, nothing else
    assert tags(rw.path, "ExifIFD:DateTimeOriginal")["ExifIFD:DateTimeOriginal"] == "2026:11:13 08:26:33"
    # … and it goes away again
    entry = rw.get("/api/journal").json()[0]
    assert rw.post(f"/api/journal/{entry['id']}/undo").status_code == 200
    assert heif_image_hash(rw.path.read_bytes()) == pixels
    assert rw.app.state.index.refresh("Familie/telefon/IMG_0001.HEIC")["keywords"] == []
    assert not list(rw.path.parent.glob(".augenblick-*"))


@pytest.mark.parametrize("degrees", [90, 180, 270])
def test_turning_changes_one_byte(rw, degrees):
    original = rw.path.read_bytes()
    before = shown(rw.path)
    key = key_of(rw, "IMG_0001.HEIC")
    body = rw.post(f"/api/photos/{key}/rotate", json={"degrees": degrees}).json()
    assert body["key"] != key and body["orientation"] == 1
    assert (body["width"], body["height"]) == ((64, 96) if degrees != 180 else (96, 64))
    after = rw.path.read_bytes()
    at, quarter = heif_turn(after)
    assert quarter == (4 - degrees // 90) % 4
    assert after == original[:at] + bytes([quarter]) + original[at + 1:]      # the picture data is untouched
    assert alike(shown(rw.path), before.rotate(-degrees, expand=True))
    preview = Image.open(io.BytesIO(rw.get(f"/api/photos/{body['key']}/thumb").content))
    assert (preview.width > preview.height) == (body["width"] > body["height"])
    entry = rw.get("/api/journal").json()[0]
    assert entry["action"] == "rotate" and entry["after"]["lossless"] is True and entry["after"]["turn"] == degrees
    # twice the same way adds up
    again = rw.post(f"/api/photos/{body['key']}/rotate", json={"degrees": degrees}).json()
    assert alike(shown(rw.path), before.rotate(-2 * degrees, expand=True))
    for entry in rw.get("/api/journal").json()[:2]:
        assert rw.post(f"/api/journal/{entry['id']}/undo").status_code == 200
    assert rw.path.read_bytes() == original
    assert rw.app.state.index.by_path("Familie/telefon/IMG_0001.HEIC")["key"] == key
    assert (again["key"] == key) is (degrees == 180)            # twice half round is where it began


def test_faces_and_the_exif_copy_turn_with_it(rw):
    key = key_of(rw, "IMG_0001.HEIC")
    # the bright corner is top left; a phone also writes the orientation into the Exif
    rw.patch(f"/api/photos/{key}", json={"rating": 1})
    subprocess.run(["exiftool", "-q", "-n", "-overwrite_original", "-IFD0:Orientation=1", str(rw.path)], check=True)
    rw.app.state.index.scan()
    named = rw.put(f"/api/photos/{key}/faces", json={"x": 0.15, "y": 0.15, "w": 0.2, "h": 0.2, "name": "Ecke"})
    assert named.status_code == 200, named.text
    body = rw.post(f"/api/photos/{key}/rotate", json={"degrees": 90}).json()
    assert body["people"] == ["Ecke"]
    box = rw.get(f"/api/photos/{body['key']}/faces").json()[0]
    assert abs(box["x"] - 0.85) < 0.01 and abs(box["y"] - 0.15) < 0.01      # top left went to top right
    found = tags(rw.path, "IFD0:Orientation", "XMP-mwg-rs:RegionInfo")
    assert found["IFD0:Orientation"] == 6                                    # as a phone held upright writes it
    crop = shown(rw.path).crop((44, 0, 64, 30)).resize((1, 1)).getpixel((0, 0))
    assert crop[2] > 150                                                     # the bright corner is where the box is
    entry = rw.get("/api/journal").json()[0]
    assert rw.post(f"/api/journal/{entry['id']}/undo").status_code == 200
    back = rw.get(f"/api/photos/{key}/faces").json()[0]
    assert abs(back["x"] - 0.15) < 0.01 and tags(rw.path, "IFD0:Orientation")["IFD0:Orientation"] == 1
