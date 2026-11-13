"""Albums that fill themselves, raw files, and what the overview needs to show pictures in their shape."""

import dataclasses
import io
import shutil
from datetime import date
from pathlib import Path

import pytest
from PIL import Image

from augenblick.app import create_app
from conftest import PASSWORD, browser, make_jpeg
from test_live import FILM, live_jpeg

needs_exiftool = pytest.mark.skipif(not shutil.which("exiftool"), reason="exiftool is not installed")


def raw_file(path: Path) -> Path:
    """Built like the phone's raw file: a TIFF whose first picture is the finished one, lying on its side."""
    exif = Image.Exif()
    exif[0x0112] = 6
    exif[0x010F], exif[0x0110] = "Apple", "TestPhone"
    exif[0x8769] = {0x9003: "2022:04:15 16:30:51"}
    out = io.BytesIO()
    Image.new("RGB", (80, 60), (20, 40, 60)).save(out, "TIFF", exif=exif.tobytes())
    path.write_bytes(out.getvalue())
    return path


@pytest.fixture
def rw(settings):
    settings = dataclasses.replace(settings, readonly=False)
    today = date.today()
    make_jpeg(settings.photos / "Familie/Weit/pano.jpg", size=(1600, 500), taken="2021:06:01 12:00:00", model="TestCam 1")
    make_jpeg(settings.photos / "Familie/Weit/streifen.jpg", size=(297, 600), color=(3, 3, 3), model="TestCam 1")    # a cut-out
    make_jpeg(settings.photos / "Familie/Weit/damals.jpg", color=(7, 7, 7), model="TestCam 1",
              taken=f"{today.year - 3}:{today.month:02d}:{today.day:02d} 09:00:00")
    make_jpeg(settings.photos / "Familie/Weit/lang.jpg", size=(100, 300), color=(8, 8, 8))     # a long screenshot: no camera
    raw_file(settings.photos / "Familie/Weit/IMG_1708.DNG")
    live_jpeg(settings.photos / "Familie/Neu/IMG_1.jpg")
    shutil.copy(FILM, settings.photos / "Familie/Neu/IMG_1.mov")
    shutil.copy(FILM, settings.photos / "Familie/Weit/film.mov")
    app = create_app(settings, background=False)
    app.state.index.scan()
    client = browser(app)
    assert client.post("/api/setup", json={"username": "oliver", "password": PASSWORD}).status_code == 201
    client.index = app.state.index
    return client


def paths(client, auto):
    months = client.get("/api/timeline", params={"auto": auto}).json()
    keys = [item["key"] for m in months for item in client.get("/api/photos", params={"month": m["month"], "auto": auto}).json()]
    return sorted(client.get(f"/api/photos/{key}").json()["path"] for key in keys)


@needs_exiftool
def test_albums_that_fill_themselves(rw):
    found = {a["id"]: (a["name"], a["count"]) for a in rw.get("/api/auto").json()}
    assert found == {"videos": ("Videos", 2), "live": ("Live Photos", 1), "panoramen": ("Panoramen", 1),
                     "raw": ("RAW", 1), "heute": ("An diesem Tag", 1)}        # no favourites yet: not listed
    assert paths(rw, "videos") == ["Familie/2024/film.mov", "Familie/Weit/film.mov"]    # the film of the Live Photo is not one
    assert paths(rw, "live") == ["Familie/Neu/IMG_1.jpg"]
    assert paths(rw, "panoramen") == ["Familie/Weit/pano.jpg"]
    assert paths(rw, "heute") == ["Familie/Weit/damals.jpg"]
    assert paths(rw, "gibtesnicht") == paths(rw, "")                              # an unknown name filters nothing

    key = rw.index.by_path("Familie/Weit/pano.jpg")["key"]
    assert rw.patch(f"/api/photos/{key}", json={"rating": 5}).status_code == 200
    assert paths(rw, "favoriten") == ["Familie/Weit/pano.jpg"]
    assert rw.get("/api/auto", params={"areas": "Greta"}).json() == []


def test_raw_files_are_shown_and_left_alone(rw):
    raw = rw.index.by_path("Familie/Weit/IMG_1708.DNG")
    assert (raw["kind"], raw["taken"], raw["model"], raw["error"]) == ("image", "2022-04-15T16:30:51", "TestPhone", "")
    assert (raw["width"], raw["height"], raw["orientation"]) == (60, 80, 1)       # upright, as the decoder hands it over
    shown = rw.get(f"/api/photos/{raw['key']}").json()
    assert shown["can_delete"] and not shown["can_write"] and not shown["can_turn"]
    assert rw.get(f"/api/photos/{raw['key']}/thumb").status_code == 200
    before = (rw.index.root / raw["path"]).read_bytes()
    refused = rw.patch(f"/api/photos/{raw['key']}", json={"description": "nein"})
    assert refused.status_code == 422 and "raw files" in refused.json()["detail"]
    assert (rw.index.root / raw["path"]).read_bytes() == before


def test_the_overview_knows_the_shape_of_every_picture(rw):
    items = rw.get("/api/photos", params={"month": "2021-06"}).json()
    assert [(item["w"], item["h"]) for item in items] == [(1600, 500)]
