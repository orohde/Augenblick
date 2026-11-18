"""Writing to photos. Metadata goes in with exiftool, JPEGs are turned with jpegtran.

The rule for every change: write a new file next to the photo, prove that it is what was
asked for and that the picture itself is untouched, and only then put it in the photo's
place. If any check fails, the photo stays exactly as it was.
"""

from __future__ import annotations

import copy
import io
import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import threading
from datetime import datetime, timedelta
from pathlib import Path

from PIL import Image, ImageChops, ImageOps, ImageStat

from .index import Index
from .journal import Journal
from .media import (ALBUM_ROOT, HEIF_EXT, HEIF_ORIENTATION, clean_creator, clean_name, file_hash, heif_image_hash, heif_turn, is_face, jpeg_image_hash,
                    RAW_EXT, SOURCE_MARK, Frame, clean_recipe, develop, jpeg_trailer, kind_of, people_of, read_image, read_video,
                    turn_box)

log = logging.getLogger("augenblick.edit")

JPEG = ("jpg", "jpeg")

# The tags a change touches. The first group is always written; the second only where the
# file already carries that tag, so that no programme reads an outdated copy of the value.
DATE_TAGS = ("ExifIFD:DateTimeOriginal", "ExifIFD:CreateDate")
DATE_EXTRA = ("XMP-photoshop:DateCreated", "XMP-xmp:CreateDate", "XMP-exif:DateTimeOriginal",
              "IPTC:DateCreated", "IPTC:TimeCreated")
PLACE_TAGS = ("GPS:GPSLatitude", "GPS:GPSLatitudeRef", "GPS:GPSLongitude", "GPS:GPSLongitudeRef")
PLACE_EXTRA = ("XMP-exif:GPSLatitude", "XMP-exif:GPSLongitude")
TEXT_TAGS = ("IFD0:ImageDescription", "XMP-dc:Description")
TEXT_EXTRA = ("IPTC:Caption-Abstract",)
WORD_TAGS = ("XMP-dc:Subject",)
WORD_EXTRA = ("IPTC:Keywords",)
RATING_TAGS = ("XMP-xmp:Rating",)
CREATOR_TAGS = ("IFD0:Artist", "XMP-dc:Creator")      # who took the picture
CREATOR_EXTRA = ("IPTC:By-line",)
TREE_TAG = "XMP-lr:HierarchicalSubject"     # albums live here, as Album|Urlaub|USA 2010
META_TAGS = (DATE_TAGS + DATE_EXTRA + PLACE_TAGS + PLACE_EXTRA + TEXT_TAGS + TEXT_EXTRA + WORD_TAGS + WORD_EXTRA
             + RATING_TAGS + CREATOR_TAGS + CREATOR_EXTRA + (TREE_TAG,))
LIST_TAGS = WORD_TAGS + WORD_EXTRA + (TREE_TAG,)
REGION_TAG = "XMP-mwg-rs:RegionInfo"         # marked faces with their names, relative to the stored picture
TURN_TAGS = ("IFD0:Orientation", "IFD1:Orientation", "XMP-tiff:Orientation", "ExifIFD:ExifImageWidth",
             "ExifIFD:ExifImageHeight", "IFD1:ThumbnailLength", "PreviewImageLength", REGION_TAG)
# Films keep their data in QuickTime boxes: the date and place where phones put them, the words in XMP
# as with pictures. Only the QuickTime family (MOV, MP4) can be written; older formats stay as they are.
FILM_EXT = ("mov", "mp4", "m4v", "3gp")
FILM_DATE_TAGS = ("Keys:CreationDate", "QuickTime:CreateDate")
FILM_DATE_EXTRA = ("XMP-xmp:CreateDate", "XMP-exif:DateTimeOriginal", "XMP-photoshop:DateCreated")
FILM_PLACE_TAGS = ("Keys:GPSCoordinates",)
FILM_PLACE_EXTRA = ("UserData:GPSCoordinates", "ItemList:GPSCoordinates", "XMP-exif:GPSLatitude",
                    "XMP-exif:GPSLongitude")
FILM_TEXT_TAGS = ("XMP-dc:Description",)
FILM_TEXT_EXTRA = ("Keys:Description", "ItemList:Description", "UserData:Description")
FILM_CREATOR_TAGS = ("XMP-dc:Creator",)
FILM_META_TAGS = (FILM_DATE_TAGS + FILM_DATE_EXTRA + FILM_PLACE_TAGS + FILM_PLACE_EXTRA + FILM_TEXT_TAGS
                  + FILM_TEXT_EXTRA + WORD_TAGS + RATING_TAGS + FILM_CREATOR_TAGS + (TREE_TAG,))
SAME_FACE = 0.3    # two boxes that overlap this much (intersection over union) mean the same face

# EXIF orientation n says: do this to the stored picture to see it upright.
PIL_OPS = {2: Image.Transpose.FLIP_LEFT_RIGHT, 3: Image.Transpose.ROTATE_180, 4: Image.Transpose.FLIP_TOP_BOTTOM,
           5: Image.Transpose.TRANSPOSE, 6: Image.Transpose.ROTATE_270, 7: Image.Transpose.TRANSVERSE,
           8: Image.Transpose.ROTATE_90}
JPEGTRAN_OPS = {2: ["-flip", "horizontal"], 3: ["-rotate", "180"], 4: ["-flip", "vertical"], 5: ["-transpose"],
                6: ["-rotate", "90"], 7: ["-transverse"], 8: ["-rotate", "270"]}
INVERSE = {1: 1, 2: 2, 3: 3, 4: 4, 5: 5, 6: 8, 7: 7, 8: 6}
TURNS = {90: 6, 180: 3, 270: 8}      # degrees clockwise -> the orientation that means the same


def _apply(image: Image.Image, n: int) -> Image.Image:
    return image if n == 1 else image.transpose(PIL_OPS[n])


def _compose_table() -> dict[tuple[int, int], int]:
    """(first, then) -> the single orientation that does both. Worked out on a tiny picture, not by hand."""
    probe = Image.new("L", (3, 2))
    probe.putdata([10, 20, 30, 40, 50, 60])
    single = {n: (_apply(probe, n).size, _apply(probe, n).tobytes()) for n in range(1, 9)}
    table = {}
    for first in range(1, 9):
        for then in range(1, 9):
            both = _apply(_apply(probe, first), then)
            table[first, then] = next(n for n, state in single.items() if state == (both.size, both.tobytes()))
    return table


COMPOSE = _compose_table()


class EditError(Exception):
    """A change that was not made. The photo is untouched."""


_SHORT_ZONE = re.compile(r"([+-])(\d):(\d\d)$")
_ZONE = re.compile(r"[+-]\d\d:\d\d$")


def _tidy(value: str) -> str:
    """Some cameras write a time zone like +1:00; exiftool only takes +01:00."""
    return _SHORT_ZONE.sub(r"\g<1>0\2:\3", value)


def _zone(value) -> str:
    found = _ZONE.search(_tidy(str(value or "")))
    return found.group(0) if found else ""


def _nothing(value) -> bool:
    return value is None or value == "" or value == [] or value == {}


def _alike(a, b) -> bool:
    """Is this the same tag value? Tolerant only where the notation may differ, not the meaning."""
    if a in (None, []) and b in (None, []):
        return True
    if a is None or b is None:
        return False
    if isinstance(a, dict) or isinstance(b, dict):       # a structure: field by field
        # a field that holds nothing (Apple writes an empty Extensions) is not stored again –
        # that is the same as not being there
        return (isinstance(a, dict) and isinstance(b, dict)
                and all(_alike(a.get(key), b.get(key)) or (_nothing(a.get(key)) and _nothing(b.get(key)))
                        for key in a.keys() | b.keys()))
    if isinstance(a, list) or isinstance(b, list):
        a, b = (a if isinstance(a, list) else [a]), (b if isinstance(b, list) else [b])
        if any(isinstance(x, dict) for x in a + b):
            return len(a) == len(b) and all(_alike(x, y) for x, y in zip(a, b))
        return [str(x) for x in a] == [str(x) for x in b]
    try:
        x, y = float(a), float(b)
        return abs(x - y) <= 1e-7 * max(1.0, abs(x))
    except (TypeError, ValueError):
        pass
    try:        # several numbers in one value, like the "lat lon" of a film
        xs, ys = [float(v) for v in str(a).split()], [float(v) for v in str(b).split()]
        if len(xs) > 1 and len(xs) == len(ys):
            return all(abs(x - y) <= 1e-7 * max(1.0, abs(x)) for x, y in zip(xs, ys))
    except ValueError:
        pass
    return _tidy(str(a)) == _tidy(str(b))


def _is_film(path: Path) -> bool:
    return kind_of(path.suffix.lower().lstrip(".")) == "video"


def _utc(path: Path) -> list[str]:
    """A film keeps its times in UTC. Read and written that way, a time is the wall-clock time here,
    with the zone it is in – the same way the index reads it."""
    return ["-api", "QuickTimeUTC=1"] if _is_film(path) else []


def _local_zone(stamp: str) -> str:
    """The zone this machine's clock was in at that moment, as +01:00."""
    try:
        offset = datetime.strptime(stamp, "%Y:%m:%d %H:%M:%S").astimezone().strftime("%z")
    except (ValueError, OverflowError, OSError):
        return ""
    return f"{offset[:3]}:{offset[3:]}" if len(offset) == 5 else ""


def _film_hash(path: Path) -> str:
    """A fingerprint of every stream in the film – picture, sound and the rest – packet by packet, as
    ffmpeg copies them out without decoding. Where the boxes around them lie does not count."""
    tool = shutil.which("ffmpeg")
    if not tool:
        raise EditError("ffmpeg is not installed")
    try:
        done = subprocess.run([tool, "-v", "error", "-nostdin", "-i", str(path), "-map", "0", "-c", "copy",
                               "-f", "streamhash", "-hash", "sha256", "-"], capture_output=True, text=True, timeout=1800)
    except (OSError, subprocess.SubprocessError) as exc:
        raise EditError("check failed: the film cannot be read") from exc
    if done.returncode != 0 or not done.stdout.strip():
        raise EditError("check failed: the film cannot be read")
    return done.stdout


def _struct(value) -> str:
    """A structure the way exiftool wants it on the command line: {Field=value,List=[a,b]}."""
    if isinstance(value, dict):
        return "{" + ",".join(f"{key}={_struct(item)}" for key, item in value.items()) + "}"
    if isinstance(value, list):
        return "[" + ",".join(_struct(item) for item in value) + "]"
    text = re.sub(r"([|,}\]])", r"|\1", repr(value) if isinstance(value, float) else str(value))
    return "|" + text if text[:1] in ("{", "[") or text[:1].isspace() else text


def _number(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _box(region: dict) -> tuple[float, float, float, float] | None:
    """Centre, width and height of a region read by exiftool – None unless given in fractions of the picture."""
    area = region.get("Area") if isinstance(region, dict) else None
    if not isinstance(area, dict) or str(area.get("Unit", "normalized")).lower() != "normalized":
        return None
    x, y = _number(area.get("X")), _number(area.get("Y"))
    w, h = _number(area.get("W", area.get("D"))), _number(area.get("H", area.get("D")))
    return None if None in (x, y, w, h) else (x, y, w, h)


def _overlap(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> float:
    """Intersection over union of two boxes given as centre, width, height."""
    wide = min(a[0] + a[2] / 2, b[0] + b[2] / 2) - max(a[0] - a[2] / 2, b[0] - b[2] / 2)
    high = min(a[1] + a[3] / 2, b[1] + b[3] / 2) - max(a[1] - a[3] / 2, b[1] - b[3] / 2)
    if wide <= 0 or high <= 0:
        return 0.0
    return wide * high / (a[2] * a[3] + b[2] * b[3] - wide * high)


def turn_regions(info: dict, n: int) -> dict:
    """The marked regions after the stored picture went through orientation n."""
    turned = copy.deepcopy(info)
    size = turned.get("AppliedToDimensions")
    if n >= 5 and isinstance(size, dict) and "W" in size and "H" in size:
        size["W"], size["H"] = size["H"], size["W"]
    regions = turned.get("RegionList") or []
    for region in regions if isinstance(regions, list) else [regions]:
        box = _box(region)
        if box is None:         # in pixels, or not understood: better not to guess
            raise EditError("the marked regions cannot be turned")
        area = region["Area"]
        x, y, w, h = turn_box(box, n)
        area["X"], area["Y"] = round(x, 7), round(y, 7)
        if "W" in area and "H" in area:
            area["W"], area["H"] = round(w, 7), round(h, 7)
    return turned


def _names(info: dict | None) -> list[str]:
    """Who is named in these regions, read the way the index reads the file."""
    regions = (info or {}).get("RegionList") or []
    return people_of([{"name": clean_name(region.get("Name")), "type": region.get("Type")}
                      for region in regions if isinstance(region, dict) and _box(region)])


def clean_keywords(words) -> list[str]:
    seen: list[str] = []
    for word in words or []:
        word = " ".join(str(word).split())
        if word and word not in seen:
            seen.append(word)
    return seen


def clean_albums(albums) -> list[str]:
    """Album names as paths: Urlaub/USA 2010. The separators themselves cannot be part of a name."""
    seen: list[str] = []
    for album in albums or []:
        parts = [" ".join(part.replace("|", " ").split()) for part in str(album).split("/")]
        path = "/".join(part for part in parts if part)
        if path and path not in seen:
            seen.append(path)
    return seen


class Editor:
    def __init__(self, root: Path, tmp: Path, index: Index, journal: Journal):
        self.root = root
        self.tmp = tmp
        self.index = index
        self.journal = journal
        self._lock = threading.Lock()      # one change at a time
        self.on_turn = None                # told when a picture's data was turned: (old hash, new hash, orientation, path)
        self.exiftool = shutil.which("exiftool")
        self.jpegtran = shutil.which("jpegtran")

    # ------------------------------------------------------------ plumbing

    def _tell(self, old: str, new: str, n: int, path: str) -> None:
        """The picture is turned and safe – or it is the same picture under another hash, because words
        were written into a file whose hash covers all of it (n = 1). What hangs on its old hash
        elsewhere should follow. If that fails the change still stands."""
        if self.on_turn and old != new:
            try:
                self.on_turn(old, new, n, path)
            except Exception:
                log.exception("%s: what was decided about the picture could not follow the turn", path)

    def _source(self, path: str) -> Path:
        root = self.root.resolve()
        src = (root / path).resolve()
        if root not in src.parents or not src.is_file():
            raise EditError("file not found")
        if not os.access(src, os.W_OK) or not os.access(src.parent, os.W_OK):
            raise EditError("file is not writable")
        return src

    def _temp(self, suffix: str) -> Path:
        self.tmp.mkdir(parents=True, exist_ok=True)
        return self.tmp / f"{secrets.token_hex(8)}{suffix}"

    @staticmethod
    def _beside(src: Path) -> Path:
        """A hidden name in the photo's own folder, so the final swap is one atomic rename."""
        return src.parent / f".augenblick-{secrets.token_hex(6)}{src.suffix}"

    def _run(self, command: list[str]) -> subprocess.CompletedProcess:
        return subprocess.run(command, capture_output=True, text=True, timeout=300)

    def _read(self, src: Path, tags: tuple[str, ...]) -> dict:
        """The tags as they are in the file now; missing ones are simply not in the answer."""
        if not self.exiftool:
            raise EditError("exiftool is not installed")
        done = self._run([self.exiftool, "-j", "-n", "-G1", "-struct", *_utc(src), *(f"-{t}" for t in tags), str(src)])
        if done.returncode != 0:
            raise EditError(f"cannot read metadata: {done.stderr.strip()[-200:]}")
        found = json.loads(done.stdout)[0]
        found.pop("SourceFile", None)
        for tag in LIST_TAGS:
            if tag in found and not isinstance(found[tag], list):
                found[tag] = [found[tag]]
        return found

    @staticmethod
    def _arguments(tags: dict) -> list[str]:
        args: list[str] = []
        for tag, value in tags.items():
            if value is None or value == []:
                args.append(f"-{tag}=")
            elif value == "":                   # an empty value that was there is not the same as no value
                args.append(f"-{tag}^=")
            elif isinstance(value, str):
                args.append(f"-{tag}={_tidy(value)}")
            elif isinstance(value, list):
                args += [f"-{tag}={item}" for item in value]
            elif isinstance(value, dict):       # a structure, written back as it was read
                args.append(f"-{tag}={_struct(value)}")
            else:
                args.append(f"-{tag}={value}")
        return args

    def _write(self, source: Path, target: Path, tags: dict, extra: tuple[str, ...] = ()) -> None:
        """source + tags -> target. The source is never touched."""
        if not self.exiftool:
            raise EditError("exiftool is not installed")
        command = [self.exiftool, "-n", *_utc(source), *self._arguments(tags), *extra, "-o", str(target), str(source)]
        done = self._run(command)
        if "Duplicate XMP property" in done.stderr + done.stdout:
            # An earlier programme wrote one XMP entry twice. exiftool then leaves the whole XMP alone
            # unless told to pass over minor faults (-m); it keeps one of the two. Only for this fault –
            # others, like damaged maker notes, still stop the change. All checks follow as always.
            target.unlink(missing_ok=True)
            done = self._run([command[0], "-m", *command[1:]])
        if done.returncode != 0 or not target.is_file():
            target.unlink(missing_ok=True)
            reason = (done.stderr.strip() or done.stdout.strip()).splitlines()
            raise EditError(f"exiftool refused: {reason[-1][:200] if reason else 'no output'}")

    def _check_tags(self, temp: Path, tags: dict) -> None:
        """exiftool skips a single tag it cannot write and still reports success – so look for ourselves."""
        stored = self._read(temp, tuple(tags))
        wrong = [tag for tag, value in tags.items() if not _alike(stored.get(tag), value)]
        if wrong:
            raise EditError("check failed: not stored as asked (" + ", ".join(wrong) + ")")

    @staticmethod
    def _same_picture(before: Path, after: Path) -> None:
        """The picture data must be identical; only metadata may differ."""
        if _is_film(before):
            if _film_hash(before) != _film_hash(after):
                raise EditError("check failed: the film changed")
            return
        ext = before.suffix.lower().lstrip(".")
        if ext in JPEG or ext in HEIF_EXT:
            digest = jpeg_image_hash if ext in JPEG else heif_image_hash
            old, new = digest(before.read_bytes()), digest(after.read_bytes())
            if old and old == new:
                return
            if old or ext in JPEG:
                raise EditError("check failed: the picture data changed")
        with Image.open(before) as a, Image.open(after) as b:
            if a.size != b.size or a.mode != b.mode or a.tobytes() != b.tobytes():
                raise EditError("check failed: the picture changed")

    @staticmethod
    def _commit(temp: Path, src: Path) -> None:
        stat = src.stat()
        os.chmod(temp, stat.st_mode & 0o7777)
        try:
            os.chown(temp, stat.st_uid, stat.st_gid)
        except OSError:
            pass
        os.replace(temp, src)

    # ------------------------------------------------------------ metadata

    @staticmethod
    def _want(photo: dict, change: dict) -> dict:
        """What the photo should say afterwards – only the fields that really change."""
        want: dict = {}
        taken = change.get("taken")
        if change.get("shift"):
            if photo["taken_src"] == "file":     # only the file's own date is known: nothing worth moving
                raise EditError("no date to shift")
            moved = datetime.fromisoformat(photo["taken"]) + timedelta(seconds=change["shift"])
            taken = moved.strftime("%Y-%m-%dT%H:%M:%S")
        if taken:
            if len(taken) == 16:
                taken += ":00"
            try:
                parsed = datetime.fromisoformat(taken)
            except ValueError as exc:
                raise EditError("invalid date") from exc
            if not 1800 <= parsed.year <= 2200:
                raise EditError("invalid date")
            if taken != photo["taken"] or photo["taken_src"] != "exif":
                want["taken"] = taken
        if change.get("clear_place"):
            if photo["lat"] is not None:
                want["lat"] = want["lon"] = None
        elif change.get("lat") is not None and change.get("lon") is not None:
            lat, lon = round(float(change["lat"]), 7), round(float(change["lon"]), 7)
            if photo["lat"] is None or abs(photo["lat"] - lat) > 1e-6 or abs(photo["lon"] - lon) > 1e-6:
                want["lat"], want["lon"] = lat, lon
        if change.get("description") is not None:
            text = change["description"].replace("\r\n", "\n").replace("\x00", "").strip()
            if text != (photo["description"] or ""):
                want["description"] = text
        words = None
        if change.get("keywords") is not None:
            words = clean_keywords(change["keywords"])
        if change.get("add_keywords") or change.get("remove_keywords"):
            base = words if words is not None else list(photo["keywords"])
            gone = set(clean_keywords(change.get("remove_keywords")))
            words = clean_keywords([w for w in base if w not in gone] + clean_keywords(change.get("add_keywords")))
        if words is not None and words != photo["keywords"]:
            want["keywords"] = words
        albums = None
        if change.get("albums") is not None:
            albums = clean_albums(change["albums"])
        if change.get("add_albums") or change.get("remove_albums"):
            base = albums if albums is not None else list(photo.get("albums") or [])
            gone = set(clean_albums(change.get("remove_albums")))
            albums = clean_albums([a for a in base if a not in gone] + clean_albums(change.get("add_albums")))
        if albums is not None and albums != (photo.get("albums") or []):
            want["albums"] = albums
        if change.get("rating") is not None and change["rating"] != (photo["rating"] or 0):
            want["rating"] = int(change["rating"])
        if change.get("creator") is not None and clean_creator(change["creator"]) != (photo.get("creator") or ""):
            want["creator"] = clean_creator(change["creator"])
        return want

    @staticmethod
    def _film_tags_for(want: dict, present: dict) -> dict:
        """The same as _tags_for, for a film."""
        tags: dict = {}

        def put(always: tuple[str, ...], extra: tuple[str, ...], value) -> None:
            for tag in always + tuple(t for t in extra if t in present):
                tags[tag] = value(tag) if callable(value) else value

        if "taken" in want:
            stamp = want["taken"].replace("-", ":").replace("T", " ")
            # the phone's own date keeps its zone; the box date is kept in UTC and so needs ours
            put(FILM_DATE_TAGS, FILM_DATE_EXTRA, lambda tag: stamp + (
                _local_zone(stamp) if tag == "QuickTime:CreateDate" else _zone(present.get(tag))))
        if "lat" in want:
            lat, lon = want["lat"], want["lon"]
            put(FILM_PLACE_TAGS, FILM_PLACE_EXTRA, None if lat is None else lambda tag: (
                {"XMP-exif:GPSLatitude": lat, "XMP-exif:GPSLongitude": lon}.get(tag, f"{lat!r} {lon!r}")))
        if "description" in want:
            put(FILM_TEXT_TAGS, FILM_TEXT_EXTRA, want["description"] or None)
        if "keywords" in want:
            tags[WORD_TAGS[0]] = want["keywords"] or None
        if "rating" in want:
            tags["XMP-xmp:Rating"] = want["rating"] or None
        if "creator" in want:
            tags[FILM_CREATOR_TAGS[0]] = want["creator"] or None
        if "albums" in want:
            others = [str(v) for v in present.get(TREE_TAG, []) if str(v).split("|")[0] != ALBUM_ROOT]
            tags[TREE_TAG] = others + [ALBUM_ROOT + "|" + album.replace("/", "|") for album in want["albums"]] or None
        return tags

    @staticmethod
    def _tags_for(want: dict, present: dict) -> dict:
        tags: dict = {}

        def put(always: tuple[str, ...], extra: tuple[str, ...], values: dict) -> None:
            for tag in always + tuple(t for t in extra if t in present):
                tags[tag] = values[tag]

        if "taken" in want:
            stamp = want["taken"].replace("-", ":").replace("T", " ")
            # a time zone that stands next to the old time stays; the fraction of a second goes
            values = {tag: stamp + _zone(present.get(tag)) for tag in DATE_EXTRA[:3]}
            put(DATE_TAGS, DATE_EXTRA, {
                **values, "ExifIFD:DateTimeOriginal": stamp, "ExifIFD:CreateDate": stamp,
                "IPTC:DateCreated": stamp[:10],
                "IPTC:TimeCreated": stamp[11:] + (_zone(present.get("IPTC:TimeCreated")) or "+00:00")})
            # "Digitised on" is a date of its own where it differs from the date taken – the day a
            # paper photo went through the scanner. It only follows when it said the same before.
            old = str(present.get("ExifIFD:DateTimeOriginal") or "")[:19]
            for tag in ("ExifIFD:CreateDate", "XMP-xmp:CreateDate"):
                there = present.get(tag)
                if there is not None and str(there)[:19] != old:
                    tags.pop(tag, None)
        if "lat" in want:
            lat, lon = want["lat"], want["lon"]
            if lat is None:
                put(PLACE_TAGS, PLACE_EXTRA, dict.fromkeys(PLACE_TAGS + PLACE_EXTRA))
            else:
                put(PLACE_TAGS, PLACE_EXTRA, {
                    "GPS:GPSLatitude": abs(lat), "GPS:GPSLatitudeRef": "N" if lat >= 0 else "S",
                    "GPS:GPSLongitude": abs(lon), "GPS:GPSLongitudeRef": "E" if lon >= 0 else "W",
                    "XMP-exif:GPSLatitude": lat, "XMP-exif:GPSLongitude": lon})
        if "description" in want:
            put(TEXT_TAGS, TEXT_EXTRA, dict.fromkeys(TEXT_TAGS + TEXT_EXTRA, want["description"] or None))
        if "keywords" in want:
            put(WORD_TAGS, WORD_EXTRA, dict.fromkeys(WORD_TAGS + WORD_EXTRA, want["keywords"] or None))
        if "rating" in want:
            tags["XMP-xmp:Rating"] = want["rating"] or None
        if "creator" in want:
            put(CREATOR_TAGS, CREATOR_EXTRA, dict.fromkeys(CREATOR_TAGS + CREATOR_EXTRA, want["creator"] or None))
        if "albums" in want:        # other keyword trees in the same tag are none of our business: they stay
            others = [str(v) for v in present.get(TREE_TAG, []) if str(v).split("|")[0] != ALBUM_ROOT]
            tags[TREE_TAG] = others + [ALBUM_ROOT + "|" + album.replace("/", "|") for album in want["albums"]] or None
        return tags

    @staticmethod
    def _check_meta(temp: Path, want: dict) -> None:
        """Read the new file the way the index will and compare with what was asked for."""
        if _is_film(temp):
            meta = read_video(temp)
            if not meta:
                raise EditError("check failed: the new file cannot be read")
        else:
            try:
                _, meta = read_image(temp.read_bytes())
            except Exception as exc:
                raise EditError(f"check failed: the new file cannot be read ({type(exc).__name__})") from exc
        problems = []
        if "taken" in want and meta.get("taken") != want["taken"]:
            problems.append("date")
        if "lat" in want:
            if want["lat"] is None:
                if "lat" in meta:
                    problems.append("place")
            elif "lat" not in meta or abs(meta["lat"] - want["lat"]) > 1e-5 or abs(meta["lon"] - want["lon"]) > 1e-5:
                problems.append("place")
        if "description" in want and (meta.get("description") or "") != want["description"]:
            problems.append("description")
        if "keywords" in want and meta.get("keywords", []) != want["keywords"]:
            problems.append("keywords")
        if "rating" in want and meta.get("rating", 0) != want["rating"]:
            problems.append("rating")
        if "albums" in want and meta.get("albums", []) != want["albums"]:
            problems.append("albums")
        if "creator" in want and (meta.get("creator") or "") != want["creator"]:
            problems.append("creator")
        if problems:
            raise EditError("check failed: not stored as asked (" + ", ".join(problems) + ")")

    def change(self, photo: dict, change: dict, user: str, batch: str | None = None) -> dict:
        """Write date, place, description, keywords, rating or who took it into the photo."""
        film = photo["kind"] == "video"
        if film and photo["ext"] not in FILM_EXT:
            raise EditError("this kind of film cannot be changed")
        if photo["kind"] not in ("image", "video"):
            raise EditError("only pictures can be changed so far")
        if photo["ext"] in RAW_EXT:
            raise EditError("raw files are left as the camera wrote them")
        with self._lock:
            want = self._want(photo, change)
            if not want:
                return self.index.by_path(photo["path"])
            src = self._source(photo["path"])
            present = self._read(src, FILM_META_TAGS if film else META_TAGS)
            tags = self._film_tags_for(want, present) if film else self._tags_for(want, present)
            temp = self._beside(src)
            try:
                self._write(src, temp, tags)
                self._same_picture(src, temp)
                self._check_tags(temp, tags)
                self._check_meta(temp, want)
                self._commit(temp, src)
            finally:
                temp.unlink(missing_ok=True)
            record = self.index.refresh(photo["path"])
            self._tell(photo["hash"], record["hash"], 1, photo["path"])     # where the hash covers the whole file
            before = {k: photo.get(k) or [] if k == "albums" else photo[k] for k in want}
            if "taken" in want:
                before["taken_src"] = photo["taken_src"]
            self.journal.add(user=user, batch=batch or secrets.token_hex(6), action="meta", path=photo["path"],
                             before=before, after=want, tags={t: present.get(t) for t in tags},
                             sha_after=file_hash(src))
            return record

    def sign(self, path: Path, creator: str = "", taken: str = "") -> list[str]:
        """Write into a file that is not part of the collection yet – one that has just come through
        the upload door – what the door knows about it and the file does not say itself: who took
        it (the name on the key) and when (the date the phone gave, as YYYY-MM-DDTHH:MM:SS). What the
        file says already stays. The picture data must come out untouched, else the file is left as
        it came. Hands back what was written: "creator", "taken"."""
        creator = clean_creator(creator)
        ext = path.suffix.lower().lstrip(".")
        if not (creator or taken) or kind_of(ext) != "image" or ext in RAW_EXT:
            return []
        present = self._read(path, CREATOR_TAGS + CREATOR_EXTRA + DATE_TAGS + DATE_EXTRA)
        tags: dict = {}
        if creator and all(_nothing(present.get(tag)) for tag in CREATOR_TAGS + CREATOR_EXTRA):
            tags.update(dict.fromkeys(CREATOR_TAGS, creator))
        dated = taken and all(_nothing(present.get(tag)) for tag in DATE_TAGS + DATE_EXTRA) \
            and not read_image(path.read_bytes())[1].get("taken")
        if dated:
            tags.update(dict.fromkeys(DATE_TAGS, taken.replace("-", ":").replace("T", " ")))
        if not tags:
            return []
        temp = self._beside(path)
        try:
            self._write(path, temp, tags)
            self._same_picture(path, temp)
            self._check_tags(temp, tags)
            meta = read_image(temp.read_bytes())[1]
            if "IFD0:Artist" in tags and (meta.get("creator") or "") != creator:
                raise EditError("check failed: not stored as asked (creator)")
            if dated and meta.get("taken") != taken:
                raise EditError("check failed: not stored as asked (date)")
            self._commit(temp, path)
        finally:
            temp.unlink(missing_ok=True)
        return (["creator"] if "IFD0:Artist" in tags else []) + (["taken"] if dated else [])

    # ------------------------------------------------------------ turning

    def _jpegtran(self, src: Path, n: int, copy: str = "all") -> Path | None:
        """Turn without re-encoding. None if the picture's size does not allow that."""
        if not self.jpegtran:
            raise EditError("jpegtran is not installed")
        with Image.open(src) as image:
            progressive = bool(image.info.get("progressive"))
        out = self._temp(".jpg")
        done = self._run([self.jpegtran, "-perfect", "-copy", copy, "-optimize",
                          *(["-progressive"] if progressive else []), *JPEGTRAN_OPS.get(n, []),
                          "-outfile", str(out), str(src)])
        if done.returncode != 0 or not out.is_file() or out.stat().st_size == 0:
            out.unlink(missing_ok=True)
            if "perfect" in done.stderr:
                return None
            raise EditError(f"jpegtran failed: {done.stderr.strip()[-200:]}")
        return out

    def _check_turn(self, src: Path, turned: Path, n: int) -> None:
        """Two proofs: turned back it is bit for bit the old picture, and it was turned the right way."""
        plain = self._jpegtran(src, 1, copy="none")
        back = self._jpegtran(turned, INVERSE[n], copy="none")
        try:
            if plain is None or back is None or plain.read_bytes() != back.read_bytes():
                raise EditError("check failed: turning was not lossless")
        finally:
            for path in (plain, back):
                if path:
                    path.unlink(missing_ok=True)
        with Image.open(src) as a, Image.open(turned) as b:
            a.draft("L", (400, 400))
            b.draft("L", (400, 400))
            expected = _apply(a.convert("L"), n)
            got = b.convert("L")
            landscape = expected.width >= expected.height
            if landscape != (got.width >= got.height) and expected.width != expected.height:
                raise EditError("check failed: turned the wrong way")
            box = (96, 64) if landscape else (64, 96)
            difference = ImageStat.Stat(ImageChops.difference(expected.resize(box), got.resize(box))).mean[0]
        if difference > 4:
            raise EditError("check failed: turned the wrong way")

    def _small_copy(self, turned: Path, edge: int, limit: int) -> Path:
        """A small JPEG of the turned picture that is no bigger in bytes than the one it replaces –
        a bigger one would not fit into the same place in the file."""
        with Image.open(turned) as image:
            image.draft("RGB", (edge * 2, edge * 2))
            image = image.convert("RGB")
        while edge >= 32:
            copy = image.copy()
            copy.thumbnail((edge, edge), Image.LANCZOS)
            for quality in (85, 75, 65, 55, 45, 35):
                out = io.BytesIO()
                copy.save(out, "JPEG", quality=quality)
                if out.tell() <= limit:
                    small = self._temp(".jpg")
                    small.write_bytes(out.getvalue())
                    return small
            edge = int(edge * 0.8)
        raise EditError("check failed: the small picture inside the file cannot be renewed")

    def _finish_turn(self, src: Path, turned: Path, tags: dict, thumbnail: int, preview: bool = False) -> Path:
        """Put the right tags on a turned picture; the answer lies next to the photo, ready to take its place.

        The small pictures a camera stores inside the file (thumbnail, preview) are made anew from the
        turned picture – left as they were, they would show the old orientation.
        """
        final = self._beside(src)
        made: list[Path] = []
        try:
            extra: tuple[str, ...] = ()
            if thumbnail:               # its old size in bytes
                made.append(self._small_copy(turned, 160, max(int(thumbnail), 2000)))
                extra += (f"-ThumbnailImage<={made[-1]}",)
            if preview:
                old = subprocess.run([self.exiftool, "-b", "-PreviewImage", str(src)], capture_output=True, timeout=300)
                try:
                    with Image.open(io.BytesIO(old.stdout)) as image:
                        edge = max(image.size)
                except Exception as exc:
                    raise EditError("check failed: the preview inside the file cannot be read") from exc
                made.append(self._small_copy(turned, edge, len(old.stdout)))
                extra += (f"-PreviewImage<={made[-1]}",)
            if tags or extra:
                self._write(turned, final, tags, extra)
                self._check_tags(final, tags)
            else:
                shutil.copyfile(turned, final)
            if jpeg_image_hash(final.read_bytes()) != jpeg_image_hash(turned.read_bytes()):
                raise EditError("check failed: the picture data changed")
            return final
        except Exception:
            final.unlink(missing_ok=True)
            raise
        finally:
            for path in made:
                path.unlink(missing_ok=True)

    def rotate(self, photo: dict, degrees: int, user: str, batch: str | None = None) -> dict:
        """Turn a JPEG clockwise by 90, 180 or 270 degrees.

        Where the picture's size allows it, the picture itself is turned without loss and the
        orientation tag is set to "upright"; marked faces turn with it. Where not, the picture
        stays as it is and only the tag says how to show it – the marked faces refer to the
        stored picture and stay as they are.
        """
        if photo["ext"] in HEIF_EXT:
            return self._rotate_heif(photo, degrees, user, batch)
        if photo["ext"] not in JPEG:
            raise EditError("only JPEG and HEIC pictures can be turned so far")
        with self._lock:
            src = self._source(photo["path"])
            present = self._read(src, TURN_TAGS)
            now = present.get("IFD0:Orientation")
            now = now if isinstance(now, int) and 1 <= now <= 8 else 1
            total = COMPOSE[now, TURNS[degrees]]
            regions = present.get(REGION_TAG)
            turned, final, why = None, None, ""
            if total != 1:
                if (jpeg_trailer(src.read_bytes()) or b"").strip(b"\x00\r\n\t "):
                    why = "trailer"         # pictures appended to the file would be lost
                else:
                    turned = self._jpegtran(src, total)
                    why = "" if turned else "raster"
            thumbnail, preview = 0, False
            try:
                if turned:
                    self._check_turn(src, turned, total)
                    with Image.open(turned) as image:
                        width, height = image.size
                    values = {"IFD0:Orientation": 1, "XMP-tiff:Orientation": 1, "IFD1:Orientation": None,
                              "ExifIFD:ExifImageWidth": width, "ExifIFD:ExifImageHeight": height}
                    tags = {t: v for t, v in values.items() if t in present}
                    thumbnail = int(present.get("IFD1:ThumbnailLength") or 0)
                    preview = any(tag.endswith(":PreviewImageLength") for tag in present)
                    try:
                        if regions:             # the marked faces go round with the picture
                            tags[REGION_TAG] = turn_regions(regions, total)
                        final = self._finish_turn(src, turned, tags, thumbnail, preview)
                    except EditError as exc:
                        if not regions:
                            raise
                        # the regions cannot be written into this file: rather than leave them
                        # pointing at the wrong spot, the picture stays and only the tag turns
                        log.info("%s: turned by the tag only, regions could not follow (%s)", photo["path"], exc)
                        turned.unlink(missing_ok=True)
                        turned, why, thumbnail, preview = None, "regions", 0, False
                if not turned:
                    tags = {"IFD0:Orientation": total}
                    if "XMP-tiff:Orientation" in present:
                        tags["XMP-tiff:Orientation"] = total
                    final = self._beside(src)
                    try:
                        self._write(src, final, tags)
                        self._same_picture(src, final)
                        self._check_tags(final, tags)
                    except Exception:
                        final.unlink(missing_ok=True)
                        raise
                try:
                    self._commit(final, src)
                finally:
                    final.unlink(missing_ok=True)
            finally:
                if turned:
                    turned.unlink(missing_ok=True)
            record = self.index.refresh(photo["path"])
            if turned:
                self._tell(photo["hash"], record["hash"], total, photo["path"])
            self.journal.add(
                user=user, batch=batch or secrets.token_hex(6), action="rotate", path=photo["path"],
                before={"orientation": now},
                after={"orientation": 1 if turned else total, "turn": degrees, "lossless": bool(turned),
                       "op": total, "why": why, "thumbnail": thumbnail, "preview": preview},
                tags={t: present.get(t) for t in tags}, sha_after=file_hash(src))
            return record

    def _heif_turned(self, src: Path, quarter: int, n: int) -> Path:
        """A copy of a HEIF file whose turn field says ``quarter``; n is the orientation that takes
        the picture as shown now to the picture as shown then.

        Two proofs: the copy differs from the file in that one byte and nothing else – the picture
        data is untouched – and a decoder shows it turned the right way. (The colours of single
        pixels may differ by a shade: a decoder turns before it spreads the colour, which is stored
        at half the sharpness, over the pixels.)
        """
        data = src.read_bytes()
        found = heif_turn(data)
        if not found:
            raise EditError("this picture cannot be turned: it has no turn field of its own")
        at = found[0]
        turned = self._temp(src.suffix)
        turned.write_bytes(data[:at] + bytes([quarter & 3]) + data[at + 1:])
        try:
            after = turned.read_bytes()
            if len(after) != len(data) or after[:at] != data[:at] or after[at + 1:] != data[at + 1:] \
                    or heif_turn(after) != (at, quarter & 3):
                raise EditError("check failed: more than the turn changed")
            with Image.open(src) as a, Image.open(turned) as b:
                expected, got = _apply(a.convert("L"), n), b.convert("L")
                if expected.size != got.size:
                    raise EditError("check failed: turned the wrong way")
                box = (96, 64) if got.width >= got.height else (64, 96)
                difference = ImageStat.Stat(ImageChops.difference(expected.resize(box), got.resize(box))).mean[0]
            if difference > 4:
                raise EditError("check failed: turned the wrong way")
            return turned
        except Exception:
            turned.unlink(missing_ok=True)
            raise

    def _rotate_heif(self, photo: dict, degrees: int, user: str, batch: str | None = None) -> dict:
        """Turn a HEIF picture (the phone's HEIC) by its turn field: one byte, the picture data stays
        as it is. Marked faces are written relative to that data, so they stay as they are and are
        simply read the other way round afterwards."""
        with self._lock:
            src = self._source(photo["path"])
            found = heif_turn(src.read_bytes())
            if not found:
                raise EditError("this picture cannot be turned: it has no turn field of its own")
            was = found[1]
            now = (was - degrees // 90) % 4             # the field counts anti-clockwise
            n = TURNS[degrees]
            present = self._read(src, ("IFD0:Orientation", REGION_TAG))
            tags: dict = {}
            if "IFD0:Orientation" in present:
                tags["IFD0:Orientation"] = HEIF_ORIENTATION[now]
            # marked faces lie in the stored picture, which stays as it is: they need no turning
            turned = self._heif_turned(src, now, n)
            final = self._beside(src)
            try:
                if tags:
                    self._write(turned, final, tags)
                    self._check_tags(final, tags)
                    self._same_picture(turned, final)
                else:
                    shutil.copyfile(turned, final)
                self._commit(final, src)
            finally:
                final.unlink(missing_ok=True)
                turned.unlink(missing_ok=True)
            record = self.index.refresh(photo["path"])
            self._tell(photo["hash"], record["hash"], n, photo["path"])
            self.journal.add(
                user=user, batch=batch or secrets.token_hex(6), action="rotate", path=photo["path"],
                before={"orientation": 1},
                after={"orientation": 1, "turn": degrees, "lossless": True, "op": n, "why": "", "heif": [was, now]},
                tags={t: present.get(t) for t in tags}, sha_after=file_hash(src))
            return record

    # ------------------------------------------------------------ faces

    def faces(self, photo: dict, edits: list[dict], user: str, batch: str | None = None) -> dict:
        """Name faces, rename them or take a name away.

        Every edit has a box in the picture as it is shown upright (centre x, centre y, width,
        height, as fractions) and a name. A box that lies on a face already marked in the file
        means that face; an empty name removes its region. Regions of other kinds and faces
        not meant stay exactly as they are.
        """
        if photo["kind"] != "image":
            raise EditError("only pictures can be changed so far")
        if photo["ext"] in RAW_EXT:
            raise EditError("raw files are left as the camera wrote them")
        with self._lock:
            src = self._source(photo["path"])
            present = self._read(src, (REGION_TAG, "IFD0:Orientation"))
            now = present.get("IFD0:Orientation")
            now = now if isinstance(now, int) and 1 <= now <= 8 else 1
            if photo["ext"] in HEIF_EXT:        # the decoder has done the turning: what is shown lies
                turn = heif_turn(src.read_bytes(), alone=False)     # as the turn field says, not the Exif tag
                now = HEIF_ORIENTATION[turn[1]] if turn else 1
            old = present.get(REGION_TAG) if isinstance(present.get(REGION_TAG), dict) else None
            info = copy.deepcopy(old) or {}
            regions = info.get("RegionList") or []
            regions = regions if isinstance(regions, list) else [regions]
            changed = False
            for edit in edits:
                try:
                    box = tuple(float(edit[side]) for side in ("x", "y", "w", "h"))
                except (KeyError, TypeError, ValueError) as exc:
                    raise EditError("invalid face box") from exc
                if not (0 <= box[0] <= 1 and 0 <= box[1] <= 1 and 0 < box[2] <= 1 and 0 < box[3] <= 1):
                    raise EditError("invalid face box")
                box = turn_box(box, INVERSE[now])       # from what is shown to what is stored
                name = clean_name(edit.get("name"))
                best, hit = SAME_FACE, None
                for at, region in enumerate(regions):
                    there = _box(region) if is_face(region.get("Type")) else None
                    if there and _overlap(box, there) >= best:
                        best, hit = _overlap(box, there), at
                if name and hit is None:
                    x, y, w, h = (round(value, 6) for value in box)
                    regions.append({"Area": {"X": x, "Y": y, "W": w, "H": h, "Unit": "normalized"},
                                    "Name": name, "Type": "Face"})
                    changed = True
                elif name and clean_name(regions[hit].get("Name")) != name:
                    regions[hit] = {**regions[hit], "Name": name}
                    changed = True
                elif not name and hit is not None:
                    del regions[hit]
                    changed = True
            if not changed:
                return self.index.by_path(photo["path"])
            if regions:
                info["RegionList"] = regions
                if "AppliedToDimensions" not in info:
                    with Image.open(src) as image:      # the size of the stored picture, as the regions are
                        wide, high = (image.height, image.width) if now >= 5 and photo["ext"] in HEIF_EXT \
                            else (image.width, image.height)
                        info["AppliedToDimensions"] = {"W": wide, "H": high, "Unit": "pixel"}
            tags = {REGION_TAG: info if regions else None}
            want = _names(tags[REGION_TAG])
            temp = self._beside(src)
            try:
                self._write(src, temp, tags)
                self._same_picture(src, temp)
                self._check_tags(temp, tags)
                try:        # … and the index must find the same people in the new file
                    _, meta = read_image(temp.read_bytes())
                except Exception as exc:
                    raise EditError(f"check failed: the new file cannot be read ({type(exc).__name__})") from exc
                if meta.get("people", []) != want:
                    raise EditError("check failed: not stored as asked (people)")
                self._commit(temp, src)
            finally:
                temp.unlink(missing_ok=True)
            record = self.index.refresh(photo["path"])
            self._tell(photo["hash"], record["hash"], 1, photo["path"])     # where the hash covers the whole file
            self.journal.add(user=user, batch=batch or secrets.token_hex(6), action="faces", path=photo["path"],
                             before={"people": _names(old)}, after={"people": want},
                             tags={REGION_TAG: old}, sha_after=file_hash(src))
            return record

    # ------------------------------------------------------------ working on the picture itself

    DEVELOP_TAGS = ("XMP-xmpMM:DerivedFromDocumentID", "XMP-xmpMM:DerivedFromRenditionParams")

    @staticmethod
    def _carried(current: dict, recipe: dict, upright: tuple[int, int], made: tuple[int, int],
                 earlier: dict | None) -> dict | None:
        """The marked faces of the picture shown now, where they lie in the picture about to be made.
        `earlier` is the recipe the picture shown was made with – then its faces are first carried
        back to the original. A face whose middle is cut away is not carried over."""
        new, old = Frame(recipe, *upright), Frame(earlier, *upright) if earlier is not None else None
        kept = []
        for region in current.get("regions") or []:
            x, y, w, h = turn_box((region["x"], region["y"], region["w"], region["h"]), current.get("orientation") or 1)
            if old:
                (x, y), (wide, high) = old.back(x, y), old.scale
                w, h = w / wide, h / high
            (x, y), (wide, high) = new.forward(x, y), new.scale
            w, h = min(1.0, w * wide), min(1.0, h * high)
            if 0 <= x <= 1 and 0 <= y <= 1:
                area = {"X": round(x, 6), "Y": round(y, 6), "W": round(w, 6), "H": round(h, 6), "Unit": "normalized"}
                kept.append({"Area": area, **({"Name": region["name"]} if region["name"] else {}),
                             "Type": region["type"] or "Face"})
        if not kept:
            return None
        return {"AppliedToDimensions": {"W": made[0], "H": made[1], "Unit": "pixel"}, "RegionList": kept}

    def develop(self, photo: dict, recipe: dict, user: str, batch: str | None = None) -> dict:
        """Cut the picture, set it straight, change light and colours – as a new JPEG beside the
        original, which is never changed: it is renamed to …_original and stands behind the new
        picture from then on. Doing it again starts from the original once more."""
        if photo["kind"] != "image" or photo["ext"] in RAW_EXT or photo["ext"] == "gif":
            raise EditError("only pictures can be worked on")
        try:
            recipe = clean_recipe(recipe)
        except ValueError as exc:
            raise EditError(str(exc)) from exc
        if not recipe:
            raise EditError("nothing to change")
        if not self.exiftool:
            raise EditError("exiftool is not installed")
        with self._lock:
            current = self.index.by_path(photo["path"])
            if not current:
                raise EditError("file not found")
            original = self.index.original_of(current)
            if current.get("source") and not original:
                raise EditError("the original of this picture is not beside it")
            base = original or current
            src, donor = self._source(base["path"]), self._source(current["path"])
            try:
                image, _ = read_image(src.read_bytes())
                profile = image.info.get("icc_profile") if image.mode == "RGB" else None
                upright = ImageOps.exif_transpose(image)
                made = develop(upright, recipe)
            except EditError:
                raise
            except Exception as exc:
                raise EditError(f"the picture cannot be read ({type(exc).__name__})") from exc
            regions = self._carried(current, recipe, upright.size, made.size, current["recipe"] if original else None)
            raw, temp = (src.parent / f".augenblick-{secrets.token_hex(6)}.jpg" for _ in range(2))
            tags = {"IFD0:Orientation": 1, "XMP-tiff:Orientation": None, REGION_TAG: regions,
                    "ExifIFD:ExifImageWidth": made.width, "ExifIFD:ExifImageHeight": made.height,
                    self.DEVELOP_TAGS[0]: SOURCE_MARK + base["hash"],
                    self.DEVELOP_TAGS[1]: json.dumps(recipe, separators=(",", ":"), sort_keys=True)}
            try:
                made.save(raw, "JPEG", quality=95, subsampling=0, optimize=True, **({"icc_profile": profile} if profile else {}))
                # everything the picture says about itself comes along – from the one shown now, so what
                # was added since stays; the small picture inside (IFD1) would show the old cut and goes
                done = self._run([self.exiftool, "-n", "-m", "-tagsfromfile", str(donor), "-all:all", "-IFD1:all=",
                                  *self._arguments(tags), "-o", str(temp), str(raw)])
                if done.returncode != 0 or not temp.is_file():
                    reason = (done.stderr.strip() or done.stdout.strip()).splitlines()
                    raise EditError(f"exiftool refused: {reason[-1][:200] if reason else 'no output'}")
                try:
                    check, meta = read_image(temp.read_bytes())
                except Exception as exc:
                    raise EditError(f"check failed: the new file cannot be read ({type(exc).__name__})") from exc
                if (check.size != made.size or meta["source"] != base["hash"] or meta["recipe"] != recipe
                        or meta["orientation"] != 1 or meta["people"] != _names(regions)):
                    raise EditError("check failed: not stored as asked")
                os.chmod(temp, src.stat().st_mode & 0o7777)
                folder, _, name = base["path"].rpartition("/")
                stem, _, ext = name.rpartition(".")
                if original:                    # worked on before: the new picture takes the place of the last one
                    os.replace(temp, donor)
                    target, kept = current["path"], original["path"]
                else:                           # the original steps back, the new picture takes its name
                    number, free = 1, stem
                    while True:
                        target, kept = f"{folder}/{free}.jpg", f"{folder}/{free}_original.{ext}"
                        if not (self.root / kept).exists() and (target == base["path"] or not (self.root / target).exists()):
                            break
                        number += 1
                        free = f"{stem}-{number}"
                    os.rename(src, self.root / kept)
                    try:
                        os.rename(temp, self.root / target)
                    except OSError:
                        os.rename(self.root / kept, src)
                        raise
                    self.journal.repath(base["path"], kept)
                    self.index.moved(base["path"], kept)
            except OSError as exc:
                raise EditError("file is not writable") from exc
            finally:
                raw.unlink(missing_ok=True)
                temp.unlink(missing_ok=True)
            record = self.index.refresh(target)
            self.journal.add(user=user, batch=batch or secrets.token_hex(6), action="develop", path=target,
                             before={"path": base["path"], "recipe": current["recipe"] if original else {}},
                             after={"recipe": recipe, "original": kept}, tags={},
                             sha_after=file_hash(self.root / target))
            return record

    def step_forward(self, original: dict) -> dict:
        """The picture made from this original is gone: the original takes its own name again, if it is free."""
        with self._lock:
            current = self.index.by_path(original["path"])
            folder, _, name = (current or original)["path"].rpartition("/")
            stem, _, ext = name.rpartition(".")
            if not current or not stem.endswith("_original"):
                return current or original
            target = f"{folder}/{stem[:-len('_original')]}.{ext}"
            if (self.root / target).exists():
                return current
            try:
                os.rename(self._source(current["path"]), self.root / target)
            except OSError as exc:
                raise EditError("file is not writable") from exc
            self.journal.repath(current["path"], target)
            return self.index.moved(current["path"], target)

    # ------------------------------------------------------------ taking back

    def undo(self, entry: dict, user: str) -> dict:
        """Take one change back – only while the file is still exactly what that change left."""
        if entry["undone"]:
            raise EditError("already taken back")
        with self._lock:
            src = self._source(entry["path"])
            if file_hash(src) != entry["sha_after"]:
                raise EditError("the file was changed again since then")
            turned = None
            op = 1
            was = self.index.by_path(entry["path"])
            try:
                if entry["action"] == "rotate" and entry["after"].get("heif"):
                    op = INVERSE[entry["after"]["op"]]
                    turned = self._heif_turned(src, entry["after"]["heif"][0], op)
                    final = self._beside(src)
                    try:
                        if entry["tags"]:
                            self._write(turned, final, entry["tags"])
                            self._check_tags(final, entry["tags"])
                            self._same_picture(turned, final)
                        else:
                            shutil.copyfile(turned, final)
                    except Exception:
                        final.unlink(missing_ok=True)
                        raise
                elif entry["action"] == "rotate" and entry["after"].get("lossless"):
                    op = INVERSE[entry["after"]["op"]]
                    turned = self._jpegtran(src, op)
                    if turned is None:
                        raise EditError("cannot be turned back without loss")
                    self._check_turn(src, turned, op)
                    final = self._finish_turn(src, turned, entry["tags"], int(entry["after"].get("thumbnail") or 0),
                                              entry["after"].get("preview", False))
                else:
                    final = self._beside(src)
                    try:
                        self._write(src, final, entry["tags"])
                        self._same_picture(src, final)
                        self._check_tags(final, entry["tags"])
                    except Exception:
                        final.unlink(missing_ok=True)
                        raise
                try:
                    self._commit(final, src)
                finally:
                    final.unlink(missing_ok=True)
            finally:
                if turned:
                    turned.unlink(missing_ok=True)
            self.journal.mark_undone(entry["id"], user)
            record = self.index.refresh(entry["path"])
            if was:         # turned back – or words taken out of a file whose hash covers all of it
                self._tell(was["hash"], record["hash"], op, entry["path"])
            return record
