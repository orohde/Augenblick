"""Taking over the people somebody has named in Immich – once, and only by reading there.

Immich keeps who is on a photo in its own database. Here it belongs into the photo itself. So
this asks Immich which named people it knows and on which pictures they are, finds the same
pictures in the folders here, and hands the faces to the editor, which writes them as regions
with all its checks. Nothing is ever changed in Immich.
"""

from __future__ import annotations

import base64
import hashlib
import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .edit import INVERSE, SAME_FACE, _overlap
from .media import clean_name, is_face, turn_box

PAGE = 250              # pictures asked for at once
SAME_NAME = 8           # so many files of the same name are hashed at most to find the right one


class ImmichError(Exception):
    pass


class Immich:
    """The few questions asked of an Immich server, with an API key that may read people, faces and assets."""

    def __init__(self, url: str, key: str):
        url = (url or "").strip().rstrip("/")
        if urllib.parse.urlsplit(url).scheme not in ("http", "https") or not urllib.parse.urlsplit(url).netloc:
            raise ImmichError("invalid address")
        if not (key or "").strip():
            raise ImmichError("api key missing")
        self.base = url if url.endswith("/api") else url + "/api"
        self.key = key.strip()

    def call(self, method: str, path: str, body: dict | None = None):
        request = urllib.request.Request(self.base + path, method=method,
                                         data=json.dumps(body).encode() if body is not None else None)
        request.add_header("x-api-key", self.key)
        request.add_header("Accept", "application/json")
        if body is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=60) as answer:
                return json.loads(answer.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise ImmichError("immich refused the key") from exc
            raise ImmichError(f"immich answered {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            raise ImmichError("immich not reachable") from exc

    def people(self) -> list[dict]:
        """Everybody with a name who is not hidden."""
        found, page = [], 1
        while page <= 400:
            answer = self.call("GET", f"/people?page={page}&size=500&withHidden=false")
            for person in answer.get("people") or []:
                name = clean_name(person.get("name"))
                if name and not person.get("isHidden"):
                    found.append({"id": person["id"], "name": name})
            if not answer.get("hasNextPage"):
                break
            page += 1
        return found

    def assets(self, person: str):
        """The pictures somebody is on, page by page (older servers count pages, newer ones hand a cursor)."""
        body: dict = {"personIds": [person], "size": PAGE, "page": 1}
        for _ in range(4000):
            assets = (self.call("POST", "/search/metadata", body).get("assets")) or {}
            for asset in assets.get("items") or []:
                if asset.get("type") == "IMAGE" and not asset.get("isTrashed"):
                    yield asset
            if assets.get("nextCursor"):
                body = {"personIds": [person], "size": PAGE, "cursor": assets["nextCursor"]}
            elif assets.get("nextPage"):
                body = {"personIds": [person], "size": PAGE, "page": int(assets["nextPage"])}
            else:
                return

    def faces(self, asset: str) -> list[dict]:
        return self.call("GET", "/faces?id=" + urllib.parse.quote(asset))


def _match(asset: dict, by_name: dict[str, list[dict]], root: Path, hashes: dict[str, str]) -> dict | None:
    """The photo here that is this picture of Immich's: by its path, else by the very same file,
    else – if only one comes into question – by name and the moment it was taken."""
    there = (asset.get("originalPath") or "").replace("\\", "/")
    names = {(asset.get("originalFileName") or "").lower(), there.rsplit("/", 1)[-1].lower()} - {""}
    candidates = [photo for name in sorted(names) for photo in by_name.get(name, [])]
    if not candidates:
        return None
    on_path = [photo for photo in candidates if there == photo["path"] or there.endswith("/" + photo["path"])]
    if on_path:
        return max(on_path, key=lambda photo: len(photo["path"]))
    if asset.get("checksum"):
        for photo in candidates[:SAME_NAME]:
            if photo["path"] not in hashes:
                try:
                    digest = hashlib.sha1((root / photo["path"]).read_bytes()).digest()
                except OSError:
                    digest = b""
                hashes[photo["path"]] = base64.b64encode(digest).decode()
            if hashes[photo["path"]] == asset["checksum"]:
                return photo
    moment = (asset.get("localDateTime") or "")[:19]
    at_time = [photo for photo in candidates if moment and photo["taken"][:19] == moment]
    return at_time[0] if len(at_time) == 1 else None


def plan(immich: Immich, photos: list[dict], root: Path) -> dict:
    """What an import would do, without doing it: for everybody named in Immich how many pictures
    there are, how many of them were found here and how many already carry the name."""
    by_name: dict[str, list[dict]] = {}
    for photo in photos:
        by_name.setdefault(photo["name"].lower(), []).append(photo)
    hashes: dict[str, str] = {}
    people, items, missed = [], {}, []
    for person in immich.people():
        row = {"name": person["name"], "immich": 0, "found": 0, "named": 0, "todo": 0}
        for asset in immich.assets(person["id"]):
            row["immich"] += 1
            photo = _match(asset, by_name, root, hashes)
            if photo is None:
                if len(missed) < 20:
                    missed.append(asset.get("originalPath") or asset.get("originalFileName") or "")
                continue
            row["found"] += 1
            if person["name"] in photo["people"]:
                row["named"] += 1
                continue
            row["todo"] += 1
            item = items.setdefault(asset["id"], {"asset": asset["id"], "path": photo["path"], "names": []})
            item["names"].append(person["name"])
        people.append(row)
    merged: dict[str, dict] = {}        # two people of the same name in Immich are one here
    for row in people:
        if row["name"] in merged:
            for key in ("immich", "found", "named", "todo"):
                merged[row["name"]][key] += row[key]
        else:
            merged[row["name"]] = row
    return {"people": sorted(merged.values(), key=lambda row: row["name"].lower()), "items": list(items.values()),
            "missed": missed}


def edits_for(faces: list[dict], photo: dict, names: set[str]) -> list[dict]:
    """Immich's faces on one picture as the editor wants them: boxes as fractions of the upright
    picture, with the name. Faces whose place here already carries another name are left alone."""
    stored = [(region["x"], region["y"], region["w"], region["h"], region["name"])
              for region in photo["regions"] if is_face(region["type"])]
    orientation = photo["orientation"] or 1
    edits = []
    for face in faces:
        name = clean_name((face.get("person") or {}).get("name"))
        if not name or name not in names:
            continue
        wide, high = face.get("imageWidth") or 0, face.get("imageHeight") or 0
        if wide <= 0 or high <= 0:
            continue
        if photo["width"] and photo["height"] and (wide > high) != (photo["width"] > photo["height"]) \
                and wide != high and photo["width"] != photo["height"]:
            raise ImmichError("immich saw this picture turned another way")
        left, right = sorted((max(0, face["boundingBoxX1"]) / wide, min(wide, face["boundingBoxX2"]) / wide))
        top, bottom = sorted((max(0, face["boundingBoxY1"]) / high, min(high, face["boundingBoxY2"]) / high))
        if right - left <= 0 or bottom - top <= 0:
            continue
        box = ((left + right) / 2, (top + bottom) / 2, right - left, bottom - top)
        in_file = turn_box(box, INVERSE[orientation])
        if any(other and other != name and _overlap(in_file, (x, y, w, h)) >= SAME_FACE for x, y, w, h, other in stored):
            continue
        edits.append(dict(zip("xywh", box), name=name))
    return edits
