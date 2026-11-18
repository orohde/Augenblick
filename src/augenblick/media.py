"""Reading one photo: identity, metadata, previews. Nothing here ever writes to a photo."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import math
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from xml.etree import ElementTree

from PIL import Image, ImageEnhance, ImageOps

try:  # HEIC from phones; optional so the tests run without it
    from pillow_heif import register_heif_opener
    register_heif_opener()
except ImportError:  # pragma: no cover
    pass

IMAGE_EXT = {"jpg", "jpeg", "png", "heic", "heif", "webp", "tif", "tiff", "gif", "dng"}
RAW_EXT = ("dng",)      # the phone's raw files: shown through the finished picture they carry, never written to
VIDEO_EXT = {"mov", "mp4", "m4v", "avi", "mts", "3gp", "mpg", "mpeg"}
MEDIA_TYPES = {
    "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "heic": "image/heic", "heif": "image/heif",
    "webp": "image/webp", "tif": "image/tiff", "tiff": "image/tiff", "gif": "image/gif", "dng": "image/x-adobe-dng",
    "mov": "video/quicktime", "mp4": "video/mp4", "m4v": "video/mp4", "avi": "video/x-msvideo",
    "mts": "video/mp2t", "3gp": "video/3gpp", "mpg": "video/mpeg", "mpeg": "video/mpeg",
}
THUMB_EDGE = 480
PREVIEW_EDGE = 2048

Image.MAX_IMAGE_PIXELS = 400_000_000


def kind_of(ext: str) -> str | None:
    if ext in IMAGE_EXT:
        return "image"
    if ext in VIDEO_EXT:
        return "video"
    return None


# ------------------------------------------------------------------ identity

def jpeg_image_hash(data: bytes) -> str | None:
    """SHA-256 over the picture itself: everything in a JPEG except the metadata segments
    (APP0–APP15, comments). Writing a date, a place or keywords does not change it."""
    if data[:2] != b"\xff\xd8":
        return None
    digest = hashlib.sha256()
    pos, size = 2, len(data)
    while pos + 4 <= size:
        if data[pos] != 0xFF:
            return None
        marker = data[pos + 1]
        if marker == 0xFF:              # fill byte
            pos += 1
            continue
        if marker == 0xDA:              # start of scan: the rest is picture data
            digest.update(data[pos:])
            return digest.hexdigest()
        length = int.from_bytes(data[pos + 2:pos + 4], "big")
        if length < 2:
            return None
        if not (0xE0 <= marker <= 0xEF or marker == 0xFE):
            digest.update(data[pos:pos + 2 + length])
        pos += 2 + length
    return None


def jpeg_trailer(data: bytes) -> bytes | None:
    """What follows the end of the picture in a JPEG file: appended previews, a phone's short film.
    Empty if there is nothing; None if the file cannot be walked through."""
    if data[:2] != b"\xff\xd8":
        return None
    pos, size = 2, len(data)
    while pos + 2 <= size:
        if data[pos] != 0xFF:
            return None
        marker = data[pos + 1]
        if marker == 0xFF:
            pos += 1
            continue
        if marker == 0xD9:              # end of image
            return data[pos + 2:]
        if 0xD0 <= marker <= 0xD7 or marker == 0x01:
            pos += 2
            continue
        length = int.from_bytes(data[pos + 2:pos + 4], "big")
        if length < 2:
            return None
        pos += 2 + length
        if marker == 0xDA:              # picture data: run on to the next real marker
            while True:
                pos = data.find(b"\xff", pos)
                if pos < 0 or pos + 1 >= size:
                    return None
                follower = data[pos + 1]
                if follower == 0x00 or 0xD0 <= follower <= 0xD7:
                    pos += 2
                elif follower == 0xFF:
                    pos += 1
                else:
                    break
    return None


HEIF_EXT = ("heic", "heif")
# A HEIF file says how to turn its picture in a field of its own, as quarter turns anti-clockwise.
# The phone repeats that in the Exif orientation; readers go by the field, so the two are kept alike.
HEIF_ORIENTATION = {0: 1, 1: 8, 2: 3, 3: 6}
_HEIF_META = (b"Exif", b"mime", b"uri ")       # items that describe the picture instead of being it


def _boxes(data: bytes, start: int, end: int):
    """The boxes between start and end: (type, where it begins, where its content begins, where it ends)."""
    pos = start
    while pos + 8 <= end:
        size, head = int.from_bytes(data[pos:pos + 4], "big"), 8
        if size == 1:
            size, head = int.from_bytes(data[pos + 8:pos + 16], "big"), 16
        elif size == 0:
            size = end - pos
        if size < head or pos + size > end:
            raise ValueError("broken box")
        yield data[pos + 4:pos + 8], pos, pos + head, pos + size
        pos += size


def heif_parts(data: bytes) -> dict | None:
    """What a HEIF file (the phone's HEIC) is made of: its items with their type and where their data
    lies, the properties, which item has which, and the main picture. None if it is not such a file
    or is built in a way this does not know."""
    try:
        if data[4:8] != b"ftyp":
            return None
        top = {kind: (body, end) for kind, _, body, end in _boxes(data, 0, len(data)) if kind == b"meta"}
        if b"meta" not in top:
            return None
        inner = {kind: (body, end) for kind, _, body, end in _boxes(data, top[b"meta"][0] + 4, top[b"meta"][1])}
        if not all(kind in inner for kind in (b"iinf", b"iloc", b"iprp", b"pitm")):
            return None
        num = lambda at, width: int.from_bytes(data[at:at + width], "big")      # noqa: E731

        body, end = inner[b"pitm"]
        primary = num(body + 4, 2 if data[body] == 0 else 4)

        body, end = inner[b"iinf"]
        types: dict[int, bytes] = {}
        for kind, _, at, _ in _boxes(data, body + 4 + (2 if data[body] == 0 else 4), end):
            version = data[at]
            if kind != b"infe" or version < 2:
                return None
            width = 2 if version == 2 else 4
            types[num(at + 4, width)] = data[at + 4 + width + 2:at + 4 + width + 6]

        body, end = inner[b"iloc"]
        version = data[body]
        if version > 2:
            return None
        offset_size, length_size = data[body + 4] >> 4, data[body + 4] & 15
        base_size, index_size = data[body + 5] >> 4, (data[body + 5] & 15) if version else 0
        pos = body + 6
        count = num(pos, 2 if version < 2 else 4)
        pos += 2 if version < 2 else 4
        where: dict[int, tuple[int, list[tuple[int, int]]]] = {}
        for _ in range(count):
            item = num(pos, 2 if version < 2 else 4)
            pos += 2 if version < 2 else 4
            method = 0
            if version:
                method = num(pos, 2) & 15
                pos += 2
            pos += 2                                    # data reference: always this file
            base = num(pos, base_size)
            pos += base_size
            extents = []
            many = num(pos, 2)
            pos += 2
            for _ in range(many):
                pos += index_size
                offset = num(pos, offset_size)
                pos += offset_size
                extents.append((base + offset, num(pos, length_size)))
                pos += length_size
            where[item] = (method, extents)
        if pos > end:
            return None

        body, end = inner[b"iprp"]
        props: list[tuple[bytes, int, int]] = []        # (type, where its content begins, where it ends)
        owns: dict[int, list[int]] = {}
        ipco = None
        for kind, at, inside, stop in _boxes(data, body, end):
            if kind == b"ipco":
                ipco = (at, stop)
                props = [(k, b, e) for k, _, b, e in _boxes(data, inside, stop)]
            elif kind == b"ipma":
                wide, big = data[inside] >= 1, data[inside + 3] & 1
                pos = inside + 8
                for _ in range(num(inside + 4, 4)):
                    item = num(pos, 4 if wide else 2)
                    pos += 4 if wide else 2
                    n = data[pos]
                    pos += 1
                    owns.setdefault(item, [])
                    for _ in range(n):
                        owns[item].append(num(pos, 2) & 0x7FFF if big else data[pos] & 0x7F)
                        pos += 2 if big else 1
        if ipco is None or primary not in types:
            return None
        return {"primary": primary, "types": types, "where": where, "props": props, "owns": owns, "ipco": ipco,
                "idat": inner.get(b"idat")}
    except (ValueError, IndexError):
        return None


def heif_image_hash(data: bytes) -> str | None:
    """SHA-256 over the picture itself in a HEIF file: every item that is picture (not Exif, not XMP)
    and every property – the turn field among them, so a turned picture is another picture, as with
    a JPEG turned without loss. Writing a date, a place or names does not change it."""
    parts = heif_parts(data)
    if not parts:
        return None
    digest = hashlib.sha256(b"heif")
    for item in sorted(parts["types"]):
        kind = parts["types"][item]
        if kind in _HEIF_META:
            continue
        method, extents = parts["where"].get(item, (0, []))
        if method == 1 and parts["idat"]:
            start = parts["idat"][0]
        elif method == 0:
            start = 0
        else:
            return None
        digest.update(item.to_bytes(4, "big") + kind)
        for offset, length in extents:
            if start + offset + length > len(data):
                return None
            digest.update(data[start + offset:start + offset + length])
    digest.update(data[parts["ipco"][0]:parts["ipco"][1]])
    return digest.hexdigest()


def heif_turn(data: bytes, alone: bool = True) -> tuple[int, int] | None:
    """Where the turn of the main picture stands in a HEIF file and what it says: (place of the byte,
    quarter turns anti-clockwise). None if the file has no such field, more than one that differ in
    what they belong to, or mirrors the picture as well – then turning it here is not safe. Those who
    only want to read how the main picture is turned ask with `alone=False`: other pictures in the
    file (the depth map, the tiles) may then turn their own way."""
    parts = heif_parts(data)
    if not parts:
        return None
    kinds = {at: prop for at, prop in enumerate(parts["props"], 1)}
    mine = [at for at in parts["owns"].get(parts["primary"], []) if at in kinds]
    turns = [at for at in mine if kinds[at][0] == b"irot"]
    if len(turns) != 1 or any(kinds[at][0] == b"imir" for at in mine):
        return None
    if alone and sum(1 for prop in parts["props"] if prop[0] in (b"irot", b"imir")) != 1:
        return None                 # other pictures in the file turn their own way
    _, body, end = kinds[turns[0]]
    if end - body != 1:
        return None
    return body, data[body] & 3


def image_hash(data: bytes, ext: str) -> str:
    """What makes a picture this picture: its picture data where the format lets us tell them from
    the metadata, else the whole file."""
    found = None
    if ext in ("jpg", "jpeg"):
        found = jpeg_image_hash(data)
    elif ext in HEIF_EXT:
        found = heif_image_hash(data)
    return found or hashlib.sha256(data).hexdigest()


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ------------------------------------------------------------------ metadata

_NAME_DATES = (
    re.compile(r"(?<!\d)((?:19|20)\d\d)(\d\d)(\d\d)[_\- ]?(\d\d)(\d\d)(\d\d)(?!\d)"),
    re.compile(r"(?<!\d)((?:19|20)\d\d)[-_.](\d\d)[-_.](\d\d)[ _T\-]+(\d\d)[-_.:](\d\d)[-_.:](\d\d)(?!\d)"),
)


def _valid(parts) -> str | None:
    try:
        return datetime(*[int(p) for p in parts]).strftime("%Y-%m-%dT%H:%M:%S")
    except (ValueError, TypeError):
        return None


def date_from_name(name: str) -> str | None:
    for pattern in _NAME_DATES:
        match = pattern.search(name)
        if match:
            found = _valid(match.groups())
            if found:
                return found
    return None


def _exif_date(value) -> str | None:
    match = re.match(r"\s*(\d{4})\D(\d\d)\D(\d\d)\D+(\d\d)\D(\d\d)\D(\d\d)", str(value or ""))
    if not match or match.group(1) == "0000":
        return None
    return _valid(match.groups())


def _text(value) -> str:
    """EXIF text as written by exiftool is UTF-8; older Pillow versions hand it over as Latin-1."""
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    if not isinstance(value, str):
        return ""
    try:
        value = value.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        pass
    return value.replace("\x00", "").strip()


def _degrees(value, ref) -> float | None:
    try:
        d, m, s = (float(x) for x in value)
        result = d + m / 60 + s / 3600
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    if str(ref or "").upper()[:1] in ("S", "W"):
        result = -result
    return round(result, 7) if -180 <= result <= 180 else None


_XMP_LIST = re.compile(rb"<dc:subject>.*?</dc:subject>", re.S)
_XMP_LI = re.compile(rb"<rdf:li[^>]*>(.*?)</rdf:li>", re.S)
_XMP_DESC = re.compile(rb"<dc:description>.*?<rdf:li[^>]*>(.*?)</rdf:li>", re.S)
_XMP_CREATOR = re.compile(rb"<dc:creator>.*?<rdf:li[^>]*>(.*?)</rdf:li>", re.S)
_XMP_TREE = re.compile(rb"<lr:hierarchicalSubject>.*?</lr:hierarchicalSubject>", re.S)
ALBUM_ROOT = "Album"        # albums are hierarchical keywords below this one: Album|Urlaub|USA 2010
_XMP_RATING = re.compile(rb"xmp:Rating(?:=\"|>)(-?\d)")
_XMP_APPLIED = (re.compile(rb"stDim:w(?:=\"|>)(\d+)"), re.compile(rb"stDim:h(?:=\"|>)(\d+)"))
# a picture worked on here says which picture it was made from, and how
SOURCE_MARK = "augenblick:"
_XMP_SOURCE = re.compile(rb"stRef:documentID(?:=\"|>)augenblick:([0-9a-f]{64})")
_XMP_RECIPE = re.compile(rb"stRef:renditionParams(?:=\"|>)([^<\"]*)")


# Marked regions (faces with names) as the Metadata Working Group laid them down. A region says
# where it is as centre, width and height in fractions of the stored picture – before the
# orientation tag is applied.
REGION_NS = "http://www.metadataworkinggroup.com/schemas/regions/"
_AREA_NS = "http://ns.adobe.com/xmp/sType/Area#"
_RDF_NS = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
_XMP_BODY = re.compile(rb"<rdf:RDF\b.*</rdf:RDF>", re.S)


def turn_box(box: tuple[float, float, float, float], n: int) -> tuple[float, float, float, float]:
    """Where a box (centre x, centre y, width, height, all as fractions) lies once the picture
    was put through orientation n."""
    x, y, w, h = box
    if n in (2, 3):
        x = 1 - x
    if n in (3, 4):
        y = 1 - y
    if n >= 5:
        x, y, w, h = {5: (y, x), 6: (1 - y, x), 7: (1 - y, 1 - x), 8: (y, 1 - x)}[n] + (h, w)
    return x, y, w, h


def is_face(kind) -> bool:
    """Programmes that only know faces leave the type out."""
    return str(kind or "face").strip().lower() == "face"


def clean_name(name) -> str:
    return " ".join(str(name or "").replace("\x00", "").split())[:100]


def clean_creator(name) -> str:
    """Who took the picture, as one line."""
    return " ".join(str(name or "").replace("\x00", "").split())[:100]


def people_of(regions: list[dict]) -> list[str]:
    """The names on the faces, each once, in the order they stand in the file."""
    seen: list[str] = []
    for region in regions:
        if region["name"] and is_face(region["type"]) and region["name"] not in seen:
            seen.append(region["name"])
    return seen


def _fields(node) -> dict:
    """The properties of an RDF node, whether written as attributes, as elements or inside a Description."""
    inner = node.find(f"{{{_RDF_NS}}}Description")
    found: dict = {}
    for source in (node, inner) if inner is not None else (node,):
        found.update({key: value for key, value in source.attrib.items() if not key.startswith(f"{{{_RDF_NS}}}")})
        found.update({child.tag: child for child in source if child.tag != f"{{{_RDF_NS}}}Description"})
    return found


def _value(field) -> str:
    return field if isinstance(field, str) else "".join(field.itertext())


def read_regions(packet: bytes) -> list[dict]:
    """The marked regions in an XMP packet: name, type and the box in the stored picture."""
    if REGION_NS.encode() not in packet or b"<!DOCTYPE" in packet or b"<!ENTITY" in packet:
        return []
    body = _XMP_BODY.search(packet)
    try:
        root = ElementTree.fromstring(body.group(0) if body else packet)
    except ElementTree.ParseError:
        return []
    regions: list[dict] = []
    for holder in root.iter(f"{{{REGION_NS}}}RegionList"):
        for item in holder.iter(f"{{{_RDF_NS}}}li"):
            fields = _fields(item)
            area = fields.get(f"{{{REGION_NS}}}Area")
            if area is None or isinstance(area, str):
                continue
            box = {key.rsplit("}", 1)[-1]: _value(value) for key, value in _fields(area).items()
                   if key.startswith(f"{{{_AREA_NS}}}")}
            if box.get("unit", "normalized").strip().lower() != "normalized":
                continue
            try:
                x, y = float(box["x"]), float(box["y"])
                w, h = float(box.get("w") or box.get("d") or 0), float(box.get("h") or box.get("d") or 0)
            except (KeyError, ValueError):
                continue
            if not (0 <= x <= 1 and 0 <= y <= 1 and 0 <= w <= 1 and 0 <= h <= 1):
                continue
            regions.append({"x": x, "y": y, "w": w, "h": h,
                            "name": clean_name(_value(fields.get(f"{{{REGION_NS}}}Name", ""))),
                            "type": _value(fields.get(f"{{{REGION_NS}}}Type", "")).strip()})
    return regions


def _unescape(raw: bytes) -> str:
    text = raw.decode("utf-8", "replace")
    for old, new in (("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'), ("&apos;", "'"), ("&#39;", "'"), ("&amp;", "&")):
        text = text.replace(old, new)
    return text.strip()


def read_xmp(packet: bytes | str | None) -> dict:
    if not packet:
        return {}
    if isinstance(packet, str):
        packet = packet.encode("utf-8", "replace")
    found: dict = {}
    subjects = _XMP_LIST.search(packet)
    if subjects:
        found["keywords"] = [k for k in (_unescape(m) for m in _XMP_LI.findall(subjects.group(0))) if k]
    tree = _XMP_TREE.search(packet)
    if tree:
        paths = [_unescape(m).split("|") for m in _XMP_LI.findall(tree.group(0))]
        found["albums"] = ["/".join(p[1:]) for p in paths if p[0] == ALBUM_ROOT and len(p) > 1 and all(p[1:])]
    description = _XMP_DESC.search(packet)
    if description:
        found["description"] = _unescape(description.group(1))
    creator = _XMP_CREATOR.search(packet)
    if creator:
        found["creator"] = clean_creator(_unescape(creator.group(1)))
    rating = _XMP_RATING.search(packet)
    if rating:
        found["rating"] = int(rating.group(1))
    regions = read_regions(packet)
    if regions:
        found["regions"] = regions
        size = [pattern.search(packet) for pattern in _XMP_APPLIED]
        if all(size):
            found["applied"] = (int(size[0].group(1)), int(size[1].group(1)))
    source = _XMP_SOURCE.search(packet)
    if source:
        found["source"] = source.group(1).decode()
        recipe = _XMP_RECIPE.search(packet)
        try:
            found["recipe"] = clean_recipe(json.loads(_unescape(recipe.group(1)))) if recipe else {}
        except ValueError:
            found["recipe"] = {}
    return found


def read_image(data: bytes) -> tuple[Image.Image, dict]:
    """Open a picture and collect what its metadata says."""
    image = Image.open(io.BytesIO(data))
    meta: dict = {"width": image.width, "height": image.height, "orientation": 1}
    try:
        exif = image.getexif()
    except Exception:  # broken EXIF must not hide the picture
        exif = None
    if exif:
        orientation = exif.get(0x0112)
        if isinstance(orientation, int) and 1 <= orientation <= 8:
            meta["orientation"] = orientation
        meta["make"] = _text(exif.get(0x010F))
        meta["model"] = _text(exif.get(0x0110))
        meta["description"] = _text(exif.get(0x010E))
        meta["creator"] = clean_creator(_text(exif.get(0x013B)))
        try:
            sub = exif.get_ifd(0x8769)
            gps = exif.get_ifd(0x8825)
        except Exception:
            sub, gps = {}, {}
        meta["taken"] = _exif_date(sub.get(0x9003)) or _exif_date(sub.get(0x9004))
        meta["live"] = live_id(sub.get(0x927C))
        if gps:
            lat, lon = _degrees(gps.get(2), gps.get(1)), _degrees(gps.get(4), gps.get(3))
            if lat is not None and lon is not None and (lat, lon) != (0.0, 0.0) and abs(lat) <= 90:
                meta["lat"], meta["lon"] = lat, lon
    if image.format in ("HEIF", "AVIF", "TIFF"):    # the decoder has turned it already (TIFF: also the raw files
        meta["orientation"] = 1                     # built on it); what the Exif says is only a copy
    xmp = read_xmp(image.info.get("xmp") or image.info.get("XML:com.adobe.xmp"))
    if xmp.get("description") and not meta.get("description"):
        meta["description"] = xmp["description"]
    if xmp.get("creator") and not meta.get("creator"):
        meta["creator"] = xmp["creator"]
    meta["keywords"] = xmp.get("keywords", [])
    meta["albums"] = xmp.get("albums", [])
    meta["rating"] = xmp.get("rating", 0)
    meta["regions"] = xmp.get("regions", [])
    wide, high = xmp.get("applied") or (0, 0)
    if wide and high and image.width and image.height:
        # The regions say for which picture they were marked. A picture of another shape was cut from
        # that one by a programme that copied the marks along without moving them: they point nowhere.
        # A name among them is still kept – it is not for us to drop who somebody said is in the picture.
        shape, was = image.width / image.height, wide / high
        if min(abs(shape / was - 1), abs(shape * was - 1)) > 0.03:
            meta["regions"] = [region for region in meta["regions"] if region["name"]]
    if meta["regions"] and image.format in ("HEIF", "AVIF"):
        # Marked regions lie in the picture as it is stored – in a HEIF file that is the picture before
        # its turn field is applied (the phone writes the faces it saw that way). What the decoder hands
        # out, and all that is shown and found here, is the turned picture: so the regions are turned too.
        turn = heif_turn(data, alone=False)
        if turn and turn[1]:
            meta["regions"] = [dict(region, **dict(zip("xywh", turn_box(
                (region["x"], region["y"], region["w"], region["h"]), HEIF_ORIENTATION[turn[1]]))))
                for region in meta["regions"]]
    meta["source"] = xmp.get("source", "")
    meta["recipe"] = xmp.get("recipe", {})
    meta["people"] = people_of(meta["regions"])
    if meta["orientation"] >= 5:   # turned by 90°: what you see is the other way round
        meta["width"], meta["height"] = meta["height"], meta["width"]
    return image, meta


# ------------------------------------------------------------------ films

_LIVE_ID = re.compile(r"[0-9A-Za-z-]{8,64}")


def live_id(note) -> str:
    """What an iPhone writes into a Live Photo and into its short film alike, so the two find each
    other: the content identifier in Apple's maker note. '' if the picture carries none."""
    if not isinstance(note, bytes) or note[:10] != b"Apple iOS\x00" or note[12:14] not in (b"MM", b"II"):
        return ""
    order = "big" if note[12:14] == b"MM" else "little"
    for at in range(16, min(16 + 12 * int.from_bytes(note[14:16], order), len(note) - 11), 12):
        if int.from_bytes(note[at:at + 2], order) != 0x11 or int.from_bytes(note[at + 2:at + 4], order) != 2:
            continue
        length = int.from_bytes(note[at + 4:at + 8], order)
        start = at + 8 if length <= 4 else int.from_bytes(note[at + 8:at + 12], order)
        text = note[start:start + length].split(b"\x00")[0].decode("ascii", "ignore").strip()
        return text.upper() if _LIVE_ID.fullmatch(text) else ""
    return ""


_VIDEO_TAGS = ("Keys:ContentIdentifier", "Keys:CreationDate", "QuickTime:CreateDate", "DateTimeOriginal",
               "ImageWidth", "ImageHeight", "Composite:Rotation", "QuickTime:Duration", "Duration",
               "Composite:GPSLatitude", "Composite:GPSLongitude", "Make", "Model")


def read_video(path: Path) -> dict:
    """What a film says about itself, read by exiftool: when and where it was taken, how large and how
    long it is, and whether it belongs to a Live Photo. Empty if exiftool is missing or cannot read it."""
    tool = shutil.which("exiftool")
    if not tool:
        return {}
    try:
        done = subprocess.run([tool, "-j", "-n", "-b", "-api", "QuickTimeUTC=1", *(f"-{tag}" for tag in _VIDEO_TAGS),
                               "-XMP", str(path)], capture_output=True, text=True, timeout=120)
        tags = json.loads(done.stdout)[0]
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return {}
    if tags.get("Error") or not any(tag in tags for tag in ("ImageWidth", "Duration", "CreateDate", "CreationDate")):
        return {}                   # not a film exiftool can make sense of
    number = lambda name: tags[name] if isinstance(tags.get(name), (int, float)) else None      # noqa: E731
    meta: dict = {"make": _text(str(tags.get("Make") or "")), "model": _text(str(tags.get("Model") or ""))}
    # the phone notes the time with its zone: the time on the wall where the film was taken
    meta["taken"] = (_exif_date(tags.get("CreationDate")) or _exif_date(tags.get("DateTimeOriginal"))
                     or _exif_date(tags.get("CreateDate")))
    width, height = number("ImageWidth"), number("ImageHeight")
    if width and height:
        if number("Rotation") in (90, 270):
            width, height = height, width
        meta["width"], meta["height"] = int(width), int(height)
    seconds = number("Duration")
    if seconds and 0 < seconds < 360000:
        meta["seconds"] = round(float(seconds), 2)
    lat, lon = number("GPSLatitude"), number("GPSLongitude")
    if lat is not None and lon is not None and (lat, lon) != (0, 0) and abs(lat) <= 90 and abs(lon) <= 180:
        meta["lat"], meta["lon"] = round(float(lat), 7), round(float(lon), 7)
    live = str(tags.get("ContentIdentifier") or "").strip()
    if _LIVE_ID.fullmatch(live):
        meta["live"] = live.upper()
    # words, albums, rating and who filmed it: in XMP, as with pictures
    packet = tags.get("XMP")
    if isinstance(packet, str) and packet.startswith("base64:"):
        try:
            packet = base64.b64decode(packet[7:])
        except ValueError:
            packet = None
    xmp = read_xmp(packet if isinstance(packet, (str, bytes)) else None)
    for key in ("description", "creator", "keywords", "albums", "rating"):
        if key in xmp:
            meta[key] = xmp[key]
    return meta


def video_frame(path: Path, seconds: float = 0.0) -> Image.Image | None:
    """One picture out of a film, the right way up – None without ffmpeg or if it cannot read the film."""
    tool = shutil.which("ffmpeg")
    if not tool:
        return None
    for at in ([min(1.0, seconds / 2)] if seconds else []) + [0.0]:     # not the very first frame: often black
        try:
            done = subprocess.run([tool, "-v", "error", "-nostdin", "-ss", f"{at:.2f}", "-i", str(path),
                                   "-frames:v", "1", "-vf", f"scale={THUMB_EDGE * 2}:{THUMB_EDGE * 2}"
                                   ":force_original_aspect_ratio=decrease", "-f", "image2pipe", "-c:v", "mjpeg",
                                   "-q:v", "3", "-"], capture_output=True, timeout=120)
            if done.returncode == 0 and done.stdout:
                image = Image.open(io.BytesIO(done.stdout))
                image.load()
                return image
        except (OSError, subprocess.SubprocessError):
            return None
        except Exception:       # not a picture after all
            continue
    return None


# ------------------------------------------------------------------ working on a picture

LIGHT = ("brightness", "contrast", "saturation", "warmth")     # each from -100 to 100, 0 = as it is
MAX_ANGLE = 30.0


def clean_recipe(given) -> dict:
    """What is to be done to a picture, tidied: only what really changes something is kept.
    angle: degrees clockwise to set it straight; crop: [left, top, width, height] as fractions of the
    straightened picture; auto: even out light and dark; the rest as in LIGHT."""
    if not isinstance(given, dict):
        raise ValueError("invalid recipe")
    recipe: dict = {}
    try:
        angle = round(float(given.get("angle") or 0), 2)
        if not -MAX_ANGLE <= angle <= MAX_ANGLE:
            raise ValueError("invalid angle")
        if angle:
            recipe["angle"] = angle
        crop = given.get("crop")
        if crop is not None:
            left, top, width, height = (round(float(value), 5) for value in crop)
            if not (0 <= left < 1 and 0 <= top < 1 and 0.02 <= width <= 1 and 0.02 <= height <= 1
                    and left + width <= 1.00001 and top + height <= 1.00001):
                raise ValueError("invalid crop")
            if (left, top, width, height) != (0, 0, 1, 1):
                recipe["crop"] = [left, top, min(width, 1 - left), min(height, 1 - top)]
        for name in LIGHT:
            value = round(float(given.get(name) or 0))
            if not -100 <= value <= 100:
                raise ValueError(f"invalid {name}")
            if value:
                recipe[name] = value
    except (TypeError, ValueError) as exc:
        raise ValueError(str(exc) if str(exc).startswith("invalid") else "invalid recipe") from exc
    if given.get("auto"):
        recipe["auto"] = True
    return recipe


class Frame:
    """Where a point of the upright picture lies in the picture made from it, and back. Points are
    fractions (0–1) of the one or the other; the size of the upright picture is in pixels."""

    def __init__(self, recipe: dict, width: int, height: int):
        self.w, self.h = width, height
        angle = math.radians(recipe.get("angle", 0))
        self.cos, self.sin = math.cos(angle), math.sin(angle)
        c, s = abs(self.cos), abs(self.sin)
        # set straight, the corners are lost: what stays is the largest upright piece of the same shape
        self.keep = min(width / (width * c + height * s), height / (width * s + height * c))
        self.crop = recipe.get("crop") or [0, 0, 1, 1]

    @property
    def size(self) -> tuple[int, int]:
        return (max(1, round(self.w * self.keep * self.crop[2])), max(1, round(self.h * self.keep * self.crop[3])))

    @property
    def scale(self) -> tuple[float, float]:
        """How much larger a thing of the upright picture is in the new one, as a share of its width and height."""
        return 1 / (self.keep * self.crop[2]), 1 / (self.keep * self.crop[3])

    def forward(self, x: float, y: float) -> tuple[float, float]:
        px, py = (x - 0.5) * self.w, (y - 0.5) * self.h
        qx, qy = px * self.cos - py * self.sin, px * self.sin + py * self.cos
        fx, fy = qx / (self.w * self.keep) + 0.5, qy / (self.h * self.keep) + 0.5
        return (fx - self.crop[0]) / self.crop[2], (fy - self.crop[1]) / self.crop[3]

    def back(self, x: float, y: float) -> tuple[float, float]:
        fx, fy = x * self.crop[2] + self.crop[0], y * self.crop[3] + self.crop[1]
        qx, qy = (fx - 0.5) * self.w * self.keep, (fy - 0.5) * self.h * self.keep
        px, py = qx * self.cos + qy * self.sin, -qx * self.sin + qy * self.cos
        return px / self.w + 0.5, py / self.h + 0.5


def _curve(image: Image.Image, red, green, blue) -> Image.Image:
    table = [min(255, max(0, round(band(value / 255) * 255))) for band in (red, green, blue) for value in range(256)]
    return image.point(table)


def develop(image: Image.Image, recipe: dict, crop: bool = True) -> Image.Image:
    """The picture worked on as the recipe says: set straight, cut, light and colours. Hands back a
    new upright RGB picture; the one given is left as it is. Without `crop` the cut is left out,
    for showing where it will go."""
    image = ImageOps.exif_transpose(image)
    if image.mode != "RGB":
        image = image.convert("RGB")
    frame = Frame(recipe if crop else {k: v for k, v in recipe.items() if k != "crop"}, *image.size)
    if recipe.get("angle"):
        image = image.rotate(-recipe["angle"], resample=Image.BICUBIC)
    if frame.keep < 1 or frame.crop != [0, 0, 1, 1]:
        wide, high = image.width * frame.keep, image.height * frame.keep
        left = (image.width - wide) / 2 + frame.crop[0] * wide
        top = (image.height - high) / 2 + frame.crop[1] * high
        box = (round(left), round(top), round(left + frame.crop[2] * wide), round(top + frame.crop[3] * high))
        image = image.crop((box[0], box[1], max(box[2], box[0] + 1), max(box[3], box[1] + 1)))
    if recipe.get("auto"):
        image = ImageOps.autocontrast(image, cutoff=(0.4, 0.4), preserve_tone=True)
    warmth, light = recipe.get("warmth", 0) / 100, recipe.get("brightness", 0) / 100
    if warmth or light:
        bend = 2 ** -light                  # a curve, not a shift: black stays black, white stays white
        image = _curve(image, lambda v: (v ** bend) * (1 + 0.18 * warmth), lambda v: v ** bend,
                       lambda v: (v ** bend) * (1 - 0.18 * warmth))
    if recipe.get("contrast"):
        image = ImageEnhance.Contrast(image).enhance(1 + recipe["contrast"] / 100 * 0.6)
    if recipe.get("saturation"):
        image = ImageEnhance.Color(image).enhance(1 + recipe["saturation"] / 100)
    return image


# ------------------------------------------------------------------ previews

def render(image: Image.Image, edge: int) -> bytes:
    """A small JPEG the right way up."""
    if image.format == "JPEG":
        image.draft("RGB", (edge, edge))
    # phones take pictures in a wider colour space (Display P3); without its profile the small picture
    # would be shown as if its numbers meant sRGB, with duller colours
    profile = image.info.get("icc_profile") if image.mode == "RGB" else None
    image = ImageOps.exif_transpose(image)
    image.thumbnail((edge, edge), Image.LANCZOS)
    if image.mode != "RGB":
        if image.mode in ("RGBA", "LA", "P"):
            rgba = image.convert("RGBA")
            background = Image.new("RGB", rgba.size, (255, 255, 255))
            background.paste(rgba, mask=rgba.split()[-1])
            image = background
        else:
            image = image.convert("RGB")
    out = io.BytesIO()
    image.save(out, "JPEG", quality=82, optimize=True, **({"icc_profile": profile} if profile else {}))
    return out.getvalue()


def cache_path(root: Path, digest: str, orientation: int) -> Path:
    return root / digest[:2] / f"{digest}_{orientation}.jpg"


def store(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_bytes(data)
    temp.replace(path)


# ------------------------------------------------------------------ one file

def examine(root: Path, rel: str, thumbs: Path) -> dict:
    """Everything the index wants to know about one file. Makes the small preview on the way."""
    path = root / rel
    stat = path.stat()
    name = path.name
    ext = path.suffix.lower().lstrip(".")
    kind = kind_of(ext)
    record: dict = {
        "path": rel, "name": name, "ext": ext, "kind": kind, "size": stat.st_size, "mtime": stat.st_mtime,
        "width": 0, "height": 0, "orientation": 1, "lat": None, "lon": None, "make": "", "model": "",
        "description": "", "keywords": [], "albums": [], "rating": 0, "taken": None, "error": "",
        "regions": [], "people": [], "live": "", "seconds": 0, "source": "", "recipe": {}, "creator": "",
    }
    if kind == "image":
        data = path.read_bytes()
        record["hash"] = image_hash(data, ext)
        try:
            image, meta = read_image(data)
            record.update({k: v for k, v in meta.items() if v is not None})
            target = cache_path(thumbs, record["hash"], record["orientation"])
            if not target.exists():
                store(target, render(image, THUMB_EDGE))
        except Exception as exc:  # unreadable picture: keep it in the index, say why
            record["error"] = f"{type(exc).__name__}: {exc}"[:300]
    else:
        record["hash"] = file_hash(path)
        record.update({k: v for k, v in read_video(path).items() if v is not None})
        target = cache_path(thumbs, record["hash"], 1)
        if not target.exists():
            frame = video_frame(path, record["seconds"])
            if frame is not None:
                store(target, render(frame, THUMB_EDGE))
    if record.get("taken"):
        record["taken_src"] = "exif"
    else:
        named = date_from_name(name)
        record["taken"] = named or datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%dT%H:%M:%S")
        record["taken_src"] = "name" if named else "file"
    return record
