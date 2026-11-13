"""Pictures that are there more than once: finding them, and keeping one."""
import shutil
import subprocess
from pathlib import Path

import pytest
from PIL import Image, ImageEnhance, ImageOps

from augenblick.app import create_app
from augenblick.config import Settings
from conftest import PASSWORD, browser, make_jpeg

PICTURE = Path(__file__).parent / "data" / "astronaut.jpg"
tools = pytest.mark.skipif(not shutil.which("exiftool"), reason="exiftool is not installed")


@pytest.fixture
def twice(tmp_path):
    photos = tmp_path / "fotos"
    (photos / "Familie/a").mkdir(parents=True)
    (photos / "Familie/b").mkdir(parents=True)
    picture = Image.open(PICTURE).convert("RGB")
    picture.save(photos / "Familie/a/gross.jpg", "JPEG", quality=92)
    picture.resize((picture.width // 3, picture.height // 3)).save(photos / "Familie/b/klein.jpg", "JPEG", quality=60)
    shutil.copy(photos / "Familie/a/gross.jpg", photos / "Familie/b/kopie.jpg")
    ImageOps.mirror(picture).save(photos / "Familie/a/gespiegelt.jpg", "JPEG", quality=92)       # looks alike, is not
    ImageEnhance.Brightness(picture).enhance(0.35).save(photos / "Familie/a/dunkel.jpg", "JPEG")  # another exposure
    make_jpeg(photos / "Familie/a/leer1.jpg", color=(250, 250, 250))                              # blank pages
    make_jpeg(photos / "Familie/a/leer2.jpg", color=(249, 249, 249))
    app = create_app(Settings(photos=photos, data=tmp_path / "data", readonly=False), background=False)
    app.state.index.scan()
    client = browser(app)
    assert client.post("/api/setup", json={"username": "oliver", "password": PASSWORD}).status_code == 201
    client.photos = photos
    return client


def test_copies_are_found_whatever_their_size(twice):
    assert twice.get("/api/dupes").json()["groups"][0]["exact"]          # the same data needs no looking
    assert twice.app.state.dupes.search(["Familie"]) == 6
    assert twice.app.state.dupes.search(["Familie"]) == 0                # what is described stays described
    found = twice.get("/api/dupes").json()
    assert found["total"] == 1 and found["spare"] == 2
    files = found["groups"][0]["files"]
    assert sorted(row["path"] for row in files) == ["Familie/a/gross.jpg", "Familie/b/klein.jpg", "Familie/b/kopie.jpg"]
    assert files[-1]["path"] == "Familie/b/klein.jpg" and files[0]["width"] > files[-1]["width"]   # the best first
    assert not found["groups"][0]["exact"] and all(row["can_delete"] for row in files)
    assert found["sure"] == 1 and found["sure_spare"] == 2            # the same data twice and once smaller: a sure thing
    assert twice.get("/api/jobs").json()["dupes"]["pictures"] == 6


def test_look_alikes_can_be_declared_different(twice):
    twice.app.state.dupes.search(["Familie"])
    bundle = twice.get("/api/dupes").json()["groups"][0]
    assert twice.post("/api/dupes/apart", json={"id": bundle["id"]}).json() == {"apart": 2}
    left = twice.get("/api/dupes").json()
    assert left["total"] == 1 and left["groups"][0]["exact"]             # the two files with the same data remain
    assert twice.post("/api/dupes/apart", json={"id": left["groups"][0]["id"]}).status_code == 422


@tools
def test_one_stays_and_takes_what_the_others_knew(twice):
    twice.app.state.dupes.search(["Familie"])
    files = {row["path"]: row for row in twice.get("/api/dupes").json()["groups"][0]["files"]}
    small = files["Familie/b/klein.jpg"]
    assert twice.patch(f"/api/photos/{small['key']}", json={"add_albums": ["Urlaub/USA"], "add_keywords": ["Raumfahrt"],
                                                           "rating": 4}).status_code == 200
    assert twice.put(f"/api/photos/{small['key']}/faces", json={"x": 0.4, "y": 0.5, "w": 0.3, "h": 0.4,
                                                               "name": "Eileen"}).status_code == 200
    big = files["Familie/a/gross.jpg"]
    done = twice.post("/api/dupes/resolve", json={"keep": big["key"], "drop": [small["key"], files["Familie/b/kopie.jpg"]["key"]]})
    assert done.status_code == 200, done.text
    assert len(done.json()["removed"]) == 2 and set(done.json()["merged"]) == {"albums", "keywords", "rating", "people"}
    kept = twice.get(f"/api/photos/{big['key']}").json()
    assert kept["albums"] == ["Urlaub/USA"] and kept["keywords"] == ["Raumfahrt"] and kept["rating"] == 4
    assert kept["people"] == ["Eileen"]
    assert not (twice.photos / "Familie/b/klein.jpg").exists() and not (twice.photos / "Familie/b/kopie.jpg").exists()
    assert len(twice.get("/api/trash").json()["entries"]) == 2           # nothing is gone for good
    assert twice.get("/api/dupes").json()["total"] == 0
    out = subprocess.run(["exiftool", "-s3", "-XMP-mwg-rs:RegionName", str(twice.photos / "Familie/a/gross.jpg")],
                         capture_output=True, text=True).stdout
    assert out.strip() == "Eileen"


@tools
def test_all_copies_at_once(twice):
    import time
    twice.app.state.dupes.search(["Familie"])
    assert twice.get("/api/dupes?level=alike").json()["total"] >= 1
    assert twice.post("/api/dupes/resolve-all", json={"merge": True}).json() == {"started": 1}
    for _ in range(100):
        if not twice.get("/api/jobs").json()["job"]["running"]:
            break
        time.sleep(0.1)
    job = twice.get("/api/jobs").json()["job"]
    assert job["kind"] == "dupes" and job["done"] == 1 and job["failed"] == 0
    assert (twice.photos / "Familie/a/gross.jpg").exists() and not (twice.photos / "Familie/b/klein.jpg").exists()
    assert twice.get("/api/dupes").json()["total"] == 0 and len(twice.get("/api/trash").json()["entries"]) == 2


def test_two_shots_of_one_size_are_not_a_sure_thing(twice):
    """Same size, a little different: that may be the next shot from a tripod – shown, never cleared unseen."""
    from PIL import ImageDraw
    picture = Image.open(PICTURE).convert("RGB")
    ImageDraw.Draw(picture).rectangle((picture.width * 0.47, picture.height * 0.47, picture.width * 0.53, picture.height * 0.53), fill=(250, 250, 250))
    picture.save(twice.photos / "Familie/a/naechste.jpg", "JPEG", quality=92)
    for name in ("kopie.jpg", "klein.jpg"):
        (twice.photos / "Familie/b" / name).unlink()
    twice.app.state.index.scan()
    twice.app.state.dupes.search(["Familie"])
    found = twice.get("/api/dupes?level=alike").json()
    assert found["total"] == 1 and found["sure"] == 0 and not found["groups"][0]["exact"]
    assert twice.post("/api/dupes/resolve-all", json={"merge": True}).json() == {"started": 0}
