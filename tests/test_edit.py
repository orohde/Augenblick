"""Writing into photos: every test works on real files with exiftool and jpegtran."""
import dataclasses
import io
import json
import shutil
import subprocess

import pytest
from PIL import Image, ImageChops, ImageStat

from augenblick.app import create_app
from augenblick.edit import COMPOSE, INVERSE, PIL_OPS
from augenblick.media import jpeg_image_hash
from conftest import PASSWORD, browser

pytestmark = pytest.mark.skipif(not (shutil.which("exiftool") and shutil.which("jpegtran")),
                                reason="needs exiftool and jpegtran")


def picture(path, size=(64, 48), orientation=None, thumbnail=False, fmt="JPEG"):
    """A picture without any symmetry, so a wrong turn shows."""
    image = Image.new("RGB", size)
    width, height = size
    image.putdata([(x * 255 // width, y * 255 // height, (x * 3 + y * 5) % 256)
                   for y in range(height) for x in range(width)])
    exif = Image.Exif()
    if orientation:
        exif[0x0112] = orientation
    exif[0x8769] = {0x9003: "2020:01:02 03:04:05", 0xA002: width, 0xA003: height}
    path.parent.mkdir(parents=True, exist_ok=True)
    out = io.BytesIO()
    image.save(out, fmt, exif=exif.tobytes(), **({"quality": 92} if fmt == "JPEG" else {}))
    path.write_bytes(out.getvalue())
    if thumbnail:
        small = path.with_suffix(".small.jpg")
        image.resize((16, 12)).save(small, "JPEG")
        subprocess.run(["exiftool", "-q", "-overwrite_original", f"-ThumbnailImage<={small}", str(path)], check=True)
        small.unlink()
    return path


def tags(path, *names):
    out = subprocess.run(["exiftool", "-j", "-n", "-G1", *(f"-{n}" for n in names), str(path)],
                         capture_output=True, text=True, check=True).stdout
    return json.loads(out)[0]


def shown(path):
    """The picture as a viewer shows it: stored data plus the orientation tag."""
    with Image.open(path) as image:
        image.load()
        n = image.getexif().get(0x0112, 1)
        return image.transpose(PIL_OPS[n]) if n != 1 else image.copy()


def alike(a, b):
    return a.size == b.size and ImageStat.Stat(ImageChops.difference(a.convert("RGB"), b.convert("RGB"))).mean[0] < 3


@pytest.fixture
def rw(settings):
    settings = dataclasses.replace(settings, readonly=False)
    picture(settings.photos / "Familie/dreh/passt.jpg", thumbnail=True)
    picture(settings.photos / "Familie/dreh/krumm.jpg", size=(70, 50))
    picture(settings.photos / "Familie/dreh/liegt.jpg", orientation=6)
    picture(settings.photos / "Familie/dreh/bild.png", fmt="PNG")
    app = create_app(settings, background=False)
    app.state.index.scan()
    client = browser(app)
    assert client.post("/api/setup", json={"username": "oliver", "password": PASSWORD}).status_code == 201
    client.photos = settings.photos
    return client


def key_of(client, name):
    for month in client.get("/api/timeline").json():
        for item in client.get(f"/api/photos?month={month['month']}").json():
            if client.get(f"/api/photos/{item['key']}").json()["name"] == name:
                return item["key"]
    raise AssertionError(name)


def test_compose_table_is_a_group():
    for n in range(1, 9):
        assert COMPOSE[n, 1] == COMPOSE[1, n] == n
        assert COMPOSE[n, INVERSE[n]] == 1
    assert COMPOSE[6, 6] == 3 and COMPOSE[6, 8] == 1 and COMPOSE[3, 6] == 8


def test_readonly_refuses(admin):
    key = admin.get("/api/photos?month=2024-07").json()[0]["key"]
    assert admin.patch(f"/api/photos/{key}", json={"rating": 3}).status_code == 403
    assert admin.get("/api/me").json()["write"] == []


def test_metadata_goes_into_the_file(rw):
    key = key_of(rw, "passt.jpg")
    path = rw.photos / "Familie/dreh/passt.jpg"
    pixels = jpeg_image_hash(path.read_bytes())
    body = rw.patch(f"/api/photos/{key}", json={
        "taken": "1999-12-31T23:59:58", "lat": 49.8451234, "lon": -7.5, "description": "Größe: 3 m\nzweite Zeile",
        "keywords": ["Urlaub", "Öl & Essig", "Urlaub"], "rating": 4}).json()
    assert body["key"] == key and body["taken"] == "1999-12-31T23:59:58" and body["taken_src"] == "exif"
    assert abs(body["lat"] - 49.8451234) < 1e-6 and abs(body["lon"] + 7.5) < 1e-6
    assert body["description"] == "Größe: 3 m\nzweite Zeile"
    assert body["keywords"] == ["Urlaub", "Öl & Essig"] and body["rating"] == 4
    assert jpeg_image_hash(path.read_bytes()) == pixels
    found = tags(path, "ExifIFD:DateTimeOriginal", "ExifIFD:CreateDate", "GPS:GPSLongitudeRef", "XMP-dc:Subject",
                 "XMP-xmp:Rating", "XMP-dc:Description")
    assert found["ExifIFD:DateTimeOriginal"] == found["ExifIFD:CreateDate"] == "1999:12:31 23:59:58"
    assert found["GPS:GPSLongitudeRef"] == "W" and found["XMP-xmp:Rating"] == 4
    assert found["XMP-dc:Subject"] == ["Urlaub", "Öl & Essig"]
    assert not list(path.parent.glob(".augenblick-*"))
    # the index can be thrown away: everything comes back from the file
    assert rw.app.state.index.refresh("Familie/dreh/passt.jpg")["keywords"] == ["Urlaub", "Öl & Essig"]


def test_shift_keywords_and_clear(rw):
    key = key_of(rw, "passt.jpg")
    rw.patch(f"/api/photos/{key}", json={"keywords": ["a", "b"], "lat": 1.5, "lon": 2.5})
    body = rw.patch(f"/api/photos/{key}", json={"shift": 3600 * 25, "add_keywords": ["c"], "remove_keywords": ["a"],
                                                "clear_place": True}).json()
    assert body["taken"] == "2020-01-03T04:04:05" and body["keywords"] == ["b", "c"] and body["lat"] is None
    assert "GPS:GPSLatitude" not in tags(rw.photos / "Familie/dreh/passt.jpg", "GPS:GPSLatitude")
    count = len(rw.get("/api/journal").json())
    rw.patch(f"/api/photos/{key}", json={"keywords": ["b", "c"], "rating": 0})     # nothing new
    assert len(rw.get("/api/journal").json()) == count


def test_shift_needs_a_date(rw):
    path = rw.photos / "Familie/dreh/ohne.jpg"
    Image.new("RGB", (32, 32), (1, 2, 3)).save(path, "JPEG")
    rw.app.state.index.scan()
    key = key_of(rw, "ohne.jpg")
    assert rw.patch(f"/api/photos/{key}", json={"shift": 60}).status_code == 422
    assert rw.patch(f"/api/photos/{key}", json={"taken": "2000-01-01T10:00"}).json()["taken_src"] == "exif"


def test_png_gets_metadata_too(rw):
    key = key_of(rw, "bild.png")
    path = rw.photos / "Familie/dreh/bild.png"
    with Image.open(path) as image:
        before = image.tobytes()
    response = rw.patch(f"/api/photos/{key}", json={"taken": "2001-02-03T04:05:06", "keywords": ["png"], "rating": 2})
    assert response.status_code == 200, response.text
    assert response.json()["taken"] == "2001-02-03T04:05:06" and response.json()["keywords"] == ["png"]
    with Image.open(path) as image:
        assert image.tobytes() == before
    assert rw.post(f"/api/photos/{response.json()['key']}/jpeg").status_code == 200       # now it may be made a JPEG


@pytest.mark.parametrize("degrees", [90, 180, 270])
def test_png_turns_its_pixels_without_loss(rw, degrees):
    path = rw.photos / "Familie/dreh/bild.png"
    key = key_of(rw, "bild.png")
    rw.patch(f"/api/photos/{key}", json={"description": "Wärmebild", "keywords": ["Fenster"]})
    key = key_of(rw, "bild.png")
    with Image.open(path) as image:
        before = image.copy()
    photo = rw.get(f"/api/photos/{key}").json()
    assert photo["can_turn"] and photo["can_jpeg"]
    body = rw.post(f"/api/photos/{key}/rotate", json={"degrees": degrees})
    assert body.status_code == 200, body.text
    body = body.json()
    assert body["orientation"] == 1 and body["description"] == "Wärmebild" and body["keywords"] == ["Fenster"]
    assert (body["width"], body["height"]) == ((48, 64) if degrees != 180 else (64, 48))
    with Image.open(path) as image:
        assert image.tobytes() == before.rotate(-degrees, expand=True).tobytes()     # exact: PNG keeps every pixel
    entry = rw.get("/api/journal").json()[0]
    assert entry["action"] == "rotate" and entry["after"]["lossless"] and entry["after"]["png"]
    assert rw.post(f"/api/journal/{entry['id']}/undo").status_code == 200
    with Image.open(path) as image:
        assert image.tobytes() == before.tobytes() and image.size == before.size
    assert rw.get(f"/api/photos/{key_of(rw, 'bild.png')}").json()["description"] == "Wärmebild"


def test_png_lying_by_its_tag_turns_upright(rw):
    path = picture(rw.photos / "Familie/dreh/liegt.png", fmt="PNG", orientation=6)
    rw.app.state.index.scan()
    before = shown(path)
    body = rw.post(f"/api/photos/{key_of(rw, 'liegt.png')}/rotate", json={"degrees": 90}).json()
    assert body["orientation"] == 1 and alike(shown(path), before.rotate(-90, expand=True))
    assert tags(path, "IFD0:Orientation").get("IFD0:Orientation", 1) == 1


def test_png_becomes_a_jpeg_and_back(rw):
    path = rw.photos / "Familie/dreh/bild.png"
    key = key_of(rw, "bild.png")
    key = rw.patch(f"/api/photos/{key}", json={"description": "Wärmebild", "keywords": ["Fenster"], "rating": 3}).json()["key"]
    with Image.open(path) as image:
        before = image.convert("RGB")
    response = rw.post(f"/api/photos/{key}/jpeg")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["name"] == "bild.jpg" and body["ext"] == "jpg" and not body["can_jpeg"]
    assert (body["description"], body["keywords"], body["rating"], body["taken"]) == \
        ("Wärmebild", ["Fenster"], 3, "2020-01-02T03:04:05")
    assert not path.exists() and alike(Image.open(rw.photos / "Familie/dreh/bild.jpg"), before)
    assert [e["name"] for e in rw.get("/api/trash").json()["entries"]] == ["bild.png"]
    entry = rw.get("/api/journal").json()[0]
    assert entry["action"] == "jpeg" and entry["before"]["path"] == "Familie/dreh/bild.png"
    assert rw.post(f"/api/journal/{entry['id']}/undo").status_code == 200
    assert path.exists() and not (rw.photos / "Familie/dreh/bild.jpg").exists()
    assert rw.get(f"/api/photos/{key_of(rw, 'bild.png')}").json()["description"] == "Wärmebild"
    assert rw.post(f"/api/journal/{entry['id']}/undo").status_code == 422


def test_a_name_already_taken_gets_a_number_and_jpegs_stay(rw):
    picture(rw.photos / "Familie/dreh/passt.png", fmt="PNG")
    rw.app.state.index.scan()
    body = rw.post(f"/api/photos/{key_of(rw, 'passt.png')}/jpeg").json()
    assert body["name"] == "passt-2.jpg"
    assert rw.post(f"/api/photos/{key_of(rw, 'krumm.jpg')}/jpeg").status_code == 422


def test_a_see_through_png_stays_a_png(rw):
    path = rw.photos / "Familie/dreh/logo.png"
    Image.new("RGBA", (20, 10), (255, 0, 0, 0)).save(path)
    rw.app.state.index.scan()
    response = rw.post(f"/api/photos/{key_of(rw, 'logo.png')}/jpeg")
    assert response.status_code == 422 and "transparent" in response.json()["detail"]
    assert path.exists() and not (rw.photos / "Familie/dreh/logo.jpg").exists()
    # an alpha channel that is opaque everywhere is no reason to refuse
    Image.new("RGBA", (20, 10), (255, 0, 0, 255)).save(path)
    rw.app.state.index.scan()
    response = rw.post(f"/api/photos/{key_of(rw, 'logo.png')}/jpeg")
    assert response.status_code == 200, response.text


def test_undo_restores_the_exact_tags(rw):
    key = key_of(rw, "passt.jpg")
    path = rw.photos / "Familie/dreh/passt.jpg"
    names = ("ExifIFD:DateTimeOriginal", "ExifIFD:CreateDate", "XMP-dc:Subject", "GPS:GPSLatitude", "XMP-xmp:Rating")
    before = tags(path, *names)
    rw.patch(f"/api/photos/{key}", json={"taken": "1999-01-01T00:00", "keywords": ["x"], "lat": 5, "lon": 6, "rating": 5})
    entry = rw.get("/api/journal").json()[0]
    assert entry["after"]["taken"] == "1999-01-01T00:00:00" and entry["before"]["taken"] == "2020-01-02T03:04:05"
    assert entry["key"] == key and "tags" not in entry
    undone = rw.post(f"/api/journal/{entry['id']}/undo")
    assert undone.status_code == 200, undone.text
    assert tags(path, *names) == before
    assert undone.json()["taken"] == "2020-01-02T03:04:05" and undone.json()["rating"] == 0
    assert rw.post(f"/api/journal/{entry['id']}/undo").status_code == 422      # only once


def test_undo_refuses_after_a_later_change(rw):
    key = key_of(rw, "passt.jpg")
    rw.patch(f"/api/photos/{key}", json={"rating": 1})
    first = rw.get("/api/journal").json()[0]["id"]
    rw.patch(f"/api/photos/{key}", json={"rating": 2})
    assert rw.post(f"/api/journal/{first}/undo").status_code == 422
    assert rw.get(f"/api/photos/{key}").json()["rating"] == 2


@pytest.mark.parametrize("degrees", [90, 180, 270])
def test_turn_without_loss(rw, degrees):
    path = rw.photos / "Familie/dreh/passt.jpg"
    original = path.read_bytes()
    before = shown(path)
    key = key_of(rw, "passt.jpg")
    body = rw.post(f"/api/photos/{key}/rotate", json={"degrees": degrees}).json()
    assert body["key"] != key and body["orientation"] == 1
    assert (body["width"], body["height"]) == ((48, 64) if degrees != 180 else (64, 48))
    expected = before.rotate(-degrees, expand=True)
    assert alike(shown(path), expected)
    found = tags(path, "IFD0:Orientation", "ExifIFD:ExifImageWidth", "ExifIFD:ExifImageHeight")
    assert found.get("IFD0:Orientation", 1) == 1
    assert (found["ExifIFD:ExifImageWidth"], found["ExifIFD:ExifImageHeight"]) == (body["width"], body["height"])
    thumb = subprocess.run(["exiftool", "-b", "-ThumbnailImage", str(path)], capture_output=True, check=True).stdout
    with Image.open(io.BytesIO(thumb)) as small:
        assert (small.width > small.height) == (body["width"] > body["height"])
        assert alike(small.resize(expected.size), expected) or ImageStat.Stat(ImageChops.difference(
            small.convert("RGB").resize(expected.size), expected.convert("RGB"))).mean[0] < 12
    entry = rw.get("/api/journal").json()[0]
    assert entry["action"] == "rotate" and entry["after"]["lossless"] is True
    # preview follows
    preview = Image.open(io.BytesIO(rw.get(f"/api/photos/{body['key']}/thumb").content))
    assert (preview.width > preview.height) == (body["width"] > body["height"])
    # and back: the picture data is bit for bit what it was
    assert rw.post(f"/api/journal/{entry['id']}/undo").status_code == 200
    plain = lambda data: subprocess.run(["jpegtran", "-copy", "none", "-optimize"], input=data, capture_output=True,
                                        check=True).stdout
    assert plain(path.read_bytes()) == plain(original)
    assert alike(shown(path), before)


def test_turn_by_tag_when_the_size_does_not_fit(rw):
    path = rw.photos / "Familie/dreh/krumm.jpg"
    pixels = jpeg_image_hash(path.read_bytes())
    before = shown(path)
    key = key_of(rw, "krumm.jpg")
    body = rw.post(f"/api/photos/{key}/rotate", json={"degrees": 90}).json()
    assert body["key"] == key and body["orientation"] == 6 and (body["width"], body["height"]) == (50, 70)
    assert jpeg_image_hash(path.read_bytes()) == pixels            # not one bit of the picture touched
    assert alike(shown(path), before.rotate(-90, expand=True))
    entry = rw.get("/api/journal").json()[0]
    assert entry["after"]["lossless"] is False and entry["after"]["why"] == "raster"
    body = rw.post(f"/api/photos/{key}/rotate", json={"degrees": 270}).json()      # back upright
    assert body["orientation"] == 1 and alike(shown(path), before)


def test_turn_takes_an_existing_tag_into_account(rw):
    path = rw.photos / "Familie/dreh/liegt.jpg"      # stored lying, tag says: turn right to show
    before = shown(path)
    key = key_of(rw, "liegt.jpg")
    body = rw.post(f"/api/photos/{key}/rotate", json={"degrees": 90}).json()
    assert body["orientation"] == 1                   # now really turned, tag back to upright
    assert alike(shown(path), before.rotate(-90, expand=True))
    with Image.open(path) as stored:
        assert alike(stored, before.rotate(-90, expand=True))
    undo = rw.get("/api/journal").json()[0]["id"]
    assert rw.post(f"/api/journal/{undo}/undo").status_code == 200
    assert tags(path, "IFD0:Orientation")["IFD0:Orientation"] == 6 and alike(shown(path), before)


def test_appended_pictures_are_not_thrown_away(rw):
    path = rw.photos / "Familie/dreh/passt.jpg"
    path.write_bytes(path.read_bytes() + b"\xff\xd8 ein angehaengtes Vorschaubild \xff\xd9")
    rw.app.state.index.scan()
    key = key_of(rw, "passt.jpg")
    body = rw.post(f"/api/photos/{key}/rotate", json={"degrees": 90}).json()
    assert body["orientation"] == 6
    assert rw.get("/api/journal").json()[0]["after"]["why"] == "trailer"
    assert b"ein angehaengtes Vorschaubild" in path.read_bytes()


def test_undo_keeps_odd_values(rw):
    """An empty description and a time zone written as +1:00 – both seen in real files."""
    path = rw.photos / "Familie/dreh/passt.jpg"
    subprocess.run(["exiftool", "-q", "-overwrite_original", "-XMP-dc:Description^=", "-IPTC:Caption-Abstract^=",
                    "-XMP-xmp:CreateDate=2020:01:02 03:04:05+01:00", "-IPTC:TimeCreated=03:04:05+01:00",
                    "-IPTC:DateCreated=2020:01:02", str(path)], check=True)
    rw.app.state.index.scan()
    names = ("XMP-dc:Description", "IPTC:Caption-Abstract", "XMP-xmp:CreateDate", "IPTC:TimeCreated", "IPTC:DateCreated")
    before = tags(path, *names)
    assert before["XMP-dc:Description"] == "" and before["IPTC:Caption-Abstract"] == ""
    key = key_of(rw, "passt.jpg")
    response = rw.patch(f"/api/photos/{key}", json={"shift": 86400 + 3600, "description": "Neu"})
    assert response.status_code == 200, response.text
    changed = tags(path, *names)
    assert changed["XMP-xmp:CreateDate"] == "2020:01:03 04:04:05+01:00"       # the zone stays
    assert changed["IPTC:TimeCreated"] == "04:04:05+01:00" and changed["IPTC:DateCreated"] == "2020:01:03"
    assert changed["IPTC:Caption-Abstract"] == "Neu"
    entry = rw.get("/api/journal").json()[0]["id"]
    assert rw.post(f"/api/journal/{entry}/undo").status_code == 200
    assert tags(path, *names) == before


def test_many_at_once(rw):
    keys = [key_of(rw, "passt.jpg"), key_of(rw, "krumm.jpg"), "gibtsnicht"]
    results = rw.post("/api/edit", json={"keys": keys, "batch": "abcdef12", "change": {"add_keywords": ["Serie"]}}).json()
    assert [r["ok"] for r in results] == [True, True, False]
    assert all(e["batch"] == "abcdef12" for e in rw.get("/api/journal").json()[:2])
    results = rw.post("/api/rotate", json={"keys": keys[:2], "degrees": 180}).json()
    assert all(r["ok"] for r in results) and results[0]["photo"]["key"] != keys[0]


def test_write_needs_the_right(rw):
    rw.post("/api/users", json={"name": "gast", "password": PASSWORD, "areas": {"Familie": "read"}})
    guest = browser(rw.app)
    assert guest.post("/api/login", json={"username": "gast", "password": PASSWORD}).status_code == 200
    key = key_of(guest, "passt.jpg")
    assert guest.patch(f"/api/photos/{key}", json={"rating": 5}).status_code == 403
    assert guest.post(f"/api/photos/{key}/rotate", json={"degrees": 90}).status_code == 403
    assert guest.get(f"/api/photos/{key}").json()["can_write"] is False
    rw.patch(f"/api/photos/{key}", json={"rating": 5})
    entry = guest.get("/api/journal").json()[0]["id"]
    assert guest.post(f"/api/journal/{entry}/undo").status_code == 403


def test_albums_live_in_the_file(rw):
    key = key_of(rw, "passt.jpg")
    path = rw.photos / "Familie/dreh/passt.jpg"
    subprocess.run(["exiftool", "-q", "-overwrite_original", "-XMP-lr:HierarchicalSubject=Orte|Europa", str(path)], check=True)
    rw.app.state.index.scan()
    body = rw.patch(f"/api/photos/{key}", json={"albums": ["Urlaub / USA 2010", "Boote/Flo", "Boote/Flo", "a|b"]}).json()
    assert body["albums"] == ["Urlaub/USA 2010", "Boote/Flo", "a b"]
    stored = tags(path, "XMP-lr:HierarchicalSubject")["XMP-lr:HierarchicalSubject"]
    assert stored == ["Orte|Europa", "Album|Urlaub|USA 2010", "Album|Boote|Flo", "Album|a b"]     # the foreign tree stays
    body = rw.patch(f"/api/photos/{key}", json={"add_albums": ["Urlaub"], "remove_albums": ["a b"]}).json()
    assert body["albums"] == ["Urlaub/USA 2010", "Boote/Flo", "Urlaub"] and body["keywords"] == []
    tree = {a["path"]: (a["count"], a["total"]) for a in rw.get("/api/albums").json()}
    assert tree == {"Boote": (0, 1), "Boote/Flo": (1, 1), "Urlaub": (1, 1), "Urlaub/USA 2010": (1, 1)}
    lies = {a["path"]: (a["areas"], a["folders"], a["where"]) for a in rw.get("/api/albums").json()}
    assert lies["Boote"] == (["Familie"], 1, [["Familie/dreh", 1]]) == lies["Urlaub/USA 2010"]      # an album says where its pictures lie
    assert sum(m["count"] for m in rw.get("/api/timeline?album=Urlaub").json()) == 1
    assert rw.get("/api/timeline?album=Urlau").json() == [] and rw.get("/api/timeline?album=Boote").json() != []
    # throw the index away: the albums come back from the file
    index = rw.app.state.index
    index.forget("Familie/dreh/passt.jpg")
    index.scan()
    assert {a["path"] for a in rw.get("/api/albums").json()} == {"Boote", "Boote/Flo", "Urlaub", "Urlaub/USA 2010"}
    entry = rw.get("/api/journal").json()[0]
    assert rw.post(f"/api/journal/{entry['id']}/undo").json()["albums"] == ["Urlaub/USA 2010", "Boote/Flo", "a b"]


def test_folder_becomes_album(rw):
    import time
    assert rw.post("/api/albums/from-folder", json={"folder": "Familie"}).status_code == 400
    started = rw.post("/api/albums/from-folder", json={"folder": "Familie/dreh"})
    assert started.status_code == 202 and started.json()["total"] == 4
    for _ in range(200):
        job = rw.get("/api/job").json()
        if not job["running"]:
            break
        time.sleep(0.1)
    assert job["done"] == 4 and job["failed"] == 0 and job["kind"] == "album"
    assert {a["path"]: a["total"] for a in rw.get("/api/albums").json()} == {"dreh": 4}
    assert rw.get(f"/api/photos/{key_of(rw, 'bild.png')}").json()["albums"] == ["dreh"]
    assert len({e["batch"] for e in rw.get("/api/journal").json()}) == 1


def test_a_job_names_the_pictures_it_left_alone_until_they_are_seen(rw, monkeypatch):
    import time
    editor, real = rw.app.state.editor, rw.app.state.editor.change

    def picky(photo, change, *args, **kwargs):
        if photo["path"].endswith("bild.png"):
            raise RuntimeError("check failed: not stored as asked (albums)")
        return real(photo, change, *args, **kwargs)

    monkeypatch.setattr(editor, "change", picky)
    assert rw.post("/api/albums/from-folder", json={"folder": "Familie/dreh"}).status_code == 202
    for _ in range(200):
        job = rw.get("/api/job").json()
        if not job["running"]:
            break
        time.sleep(0.1)
    assert job["done"] == 4 and job["failed"] == 1
    assert job["errors"] == [{"path": "Familie/dreh/bild.png", "key": key_of(rw, "bild.png"),
                              "error": "check failed: not stored as asked (albums)"}]
    assert rw.post("/api/job/seen").json() == {"ok": True}
    job = rw.get("/api/job").json()
    assert job["errors"] == [] and job["failed"] == 1          # the list goes, the count in the summary stays


def test_a_doubled_xmp_entry_does_not_block_writing(rw):
    """Seen in real files: a face tool wrote stDim:unit twice; exiftool then refuses to touch the XMP."""
    path = rw.photos / "Familie/dreh/passt.jpg"
    packet = path.with_suffix(".xmp")
    packet.write_text("""<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
<rdf:Description rdf:about="" xmlns:mwg-rs="http://www.metadataworkinggroup.com/schemas/regions/"
 xmlns:stDim="http://ns.adobe.com/xap/1.0/sType/Dimensions#" xmlns:stArea="http://ns.adobe.com/xmp/sType/Area#">
<mwg-rs:Regions rdf:parseType="Resource">
<mwg-rs:AppliedToDimensions rdf:parseType="Resource"><stDim:w>64</stDim:w><stDim:h>48</stDim:h>
<stDim:unit>pixel</stDim:unit><stDim:unit>pixel</stDim:unit></mwg-rs:AppliedToDimensions>
<mwg-rs:RegionList><rdf:Bag><rdf:li rdf:parseType="Resource"><mwg-rs:Type>Face</mwg-rs:Type>
<mwg-rs:Area rdf:parseType="Resource"><stArea:x>0.5</stArea:x><stArea:y>0.5</stArea:y><stArea:w>0.2</stArea:w>
<stArea:h>0.2</stArea:h><stArea:unit>normalized</stArea:unit></mwg-rs:Area></rdf:li></rdf:Bag></mwg-rs:RegionList>
</mwg-rs:Regions></rdf:Description></rdf:RDF></x:xmpmeta>""")
    subprocess.run(["exiftool", "-q", "-overwrite_original", f"-xmp<={packet}", str(path)], check=True)
    packet.unlink()
    probe = path.with_name("probe.jpg")
    plain = subprocess.run(["exiftool", "-XMP-xmp:Rating=3", "-o", str(probe), str(path)], capture_output=True, text=True)
    assert "Duplicate XMP property" in plain.stderr + plain.stdout          # the fault is really there
    probe.unlink(missing_ok=True)
    rw.app.state.index.scan()
    key = key_of(rw, "passt.jpg")
    pixels = jpeg_image_hash(path.read_bytes())
    answer = rw.patch(f"/api/photos/{key}", json={"albums": ["Boote/Flo"], "rating": 4})
    assert answer.status_code == 200, answer.text
    assert answer.json()["albums"] == ["Boote/Flo"] and answer.json()["rating"] == 4
    assert jpeg_image_hash(path.read_bytes()) == pixels
    kept = tags(path, "XMP-mwg-rs:RegionType", "XMP-mwg-rs:RegionAppliedToDimensionsUnit")
    assert kept["XMP-mwg-rs:RegionType"] == "Face" and kept["XMP-mwg-rs:RegionAppliedToDimensionsUnit"] == "pixel"
    entry = rw.get("/api/journal").json()[0]["id"]
    assert rw.post(f"/api/journal/{entry}/undo").json()["albums"] == []


def test_scan_date_survives_a_new_date(rw):
    """A scanned paper photo: taken some time in 1980, digitised in 2014. Changing the first leaves the second."""
    path = rw.photos / "Familie/dreh/passt.jpg"
    subprocess.run(["exiftool", "-q", "-overwrite_original", "-ExifIFD:DateTimeOriginal=1980:01:01 00:00:00",
                    "-ExifIFD:CreateDate=2014:02:14 12:27:54", "-XMP-xmp:CreateDate=2014:02:14 12:27:54",
                    "-XMP-photoshop:DateCreated=1980:01:01 00:00:00", str(path)], check=True)
    rw.app.state.index.scan()
    key = key_of(rw, "passt.jpg")
    body = rw.patch(f"/api/photos/{key}", json={"taken": "1981-06-07T08:09:10"}).json()
    assert body["taken"] == "1981-06-07T08:09:10"
    found = tags(path, "ExifIFD:DateTimeOriginal", "ExifIFD:CreateDate", "XMP-xmp:CreateDate", "XMP-photoshop:DateCreated")
    assert found["ExifIFD:DateTimeOriginal"] == found["XMP-photoshop:DateCreated"] == "1981:06:07 08:09:10"
    assert found["ExifIFD:CreateDate"] == found["XMP-xmp:CreateDate"] == "2014:02:14 12:27:54"
    # a camera photo keeps both in step
    krumm = rw.photos / "Familie/dreh/krumm.jpg"
    subprocess.run(["exiftool", "-q", "-overwrite_original", "-ExifIFD:CreateDate=2020:01:02 03:04:05", str(krumm)], check=True)
    rw.app.state.index.scan()
    rw.patch(f"/api/photos/{key_of(rw, 'krumm.jpg')}", json={"shift": 60})
    both = tags(krumm, "ExifIFD:DateTimeOriginal", "ExifIFD:CreateDate")
    assert both["ExifIFD:DateTimeOriginal"] == both["ExifIFD:CreateDate"] == "2020:01:02 03:05:05"


def test_empty_structure_field_is_like_a_missing_one():
    from augenblick.edit import _alike
    region = {"Area": {"X": 0.5}, "Type": "Face"}
    assert _alike({"RegionList": [region]}, {"RegionList": [dict(region, Extensions="")]})
    assert not _alike({"RegionList": [region]}, {"RegionList": [dict(region, Name="Oma")]})


def test_who_took_it_lives_in_the_file(rw):
    key = key_of(rw, "passt.jpg")
    path = rw.photos / "Familie/dreh/passt.jpg"
    pixels = jpeg_image_hash(path.read_bytes())
    names = ("IFD0:Artist", "XMP-dc:Creator", "IPTC:By-line")
    assert rw.get(f"/api/photos/{key}").json()["creator"] == "" and rw.get("/api/creators").json() == []
    body = rw.patch(f"/api/photos/{key}", json={"creator": "  Greta   Müller "}).json()
    assert body["creator"] == "Greta Müller" and jpeg_image_hash(path.read_bytes()) == pixels
    assert tags(path, *names) == {"SourceFile": str(path), "IFD0:Artist": "Greta Müller", "XMP-dc:Creator": "Greta Müller"}
    # the index can be thrown away: the name comes back from the file
    assert rw.app.state.index.refresh("Familie/dreh/passt.jpg")["creator"] == "Greta Müller"
    # found by the name, in the list and in the search
    assert rw.get("/api/creators").json() == [{"creator": "Greta Müller", "count": 1}]
    month = "2020-01"
    assert [p["key"] for p in rw.get(f"/api/photos?month={month}&creator=Greta%20M%C3%BCller").json()] == [key]
    assert rw.get(f"/api/photos?month={month}&creator=Jemand").json() == []
    assert [p["key"] for p in rw.get(f"/api/photos?month={month}&q=greta").json()] == [key]
    # many at once: the one that has the name already is left alone
    other = key_of(rw, "krumm.jpg")
    done = rw.post("/api/edit", json={"keys": [key, other], "change": {"creator": "Greta Müller"}})
    assert done.status_code == 200, done.text
    assert rw.get("/api/creators").json() == [{"creator": "Greta Müller", "count": 2}]
    entry = rw.get("/api/journal").json()[0]
    assert entry["before"] == {"creator": ""} and entry["after"] == {"creator": "Greta Müller"}
    # taking the name away, and taking that back
    assert rw.patch(f"/api/photos/{key}", json={"creator": ""}).json()["creator"] == ""
    assert tags(path, *names) == {"SourceFile": str(path)}
    undone = rw.post(f"/api/journal/{rw.get('/api/journal').json()[0]['id']}/undo")
    assert undone.status_code == 200 and undone.json()["creator"] == "Greta Müller"
    assert tags(path, "IFD0:Artist")["IFD0:Artist"] == "Greta Müller"


needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")


@needs_ffmpeg
def test_a_film_takes_date_place_and_words(rw):
    from test_live import FILM
    path = rw.photos / "Familie/film/klein.mov"
    path.parent.mkdir(parents=True)
    shutil.copy(FILM, path)
    rw.app.state.index.scan()
    key = key_of(rw, "klein.mov")
    assert rw.get(f"/api/photos/{key}").json()["can_change"]
    body = rw.patch(f"/api/photos/{key}", json={
        "taken": "2024-07-15T11:00", "lat": 49.861145, "lon": 7.804155, "description": "Im Garten",
        "add_keywords": ["Katze"], "rating": 4, "creator": "Paul", "add_albums": ["Urlaub/USA 2010"]})
    assert body.status_code == 200, body.text
    photo = body.json()
    assert photo["taken"] == "2024-07-15T11:00:00" and (photo["lat"], photo["lon"]) == (49.861145, 7.804155)
    assert photo["description"] == "Im Garten" and photo["keywords"] == ["Katze"] and photo["rating"] == 4
    assert photo["creator"] == "Paul" and photo["albums"] == ["Urlaub/USA 2010"]
    found = tags(path, "Keys:CreationDate", "Keys:GPSCoordinates", "Keys:ContentIdentifier")
    assert found["Keys:CreationDate"] == "2024:07:15 11:00:00+02:00"         # the phone's zone stays
    assert found["Keys:ContentIdentifier"] == "11111111-2222-3333-4444-555555555555"
    # taken back: the film says again what it said before
    entry = rw.get("/api/journal").json()[0]
    undone = rw.post(f"/api/journal/{entry['id']}/undo")
    assert undone.status_code == 200, undone.text
    back = undone.json()
    assert back["taken"] == "2024-07-14T10:20:29" and back["lat"] is None and back["keywords"] == []
    assert back["description"] == "" and back["rating"] == 0 and back["albums"] == []


@needs_ffmpeg
def test_a_film_without_any_date_gets_one(rw):
    path = rw.photos / "Familie/film/kamera.mp4"
    path.parent.mkdir(parents=True)
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=64x48:rate=5", "-t", "1",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)], check=True)
    rw.app.state.index.scan()
    key = key_of(rw, "kamera.mp4")
    body = rw.patch(f"/api/photos/{key}", json={"taken": "2023-01-02T03:04:05", "lat": -33.5, "lon": -70.25})
    assert body.status_code == 200, body.text
    assert body.json()["taken"] == "2023-01-02T03:04:05" and (body.json()["lat"], body.json()["lon"]) == (-33.5, -70.25)
    cleared = rw.patch(f"/api/photos/{body.json()['key']}", json={"clear_place": True})
    assert cleared.status_code == 200, cleared.text
    cleared = cleared.json()
    assert cleared["lat"] is None


def test_old_film_formats_are_left_alone(rw):
    path = rw.photos / "Familie/film/alt.mpg"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"kein echter Film")
    rw.app.state.index.scan()
    key = key_of(rw, "alt.mpg")
    assert not rw.get(f"/api/photos/{key}").json()["can_change"]
    answer = rw.patch(f"/api/photos/{key}", json={"rating": 3})
    assert answer.status_code == 422 and "cannot be changed" in answer.text
