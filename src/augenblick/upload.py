"""The way in for the phone: a small WebDAV door that leads into one area and nowhere else.

Apps that back up photos (PhotoSync and the like) speak WebDAV. What they get here is the least they
need: look what is there, make a folder, put a file. They cannot read a photo back, change or delete
one, and they never see the rest of the library. The key for this door is its own – it opens nothing
else, so it may live on a phone. Only its hash is kept.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from email.utils import formatdate
from pathlib import Path
from urllib.parse import quote, unquote
from xml.sax.saxutils import escape

import anyio
from fastapi import Request
from fastapi.responses import Response

from .media import file_hash, kind_of

METHODS = ["OPTIONS", "PROPFIND", "MKCOL", "PUT", "HEAD", "GET", "DELETE", "MOVE", "COPY", "PROPPATCH", "LOCK", "UNLOCK"]
ALLOWED = "OPTIONS, PROPFIND, MKCOL, PUT, HEAD"
TRIES = 10              # wrong keys from one address …
PAUSE = 600             # … before it has to wait this many seconds
SETTLE = 20             # seconds after the last upload before the new files are read in
PART = ".augenblick-upload-"


class Inbox:
    """Where uploads go and what opens the door. Kept apart from the other settings: it holds a secret."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()

    def load(self) -> dict:
        try:
            stored = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            stored = {}
        keys = [{"id": str(k.get("id") or ""), "name": str(k.get("name") or ""), "area": str(k.get("area") or ""),
                 "hash": str(k.get("hash") or ""), "made": float(k.get("made") or 0), "last": float(k.get("last") or 0),
                 "count": int(k.get("count") or 0)}
                for k in stored.get("keys") or [] if isinstance(k, dict) and k.get("id") and k.get("hash")]
        if stored.get("hash") and not keys:         # from the time of the one key: it goes on working
            keys = [{"id": "erster", "name": "", "area": "", "hash": str(stored["hash"]),
                     "made": float(stored.get("made") or 0), "last": float(stored.get("last") or 0),
                     "count": int(stored.get("count") or 0)}]
        return {"area": str(stored.get("area") or ""), "keys": keys}

    def save(self, **changes) -> dict:
        with self._lock:
            return self._write({**self.load(), **changes})

    def _write(self, state: dict) -> dict:
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps({"area": state["area"], "keys": state["keys"]}, indent=1), encoding="utf-8")
        os.chmod(temp, 0o600)
        temp.replace(self.path)
        return state

    def new_key(self, name: str = "", area: str = "", key_id: str = "") -> tuple[str, str]:
        """A new key, or a new one in place of the one named by `key_id` – that one stops working at
        once. Each key has a name (whose phone) and may lead into an area of its own instead of the
        usual one. It is shown this once and never again. Hands back (id, key)."""
        key = secrets.token_urlsafe(32)
        digest = hashlib.sha256(key.encode()).hexdigest()
        with self._lock:
            state = self.load()
            found = next((k for k in state["keys"] if k["id"] == key_id), None) if key_id else None
            if key_id and not found:
                raise KeyError(key_id)
            if found:
                found.update(hash=digest, made=time.time())
            else:
                found = {"id": secrets.token_hex(4), "name": " ".join(name.split())[:40], "area": area, "hash": digest,
                         "made": time.time(), "last": 0.0, "count": 0}
                state["keys"].append(found)
            self._write(state)
            return found["id"], key

    def drop_key(self, key_id: str | None = None) -> None:
        """Take one key away – or all of them."""
        with self._lock:
            state = self.load()
            state["keys"] = [k for k in state["keys"] if key_id is not None and k["id"] != key_id]
            self._write(state)

    def change_key(self, key_id: str, name: str | None = None, area: str | None = None) -> None:
        with self._lock:
            state = self.load()
            found = next((k for k in state["keys"] if k["id"] == key_id), None)
            if not found:
                raise KeyError(key_id)
            if name is not None:
                found["name"] = " ".join(name.split())[:40]
            if area is not None:
                found["area"] = area
            self._write(state)

    def opens(self, key: str) -> dict | None:
        """The entry of the key that fits – None if none does."""
        if not key:
            return None
        digest = hashlib.sha256(key.encode()).hexdigest()
        hit = None
        for entry in self.load()["keys"]:           # look at every one, so the time taken tells nothing
            if hmac.compare_digest(digest, entry["hash"]):
                hit = entry
        return hit

    def arrived(self, key_id: str, new: bool) -> None:
        with self._lock:
            state = self.load()
            for entry in state["keys"]:
                if entry["id"] == key_id:
                    entry["last"] = time.time()
                    entry["count"] += 1 if new else 0
            self._write(state)

    def areas(self) -> set[str]:
        """Every area an upload can land in: the usual one and those keys lead into."""
        state = self.load()
        return {area for area in [state["area"], *(k["area"] for k in state["keys"])] if area}

    def public(self) -> dict:
        state = self.load()
        keys = [{k: entry[k] for k in ("id", "name", "area", "made", "last", "count")} for entry in state["keys"]]
        return {"area": state["area"], "keys": keys, "has_key": bool(keys),
                "made": max((k["made"] for k in keys), default=0), "last": max((k["last"] for k in keys), default=0),
                "count": sum(k["count"] for k in keys)}


def _key_of(request: Request) -> str:
    """The key as the app sends it: as the password of a sign-in with any name, or as a bearer token."""
    header = request.headers.get("authorization", "")
    if header.startswith("Bearer "):
        return header[7:].strip()
    if header.startswith("Basic "):
        try:
            return base64.b64decode(header[6:]).decode("utf-8").partition(":")[2]
        except (ValueError, UnicodeDecodeError):
            return ""
    return ""


def _parts(path: str) -> list[str] | None:
    """The folders and the name of a path as the app asked for it – None if it tries to get out
    or to touch what is hidden."""
    parts = [unquote(part) for part in path.split("/") if part]
    for part in parts:
        if part in (".", "..") or part.startswith(".") or len(part) > 200 or part != part.strip():
            return None
        if any(ord(sign) < 32 or sign in '\\:*?"<>|' for sign in part):
            return None
    return parts if len(parts) <= 8 else None


def _entry(href: str, path: Path) -> str:
    stat = path.stat()
    kind = "<D:resourcetype><D:collection/></D:resourcetype>" if path.is_dir() else (
        f"<D:resourcetype/><D:getcontentlength>{stat.st_size}</D:getcontentlength>")
    return (f"<D:response><D:href>{escape(href)}</D:href><D:propstat><D:prop>{kind}"
            f"<D:displayname>{escape(path.name)}</D:displayname>"
            f"<D:getlastmodified>{formatdate(stat.st_mtime, usegmt=True)}</D:getlastmodified>"
            "</D:prop><D:status>HTTP/1.1 200 OK</D:status></D:propstat></D:response>")


def mount(app, inbox: Inbox, root: Path, readonly: bool, client_ip, wake: threading.Event, base: str = "/dav") -> None:
    """Hang the door into the app."""
    misses: dict[str, list[float]] = {}
    settle: list[threading.Timer] = []
    guard = threading.Lock()

    def refuse(request: Request) -> Response | None:
        """None if the key fits; else the answer that sends the caller away."""
        who = client_ip(request)
        now = time.time()
        with guard:
            recent = [at for at in misses.get(who, []) if now - at < PAUSE]
            misses[who] = recent
            if len(recent) >= TRIES:
                return Response(status_code=429, headers={"Retry-After": str(PAUSE)})
            fits = inbox.opens(_key_of(request))
            if fits:
                request.state.upload_key = fits
                return None
            if request.headers.get("authorization"):
                recent.append(now)
        return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="Augenblick! Upload", charset="UTF-8"'})

    def home(key: dict) -> Path | None:
        area = key["area"] or inbox.load()["area"]       # the key's own area, else the usual one
        if not area or "/" in area or area.startswith((".", "@")):
            return None
        folder = root / area
        return folder if folder.is_dir() else None

    def read_in_soon() -> None:
        with guard:
            for timer in settle:
                timer.cancel()
            settle[:] = [threading.Timer(SETTLE, wake.set)]
            settle[0].daemon = True
            settle[0].start()

    def put(target: Path, temp: Path) -> tuple[int, Path]:
        """Put the finished upload in place. The same file again changes nothing; another file of
        the same name gets a number – nothing is ever overwritten."""
        if target.exists():
            if target.is_file() and target.stat().st_size == temp.stat().st_size and file_hash(target) == file_hash(temp):
                temp.unlink()
                return 204, target
            number = 2
            while (free := target.with_name(f"{target.stem}-{number}{target.suffix}")).exists():
                number += 1
            target = free
        os.chmod(temp, 0o664)
        os.rename(temp, target)
        return 201, target

    async def door(request: Request) -> Response:
        method = request.method
        if method == "OPTIONS":
            return Response(status_code=200, headers={"DAV": "1", "Allow": ALLOWED, "MS-Author-Via": "DAV"})
        sent_away = refuse(request)
        if sent_away is not None:
            return sent_away
        folder = home(request.state.upload_key)
        if folder is None:
            return Response(status_code=503, content="no area set for uploads")
        parts = _parts(request.path_params.get("path", ""))
        if parts is None:
            return Response(status_code=403)
        path = folder.joinpath(*parts)
        href = base + "".join("/" + quote(part) for part in parts)

        if method == "PROPFIND":
            if not path.exists():
                return Response(status_code=404)
            entries = [_entry(href + ("/" if path.is_dir() else ""), path)]
            if path.is_dir() and request.headers.get("depth", "1") != "0":
                for child in sorted(path.iterdir(), key=lambda p: p.name.lower()):
                    if child.name.startswith(".") or not (child.is_dir() or child.is_file()):
                        continue
                    entries.append(_entry(href + "/" + quote(child.name) + ("/" if child.is_dir() else ""), child))
            body = '<?xml version="1.0" encoding="utf-8"?><D:multistatus xmlns:D="DAV:">' + "".join(entries) + "</D:multistatus>"
            return Response(status_code=207, content=body, media_type="application/xml; charset=utf-8")

        if method == "HEAD":
            if path.is_file():
                return Response(status_code=200, headers={"Content-Length": str(path.stat().st_size)})
            return Response(status_code=200 if path.is_dir() else 404)

        if method in ("MKCOL", "PUT") and readonly:
            return Response(status_code=403, content="read-only")

        if method == "MKCOL":
            if not parts or path.exists():
                return Response(status_code=405)
            if not path.parent.is_dir():
                return Response(status_code=409)
            path.mkdir()
            os.chmod(path, 0o775)
            return Response(status_code=201)

        if method == "PUT":
            if not parts or path.is_dir():
                return Response(status_code=405)
            if kind_of(parts[-1].rsplit(".", 1)[-1].lower() if "." in parts[-1] else "") is None:
                return Response(status_code=415, content="only photos and videos")
            if not path.parent.is_dir():
                return Response(status_code=409)
            if "content-range" in request.headers:      # a file in pieces: taking one piece for the file would ruin it
                return Response(status_code=400, content="uploads in pieces are not supported: send the file in one go")
            temp = path.with_name(PART + secrets.token_hex(6))
            try:
                async with await anyio.open_file(temp, "wb") as out:
                    async for chunk in request.stream():
                        await out.write(chunk)
                wanted = request.headers.get("content-length")
                if wanted and wanted.isdigit() and int(wanted) != temp.stat().st_size:
                    raise OSError("the upload broke off")
                status, landed = await anyio.to_thread.run_sync(put, path, temp)
            except BaseException:               # also when the phone hangs up half way: leave no half file
                temp.unlink(missing_ok=True)
                raise
            inbox.arrived(request.state.upload_key["id"], status == 201)
            if status == 201:
                read_in_soon()
            headers = {"Location": (href.rsplit("/", 1)[0] + "/" + quote(landed.name))} if landed != path else {}
            return Response(status_code=status, headers=headers)

        # reading a photo back, changing or deleting one: not through this door
        return Response(status_code=403 if method in ("GET", "DELETE", "MOVE", "COPY", "PROPPATCH") else 405,
                        headers={"Allow": ALLOWED})

    app.add_route(base, door, methods=METHODS, include_in_schema=False)
    app.add_route(base + "/{path:path}", door, methods=METHODS, include_in_schema=False)
