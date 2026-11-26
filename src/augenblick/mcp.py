"""MCP endpoint: the photo library as tools for an AI assistant – as in Wissen!.

A thin layer over the REST API, spoken as JSON-RPC over HTTP (the "streamable HTTP"
transport of the Model Context Protocol, answered with plain JSON). The API key from
*Settings → Connections* goes along as ``Authorization: Bearer <key>``, exactly as for the
REST API; what a key may not do there it may not do here either. The endpoint is off
until an administrator switches it on (Settings → Connections).

Every tool calls the same functions the REST routes use, so rights, the journal, the
trash and what is written into the files behave the same. Nothing here configures the
installation, and nothing removes a photo for good: the trash is as far as it goes.
"""

from __future__ import annotations

import base64
import json
import secrets
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from . import __version__
from .auth import User, current_user, request_lang

PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")

INSTRUCTIONS = (
    "Augenblick! is a folder-based photo library. Photos and videos live in areas (top folders) and folders "
    "below them. Date, place, description, keywords, rating, photographer, albums and the names on faces are "
    "written into the files themselves; an album exists as soon as one photo carries it. Every photo has a "
    "short key; use it for every later call. Find photos with search_photos (newest first), read one with "
    "get_photo and look at it with view_photo (a small preview, never the original). Every change stands in "
    "the journal and can be taken back with undo_change. trash_photos only moves photos to the trash, from "
    "where restore_from_trash brings them back; nothing is deleted for good through this access."
)

PHOTO_FIELDS = ("key", "name", "path", "area", "folder", "kind", "ext", "size", "width", "height", "taken", "taken_src",
                "lat", "lon", "city", "state", "country", "make", "model", "creator", "description", "keywords",
                "rating", "albums", "people", "error", "film", "original", "inbox", "proposal", "can_change",
                "can_turn", "can_delete")


class ToolError(Exception):
    pass


def _text(value: Any) -> dict:
    return {"type": "text", "text": value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=1)}


def _schema(properties: dict, required: tuple[str, ...] = ()) -> dict:
    return {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}


def _brief(photo: dict) -> dict:
    """A photo in a few words: what an assistant needs to tell it apart and go on."""
    out = {"key": photo["key"], "path": photo.get("path"), "taken": photo.get("taken")}
    for field in ("description", "keywords", "albums", "people", "rating", "city", "country"):
        if photo.get(field):
            out[field] = photo[field]
    return out


def _moment(text: str, end: bool) -> str:
    """A day, a month or a year as the index writes times; for the end, the first moment after it."""
    text = (text or "").strip()
    if not text:
        return ""
    try:
        if len(text) == 4:
            start, after = date(int(text), 1, 1), date(int(text) + 1, 1, 1)
        elif len(text) == 7:
            year, month = int(text[:4]), int(text[5:7])
            start = date(year, month, 1)
            after = date(year + month // 12, month % 12 + 1, 1)
        elif len(text) == 10:
            start = date.fromisoformat(text)
            after = start + timedelta(days=1)
        else:
            moment = datetime.fromisoformat(text)
            return moment.strftime("%Y-%m-%dT%H:%M:%S")
    except ValueError as exc:
        raise ToolError(f"not a date: {text} (use YYYY, YYYY-MM, YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS)") from exc
    return (after if end else start).strftime("%Y-%m-%dT00:00:00")


_KEY = {"type": "string", "description": "The photo's key, as search_photos returns it."}
_KEYS = {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 100,
         "description": "Keys of the photos (at most 100)."}
_AREA = {"type": "string", "description": "Only this area (see list_areas); empty: every area."}
_ENTRY = {"type": "integer"}
_WORDS = {"type": "array", "items": {"type": "string"}}


def build_tools(ops: SimpleNamespace) -> dict[str, dict]:
    """The tools by name: description, input schema, whether it writes, and the function behind it."""
    from .app import Change, FaceName, InboxAccept, Many, ManyChange, ManyMove, ManyTurn

    # ------------------------------------------------------------- reading

    def list_areas(caller):
        return ops.areas(user=caller.user)

    def list_folders(caller, area: str = ""):
        return [{"folder": row["path"], "photos": row["count"], "with_subfolders": row["total"]}
                for row in ops.tree(areas=area or None, user=caller.user)]

    def list_albums(caller, area: str = ""):
        return [{"album": row["path"], "photos": row["count"], "with_subalbums": row["total"], "areas": row["areas"]}
                for row in ops.albums(areas=area or None, user=caller.user)]

    def list_people(caller, area: str = ""):
        return [{"name": row["name"], "photos": row["count"]} for row in ops.people_list(areas=area or None, user=caller.user)]

    def list_places(caller, area: str = ""):
        return ops.place_names(areas=area or None, user=caller.user)

    def list_cameras(caller, area: str = ""):
        return {"cameras": ops.cameras(areas=area or None, user=caller.user),
                "photographers": ops.creators(areas=area or None, user=caller.user)}

    def chosen(query: str = "", folder: str = "", recursive: bool = True, album: str = "", person: str = "",
               country: str = "", city: str = "", camera: str = "", photographer: str = "", since: str = "",
               until: str = "", kind: str = "", favorites: bool = False, without_place: bool = False,
               without_date: bool = False) -> dict:
        if kind not in ("", "image", "video"):
            raise ToolError("kind must be \"image\" or \"video\"")
        return ops.filters(folder=folder[:2000], recursive=bool(recursive), q=query[:200], camera=camera[:200],
                           creator=photographer[:100], no_place=bool(without_place), no_date=bool(without_date),
                           kind=kind, misfit=False, album=album[:2000], person=person[:100], country=country[:200],
                           city=city[:200], auto="favoriten" if favorites else "", since=_moment(since, False),
                           until=_moment(until, True), keys="")

    def search_photos(caller, area: str = "", limit: int = 50, offset: int = 0, oldest_first: bool = False, **wanted):
        rows = ops.sequence(areas=area or None, chosen=chosen(**wanted), user=caller.user)
        if not oldest_first:
            rows.reverse()
        offset = max(0, int(offset))
        shown = rows[offset:offset + max(1, min(int(limit), 500))]
        return {"total": len(rows), "offset": offset, "photos": [
            {"key": r["key"], "kind": r["kind"], "taken": r["taken"],
             **({"place": ", ".join(p for p in (r["city"], r["country"]) if p)} if r["city"] or r["country"] else {}),
             **({"description": r["desc"]} if r["desc"] else {})} for r in shown]}

    def timeline(caller, area: str = "", **wanted):
        return ops.timeline(areas=area or None, chosen=chosen(**wanted), user=caller.user)

    def get_photo(caller, key: str):
        data = ops.photo(key, user=caller.user)
        return {field: data.get(field) for field in PHOTO_FIELDS if field in data}

    def view_photo(caller, key: str):
        data = ops.photo(key, user=caller.user)
        picture = Path(ops.photo_thumb(key, user=caller.user).path).read_bytes()
        return [_text(_brief(data)),
                {"type": "image", "data": base64.b64encode(picture).decode(), "mimeType": "image/jpeg"}]

    def get_photo_faces(caller, key: str):
        return [{**{k: round(face[k], 4) for k in "xywh"}, "name": face["name"],
                 **({"proposal": face["proposal"]} if face.get("proposal") else {}),
                 **({"ignored": True} if face.get("ignored") else {}), "marked_in_file": face["marked"]}
                for face in ops.photo_faces(key, user=caller.user)]

    def list_memories(caller, area: str = ""):
        return [{k: m[k] for k in ("id", "title", "kind", "since", "until", "days", "count", "text", "picks") if m.get(k)}
                for m in ops.list_memories(areas=area or None, user=caller.user, lang=caller.lang)]

    def list_inbox(caller, limit: int = 50):
        areas = [a["name"] for a in ops.areas(user=caller.user) if a["inbox"]]
        if not areas:
            return {"inbox_areas": [], "total": 0, "photos": []}
        rows = ops.sequence(areas=",".join(areas), chosen=chosen(), user=caller.user)
        rows.reverse()
        photos = []
        for row in rows[:max(1, min(int(limit), 200))]:
            data = ops.photo(row["key"], user=caller.user)
            photos.append({**_brief(data), "area": data["area"], "proposal": data.get("proposal")})
        return {"inbox_areas": areas, "total": len(rows), "photos": photos}

    def list_trash(caller):
        return ops.trash_list(user=caller.user)

    def list_journal(caller, limit: int = 50, before: int | None = None):
        return ops.journal_list(limit=max(1, min(int(limit), 500)), before=before, user=caller.user)

    # ------------------------------------------------------------- writing

    def results(rows: list[dict]) -> list[dict]:
        return [{"key": r["key"], "ok": True, **_brief(r["photo"])} if r["ok"] else r for r in rows]

    def change(caller, keys: list[str], what: dict) -> list[dict]:
        if not what:
            raise ToolError("nothing to change: give at least one field")
        body = ManyChange(keys=keys, change=Change(**what), batch=secrets.token_hex(6))
        return results(ops.change_many(body, user=caller.user))

    def update_photos(caller, keys: list[str], photographer: str | None = None, shift_seconds: int | None = None,
                      **what):
        if photographer is not None:
            what["creator"] = photographer
        if shift_seconds is not None:
            what["shift"] = shift_seconds
        return change(caller, keys, {k: v for k, v in what.items() if v is not None and v is not False})

    def add_to_album(caller, keys: list[str], album: str):
        return change(caller, keys, {"add_albums": [album.strip("/")]})

    def remove_from_album(caller, keys: list[str], album: str):
        return change(caller, keys, {"remove_albums": [album.strip("/")]})

    def rotate_photos(caller, keys: list[str], degrees: int):
        return results(ops.turn_many(ManyTurn(keys=keys, degrees=degrees, batch=secrets.token_hex(6)), user=caller.user))

    def convert_to_jpeg(caller, keys: list[str]):
        return results(ops.jpeg_many(Many(keys=keys, batch=secrets.token_hex(6)), user=caller.user))

    def name_face(caller, key: str, x: float, y: float, w: float, h: float, name: str = ""):
        data = ops.name_face(key, FaceName(x=x, y=y, w=w, h=h, name=name), user=caller.user)
        return {**_brief(data), "people": data.get("people", [])}

    def move_photos(caller, keys: list[str], folder: str):
        return results(ops.move_many(ManyMove(keys=keys, folder=folder, batch=secrets.token_hex(6)), user=caller.user))

    def accept_inbox(caller, keys: list[str], folder: str, take: bool = True, description: str | None = None,
                     keywords: list[str] | None = None):
        body = InboxAccept(keys=keys, folder=folder, take=take, description=description, keywords=keywords,
                           batch=secrets.token_hex(6))
        return results(ops.inbox_accept(body, user=caller.user))

    def trash_photos(caller, keys: list[str]):
        return ops.delete_many(Many(keys=keys, batch=secrets.token_hex(6)), user=caller.user)

    def restore_from_trash(caller, entry: int):
        return _brief(ops.trash_restore(int(entry), user=caller.user))

    def undo_change(caller, entry: int):
        return _brief(ops.journal_undo(int(entry), user=caller.user))

    def tool(fn: Callable, title: str, description: str, schema: dict, write: bool = False, destructive: bool = False) -> dict:
        return {"fn": fn, "title": title, "description": description, "schema": schema, "write": write,
                "destructive": destructive}

    filters = {
        "query": {"type": "string", "description": "Words that must all occur in file name, folder, description, "
                                                   "keywords, place or photographer."},
        "folder": {"type": "string", "description": "Folder with its area in front, e.g. \"Familie/2024\"."},
        "recursive": {"type": "boolean", "description": "Include subfolders of the folder (default true)."},
        "album": {"type": "string", "description": "Album path, e.g. \"Urlaub/USA 2010\"; albums below it count too."},
        "person": {"type": "string", "description": "A name on a face (see list_people)."},
        "country": {"type": "string"}, "city": {"type": "string"},
        "camera": {"type": "string", "description": "Camera model exactly as list_cameras names it."},
        "photographer": {"type": "string", "description": "Who took it, exactly as list_cameras names it."},
        "since": {"type": "string", "description": "Taken on or after: YYYY, YYYY-MM, YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS."},
        "until": {"type": "string", "description": "Taken up to and including this year, month or day."},
        "kind": {"type": "string", "enum": ["image", "video"]},
        "favorites": {"type": "boolean", "description": "Only photos rated 4 or 5 stars."},
        "without_place": {"type": "boolean", "description": "Only photos that do not know where they were taken."},
        "without_date": {"type": "boolean", "description": "Only photos whose date is not from the camera."},
    }
    edits = {
        "taken": {"type": "string", "description": "New date and time, local: YYYY-MM-DDTHH:MM or YYYY-MM-DDTHH:MM:SS."},
        "shift_seconds": {"type": "integer", "description": "Move the date by so many seconds (negative: earlier)."},
        "lat": {"type": "number", "minimum": -90, "maximum": 90}, "lon": {"type": "number", "minimum": -180, "maximum": 180},
        "clear_place": {"type": "boolean", "description": "Take the place away."},
        "description": {"type": "string", "description": "New description; \"\" takes it away."},
        "keywords": {**_WORDS, "description": "Replace all keywords with these."},
        "add_keywords": _WORDS, "remove_keywords": _WORDS,
        "rating": {"type": "integer", "minimum": 0, "maximum": 5, "description": "Stars; 0 takes the rating away."},
        "photographer": {"type": "string", "description": "Who took the picture; \"\" takes the name away."},
        "albums": {**_WORDS, "description": "Replace all albums with these album paths."},
        "add_albums": _WORDS, "remove_albums": _WORDS,
    }
    return {
        "list_areas": tool(list_areas, "List areas",
                           "The areas (top folders) with their number of photos; inbox: true marks where phones upload to.",
                           _schema({})),
        "list_folders": tool(list_folders, "List folders", "Every folder with its photos, optionally of one area only.",
                             _schema({"area": _AREA})),
        "list_albums": tool(list_albums, "List albums", "Every album with how many photos it holds.", _schema({"area": _AREA})),
        "list_people": tool(list_people, "List people", "Everyone named on a face, with the number of photos.",
                            _schema({"area": _AREA})),
        "list_places": tool(list_places, "List places", "Countries and towns the photos were taken in, with counts.",
                            _schema({"area": _AREA})),
        "list_cameras": tool(list_cameras, "List cameras and photographers",
                             "The camera models and the photographers named in the files, with counts.", _schema({"area": _AREA})),
        "search_photos": tool(
            search_photos, "Search photos",
            "Find photos and videos by words, folder, album, person, place, camera, photographer, time span, kind or "
            "favourites; all given filters must hold. Newest first. Returns key, date, place and description.",
            _schema({"area": _AREA, **filters,
                     "oldest_first": {"type": "boolean"},
                     "limit": {"type": "integer", "minimum": 1, "maximum": 500, "description": "Default 50."},
                     "offset": {"type": "integer", "minimum": 0}})),
        "timeline": tool(timeline, "Photos per month", "How many photos each month has, newest first – with the same filters as search_photos.",
                         _schema({"area": _AREA, **filters})),
        "get_photo": tool(get_photo, "Read a photo's details",
                          "Everything known about one photo: file, date, place, camera, description, keywords, rating, "
                          "albums, people, and what may be changed.", _schema({"key": _KEY}, ("key",))),
        "view_photo": tool(view_photo, "Look at a photo", "A small preview of the photo (a still for a video) as an image.",
                           _schema({"key": _KEY}, ("key",))),
        "get_photo_faces": tool(
            get_photo_faces, "Faces on a photo",
            "The faces on a photo with their boxes (centre x, y and width w, height h as fractions of the upright "
            "picture), the name if there is one and a proposed name where one looks like somebody named already.",
            _schema({"key": _KEY}, ("key",))),
        "list_memories": tool(list_memories, "List memories",
                              "Journeys and looks back found in the photos: a time span, a title and the keys of a few photos that stand for it.",
                              _schema({"area": _AREA})),
        "list_inbox": tool(list_inbox, "List the inbox",
                           "Photos that came from a phone and are not sorted in yet, with what the AI proposed for them.",
                           _schema({"limit": {"type": "integer", "minimum": 1, "maximum": 200, "description": "Default 50."}})),
        "list_trash": tool(list_trash, "List the trash", "What lies in the trash, newest first, and after how many days it goes for good.",
                           _schema({})),
        "list_journal": tool(list_journal, "List changes",
                             "What was changed in the photos, newest first; each entry has an id for undo_change.",
                             _schema({"limit": {"type": "integer", "minimum": 1, "maximum": 500, "description": "Default 50."},
                                      "before": {"type": "integer", "description": "Only entries older than this id."}})),
        "update_photos": tool(
            update_photos, "Change photos",
            "Write date, place, description, keywords, rating, photographer or albums into one or more photos "
            "(the files themselves). Fields left out stay as they are. Each change can be taken back.",
            _schema({"keys": _KEYS, **edits}, ("keys",)), write=True),
        "add_to_album": tool(add_to_album, "Add photos to an album",
                             "Put photos into an album; a new album name makes the album.",
                             _schema({"keys": _KEYS, "album": {"type": "string", "description": "e.g. \"Urlaub/USA 2010\""}},
                                     ("keys", "album")), write=True),
        "remove_from_album": tool(remove_from_album, "Take photos out of an album", "The photos themselves stay.",
                                  _schema({"keys": _KEYS, "album": {"type": "string"}}, ("keys", "album")), write=True),
        "rotate_photos": tool(rotate_photos, "Rotate photos", "Turn photos clockwise, without loss where the picture allows it.",
                              _schema({"keys": _KEYS, "degrees": {"type": "integer", "enum": [90, 180, 270]}},
                                      ("keys", "degrees")), write=True),
        "convert_to_jpeg": tool(
            convert_to_jpeg, "Convert PNG to JPEG",
            "Make PNG pictures (screenshots and the like) into JPEGs of the same name: date, place, description, "
            "keywords, albums and faces come along, the file gets much smaller. The PNG goes to the trash; "
            "undo_change brings it back. Pictures with transparent parts stay PNG. Each photo gets a new key.",
            _schema({"keys": _KEYS}, ("keys",)), write=True, destructive=True),
        "name_face": tool(
            name_face, "Name a face",
            "Write a name on a face of a photo, with the box from get_photo_faces; a new box marks a face by hand. "
            "An empty name takes the name away.",
            _schema({"key": _KEY, "x": {"type": "number"}, "y": {"type": "number"}, "w": {"type": "number"},
                     "h": {"type": "number"}, "name": {"type": "string"}}, ("key", "x", "y", "w", "h")), write=True),
        "move_photos": tool(
            move_photos, "Move photos",
            "Move photos into another folder or area. Give the folder with its area in front (\"Familie/2024\"), or "
            "only an area to let its sorting rules decide.",
            _schema({"keys": _KEYS, "folder": {"type": "string"}}, ("keys", "folder")), write=True),
        "accept_inbox": tool(
            accept_inbox, "Sort in from the inbox",
            "Take photos out of the inbox into another area or folder; with take (default) the AI's proposal "
            "(description where there is none, keywords added) is written first. For a single photo, description "
            "and keywords can be given instead.",
            _schema({"keys": _KEYS, "folder": {"type": "string", "description": "Area or folder outside the inbox."},
                     "take": {"type": "boolean"}, "description": {"type": "string"}, "keywords": _WORDS},
                    ("keys", "folder")), write=True),
        "trash_photos": tool(trash_photos, "Move photos to the trash",
                             "Moves photos into the trash of their area; restore_from_trash brings them back until the trash is emptied.",
                             _schema({"keys": _KEYS}, ("keys",)), write=True, destructive=True),
        "restore_from_trash": tool(restore_from_trash, "Restore from the trash", "Put a photo from the trash back where it was.",
                                   _schema({"entry": {**_ENTRY, "description": "id from list_trash or trash_photos."}},
                                           ("entry",)), write=True),
        "undo_change": tool(undo_change, "Take a change back",
                            "Undo one entry of the journal – possible while the file is still what that change left behind.",
                            _schema({"entry": {**_ENTRY, "description": "id from list_journal."}}, ("entry",)), write=True),
    }


def mount_mcp(app: FastAPI, ops: SimpleNamespace) -> None:
    tools = build_tools(ops)

    def can_write(user: User) -> bool:
        return bool(ops.writable_areas(user))

    def listing(user: User) -> list[dict]:
        writer = can_write(user)
        return [
            {"name": name, "title": t["title"], "description": t["description"], "inputSchema": t["schema"],
             "annotations": {"title": t["title"], "readOnlyHint": not t["write"], "destructiveHint": t["destructive"],
                             "idempotentHint": False, "openWorldHint": False}}
            for name, t in tools.items() if writer or not t["write"]      # a reading key is not offered what it may not do
        ]

    def call(caller: SimpleNamespace, name: str, arguments: dict) -> dict:
        spec = tools.get(name)
        if not spec:
            raise ValueError(f"unknown tool: {name}")
        allowed = spec["schema"]["properties"]
        try:
            if spec["write"] and not can_write(caller.user):
                raise ToolError("this key may only read")
            unknown = sorted(set(arguments) - set(allowed))
            if unknown:
                raise ToolError(f"unknown argument: {', '.join(unknown)}")
            missing = [key for key in spec["schema"]["required"] if arguments.get(key) in (None, "")]
            if missing:
                raise ToolError(f"missing argument: {', '.join(missing)}")
            result = spec["fn"](caller, **arguments)
        except ToolError as exc:
            return {"content": [_text(str(exc))], "isError": True}
        except HTTPException as exc:
            return {"content": [_text(f"{exc.detail} (status {exc.status_code})")], "isError": True}
        except (TypeError, ValueError) as exc:        # wrong kind of argument, or one the models refuse
            return {"content": [_text(f"invalid arguments: {exc}")], "isError": True}
        content = result if isinstance(result, list) and result and isinstance(result[0], dict) and "type" in result[0] \
            else [_text(result)]
        return {"content": content, "isError": False}

    def answer(caller: SimpleNamespace, message: Any) -> dict | None:
        """One JSON-RPC message in, its answer out (None for a notification)."""
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
            return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "invalid request"}}
        method, params, msg_id = message["method"], message.get("params") or {}, message.get("id")
        if "id" not in message:
            return None
        try:
            if method == "initialize":
                wanted = params.get("protocolVersion")
                result = {"protocolVersion": wanted if wanted in PROTOCOLS else PROTOCOLS[0],
                          "capabilities": {"tools": {"listChanged": False}},
                          "serverInfo": {"name": "augenblick", "title": "Augenblick!", "version": __version__},
                          "instructions": INSTRUCTIONS}
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": listing(caller.user)}
            elif method == "tools/call":
                arguments = params.get("arguments") or {}
                if not isinstance(params.get("name"), str) or not isinstance(arguments, dict):
                    raise ValueError("tools/call needs a name and an arguments object")
                result = call(caller, params["name"], arguments)
            else:
                return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32601, "message": f"method not found: {method}"}}
        except ValueError as exc:
            return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32602, "message": str(exc)}}
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    def answers(caller: SimpleNamespace, body: Any) -> Response:
        if isinstance(body, list):
            replies = [a for a in (answer(caller, m) for m in body) if a is not None]
            return JSONResponse(replies) if replies else Response(status_code=202)
        reply = answer(caller, body)
        return JSONResponse(reply) if reply is not None else Response(status_code=202)

    @app.post("/api/mcp", tags=["mcp"])
    async def mcp(request: Request, user: User = Depends(current_user)):
        """Model Context Protocol over HTTP: JSON-RPC in, JSON out. Connect an assistant to this address.

        Off in a fresh installation; an administrator turns it on under Settings → Connections.
        """
        if not app.state.prefs.load()["mcp"]:
            return JSONResponse({"jsonrpc": "2.0", "id": None, "error": {
                "code": -32000, "message": "MCP is switched off in this installation (Settings → Connections)"}}, status_code=403)
        try:
            body = json.loads(await request.body())
        except ValueError:
            return JSONResponse({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}, status_code=400)
        # the tools read and write files: off the event loop, like the plain routes
        return await run_in_threadpool(answers, SimpleNamespace(user=user, lang=request_lang(request)), body)

    @app.get("/api/mcp", tags=["mcp"], include_in_schema=False)
    @app.delete("/api/mcp", tags=["mcp"], include_in_schema=False)
    def mcp_no_stream(user: User = Depends(current_user)):
        # no server-initiated messages and no sessions: there is nothing to listen to or to end
        return Response(status_code=405, headers={"Allow": "POST"})
