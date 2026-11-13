import os

from augenblick.index import Index
from augenblick.media import date_from_name, jpeg_image_hash, read_xmp
from conftest import make_jpeg


def with_comment(data: bytes, text: bytes) -> bytes:
    """Insert a metadata segment right after the start marker, as a metadata writer would."""
    segment = b"\xff\xe1" + (len(text) + 2).to_bytes(2, "big") + text
    return data[:2] + segment + data[2:]


def test_picture_hash_ignores_metadata(tmp_path):
    data = make_jpeg(tmp_path / "a.jpg", taken="2020:01:01 10:00:00").read_bytes()
    other = make_jpeg(tmp_path / "b.jpg", taken="1999:12:31 23:59:59", model="Andere Kamera").read_bytes()
    assert data != other
    assert jpeg_image_hash(data) == jpeg_image_hash(other) == jpeg_image_hash(with_comment(data, b"Exif\0\0neu"))


def test_picture_hash_sees_the_picture(tmp_path):
    red = make_jpeg(tmp_path / "a.jpg", color=(255, 0, 0)).read_bytes()
    blue = make_jpeg(tmp_path / "b.jpg", color=(0, 0, 255)).read_bytes()
    assert jpeg_image_hash(red) != jpeg_image_hash(blue)
    assert jpeg_image_hash(b"kein jpeg") is None


def test_date_from_name():
    assert date_from_name("20240714_102030_ab12cd.jpg") == "2024-07-14T10:20:30"
    assert date_from_name("2015-01-23_13-03-24.jpg") == "2015-01-23T13:03:24"
    assert date_from_name("P1030105.jpg") is None
    assert date_from_name("20241341_102030.jpg") is None


def test_xmp():
    packet = (b'<x:xmpmeta><rdf:Description xmp:Rating="4"><dc:subject><rdf:Bag><rdf:li>Oma</rdf:li>'
              b'<rdf:li>Garten &amp; Haus</rdf:li></rdf:Bag></dc:subject><dc:description><rdf:Alt>'
              b'<rdf:li xml:lang="x-default">Im Garten</rdf:li></rdf:Alt></dc:description></rdf:Description></x:xmpmeta>')
    assert read_xmp(packet) == {"keywords": ["Oma", "Garten & Haus"], "description": "Im Garten", "rating": 4}


def test_scan_follows_the_folders(settings):
    index = Index(settings.db_path, settings.photos, settings.thumbs)
    assert index.scan() == {"added": 5, "removed": 0}
    assert index.scan() == {"added": 0, "removed": 0}
    assert [a["name"] for a in index.areas()] == ["Familie", "Greta"]

    old = index.photo(index.month(["Familie"], "1981-05")[0]["key"], ["Familie"])
    assert old["taken"] == "1981-05-11T12:00:00" and old["taken_src"] == "name"

    make_jpeg(settings.photos / "Greta/2023/neu.jpg", color=(1, 2, 3), taken="2023:12:25 09:00:00")
    (settings.photos / "Familie/2024/IMG_0001.jpg").unlink()
    assert index.scan() == {"added": 1, "removed": 1}
    assert index.count() == 5


def test_changed_file_is_read_again(settings):
    index = Index(settings.db_path, settings.photos, settings.thumbs)
    index.scan()
    path = settings.photos / "Greta/2023/bild.jpg"
    make_jpeg(path, color=(0, 200, 0), taken="2001:02:03 04:05:06")
    os.utime(path, (path.stat().st_atime, path.stat().st_mtime + 5))
    assert index.scan()["added"] == 1
    assert [m["month"] for m in index.months(["Greta"])] == ["2001-02"]


def test_same_picture_twice_gets_two_keys(settings):
    source = settings.photos / "Greta/2023/bild.jpg"
    (settings.photos / "Greta/2023/kopie.jpg").write_bytes(source.read_bytes())
    index = Index(settings.db_path, settings.photos, settings.thumbs)
    index.scan()
    keys = [p["key"] for p in index.month(["Greta"], "2023-12")]
    assert len(keys) == 2 and len(set(keys)) == 2 and keys[0][:16] == keys[1][:16]


def test_index_is_rebuilt_from_the_photos(settings):
    index = Index(settings.db_path, settings.photos, settings.thumbs)
    index.scan()
    settings.db_path.unlink()
    for extra in ("-wal", "-shm"):
        settings.db_path.with_name(settings.db_path.name + extra).unlink(missing_ok=True)
    again = Index(settings.db_path, settings.photos, settings.thumbs)
    assert again.count() == 0
    assert again.scan()["added"] == 5
