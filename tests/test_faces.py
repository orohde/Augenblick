"""People: names live in the photos as marked regions; where the faces are is worked out and may be thrown away."""
import dataclasses
import io
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest
from PIL import Image, ImageStat

from augenblick.app import create_app
from augenblick.config import Settings
from augenblick.edit import INVERSE, PIL_OPS, _struct, turn_regions
from augenblick.faces import ENGINE, MODELS, Engine, People
from augenblick.media import jpeg_image_hash, read_image, read_xmp, turn_box
from conftest import who, PASSWORD, browser, make_jpeg

needs_tools = pytest.mark.skipif(not (shutil.which("exiftool") and shutil.which("jpegtran")),
                                 reason="needs exiftool and jpegtran")
REGIONS = "XMP-mwg-rs:RegionInfo"
BLOCK = (0.25, 1 / 3, 0.25, 1 / 3)       # where the bright block of block_picture() lies: centre, width, height


# ------------------------------------------------------------------ helpers

def block_picture(path, size=(64, 48), orientation=None):
    """Dark, with one bright block in the upper left – so it shows where a region ended up."""
    image = Image.new("RGB", size, (20, 30, 40))
    image.paste((250, 250, 250), (size[0] // 8, size[1] // 6, size[0] * 3 // 8, size[1] // 2))
    exif = Image.Exif()
    if orientation:
        exif[0x0112] = orientation
    exif[0x8769] = {0x9003: "2021:05:06 07:08:09", 0xA002: size[0], 0xA003: size[1]}
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, "JPEG", exif=exif.tobytes(), quality=92)
    return path


def mark(path, *regions, size=(64, 48)):
    """Write regions the way another programme would have."""
    info = {"AppliedToDimensions": {"W": size[0], "H": size[1], "Unit": "pixel"}, "RegionList": list(regions)}
    subprocess.run(["exiftool", "-q", "-n", "-overwrite_original", f"-{REGIONS}={_struct(info)}", str(path)], check=True)


def face(box, name=None, **more):
    region = {"Area": dict(zip("XYWH", box), Unit="normalized"), "Type": "Face", **more}
    return dict(region, Name=name) if name else region


def stored(path):
    out = subprocess.run(["exiftool", "-j", "-n", "-G1", "-struct", f"-{REGIONS}", str(path)],
                         capture_output=True, text=True, check=True).stdout
    return json.loads(out)[0].get(REGIONS)


def shown(path):
    with Image.open(path) as image:
        image.load()
        n = image.getexif().get(0x0112, 1)
        return (image.transpose(PIL_OPS[n]) if n != 1 else image.copy()), n


def brightness(image, box):
    """How bright the shown picture is inside a box (centre, width, height as fractions), a little inside the edge."""
    x, y, w, h = box
    w, h = w * 0.7, h * 0.7
    crop = image.convert("L").crop((round((x - w / 2) * image.width), round((y - h / 2) * image.height),
                                    round((x + w / 2) * image.width), round((y + h / 2) * image.height)))
    return ImageStat.Stat(crop).mean[0]


def region_points_at_block(path):
    """The first face region of the file, taken to the shown picture, must lie on the bright block."""
    image, n = shown(path)
    _, meta = read_image(path.read_bytes())
    region = next(r for r in meta["regions"] if r["type"] == "Face")
    return brightness(image, turn_box((region["x"], region["y"], region["w"], region["h"]), n)) > 220


def key_of(client, name):
    for month in client.get("/api/timeline").json():
        for item in client.get(f"/api/photos?month={month['month']}").json():
            if client.get(f"/api/photos/{item['key']}").json()["name"] == name:
                return item["key"]
    raise AssertionError(name)


class FakeEngine:
    """Stands in for the networks: one face per picture; who it is follows from the picture's colour."""
    missing = ""

    def __init__(self):
        self.seen = 0

    def find(self, data):
        import numpy
        self.seen += 1
        image = Image.open(io.BytesIO(data))
        n = image.getexif().get(0x0112, 1)
        red, green, _ = image.convert("RGB").getpixel((image.width - 2, image.height - 2))
        vector = numpy.zeros(128, dtype="float32")
        vector[red // 64] = 1.0                      # four people …
        vector[10 + green // 64] = 0.25              # … who look a little different from photo to photo
        vector /= numpy.linalg.norm(vector)
        x, y, w, h = turn_box(BLOCK, INVERSE[n])
        return [{"x": x, "y": y, "w": w, "h": h, "score": 0.9, "vector": vector.tobytes(), "thumb": b""}]


@pytest.fixture
def rw(tmp_path):
    photos = tmp_path / "fotos"
    block_picture(photos / "Familie/p/block.jpg")
    block_picture(photos / "Familie/p/krumm.jpg", size=(72, 50))
    block_picture(photos / "Familie/p/liegt.jpg", orientation=6)
    make_jpeg(photos / "Greta/x.jpg", color=(0, 200, 0), taken="2023:12:24 18:00:00")
    app = create_app(Settings(photos=photos, data=tmp_path / "data", readonly=False), background=False)
    app.state.index.scan()
    client = browser(app)
    assert client.post("/api/setup", json={"username": "oliver", "password": PASSWORD}).status_code == 201
    client.photos = photos
    return client


# ------------------------------------------------------------------ reading

def test_boxes_turn_like_pictures():
    for n in range(1, 9):
        image = Image.new("L", (200, 100))
        image.paste(255, (20, 10, 60, 40))
        turned = image.transpose(PIL_OPS[n]) if n != 1 else image
        x, y, w, h = turn_box((0.2, 0.25, 0.2, 0.3), n)
        assert turned.getbbox() == (round((x - w / 2) * turned.width), round((y - h / 2) * turned.height),
                                    round((x + w / 2) * turned.width), round((y + h / 2) * turned.height)), n
        back = turn_box((x, y, w, h), INVERSE[n])
        assert all(abs(a - b) < 1e-9 for a, b in zip(back, (0.2, 0.25, 0.2, 0.3)))


def test_regions_in_both_notations():
    head = (b'<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
            b'<rdf:Description rdf:about="" xmlns:mwg-rs="http://www.metadataworkinggroup.com/schemas/regions/" '
            b'xmlns:stArea="http://ns.adobe.com/xmp/sType/Area#"><mwg-rs:Regions rdf:parseType="Resource">'
            b'<mwg-rs:RegionList><rdf:Bag>')
    tail = b'</rdf:Bag></mwg-rs:RegionList></mwg-rs:Regions></rdf:Description></rdf:RDF></x:xmpmeta>'
    short = (b'<rdf:li><rdf:Description mwg-rs:Name="Opa &amp; Oma" mwg-rs:Type="Face"><mwg-rs:Area stArea:x="0.3" '
             b'stArea:y="0.4" stArea:w="0.1" stArea:h="0.2" stArea:unit="normalized"/></rdf:Description></rdf:li>')
    long = (b'<rdf:li rdf:parseType="Resource"><mwg-rs:Area rdf:parseType="Resource"><stArea:x>0.6</stArea:x>'
            b'<stArea:y>0.5</stArea:y><stArea:w>0.2</stArea:w><stArea:h>0.25</stArea:h>'
            b'<stArea:unit>normalized</stArea:unit></mwg-rs:Area><mwg-rs:Name>Greta</mwg-rs:Name></rdf:li>')
    focus = (b'<rdf:li rdf:parseType="Resource"><mwg-rs:Area rdf:parseType="Resource"><stArea:x>0.5</stArea:x>'
             b'<stArea:y>0.5</stArea:y><stArea:d>0.1</stArea:d></mwg-rs:Area><mwg-rs:Type>Focus</mwg-rs:Type>'
             b'<mwg-rs:Name>Scharf</mwg-rs:Name></rdf:li>')
    regions = read_xmp(head + short + long + focus + tail)["regions"]
    assert regions == [{"x": 0.3, "y": 0.4, "w": 0.1, "h": 0.2, "name": "Opa & Oma", "type": "Face"},
                       {"x": 0.6, "y": 0.5, "w": 0.2, "h": 0.25, "name": "Greta", "type": ""},
                       {"x": 0.5, "y": 0.5, "w": 0.1, "h": 0.1, "name": "Scharf", "type": "Focus"}]
    assert "regions" not in read_xmp(head + b"<rdf:li>kaputt" + tail)        # broken XML is no reason to fail


@needs_tools
def test_people_come_from_the_file(rw):
    path = rw.photos / "Familie/p/block.jpg"
    mark(path, face(BLOCK, "Oma Erna"), face((0.7, 0.6, 0.2, 0.3), "Opa"), face((0.5, 0.8, 0.1, 0.1)),
         {"Area": {"X": 0.5, "Y": 0.5, "W": 0.1, "H": 0.1, "Unit": "normalized"}, "Type": "Focus", "Name": "Scharf"})
    index = rw.app.state.index
    index.scan()
    key = key_of(rw, "block.jpg")
    photo = rw.get(f"/api/photos/{key}").json()
    assert photo["people"] == ["Oma Erna", "Opa"] and "regions" not in photo
    assert who(rw) == [{"name": "Oma Erna", "count": 1}, {"name": "Opa", "count": 1}]
    assert [m["count"] for m in rw.get("/api/timeline", params={"person": "Opa"}).json()] == [1]
    assert rw.get("/api/timeline", params={"person": "Op"}).json() == []
    shown_faces = rw.get(f"/api/photos/{key}/faces").json()
    assert [(f["name"], f["marked"]) for f in shown_faces] == [("Oma Erna", True), ("Opa", True), ("", True)]
    # throw the index away: the people come back from the file
    index.forget("Familie/p/block.jpg")
    assert rw.get("/api/people").json() == []
    index.scan()
    assert [p["name"] for p in rw.get("/api/people").json()] == ["Oma Erna", "Opa"]


def test_old_index_reads_the_files_once_more(tmp_path):
    """An index from before people were read gets its new columns – and looks at every file again."""
    import sqlite3
    from augenblick.index import Index
    photos = tmp_path / "fotos"
    make_jpeg(photos / "Familie/a.jpg")
    index = Index(tmp_path / "i.db", photos, tmp_path / "thumbs")
    assert index.scan() == {"added": 1, "removed": 0}
    db = sqlite3.connect(tmp_path / "i.db")
    db.execute("ALTER TABLE photos DROP COLUMN people")
    db.commit()
    db.close()
    index = Index(tmp_path / "i.db", photos, tmp_path / "thumbs")
    assert index.count() == 1                       # nothing is lost meanwhile
    assert index.scan() == {"added": 1, "removed": 0}
    assert index.scan() == {"added": 0, "removed": 0}


# ------------------------------------------------------------------ turning

def test_turned_regions_by_hand():
    info = {"AppliedToDimensions": {"W": 64, "H": 48, "Unit": "pixel"},
            "RegionList": [face((0.25, 0.4, 0.2, 0.3), "A", Rotation=0.1)]}
    turned = turn_regions(info, 6)                  # a quarter turn to the right
    assert turned["AppliedToDimensions"] == {"W": 48, "H": 64, "Unit": "pixel"}
    assert turned["RegionList"][0]["Area"] == {"X": 0.6, "Y": 0.25, "W": 0.3, "H": 0.2, "Unit": "normalized"}
    assert turned["RegionList"][0]["Name"] == "A" and turned["RegionList"][0]["Rotation"] == 0.1
    assert info["RegionList"][0]["Area"]["X"] == 0.25          # the original is not touched
    assert turn_regions(turn_regions(info, 6), 8) == info


@needs_tools
@pytest.mark.parametrize("degrees", [90, 180, 270])
@pytest.mark.parametrize("name", ["block.jpg", "liegt.jpg"])
def test_regions_turn_with_the_picture(rw, degrees, name):
    path = rw.photos / "Familie/p" / name
    focus = {"Area": {"X": 0.9, "Y": 0.9, "W": 0.05, "H": 0.05, "Unit": "normalized"}, "Type": "Focus"}
    mark(path, face(BLOCK, "Oma, die | Erste"), focus)
    rw.app.state.index.scan()
    before = stored(path)
    assert region_points_at_block(path)
    key = key_of(rw, name)
    body = rw.post(f"/api/photos/{key}/rotate", json={"degrees": degrees}).json()
    entry = rw.get("/api/journal").json()[0]
    # the picture that lay on its side and is turned back upright needs no turning at all: the tag goes
    upright_again = name == "liegt.jpg" and degrees == 270
    assert entry["after"]["lossless"] is not upright_again and body["orientation"] == 1
    assert body["people"] == ["Oma, die | Erste"]
    assert region_points_at_block(path)             # the region went round with the picture
    after = stored(path)
    assert (after == before) is upright_again
    with Image.open(path) as image:
        assert (after["AppliedToDimensions"]["W"], after["AppliedToDimensions"]["H"]) == image.size
    assert len(after["RegionList"]) == 2 and after["RegionList"][1]["Type"] == "Focus"
    assert after["RegionList"][0]["Name"] == "Oma, die | Erste"
    # what the page shows is the same face, a quarter turn further
    box = rw.get(f"/api/photos/{body['key']}/faces").json()[0]
    assert brightness(shown(path)[0], (box["x"], box["y"], box["w"], box["h"])) > 220
    # and back: the regions are exactly what they were
    assert rw.post(f"/api/journal/{entry['id']}/undo").status_code == 200
    assert stored(path) == before and region_points_at_block(path)


@needs_tools
def test_turn_by_tag_leaves_regions_alone(rw):
    """Regions refer to the stored picture. Where only the tag turns, they must not move."""
    path = rw.photos / "Familie/p/krumm.jpg"        # its size does not fit the JPEG raster
    mark(path, face(BLOCK, "Oma"), size=(72, 50))
    rw.app.state.index.scan()
    before = stored(path)
    key = key_of(rw, "krumm.jpg")
    body = rw.post(f"/api/photos/{key}/rotate", json={"degrees": 90}).json()
    assert body["orientation"] == 6 and rw.get("/api/journal").json()[0]["after"]["why"] == "raster"
    assert stored(path) == before and region_points_at_block(path)
    box = rw.get(f"/api/photos/{body['key']}/faces").json()[0]
    assert brightness(shown(path)[0], (box["x"], box["y"], box["w"], box["h"])) > 220


def twice(path):
    """Leave the same XMP entry twice in the file, as an older programme did in real photos."""
    packet = subprocess.run(["exiftool", "-b", "-xmp", str(path)], capture_output=True, check=True).stdout
    assert packet.count(b"<stDim:unit>pixel</stDim:unit>") == 1
    side = path.with_suffix(".xmp")
    side.write_bytes(packet.replace(b"<stDim:unit>pixel</stDim:unit>", b"<stDim:unit>pixel</stDim:unit>" * 2))
    subprocess.run(["exiftool", "-q", "-overwrite_original", f"-xmp<={side}", str(path)], check=True)
    side.unlink()


@needs_tools
def test_doubled_xmp_entry_is_written_all_the_same(rw):
    """An earlier programme wrote one XMP entry twice, and exiftool then leaves the XMP alone unless
    told to pass over it. Such a picture now turns without loss, its face goes round with it, and
    a name can be added – the checks are the same as for every other file."""
    path = rw.photos / "Familie/p/block.jpg"
    mark(path, face(BLOCK, "Oma"))
    twice(path)
    rw.app.state.index.scan()
    key = key_of(rw, "block.jpg")
    assert rw.get(f"/api/photos/{key}").json()["people"] == ["Oma"]
    body = rw.post(f"/api/photos/{key}/rotate", json={"degrees": 90}).json()
    entry = rw.get("/api/journal").json()[0]
    assert body["orientation"] == 1 and entry["after"]["lossless"] is True and body["people"] == ["Oma"]
    assert region_points_at_block(path)
    named = rw.put(f"/api/photos/{body['key']}/faces", json={"x": 0.8, "y": 0.8, "w": 0.1, "h": 0.1, "name": "Opa"})
    assert named.status_code == 200, named.text
    assert sorted(named.json()["people"]) == ["Oma", "Opa"]
    assert not list(path.parent.glob(".augenblick-*"))


@needs_tools
def test_unknown_region_fields_are_not_dropped(rw):
    """A field exiftool can read but not write would be lost by writing the list anew – so nothing is written."""
    path = rw.photos / "Familie/p/block.jpg"
    mark(path, face(BLOCK, "Oma"))
    packet = subprocess.run(["exiftool", "-b", "-xmp", str(path)], capture_output=True, check=True).stdout
    side = path.with_suffix(".xmp")
    side.write_bytes(packet.replace(b"<mwg-rs:Type>Face</mwg-rs:Type>", b"<mwg-rs:Type>Face</mwg-rs:Type>"
                                    b"<fremd:Geheim xmlns:fremd='http://example.org/fremd/'>42</fremd:Geheim>"))
    subprocess.run(["exiftool", "-q", "-overwrite_original", f"-xmp<={side}", str(path)], check=True)
    side.unlink()
    rw.app.state.index.scan()
    data = path.read_bytes()
    key = key_of(rw, "block.jpg")
    refused = rw.put(f"/api/photos/{key}/faces", json={"x": 0.8, "y": 0.8, "w": 0.1, "h": 0.1, "name": "Opa"})
    assert refused.status_code == 422 and "check failed" in refused.json()["detail"]
    assert path.read_bytes() == data


# ------------------------------------------------------------------ naming

@needs_tools
def test_name_goes_into_the_file(rw):
    path = rw.photos / "Familie/p/block.jpg"
    foreign = [face((0.7, 0.6, 0.2, 0.3), "Opa", Rotation=0.25),
               {"Area": {"X": 0.9, "Y": 0.9, "W": 0.05, "H": 0.05, "Unit": "normalized"}, "Type": "Focus"}]
    mark(path, *foreign)
    rw.app.state.index.scan()
    pixels = jpeg_image_hash(path.read_bytes())
    key = key_of(rw, "block.jpg")
    x, y, w, h = BLOCK
    body = rw.put(f"/api/photos/{key}/faces", json={"x": x, "y": y, "w": w, "h": h, "name": "  Oma   Erna "})
    assert body.status_code == 200, body.text
    assert body.json()["people"] == ["Opa", "Oma Erna"] and body.json()["key"] == key
    assert jpeg_image_hash(path.read_bytes()) == pixels
    info = stored(path)
    assert info["RegionList"][:2] == foreign                       # what others wrote stays as it was
    assert info["RegionList"][2] == face(tuple(round(v, 6) for v in BLOCK), "Oma Erna")
    assert info["AppliedToDimensions"] == {"W": 64, "H": 48, "Unit": "pixel"}
    assert region_points_at_block(path) is False                   # the first face is Opa's; ours is the third
    entry = rw.get("/api/journal").json()[0]
    assert entry["action"] == "faces" and entry["before"] == {"people": ["Opa"]}
    assert entry["after"] == {"people": ["Opa", "Oma Erna"]}
    count = len(rw.get("/api/journal").json())
    rw.put(f"/api/photos/{key}/faces", json={"x": x, "y": y, "w": w, "h": h, "name": "Oma Erna"})       # nothing new
    assert len(rw.get("/api/journal").json()) == count
    # a box on a face already marked means that face: it is renamed, not marked twice
    renamed = rw.put(f"/api/photos/{key}/faces", json={"x": 0.72, "y": 0.61, "w": 0.2, "h": 0.3, "name": "Opa Karl"})
    assert renamed.json()["people"] == ["Opa Karl", "Oma Erna"]
    assert stored(path)["RegionList"][0] == dict(foreign[0], Name="Opa Karl")
    # taking a name away removes the mark, the rest stays
    gone = rw.put(f"/api/photos/{key}/faces", json={"x": x, "y": y, "w": w, "h": h, "name": ""})
    assert gone.json()["people"] == ["Opa Karl"]
    assert [r.get("Name") for r in stored(path)["RegionList"]] == ["Opa Karl", None]
    # back, step by step, to exactly what the other programme wrote
    for entry in rw.get("/api/journal").json()[:3]:
        assert rw.post(f"/api/journal/{entry['id']}/undo").status_code == 200
    assert stored(path)["RegionList"] == foreign
    assert not list(path.parent.glob(".augenblick-*"))


@needs_tools
def test_last_name_gone_leaves_no_empty_tag(rw):
    path = rw.photos / "Familie/p/block.jpg"
    key = key_of(rw, "block.jpg")
    x, y, w, h = BLOCK
    rw.put(f"/api/photos/{key}/faces", json={"x": x, "y": y, "w": w, "h": h, "name": "Oma"})
    assert stored(path)["RegionList"][0]["Name"] == "Oma"
    assert rw.put(f"/api/photos/{key}/faces", json={"x": x, "y": y, "w": w, "h": h, "name": ""}).json()["people"] == []
    assert stored(path) is None
    undo = rw.get("/api/journal").json()[0]["id"]
    assert rw.post(f"/api/journal/{undo}/undo").json()["people"] == ["Oma"]


@needs_tools
def test_name_on_a_picture_shown_turned(rw):
    """The page speaks of the picture as shown; the file wants the box in the stored picture."""
    path = rw.photos / "Familie/p/liegt.jpg"
    image, n = shown(path)
    assert n == 6
    seen = turn_box(BLOCK, 6)                       # where the block is on the screen
    assert brightness(image, seen) > 220
    key = key_of(rw, "liegt.jpg")
    x, y, w, h = seen
    assert rw.put(f"/api/photos/{key}/faces", json={"x": x, "y": y, "w": w, "h": h, "name": "Oma"}).status_code == 200
    area = stored(path)["RegionList"][0]["Area"]
    assert all(abs(area[k] - v) < 1e-5 for k, v in zip("XYWH", BLOCK))
    assert region_points_at_block(path)
    box = rw.get(f"/api/photos/{key}/faces").json()[0]
    assert all(abs(box[k] - v) < 1e-5 for k, v in zip("xywh", seen)) and box["name"] == "Oma"


@needs_tools
def test_naming_needs_the_right(rw, tmp_path):
    rw.post("/api/users", json={"name": "gast", "password": PASSWORD, "areas": {"Familie": "read"}})
    guest = browser(rw.app)
    assert guest.post("/api/login", json={"username": "gast", "password": PASSWORD}).status_code == 200
    key = key_of(guest, "block.jpg")
    body = {"x": 0.5, "y": 0.5, "w": 0.2, "h": 0.2, "name": "Oma"}
    assert guest.put(f"/api/photos/{key}/faces", json=body).status_code == 403
    assert guest.post("/api/faces/name", json={"faces": [1], "name": "Oma"}).json()[0]["ok"] is False
    assert guest.put("/api/faces", json={"areas": {"Familie": True}}).status_code == 403
    assert guest.get(f"/api/photos/{key}/faces").status_code == 200
    assert guest.get(f"/api/photos/{key_of(rw, 'x.jpg')}/faces").status_code == 404     # not the guest's area
    locked = create_app(dataclasses.replace(rw.app.state.settings, readonly=True), background=False)
    reader = browser(locked)
    assert reader.post("/api/login", json={"username": "oliver", "password": PASSWORD}).status_code == 200
    assert reader.put(f"/api/photos/{key}/faces", json=body).status_code == 403
    assert reader.post("/api/faces/name", json={"faces": [1], "name": "Oma"}).status_code == 403


# ------------------------------------------------------------------ finding and grouping

@pytest.fixture
def crowd(tmp_path):
    """Four people on nine photos; the engine is a stand-in that knows them by colour."""
    photos = tmp_path / "fotos"
    for number, (red, green) in enumerate([(10, 10), (10, 100), (10, 200), (100, 10), (100, 100), (200, 10),
                                           (200, 100), (200, 200), (150, 250)]):
        make_jpeg(photos / f"Familie/c/bild{number}.jpg", color=(red, green, 40), taken=f"2022:01:0{number + 1} 10:00:00",
                  orientation=6 if number == 1 else None)
    make_jpeg(photos / "Greta/h.jpg", color=(10, 10, 200), taken="2023:01:01 10:00:00")
    app = create_app(Settings(photos=photos, data=tmp_path / "data", readonly=False), background=False)
    app.state.index.scan()
    app.state.people.engine = FakeEngine()
    app.state.prefs.save(faces=["Familie", "Greta"])
    client = browser(app)
    assert client.post("/api/setup", json={"username": "oliver", "password": PASSWORD}).status_code == 201
    client.photos = photos
    return client


def all_files(root):
    return {str(path.relative_to(root)): path.read_bytes() for path in sorted(root.rglob("*")) if path.is_file()}


def test_search_reads_only_and_goes_on_where_it_stopped(crowd):
    people = crowd.app.state.people
    before = all_files(crowd.photos)
    assert crowd.get("/api/faces").json() == {
        "enabled": True, "areas": [{"name": "Familie", "enabled": True}, {"name": "Greta", "enabled": True}],
        "missing": "", "pictures": 0, "faces": 0, "ignored": 0,
        "search": {"running": False, "done": 0, "total": 0, "last": 0.0}}
    assert len(people.pending()) == 10
    budget = iter([None] * 5 + [["Greta"]] * 20)
    assert people.search(wanted=lambda: next(budget)) == {"looked": 4, "left": 6}         # switched half way
    assert len(people.pending()) == 6
    # a restart: a new object on the same file takes up the rest and looks at nothing twice
    again = People(people.db_path, people.thumbs, people.root, people.index, FakeEngine())
    assert again.search() == {"looked": 6, "left": 0} and again.engine.seen == 6
    assert again.search() == {"looked": 0, "left": 0}
    state = crowd.get("/api/faces").json()
    assert state["pictures"] == 10 and state["faces"] == 10
    assert all_files(crowd.photos) == before                                    # not one byte of a photo touched
    # a picture that is gone takes its faces with it
    (crowd.photos / "Greta/h.jpg").unlink()
    crowd.app.state.index.scan()
    assert people.tidy() == 1 and crowd.get("/api/faces").json()["faces"] == 9


def test_search_survives_a_picture_it_cannot_read(crowd):
    class Choking(FakeEngine):
        def find(self, data):
            if b"kaputt" in data:
                raise ValueError("no picture")
            return super().find(data)

    path = crowd.photos / "Familie/c/bild0.jpg"
    path.write_bytes(path.read_bytes() + b"kaputt")
    crowd.app.state.index.scan()
    people = crowd.app.state.people
    people.engine = Choking()
    assert people.search() == {"looked": 10, "left": 0}
    assert people.search() == {"looked": 0, "left": 0}             # the broken one is not tried for ever
    assert crowd.get("/api/faces").json()["faces"] == 9


@needs_tools
def test_groups_proposals_and_confirming(crowd):
    people = crowd.app.state.people
    people.search()
    before = all_files(crowd.photos)
    found = crowd.get("/api/faces/groups").json()
    assert [g["count"] for g in found["groups"]] == [4, 3, 2, 1] and found["faces"] == 10 and found["total"] == 4
    assert all(g["proposal"] == "" for g in found["groups"])
    whole = crowd.get(f"/api/faces/groups/{found['groups'][0]['id']}?limit=3").json()
    assert whole["count"] == 4 and len(whole["faces"]) == 3 and len(whole["faces"][0]["box"]) == 4 and not whole["known"]
    assert crowd.get("/api/faces/groups?shown=0").json()["groups"][0]["faces"] == []
    assert crowd.get("/api/faces/groups/987654").status_code == 404
    member = whole["faces"][2]["id"]                            # a bundle is also found by one of its faces
    assert member != whole["id"] and crowd.get(f"/api/faces/groups/{member}").json()["id"] == whole["id"]
    assert all_files(crowd.photos) == before                       # bundling proposes, it writes nothing
    three = found["groups"][1]
    thumb = crowd.get(f"/api/faces/{three['faces'][0]['id']}/thumb")
    assert thumb.status_code == 200 and Image.open(io.BytesIO(thumb.content)).size[0] > 8

    # name one face of the biggest bundle by hand, in the single view
    first = found["groups"][0]["faces"][0]
    shown_faces = crowd.get(f"/api/photos/{first['key']}/faces").json()
    assert len(shown_faces) == 1 and shown_faces[0]["face"] == first["id"] and shown_faces[0]["proposal"] == ""
    box = {k: shown_faces[0][k] for k in "xywh"}
    assert crowd.put(f"/api/photos/{first['key']}/faces", json=dict(box, name="Oma")).status_code == 200
    # … now the others that look like her carry her name as a proposal, and still nothing more is written
    found = crowd.get("/api/faces/groups").json()
    assert sorted((g["count"], g["proposal"]) for g in found["groups"]) == [(1, ""), (2, ""), (3, ""), (3, "Oma")]
    hers = next(g for g in found["groups"] if g["proposal"] == "Oma")
    other = hers["faces"][0]
    assert crowd.get(f"/api/photos/{other['key']}/faces").json()[0]["proposal"] == "Oma"
    assert who(crowd) == [{"name": "Oma", "count": 1}]
    named = crowd.get(f"/api/photos/{first['key']}/faces").json()
    assert [(f["name"], f["marked"], f["face"]) for f in named] == [("Oma", True, first["id"])]

    # confirm the proposal for the bundle: one batch, one journal entry per photo
    ids = [f["id"] for f in hers["faces"]]
    results = crowd.post("/api/faces/name", json={"faces": ids + [9999], "name": "Oma", "batch": "abcdef12"}).json()
    assert [r["ok"] for r in results] == [True, True, True, False]
    assert who(crowd) == [{"name": "Oma", "count": 4}]
    entries = crowd.get("/api/journal").json()
    assert [e["batch"] for e in entries[:3]] == ["abcdef12"] * 3 and all(e["action"] == "faces" for e in entries[:4])
    assert [g["count"] for g in crowd.get("/api/faces/groups").json()["groups"]] == [3, 2, 1]
    assert sum(m["count"] for m in crowd.get("/api/timeline", params={"person": "Oma"}).json()) == 4

    # the photo that is shown turned got its region in the stored picture
    turned = crowd.photos / "Familie/c/bild1.jpg"
    area = stored(turned)["RegionList"][0]["Area"]
    assert all(abs(area[k] - v) < 1e-5 for k, v in zip("XYWH", turn_box(BLOCK, INVERSE[6])))

    # throw away what was worked out: the names are still there, the faces are found again
    people.db_path.unlink()
    for extra in ("-wal", "-shm"):
        Path(str(people.db_path) + extra).unlink(missing_ok=True)
    restarted = create_app(crowd.app.state.settings, background=False)
    fresh = restarted.state.people
    fresh.engine = FakeEngine()
    crowd = browser(restarted)
    assert crowd.post("/api/login", json={"username": "oliver", "password": PASSWORD}).status_code == 200
    assert who(crowd) == [{"name": "Oma", "count": 4}]
    assert crowd.get("/api/faces/groups").json()["groups"] == []
    assert fresh.search() == {"looked": 10, "left": 0}
    assert [(g["count"], g["proposal"]) for g in crowd.get("/api/faces/groups").json()["groups"]] == [(3, ""), (2, ""), (1, "")]

    # and taking a confirmation back puts the face among the unnamed again
    undo = crowd.get("/api/journal").json()[0]["id"]
    assert crowd.post(f"/api/journal/{undo}/undo").status_code == 200
    assert who(crowd) == [{"name": "Oma", "count": 3}]
    assert sorted((g["count"], g["proposal"]) for g in crowd.get("/api/faces/groups").json()["groups"]) == [
        (1, ""), (1, "Oma"), (2, ""), (3, "")]


def test_groups_respect_the_areas(crowd):
    crowd.app.state.people.search()
    crowd.post("/api/users", json={"name": "greta", "password": PASSWORD, "areas": {"Greta": "read"}})
    greta = browser(crowd.app)
    assert greta.post("/api/login", json={"username": "greta", "password": PASSWORD}).status_code == 200
    mine = greta.get("/api/faces/groups").json()
    assert mine["faces"] == 1 and len(mine["groups"]) == 1
    foreign = next(g for g in crowd.get("/api/faces/groups").json()["groups"] if g["count"] == 3)["faces"][0]["id"]
    assert greta.get(f"/api/faces/{foreign}/thumb").status_code == 404
    assert greta.get(f"/api/faces/{mine['groups'][0]['faces'][0]['id']}/thumb").status_code == 200
    assert greta.get("/api/people").json() == []


def test_search_is_switched_area_by_area(crowd):
    app, people = crowd.app, crowd.app.state.people
    app.state.prefs.save(faces=[])
    assert crowd.get("/api/faces").json()["enabled"] is False and app.state.face_areas() == []
    state = crowd.put("/api/faces", json={"areas": {"Familie": True, "Nirgends": True}}).json()
    assert state["enabled"] is True and app.state.faces_wake.is_set()
    assert state["areas"] == [{"name": "Familie", "enabled": True}, {"name": "Greta", "enabled": False}]
    assert app.state.prefs.load()["faces"] == ["Familie"]
    # only that area is looked through, and only there found faces are shown
    assert people.search(wanted=app.state.face_areas) == {"looked": 9, "left": 0}
    assert crowd.get("/api/faces/groups").json()["faces"] == 9
    assert crowd.get(f"/api/photos/{key_of(crowd, 'h.jpg')}/faces").json() == []
    # the other one joins …
    crowd.put("/api/faces", json={"areas": {"Greta": True}})
    assert people.search(wanted=app.state.face_areas) == {"looked": 1, "left": 0}
    assert crowd.get("/api/faces/groups").json()["faces"] == 10
    # … and what was found in an area switched off is hidden at once and forgotten by the next tidying
    assert crowd.put("/api/faces", json={"areas": {"Familie": False}}).json()["enabled"] is True
    assert crowd.get("/api/faces/groups").json()["faces"] == 1
    assert people.tidy(app.state.face_areas()) == 9 and crowd.get("/api/faces").json()["faces"] == 1
    assert crowd.put("/api/faces", json={"areas": {"Greta": False}}).json()["enabled"] is False
    assert people.tidy(app.state.face_areas()) == 1 and crowd.get("/api/faces").json()["pictures"] == 0
    # an installation that had the one switch for everything keeps looking everywhere
    app.state.prefs.save(faces=True)
    assert app.state.face_areas() == ["Familie", "Greta"]


def test_faces_can_be_waved_away(crowd):
    people = crowd.app.state.people
    people.search()
    before = all_files(crowd.photos)
    groups = crowd.get("/api/faces/groups").json()["groups"]
    four, single = groups[0], groups[3]
    gone = four["faces"][0]
    # somebody who may only read cannot wave anything away
    crowd.post("/api/users", json={"name": "gast", "password": PASSWORD, "areas": {"Familie": "read"}})
    guest = browser(crowd.app)
    assert guest.post("/api/login", json={"username": "gast", "password": PASSWORD}).status_code == 200
    assert guest.post("/api/faces/ignore", json={"faces": [gone["id"]]}).json() == {"done": 0, "missed": 1}
    assert guest.delete("/api/faces/ignored").status_code == 403
    # one face out of a bundle, and a whole bundle
    ids = [gone["id"]] + [f["id"] for f in single["faces"]]
    assert crowd.post("/api/faces/ignore", json={"faces": ids + [9999]}).json() == {"done": 2, "missed": 1}
    found = crowd.get("/api/faces/groups").json()
    assert [g["count"] for g in found["groups"]] == [3, 3, 2] and found["faces"] == 8
    assert crowd.get("/api/faces").json()["ignored"] == 2
    on_photo = crowd.get(f"/api/photos/{gone['key']}/faces").json()
    assert [(f["ignored"], f["face"]) for f in on_photo] == [(True, gone["id"])]
    assert all_files(crowd.photos) == before                       # no photo was touched for that
    # the decision outlives the store that may be thrown away
    people.db_path.unlink()
    for extra in ("-wal", "-shm"):
        Path(str(people.db_path) + extra).unlink(missing_ok=True)
    restarted = create_app(crowd.app.state.settings, background=False)
    restarted.state.people.engine = FakeEngine()
    again = browser(restarted)
    assert again.post("/api/login", json={"username": "oliver", "password": PASSWORD}).status_code == 200
    assert restarted.state.people.search() == {"looked": 10, "left": 0}
    assert again.get("/api/faces/groups").json()["faces"] == 8
    # back again: one by one, or all of them
    back = again.get(f"/api/photos/{gone['key']}/faces").json()[0]
    assert back["ignored"] is True
    assert again.post("/api/faces/ignore", json={"faces": [back["face"]], "ignored": False}).json()["done"] == 1
    assert again.get("/api/faces/groups").json()["faces"] == 9
    assert again.delete("/api/faces/ignored").json() == {"restored": 1}
    assert again.get("/api/faces/groups").json()["faces"] == 10 and again.get("/api/faces").json()["ignored"] == 0


def wait_for_job(client):
    for _ in range(200):
        job = client.get("/api/job").json()
        if not job["running"]:
            return job
        time.sleep(0.05)
    raise AssertionError("job did not end")


@needs_tools
def test_two_people_become_one(rw):
    one, two, three = (key_of(rw, name) for name in ("block.jpg", "krumm.jpg", "liegt.jpg"))
    spot = {"x": 0.25, "y": 1 / 3, "w": 0.25, "h": 1 / 3}
    other = {"x": 0.75, "y": 0.7, "w": 0.2, "h": 0.3}
    one = rw.put(f"/api/photos/{one}/faces", json=dict(spot, name="Oma")).json()["key"]
    one = rw.put(f"/api/photos/{one}/faces", json=dict(other, name="Tante Erna")).json()["key"]
    two = rw.put(f"/api/photos/{two}/faces", json=dict(spot, name="Oma Inge")).json()["key"]
    three = rw.put(f"/api/photos/{three}/faces", json=dict(spot, name="Oma Inge")).json()["key"]
    lying = rw.photos / "Familie/p/liegt.jpg"
    area = stored(lying)["RegionList"][0]["Area"]
    assert rw.post("/api/people/rename", json={"name": "Niemand", "to": "Oma"}).status_code == 404
    assert rw.post("/api/people/rename", json={"name": "Oma", "to": " Oma "}).status_code == 400
    # merging: the name that exists takes the other one's photos
    assert rw.post("/api/people/rename", json={"name": "Oma Inge", "to": "Oma"}).json() == {"total": 2, "skipped": 0}
    job = wait_for_job(rw)
    assert (job["kind"], job["what"], job["done"], job["failed"]) == ("person", "Oma Inge → Oma", 2, 0)
    assert who(rw) == [{"name": "Oma", "count": 3}, {"name": "Tante Erna", "count": 1}]
    entries = rw.get("/api/journal").json()[:2]
    assert entries[0]["batch"] == entries[1]["batch"] and {e["action"] for e in entries} == {"faces"}
    assert stored(lying)["RegionList"] == [{"Area": area, "Name": "Oma", "Type": "Face"}]      # the box stayed where it was
    # renaming: a new name for everybody of that name; the others on the photo keep theirs
    assert rw.post("/api/people/rename", json={"name": "Oma", "to": "Ingeborg"}).json()["total"] == 3
    assert wait_for_job(rw)["failed"] == 0
    assert who(rw) == [{"name": "Ingeborg", "count": 3}, {"name": "Tante Erna", "count": 1}]
    assert sorted(r["Name"] for r in stored(rw.photos / "Familie/p/block.jpg")["RegionList"]) == ["Ingeborg", "Tante Erna"]
    # … and each photo can be taken back
    undo = rw.get("/api/journal").json()[0]["id"]
    assert rw.post(f"/api/journal/{undo}/undo").status_code == 200
    assert {p["name"]: p["count"] for p in rw.get("/api/people").json()} == {"Ingeborg": 2, "Oma": 1, "Tante Erna": 1}
    # not without the right to write, and not while the photos are read-only
    rw.post("/api/users", json={"name": "gast", "password": PASSWORD, "areas": {"Familie": "read"}})
    guest = browser(rw.app)
    assert guest.post("/api/login", json={"username": "gast", "password": PASSWORD}).status_code == 200
    assert guest.post("/api/people/rename", json={"name": "Ingeborg", "to": "X"}).status_code == 403
    locked = create_app(dataclasses.replace(rw.app.state.settings, readonly=True), background=False)
    reader = browser(locked)
    assert reader.post("/api/login", json={"username": "oliver", "password": PASSWORD}).status_code == 200
    assert reader.post("/api/people/rename", json={"name": "Ingeborg", "to": "X"}).status_code == 403


@needs_tools
def test_face_drawn_by_hand(crowd):
    crowd.app.state.people.search()
    key = key_of(crowd, "bild0.jpg")
    drawn = {"x": 0.8, "y": 0.75, "w": 0.2, "h": 0.3}
    key = crowd.put(f"/api/photos/{key}/faces", json=dict(drawn, name="Opa")).json()["key"]
    faces = crowd.get(f"/api/photos/{key}/faces").json()
    assert [(f["name"], f["marked"], f["face"] is None) for f in faces] == [("Opa", True, True), ("", False, False)]
    assert all(abs(faces[0][k] - drawn[k]) < 1e-5 for k in "xywh")
    assert who(crowd) == [{"name": "Opa", "count": 1}]


# ------------------------------------------------------------------ the real networks

MODEL_DIR = Path(os.environ.get("AUGENBLICK_MODELS") or Path(__file__).parents[1] / "models")
real = pytest.mark.skipif(bool(Engine(MODEL_DIR).missing), reason="needs OpenCV and the face models "
                          "(python -m augenblick.faces fetch models)")
PORTRAIT = Path(__file__).parent / "data" / "astronaut.jpg"


def test_models_are_pinned():
    assert set(MODELS) == {"face_detection_yunet_2023mar.onnx", "face_recognition_sface_2021dec.onnx"}
    assert all(len(digest) == 64 and "opencv_zoo" in url for url, digest in MODELS.values()) and ENGINE


@real
def test_real_face_is_found_whatever_the_orientation(tmp_path):
    import numpy
    engine = Engine(MODEL_DIR)
    upright = engine.find(PORTRAIT.read_bytes())
    assert len(upright) == 1
    face_ = upright[0]
    assert abs(face_["x"] - 0.41) < 0.05 and abs(face_["y"] - 0.5) < 0.08 and 0.2 < face_["w"] < 0.4
    assert face_["score"] > 0.8 and len(face_["vector"]) == 128 * 4
    with Image.open(io.BytesIO(face_["thumb"])) as thumb:
        assert max(thumb.size) <= 160 and abs(thumb.size[0] - thumb.size[1]) <= 2
    vector = numpy.frombuffer(face_["vector"], dtype="float32")
    assert abs(float(numpy.linalg.norm(vector)) - 1) < 1e-4
    portrait = Image.open(PORTRAIT)
    for n in (3, 6, 8):
        # store the picture turned and say so in the tag: it is shown as before
        lying = tmp_path / f"o{n}.jpg"
        exif = Image.Exif()
        exif[0x0112] = n
        portrait.transpose(PIL_OPS[INVERSE[n]]).save(lying, "JPEG", exif=exif.tobytes(), quality=90)
        found = engine.find(lying.read_bytes())
        assert len(found) == 1
        seen = turn_box((found[0]["x"], found[0]["y"], found[0]["w"], found[0]["h"]), n)
        assert all(abs(a - face_[k]) < 0.03 for a, k in zip(seen, "xywh")), n
        same = float(vector @ numpy.frombuffer(found[0]["vector"], dtype="float32"))
        assert same > 0.8, (n, same)


@real
@needs_tools
def test_real_search_to_named_person(tmp_path):
    """The whole way with the real networks: find, bundle, confirm, read back from the file."""
    photos = tmp_path / "fotos"
    (photos / "Familie").mkdir(parents=True)
    portrait = Image.open(PORTRAIT)
    portrait.save(photos / "Familie/a.jpg", "JPEG", quality=90)
    portrait.resize((480, 360)).save(photos / "Familie/b.jpg", "JPEG", quality=80)
    make_jpeg(photos / "Familie/leer.jpg", size=(320, 240))
    app = create_app(Settings(photos=photos, data=tmp_path / "data", readonly=False, models=MODEL_DIR), background=False)
    app.state.index.scan()
    client = browser(app)
    assert client.post("/api/setup", json={"username": "oliver", "password": PASSWORD}).status_code == 201
    app.state.prefs.save(faces=["Familie"])
    assert app.state.people.search() == {"looked": 3, "left": 0}
    found = client.get("/api/faces/groups").json()
    assert [g["count"] for g in found["groups"]] == [2]
    ids = [f["id"] for f in found["groups"][0]["faces"]]
    assert all(r["ok"] for r in client.post("/api/faces/name", json={"faces": ids, "name": "Eileen Collins"}).json())
    assert who(client) == [{"name": "Eileen Collins", "count": 2}]
    area = stored(photos / "Familie/a.jpg")["RegionList"][0]["Area"]
    assert abs(area["X"] - 0.41) < 0.05 and abs(area["Y"] - 0.5) < 0.08
    assert client.get("/api/faces/groups").json()["groups"] == []


def test_twins_of_named_faces(crowd):
    """A face that is a named one over again is offered with that name – and only offered."""
    crowd.app.state.people.search()
    found = crowd.get("/api/faces/groups").json()
    assert crowd.get("/api/faces/twins").json() == {"faces": []}
    first = found["groups"][0]["faces"][0]
    shown = crowd.get(f"/api/photos/{first['key']}/faces").json()[0]
    assert crowd.put(f"/api/photos/{first['key']}/faces", json=dict({k: shown[k] for k in "xywh"}, name="Oma")).status_code == 200
    before = all_files(crowd.photos)
    twins = crowd.get("/api/faces/twins").json()["faces"]
    assert len(twins) == 1 and all(t["name"] == "Oma" and t["like"] == first["key"] and len(t["box"]) == 4 for t in twins)
    assert all_files(crowd.photos) == before


def test_a_face_the_camera_marked_can_be_waved_away_too(crowd):
    """Apple marks faces without naming them; such a face must go when somebody says so."""
    mark(crowd.photos / "Familie/c/bild8.jpg", face((0.25, 1 / 3, 0.25, 1 / 3)))
    crowd.app.state.index.scan()
    crowd.app.state.people.search()
    groups = crowd.get("/api/faces/groups").json()
    lone = next(f for g in groups["groups"] for f in g["faces"]
                if crowd.get(f"/api/photos/{f['key']}").json()["path"].endswith("bild8.jpg"))
    assert crowd.get(f"/api/photos/{lone['key']}/faces").json()[0]["marked"]
    assert crowd.post("/api/faces/ignore", json={"faces": [lone["id"]]}).json()["done"] == 1
    assert crowd.get("/api/faces/groups").json()["faces"] == groups["faces"] - 1


def test_waved_away_faces_are_listed_and_a_person_gets_a_face(crowd):
    crowd.app.state.people.search()
    groups = crowd.get("/api/faces/groups").json()["groups"]
    four = groups[0]["faces"]
    assert crowd.post("/api/faces/ignore", json={"faces": [four[0]["id"], four[1]["id"]]}).json()["done"] == 2
    away = crowd.get("/api/faces/ignored").json()
    assert away["total"] == 2 and {f["id"] for f in away["faces"]} == {four[0]["id"], four[1]["id"]}
    assert crowd.post("/api/faces/ignore", json={"faces": [four[0]["id"]], "ignored": False}).json()["done"] == 1
    assert [f["id"] for f in crowd.get("/api/faces/ignored").json()["faces"]] == [four[1]["id"]]
    # a person is shown with the clearest face, until somebody picks another
    ids = [four[2]["id"], four[3]["id"]]
    assert all(r["ok"] for r in crowd.post("/api/faces/name", json={"faces": ids, "name": "Oma", "batch": "abc123"}).json())
    first = crowd.get("/api/people").json()[0]
    assert first["name"] == "Oma" and first["face"] in ids
    other = next(i for i in ids if i != first["face"])
    assert crowd.put("/api/people/cover", json={"name": "Oma", "face": other}).json() == {"name": "Oma", "face": other}
    assert crowd.get("/api/people").json()[0]["face"] == other
    assert crowd.put("/api/people/cover", json={"name": "Oma", "face": 987654}).status_code == 404


def test_ai_says_what_is_no_face_and_nothing_goes_by_itself(crowd, monkeypatch):
    """The AI is asked only "a face or not?"; what it calls no face is a proposal somebody confirms."""
    import time
    from augenblick.ai import Completion
    crowd.app.state.people.search()
    assert crowd.post("/api/faces/no-face/check", json={}).status_code == 400          # the AI is off
    crowd.app.state.ai.update({"enabled": True})
    asked = []

    def answer(system, prompt, max_tokens=0, images=None, **more):
        asked.append(len(images))
        assert "nie, wer es ist" in system and all(picture[:2] == b"\xff\xd8" for picture in images)
        said = ["true"] * len(images)
        said[0] = "false"
        return Completion(f'Gern: {{"gesichter": [{", ".join(said)}]}}', "test", 1, 1, 0.0, 0.0)

    monkeypatch.setattr(crowd.app.state.ai, "complete", answer)
    before = all_files(crowd.photos)
    assert crowd.get("/api/faces/no-face").json()["waiting"] == 10
    assert crowd.post("/api/faces/no-face/check", json={}).json() == {"started": 10, "questions": 1}
    for _ in range(100):
        if not crowd.get("/api/jobs").json()["job"]["running"]:
            break
        time.sleep(0.05)
    assert asked == [10] and crowd.get("/api/jobs").json()["job"]["failed"] == 0
    found = crowd.get("/api/faces/no-face").json()
    assert found["total"] == 1 and found["waiting"] == 0
    assert crowd.get("/api/faces/groups").json()["faces"] == 10 and all_files(crowd.photos) == before   # nothing went
    assert crowd.post("/api/faces/no-face/keep", json={"faces": [found["faces"][0]["id"]]}).json() == {"done": 1}
    assert crowd.get("/api/faces/no-face").json()["total"] == 0


def test_a_named_region_counts_even_beside_the_cameras_own_mark(crowd):
    """Apple marks a face without a name; a name written later lies beside that mark. The face is named."""
    box = (0.25, 1 / 3, 0.25, 1 / 3)
    mark(crowd.photos / "Familie/c/bild8.jpg", face((0.27, 0.30, 0.25, 0.30)), face(box, "Opa"))
    crowd.app.state.index.scan()
    crowd.app.state.people.search()
    assert who(crowd) == [{"name": "Opa", "count": 1}]
    groups = crowd.get("/api/faces/groups").json()
    keys = {f["key"] for g in groups["groups"] for f in g["faces"]}
    named = next(p for p in crowd.app.state.index.files(["Familie"]) if p["path"].endswith("bild8.jpg"))
    assert named["key"] not in keys and groups["faces"] == 9


def test_nobody_is_proposed_twice_for_one_picture():
    """A face that looks like Florian is not Florian if he is already named elsewhere in that picture."""
    import numpy
    known = numpy.array([[1.0, 0.0, 0.0], [0.96, 0.28, 0.0], [0.0, 1.0, 0.0]], dtype="float32")
    world = {"named": [{"name": "Florian"}, {"name": "Florian"}, {"name": "Jonas"}], "known": known,
             "photos": {"a": {"regions": [{"name": "Florian", "type": "Face"}, {"name": "", "type": "Face"},
                                          {"name": "Bello", "type": "Pet"}]},
                        "b": {"regions": []}}}
    assert People._present(world, "a") == {"Florian"} and People._present(world, "b") == set()
    assert People._present(world, "fehlt") == set()
    looks_like_him = numpy.array([[0.9, 0.44, 0.0]], dtype="float32")
    assert People._propose(world, looks_like_him.copy(), 0.4)[0][0] == "Florian"
    assert People._propose(world, looks_like_him.copy(), 0.4, [frozenset()])[0][0] == "Florian"
    # he is in the picture already: the next best name that is alike enough, or none
    assert People._propose(world, looks_like_him.copy(), 0.4, [frozenset({"Florian"})])[0][0] == "Jonas"
    assert People._propose(world, looks_like_him.copy(), 0.5, [frozenset({"Florian"})])[0] == ("", 0.0)
    two = numpy.vstack([looks_like_him, looks_like_him])
    assert [name for name, _ in People._propose(world, two, 0.4, [frozenset({"Florian", "Jonas"}), frozenset()])] == ["", "Florian"]


def test_a_face_is_not_proposed_as_somebody_named_beside_it(crowd):
    people = crowd.app.state.people
    people.search()
    first = crowd.get("/api/faces/groups").json()["groups"][0]["faces"]
    one, other = first[0], first[1]
    shown_face = crowd.get(f"/api/photos/{one['key']}/faces").json()[0]
    assert crowd.put(f"/api/photos/{one['key']}/faces", json=dict({k: shown_face[k] for k in "xywh"}, name="Oma")).status_code == 200
    crowd.get("/api/faces/groups")                                   # works the names out anew
    assert crowd.get(f"/api/photos/{other['key']}/faces").json()[0]["proposal"] == "Oma"
    # somebody else in that second picture is named Oma by hand, in another corner
    assert crowd.put(f"/api/photos/{other['key']}/faces", json={"x": 0.9, "y": 0.9, "w": 0.1, "h": 0.1, "name": "Oma"}).status_code == 200
    hers = [g for g in crowd.get("/api/faces/groups").json()["groups"] if g["proposal"] == "Oma"]
    loose = [f for f in crowd.get(f"/api/photos/{other['key']}/faces").json() if not f["marked"]]
    assert [f["proposal"] for f in loose] == [""]
    assert all(f["key"] != other["key"] for g in hers for f in g["faces"])


def test_a_name_twice_in_one_picture_is_listed_and_can_be_taken_off(crowd):
    crowd.app.state.people.search()
    assert crowd.get("/api/faces/doubles").json() == {"faces": []}
    first = crowd.get("/api/faces/groups").json()["groups"][0]["faces"][0]
    shown = crowd.get(f"/api/photos/{first['key']}/faces").json()[0]
    assert crowd.put(f"/api/photos/{first['key']}/faces", json=dict({k: shown[k] for k in "xywh"}, name="Oma")).status_code == 200
    assert crowd.get("/api/faces/doubles").json() == {"faces": []}                  # once is fine
    # a second face in the same picture is called Oma too, drawn by hand in another corner
    assert crowd.put(f"/api/photos/{first['key']}/faces", json={"x": 0.9, "y": 0.9, "w": 0.1, "h": 0.1, "name": "Oma"}).status_code == 200
    doubles = crowd.get("/api/faces/doubles").json()["faces"]
    assert [(d["id"], d["name"], d["key"]) for d in doubles] == [(first["id"], "Oma", first["key"])] and len(doubles[0]["box"]) == 4
    before = len(crowd.get("/api/journal").json())
    done = crowd.post("/api/faces/unname", json={"faces": [first["id"]]}).json()
    assert done == [{"face": first["id"], "ok": True}]
    assert crowd.get("/api/faces/doubles").json() == {"faces": []}
    names = [f["name"] for f in crowd.get(f"/api/photos/{first['key']}/faces").json()]
    assert sorted(names) == ["", "Oma"]                                            # the hand-drawn one keeps its name
    assert any(first["id"] in [f["id"] for f in g["faces"]] for g in crowd.get("/api/faces/groups").json()["groups"])
    journal = crowd.get("/api/journal").json()
    assert len(journal) == before + 1 and journal[0]["action"] == "faces"          # … and it can be taken back
    assert crowd.post(f"/api/journal/{journal[0]['id']}/undo").status_code == 200
    assert len(crowd.get("/api/faces/doubles").json()["faces"]) == 1


def test_faces_that_stand_alone_are_bundled_as_guesses_and_a_rest():
    """Thousands of bundles of one face help nobody: they go beside the named person they look a little
    like, and what looks like nobody goes into one last bundle."""
    import numpy

    def unit(*values):
        vector = numpy.zeros(128, dtype="float32")
        vector[:len(values)] = values
        return vector / numpy.linalg.norm(vector)

    people = People.__new__(People)
    vectors = [unit(1, 0, 0), unit(0.45, 0.89, 0, 0), unit(0.42, 0, 0.9), unit(0, 0, 0, 1), unit(0, 0, 0, 0, 1), unit(0, 0.02, 0, 0, 0, 1),
               unit(0, 0, 0, 0, 0, 1, 0.02)]
    names = ["Florian", "", "", "", "", "", ""]
    faces = [{"id": n + 1, "hash": f"h{n}", "key": f"k{n}", "box": (0.5, 0.5, 0.2, 0.2), "name": name, "marked": bool(name),
              "score": 0.9 - n * 0.01, "at": n} for n, name in enumerate(names)]
    photos = {f"h{n}": {"orientation": 1, "regions": [{"name": name, "type": "Face"}] if name else []} for n, name in enumerate(names)}
    photos["h2"]["regions"] = [{"name": "Florian", "type": "Face"}]         # he stands in that picture already
    matrix = numpy.stack(vectors)
    world = {"photos": photos, "faces": faces, "matrix": matrix, "named": [faces[0]], "known": matrix[[0]]}
    people._everything = lambda areas, stale=0: world
    found = people.groups(["Familie"], shown=100)
    kinds = [(g.get("maybe", False), g.get("rest", False), g["proposal"], [f["id"] for f in g["faces"]]) for g in found["groups"]]
    assert (False, False, "", [6, 7]) in kinds                              # two alike: a bundle as before
    assert (True, False, "Florian", [2]) in kinds                           # a little like him: a guess
    assert kinds[-1] == (False, True, "", [3, 4, 5])                        # the rest, last – with the one that may not be him
    assert found["total"] == 3 and found["faces"] == 6
