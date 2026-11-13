"""Working on the picture itself: a new picture beside the original, which is never changed."""

import dataclasses
import io
import shutil

import pytest
from PIL import Image

from augenblick.app import create_app
from augenblick.media import Frame, clean_recipe, develop
from augenblick.trash import FOLDER
from conftest import PASSWORD, browser, make_jpeg
from test_live import FILM, live_jpeg

pytestmark = pytest.mark.skipif(not shutil.which("exiftool"), reason="exiftool is not installed")
RULE = {"rules": [{"scope": "Familie", "folder": "{Jahr}", "name": "{JJJJMMTT}_{HHMMSS}"}]}


def test_recipes_are_tidied():
    assert clean_recipe({"angle": 0, "brightness": 0, "crop": [0, 0, 1, 1], "auto": False}) == {}
    assert clean_recipe({"angle": "2.5", "crop": [0.1, 0.2, 0.5, 0.5], "warmth": 10.4}) == \
        {"angle": 2.5, "crop": [0.1, 0.2, 0.5, 0.5], "warmth": 10}
    for bad in ({"angle": 45}, {"crop": [0.5, 0.5, 0.8, 0.2]}, {"crop": [0, 0, 1]}, {"contrast": 300}, "nein"):
        with pytest.raises(ValueError):
            clean_recipe(bad)


def test_the_picture_is_cut_set_straight_and_coloured():
    image = Image.new("RGB", (400, 200), (100, 100, 100))
    image.paste((250, 0, 0), (200, 0, 400, 200))                       # the right half is red
    cut = develop(image, {"crop": [0.5, 0, 0.5, 1]})
    assert cut.size == (200, 200) and cut.getpixel((100, 100))[0] > 240
    straight = develop(image, {"angle": 10})
    assert straight.size[0] < 400 and abs(straight.size[0] / straight.size[1] - 2) < 0.03     # same shape, corners gone
    assert develop(image, {"angle": 10}, crop=False).size == straight.size
    assert develop(image, {"brightness": 50}).getpixel((10, 10))[0] > 110 > develop(image, {"brightness": -50}).getpixel((10, 10))[0]
    warm = develop(image, {"warmth": 60}).getpixel((10, 10))
    assert warm[0] > warm[1] > warm[2]
    assert develop(image, {"saturation": -100}).getpixel((300, 100))[0] < 120                  # red has gone grey
    assert image.size == (400, 200) and image.getpixel((10, 10)) == (100, 100, 100)            # the one given is untouched


def test_points_are_carried_there_and_back():
    frame = Frame({"angle": 7, "crop": [0.2, 0.1, 0.6, 0.7]}, 4000, 3000)
    x, y = frame.forward(0.4, 0.55)
    back = frame.back(x, y)
    assert abs(back[0] - 0.4) < 1e-9 and abs(back[1] - 0.55) < 1e-9
    assert Frame({}, 400, 300).forward(0.25, 0.75) == (0.25, 0.75)
    assert Frame({"crop": [0.5, 0, 0.5, 1]}, 400, 300).forward(0.75, 0.5) == (0.5, 0.5)


@pytest.fixture
def rw(settings):
    settings = dataclasses.replace(settings, readonly=False)
    make_jpeg(settings.photos / "Familie/Neu/bild.jpg", size=(400, 300), taken="2024:07:14 10:20:30", orientation=6)
    live_jpeg(settings.photos / "Familie/Live/IMG_1.jpg")
    shutil.copy(FILM, settings.photos / "Familie/Live/IMG_1.mov")
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


def test_a_new_picture_beside_the_original(rw):
    before = (rw.photos / "Familie/Neu/bild.jpg").read_bytes()
    first = key(rw, "Familie/Neu/bild.jpg")
    assert rw.get(f"/api/photos/{first}").json()["can_develop"]
    for face in ({"x": 0.25, "y": 0.5, "w": 0.2, "h": 0.2, "name": "Greta"}, {"x": 0.9, "y": 0.5, "w": 0.1, "h": 0.1, "name": "Horst"}):
        assert rw.put(f"/api/photos/{first}/faces", json=face).status_code == 200
    with_faces = (rw.photos / "Familie/Neu/bild.jpg").read_bytes()
    first = key(rw, "Familie/Neu/bild.jpg")

    shown = rw.post(f"/api/photos/{first}/develop/preview", json={"brightness": 30, "crop": [0, 0, 0.5, 1]})
    assert shown.status_code == 200 and Image.open(io.BytesIO(shown.content)).size == (300, 400)     # upright, not cut yet
    assert (rw.photos / "Familie/Neu/bild.jpg").read_bytes() == with_faces

    made = rw.post(f"/api/photos/{first}/develop", json={"crop": [0, 0, 0.5, 1], "brightness": 30})
    assert made.status_code == 200, made.text
    made = made.json()
    assert names(rw, "Familie/Neu") == ["bild.jpg", "bild_original.jpg"]
    assert (rw.photos / "Familie/Neu/bild_original.jpg").read_bytes() == with_faces                   # not one byte changed
    assert made["path"] == "Familie/Neu/bild.jpg" and (made["width"], made["height"], made["orientation"]) == (150, 400, 1)
    assert made["recipe"] == {"crop": [0.0, 0.0, 0.5, 1.0], "brightness": 30} and made["taken"] == "2024-07-14T10:20:30"
    assert made["original"] == key(rw, "Familie/Neu/bild_original.jpg")
    assert made["people"] == ["Greta"]                                  # Horst stood in the half that was cut away
    box = next(f for f in rw.get(f"/api/photos/{made['key']}/faces").json() if f.get("name") == "Greta")
    assert abs(box["x"] - 0.5) < 0.01 and abs(box["w"] - 0.4) < 0.01    # twice as wide a share of half the picture

    shown = rw.get("/api/photos", params={"month": "2024-07", "folder": "Familie/Neu"}).json()
    assert [item["key"] for item in shown] == [made["key"]]             # the original stands behind it
    assert rw.get(f"/api/photos/{made['original']}/file").content == with_faces
    assert before != with_faces


def test_again_starts_from_the_original_and_back_is_back(rw):
    first = rw.post(f"/api/photos/{key(rw, 'Familie/Neu/bild.jpg')}/develop", json={"crop": [0, 0, 0.5, 0.5]}).json()
    again = rw.post(f"/api/photos/{first['key']}/develop", json={"angle": 3, "saturation": -40}).json()
    assert names(rw, "Familie/Neu") == ["bild.jpg", "bild_original.jpg"]
    assert again["recipe"] == {"angle": 3.0, "saturation": -40} and again["width"] > 250      # not the quarter any more

    entries = rw.get("/api/journal").json()
    assert [e["action"] for e in entries[:2]] == ["develop", "develop"]
    assert rw.post(f"/api/journal/{entries[0]['id']}/undo").json()["recipe"] == {"crop": [0.0, 0.0, 0.5, 0.5]}

    back = rw.delete(f"/api/photos/{key(rw, 'Familie/Neu/bild.jpg')}/develop")
    assert back.status_code == 200 and back.json()["path"] == "Familie/Neu/bild.jpg" and back.json()["original"] is None
    assert names(rw, "Familie/Neu") == ["bild.jpg"] and len(names(rw, f"Familie/{FOLDER}")) == 1
    assert rw.delete(f"/api/photos/{back.json()['key']}/develop").status_code == 422


def test_the_original_goes_wherever_the_picture_goes(rw):
    made = rw.post(f"/api/photos/{key(rw, 'Familie/Neu/bild.jpg')}/develop", json={"contrast": 20}).json()
    assert rw.put("/api/sorting", json=RULE).status_code == 200
    moved = rw.post("/api/sort", json={"keys": [made["key"]]}).json()[0]
    assert moved["ok"] and moved["photo"]["path"] == "Familie/2024/20240714_102030.jpg"
    assert {"20240714_102030.jpg", "20240714_102030_original.jpg"} <= set(names(rw, "Familie/2024"))
    assert moved["photo"]["original"]
    assert not [m for m in rw.get("/api/sort/preview").json()["moves"] if "original" in m["from"]]

    entry = rw.delete(f"/api/photos/{moved['photo']['key']}").json()["entry"]
    assert len([n for n in names(rw, f"Familie/{FOLDER}") if n.startswith(f"{entry}-")]) == 2
    restored = rw.post(f"/api/trash/{entry}/restore").json()
    assert restored["original"] and restored["recipe"] == {"contrast": 20}


def test_a_live_photo_keeps_its_film(rw):
    made = rw.post(f"/api/photos/{key(rw, 'Familie/Live/IMG_1.jpg')}/develop", json={"warmth": 15}).json()
    assert names(rw, "Familie/Live") == ["IMG_1.jpg", "IMG_1.mov", "IMG_1_original.jpg"]
    assert made["film"] == key(rw, "Familie/Live/IMG_1.mov") and made["original"]
    shown = rw.get("/api/photos", params={"month": "2024-07", "folder": "Familie/Live"}).json()
    assert [item["key"] for item in shown] == [made["key"]]


def test_what_cannot_be_worked_on(rw, greta=None):
    film = key(rw, "Familie/Live/IMG_1.mov")
    assert rw.post(f"/api/photos/{film}/develop", json={"contrast": 20}).status_code == 422
    picture = key(rw, "Familie/Neu/bild.jpg")
    assert rw.post(f"/api/photos/{picture}/develop", json={}).status_code == 422                     # nothing to do
    assert rw.post(f"/api/photos/{picture}/develop", json={"angle": 80}).status_code == 422
    assert names(rw, "Familie/Neu") == ["bild.jpg"]
