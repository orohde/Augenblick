"""FastAPI application: REST API and web UI."""

from __future__ import annotations

import io
import json
import logging
import os
import re
import shutil
import secrets
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image
from pydantic import BaseModel, Field

from . import __version__
from .ai import (MEMORY_SYSTEM, MODEL_LABELS, PHOTO_SYSTEM, PRICES, AIError, AIService, in_language, memory_prompt, parse_memory, parse_suggestion,
                 photo_prompt, prompts_of)
from .auth import (User, client_ip, cookie_name, current_user, is_external, is_https, require_admin, request_lang,
                   session_token)
from .config import Settings, load_settings
from .decisions import Decisions
from .dupes import Dupes
from .edit import EditError, Editor
from .faces import Engine, People
from .geo import Geo
from .immich import Immich, ImmichError, edits_for, plan as immich_plan
from .index import Index
from .journal import Journal
from .lang import known as known_lang
from .proposals import Proposals
from .sort import MODES, TOKENS, Rules, SortError, Sorter, clean_rules
from .trash import Prefs, Trash, TrashError
from .upload import Inbox, mount as mount_upload
from .memories import Memories
from .media import (MEDIA_TYPES, RAW_EXT, clean_recipe, develop, PREVIEW_EDGE, THUMB_EDGE, cache_path, clean_name, is_face, read_image, render, store,
                    turn_box)
from .security import (DEFAULT_INTERNAL, LoginGuard, Networks, Notifier, SecurityStore, SessionStore, describe_agent,
                       normalise_networks, otpauth_uri, qr_data_uri)
from .prefs import LANGUAGES, PrefsStore
from .users import UserError, UserStore

log = logging.getLogger("augenblick")
STATIC = Path(__file__).parent / "static"
MONTH_RE = re.compile(r"^\d{4}-\d{2}$")
IMMUTABLE = {"Cache-Control": "private, max-age=31536000, immutable"}


# Where people who like the programme can support the work. Empty: the settings show no such line.
SUPPORT_URL = "https://worldgate.de/unterstuetzen/"


AREA_RE = re.compile(r"[A-Za-z0-9ÄÖÜäöüß][A-Za-z0-9ÄÖÜäöüß ._-]{0,39}")


class AreaIn(BaseModel):
    name: str = Field(max_length=40)
    places: bool = True     # false: the pictures need no place (no place names, not listed as "without a place")
    dates: bool = True      # false: they need no date from the camera either


class AreaOptions(BaseModel):
    places: bool = True
    dates: bool = True


class UploadIn(BaseModel):
    area: str = Field(max_length=40, description="the area uploads from the phone land in; empty = none")


class UploadKeyIn(BaseModel):
    name: str | None = Field(None, max_length=40, description="whose phone this key is for")
    creator: str | None = Field(None, max_length=100, description="who takes the pictures; written into each as it arrives")
    area: str | None = Field(None, max_length=40, description="the area it uploads into; '' = the usual one")


class TreePrefs(BaseModel):
    tree_open: list[str] | None = Field(None, max_length=5000)
    language: str | None = Field(None, max_length=5)


class SetupIn(BaseModel):
    username: str = Field(max_length=100)
    password: str = Field(max_length=1000)
    display_name: str = Field("", max_length=100)


class Login(BaseModel):
    username: str = Field(max_length=100)
    password: str = Field(max_length=1000)
    code: str | None = Field(None, max_length=20, description="second factor: app code or recovery code")


class PasswordChange(BaseModel):
    old: str = Field(max_length=1000)
    new: str = Field(max_length=1000)


class PasswordConfirm(BaseModel):
    password: str = Field(max_length=1000)


class CodeIn(BaseModel):
    code: str = Field(max_length=20)


class TotpOff(BaseModel):
    password: str = Field(max_length=1000)
    code: str = Field(max_length=20)


class SecurityChange(BaseModel):
    internal_networks: str | None = Field(None, max_length=2000)
    require_2fa_external: bool | None = None
    require_2fa_internal: bool | None = None
    ntfy_url: str | None = Field(None, max_length=500)
    ntfy_token: str | None = Field(None, max_length=200)
    clear_ntfy_token: bool = False
    notify_external_login: bool | None = None
    notify_lockout: bool | None = None
    notify_2fa_changes: bool | None = None


class NewUser(BaseModel):
    name: str = Field(max_length=100)
    password: str = Field(max_length=1000)
    display_name: str = Field("", max_length=100)
    is_admin: bool = False
    areas: dict[str, str] = Field(default_factory=dict)


class UserChange(BaseModel):
    display_name: str | None = Field(None, max_length=100)
    password: str | None = Field(None, max_length=1000)
    is_admin: bool | None = None
    areas: dict[str, str] | None = None
    reset_totp: bool = False
    sign_out: bool = False


class Change(BaseModel):
    """What to write into a photo. Fields left out stay as they are."""
    taken: str | None = Field(None, pattern=r"^\d{4}-\d\d-\d\dT\d\d:\d\d(:\d\d)?$", description="local time")
    shift: int | None = Field(None, ge=-3_200_000_000, le=3_200_000_000, description="move the date by seconds")
    lat: float | None = Field(None, ge=-90, le=90)
    lon: float | None = Field(None, ge=-180, le=180)
    clear_place: bool = False
    description: str | None = Field(None, max_length=2000)
    keywords: list[str] | None = Field(None, max_length=200)
    add_keywords: list[str] | None = Field(None, max_length=200)
    remove_keywords: list[str] | None = Field(None, max_length=200)
    rating: int | None = Field(None, ge=0, le=5)
    creator: str | None = Field(None, max_length=100, description="who took the picture; '' takes the name away")
    albums: list[str] | None = Field(None, max_length=100, description="album paths like Urlaub/USA 2010")
    add_albums: list[str] | None = Field(None, max_length=100)
    remove_albums: list[str] | None = Field(None, max_length=100)


class StockRun(BaseModel):
    model: str | None = Field(None, max_length=100, description="the model to ask; left out: the small one")
    areas: list[str] | None = Field(None, max_length=100, description="only these areas; left out: all")
    limit: int | None = Field(None, ge=1, le=1_000_000, description="at most this many pictures")


class FaceCheck(BaseModel):
    limit: int = Field(2000, ge=1, le=50000, description="at most this many faces in one go")


class Cover(BaseModel):
    name: str = Field(max_length=100)
    face: int = Field(description="a found face, as the photo's face list names it")


class Apart(BaseModel):
    id: str = Field(max_length=200, description="the bundle, as the list of copies names it")
    level: Literal["copies", "alike"] = "copies"


class ResolveAll(BaseModel):
    merge: bool = True


class Resolve(BaseModel):
    keep: str = Field(max_length=64, description="key of the file that stays")
    drop: list[str] = Field(min_length=1, max_length=50, description="keys of the files that go to the trash")
    merge: bool = True


class FolderToAlbum(BaseModel):
    folder: str = Field(max_length=2000, description="every picture below it gets its folder as an album")


class Turn(BaseModel):
    degrees: Literal[90, 180, 270] = Field(description="clockwise")


class Many(BaseModel):
    keys: list[str] = Field(min_length=1, max_length=100)
    batch: str | None = Field(None, pattern=r"^[0-9a-f]{6,32}$", description="ties several calls into one change")


class InboxPropose(BaseModel):
    keys: list[str] = Field(min_length=1, max_length=500)


class ManyMove(Many):
    area: str = Field("", max_length=40, description="the area the photos go to")
    folder: str = Field("", max_length=1000, description="or the folder, with its area in front: Familie/2024")


class InboxAccept(ManyMove):
    """Take what the AI proposed over into the pictures and hand them on, out of the inbox."""
    take: bool = Field(True, description="write the stored proposal into each picture before it leaves")
    description: str | None = Field(None, max_length=2000, description="for one picture: this description instead")
    keywords: list[str] | None = Field(None, max_length=200, description="for one picture: these keywords instead")
    taken: str | None = Field(None, pattern=r"^\d{4}-\d\d-\d\dT\d\d:\d\d(:\d\d)?$",
                              description="for one picture: this date, where the picture says none itself")


class FolderIn(BaseModel):
    path: str = Field(max_length=1000, description="the new folder with its area in front: Bildschirmfotos/Arbeit")


class ManyChange(Many):
    change: Change


class AIChange(BaseModel):
    enabled: bool | None = None
    provider: str | None = Field(None, max_length=20)
    model: str | None = Field(None, max_length=200)
    api_key: str | None = Field(None, max_length=500)
    clear_key: bool = False
    base_url: str | None = Field(None, max_length=500)
    monthly_limit_usd: float | None = Field(None, ge=0, le=100000)
    excluded_areas: list[str] | None = None
    prompts: list[dict] | None = None
    default_prompt: str | None = Field(None, max_length=100)
    memories: bool | None = None
    inbox_auto: bool | None = None


class AskAI(BaseModel):
    prompt: str | None = Field(None, max_length=100, description="name of a stored instruction")
    instructions: str | None = Field(None, max_length=6000, description="a one-off instruction instead")


class SortingChange(BaseModel):
    rules: list[dict] = Field(max_length=200)
    modes: dict[str, str] = Field(default_factory=dict, description="per area: ask | auto after a date change")


class TrashPrefs(BaseModel):
    days: int = Field(ge=0, le=3650, description="how long deleted photos are kept; 0 = until someone empties it")


class ManyTurn(Many):
    degrees: Literal[90, 180, 270]


class Recipe(BaseModel):
    """What to do to the picture itself. Everything is optional; what is left out stays as it is."""
    angle: float = Field(0, ge=-30, le=30, description="degrees clockwise to set it straight")
    crop: list[float] | None = Field(None, min_length=4, max_length=4,
                                     description="left, top, width, height as fractions of the straightened picture")
    brightness: int = Field(0, ge=-100, le=100)
    contrast: int = Field(0, ge=-100, le=100)
    saturation: int = Field(0, ge=-100, le=100)
    warmth: int = Field(0, ge=-100, le=100)
    auto: bool = False


class FaceName(BaseModel):
    """A face in the photo as it is shown upright, and what it is called. An empty name removes the mark."""
    x: float = Field(ge=0, le=1, description="centre, as a fraction of the width")
    y: float = Field(ge=0, le=1)
    w: float = Field(gt=0, le=1)
    h: float = Field(gt=0, le=1)
    name: str = Field("", max_length=100)


class FacesNamed(BaseModel):
    faces: list[int] = Field(min_length=1, max_length=50, description="ids of found faces")
    name: str = Field(min_length=1, max_length=100)
    batch: str | None = Field(None, pattern=r"^[0-9a-f]{6,32}$")


class FacesUnnamed(BaseModel):
    faces: list[int] = Field(min_length=1, max_length=50, description="ids of found faces")
    batch: str | None = Field(None, pattern=r"^[0-9a-f]{6,32}$")


class FaceSearch(BaseModel):
    areas: dict[str, bool] = Field(description="per area: look for faces there or not")


class FacesIgnored(BaseModel):
    faces: list[int] = Field(min_length=1, max_length=200, description="ids of found faces")
    ignored: bool = True


class ImmichSource(BaseModel):
    url: str = Field(max_length=300, description="where Immich is, e.g. http://192.168.1.10:2283")
    key: str = Field(max_length=300, description="an API key that may read people, faces and assets")


class ImmichImport(BaseModel):
    token: str = Field(pattern=r"^[0-9a-f]{12}$", description="from the preview")
    people: list[str] | None = Field(None, max_length=5000, description="only these names; None = all")


class PersonRename(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    to: str = Field(min_length=1, max_length=100, description="the new name; one that exists merges the two")


def _worker(app: FastAPI, stop: threading.Event) -> None:
    """Keeps the index in line with the folders: at start, on request, and every few minutes."""
    settings: Settings = app.state.settings
    while not stop.is_set():
        try:
            result = app.state.index.scan(settings.scan_workers)
            if result.get("added") or result.get("removed"):
                log.info("index: %s new or changed, %s gone", result.get("added"), result.get("removed"))
            if not settings.readonly:
                gone = app.state.trash.purge(app.state.prefs.load()["trash_days"])
                if gone:
                    log.info("trash: %s photos removed for good after their time", gone)
        except Exception:
            log.exception("scan failed")
        app.state.faces_wake.set()
        app.state.inbox_wake.set()
        app.state.wake.wait(max(1, settings.rescan_minutes) * 60)
        app.state.wake.clear()


def _decisions_worker(app: FastAPI, stop: threading.Event) -> None:
    """Keeps the plain copy of what was decided, beside the photos, up to date."""
    while not stop.wait(20):
        if app.state.index.progress["running"]:
            continue                        # paths are on the move; write when they have settled
        try:
            written = app.state.decisions.write()
            if written:
                log.info("decisions: %s file(s) beside the photos brought up to date", written)
        except Exception:
            log.exception("writing the decisions beside the photos failed")


def _face_worker(app: FastAPI, stop: threading.Event) -> None:
    """Looks for faces in the pictures nobody has looked at yet – slowly, beside everything else.

    It only reads. What is done is kept, so after a restart it goes on where it stopped.
    """
    people: People = app.state.people
    wanted = app.state.face_areas       # the areas faces are looked for in
    while not stop.is_set():
        app.state.faces_wake.wait()
        app.state.faces_wake.clear()
        if stop.is_set() or app.state.index.progress["running"]:
            continue
        try:
            people.tidy(wanted())
            if not wanted():
                continue
            result = people.search(stop, wanted)
            if result.get("looked"):
                log.info("faces: looked at %s pictures, %s left", result["looked"], result["left"])
        except Exception:
            log.exception("face search failed")


def _inbox_worker(app: FastAPI, stop: threading.Event) -> None:
    """Lets the AI look at the pictures in the inbox: those put in line by hand, and – where that is
    switched on – whatever arrives. One at a time, beside everything else."""
    while not stop.is_set():
        app.state.inbox_wake.wait(300)
        app.state.inbox_wake.clear()
        if stop.is_set() or app.state.index.progress["running"]:
            continue
        try:
            app.state.propose_round(stop)
        except Exception:
            log.exception("inbox proposals failed")


def create_app(settings: Settings | None = None, background: bool = True) -> FastAPI:
    settings = settings or load_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        stop = threading.Event()
        if background:
            threading.Thread(target=_worker, args=(app, stop), daemon=True).start()
            threading.Thread(target=_face_worker, args=(app, stop), daemon=True).start()
            threading.Thread(target=_inbox_worker, args=(app, stop), daemon=True).start()
            if not settings.readonly:
                threading.Thread(target=_decisions_worker, args=(app, stop), daemon=True).start()
        yield
        stop.set()
        app.state.wake.set()
        app.state.faces_wake.set()
        app.state.inbox_wake.set()

    app = FastAPI(
        title="Augenblick!",
        version=__version__,
        description="Folder-based photo library. Sign in with the browser.",
        lifespan=lifespan,
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )
    app.state.settings = settings
    app.state.index = Index(settings.db_path, settings.photos, settings.thumbs)
    app.state.users = UserStore(settings.users_path)
    app.state.sessions = SessionStore(settings.data / "sessions.json")
    app.state.security = SecurityStore(settings.data / "security.json")
    app.state.notify = Notifier(app.state.security)
    app.state.networks = Networks(app.state.security.load().internal_networks or settings.internal_networks)
    app.state.security.import_api_token(settings.api_token)
    app.state.guard = LoginGuard()
    app.state.wake = threading.Event()
    app.state.faces_wake = threading.Event()
    app.state.inbox_wake = threading.Event()
    index: Index = app.state.index
    index.geo = Geo(settings.geo_path)
    journal = app.state.journal = Journal(settings.journal_path)
    editor = app.state.editor = Editor(settings.photos, settings.tmp, index, journal)
    prefs = app.state.prefs = Prefs(settings.data / "settings.json")
    app.state.user_prefs = PrefsStore(settings.data / "prefs.json")
    ai = app.state.ai = AIService(settings.data / "ai.json")
    proposals = app.state.proposals = Proposals(settings.data / "proposals.db")
    index.planner = Rules(prefs.load()["sort_rules"])

    def area_options() -> dict[str, dict]:
        """What each area does without – nothing is stored for an area that wants everything."""
        return {name: {"places": bool(opt.get("places", True)), "dates": bool(opt.get("dates", True))}
                for name, opt in prefs.load()["areas"].items() if isinstance(opt, dict)}

    def apply_area_options() -> None:
        options = area_options()
        index.no_places = frozenset(name for name, opt in options.items() if not opt["places"])
        index.no_dates = frozenset(name for name, opt in options.items() if not opt["dates"])

    apply_area_options()
    sorter = app.state.sorter = Sorter(settings.photos, index, journal, editor._lock)
    if index.planner.rules:
        index.replan()
    memories = app.state.memories = Memories(index, settings.data / "memories.json")
    trash = app.state.trash = Trash(settings.journal_path, settings.photos, index, editor._lock)
    people = app.state.people = People(settings.faces_path, settings.face_thumbs, settings.photos, index,
                                       Engine(settings.models_path), settings.faces_load, settings.ignored_path)

    dupes = app.state.dupes = Dupes(settings.dupes_path, settings.apart_path, settings.photos, settings.thumbs, index)
    decisions = app.state.decisions = Decisions(settings.photos, index, prefs, settings.ignored_path, settings.apart_path)

    def follow_turn(old: str, new: str, n: int, path: str) -> None:
        moved = decisions.turned(old, new, n, path)
        if n == 1:                          # the same picture under another hash: the faces found in it stay found
            still = bool(index._all("SELECT 1 FROM photos WHERE hash = ? AND path != ? LIMIT 1", (old, path)))
            moved += people.follow(old, new, still)
        if moved:
            people._changes += 1            # both keep what they worked out; it no longer holds
            dupes._worked = None

    editor.on_turn = follow_turn
    try:                                    # a data folder that was lost: take back what stands beside the photos
        back = decisions.restore()
        if back:
            log.info("decisions taken back from the files beside the photos: %s", back)
    except Exception:
        log.exception("reading the decisions beside the photos failed")

    def face_areas() -> list[str]:
        """The areas faces are looked for in – each one is switched on by itself."""
        stored = prefs.load()["faces"]
        names = [a["name"] for a in index.areas()]
        if stored is True:
            return names
        chosen = [name for name in names if isinstance(stored, list) and name in stored]
        if chosen:          # what the phone sends is looked at too, so names can be given before it is sorted
            inbox_areas = app.state.inbox.areas()
            chosen += [name for name in names if name in inbox_areas and name not in chosen]
        return chosen

    app.state.face_areas = face_areas

    def allowed_areas(user: User) -> list[str]:
        return [a["name"] for a in index.areas() if user.can_read(a["name"])]

    def pick_areas(user: User, wanted: str | None) -> list[str]:
        allowed = allowed_areas(user)
        if not wanted:
            return allowed
        chosen = set(wanted.split(","))
        return [a for a in allowed if a in chosen]

    def start_session(request: Request, response: Response, name: str, external: bool, mfa: bool) -> None:
        token = app.state.sessions.create(name, ip=client_ip(request), agent=request.headers.get("user-agent", ""),
                                          external=external, mfa=mfa)
        response.set_cookie(cookie_name(request), token, max_age=SessionStore.max_age(external),
                            httponly=True, samesite="lax", secure=is_https(request), path="/")

    # ----------------------------------------------------------- public

    @app.get("/api/version", tags=["system"])
    def version():
        return {"name": "Augenblick!", "version": __version__}

    @app.post("/api/login", tags=["auth"])
    def login(body: Login, request: Request, response: Response):
        name = body.username.strip().lower()
        external = is_external(request)
        ip = client_ip(request)
        guard: LoginGuard = app.state.guard

        def refuse(status: int, detail: str, count: bool = True):
            if count:
                failures, seconds = guard.fail(name, external)
                if seconds and failures % 5 == 0:
                    app.state.notify.send(
                        "notify_lockout", "lock.title", "lock.body", priority=4, tags="warning",
                        user=name, count=failures, ip=ip, minutes=max(1, seconds // 60),
                        where=app.state.notify.text("where.ext" if external else "where.int"))
                time.sleep(1)
            raise HTTPException(status_code=status, detail=detail)

        wait = guard.locked_for(name, external)
        if wait:
            raise HTTPException(status_code=429, detail="locked", headers={"Retry-After": str(wait)})
        account = app.state.users.authenticate(name, body.password)
        if not account:
            refuse(401, "invalid credentials")
        policy = app.state.security.load()
        if external and policy.require_2fa_external and not account.has_totp:
            refuse(403, "second factor required outside", count=False)
        mfa = False
        if account.has_totp and (external or policy.require_2fa_internal or body.code):
            if not body.code:
                raise HTTPException(status_code=401, detail="code required")
            if not app.state.users.second_factor(name, body.code):
                refuse(401, "invalid code")
            mfa = True
        guard.success(name, external)
        start_session(request, response, name, external, mfa)
        if external:
            app.state.notify.send("notify_external_login", "external.title", "external.body", user=name, ip=ip,
                                  agent=describe_agent(request.headers.get("user-agent", "")))
        return account.public()

    @app.get("/api/setup", tags=["auth"])
    def setup_state(request: Request):
        """Is this a fresh installation without any account? Then the first administrator is created here."""
        return {"needed": not app.state.users.all(), "home": not is_external(request)}

    @app.post("/api/setup", tags=["auth"], status_code=201)
    def setup(body: SetupIn, request: Request, response: Response):
        """Create the first administrator – only while there is no account, and only from the home network."""
        if app.state.users.all():
            raise HTTPException(status_code=409, detail="already set up")
        if is_external(request):
            raise HTTPException(status_code=403, detail="first setup only from the home network")
        try:
            account = app.state.users.create(body.username, body.password, body.display_name,
                                             is_admin=True, only_if_empty=True)
        except UserError as exc:
            raise HTTPException(status_code=409 if "already" in str(exc) else 400, detail=str(exc)) from exc
        log.info("first administrator %r created on the setup page", account.name)
        start_session(request, response, account.name, False, False)
        return account.public()

    @app.post("/api/logout", tags=["auth"])
    def logout(request: Request, response: Response):
        token = session_token(request)
        if token:
            app.state.sessions.revoke_token(token)
        response.delete_cookie(cookie_name(request), path="/", secure=is_https(request), httponly=True)
        return {"ok": True}

    # ------------------------------------------------------ authenticated

    @app.get("/api/me", tags=["auth"])
    def me(request: Request, user: User = Depends(current_user)):
        areas = allowed_areas(user)
        account = app.state.users.get(user.name)
        return {"name": user.name, "display_name": user.display_name, "is_admin": user.is_admin, "areas": areas,
                "readonly": settings.readonly,
                "totp": bool(account and account.has_totp),
                "recovery_left": account.public()["recovery_left"] if account else 0,
                "external": is_external(request),
                "write": [] if settings.readonly else [a for a in areas if user.can_write(a)],
                "sorting": bool(index.planner.rules), "sort_modes": prefs.load()["sort_modes"],
                "language": app.state.user_prefs.get(user.name)["language"]}

    @app.get("/api/me/prefs", tags=["auth"])
    def my_prefs(user: User = Depends(current_user)):
        return app.state.user_prefs.get(user.name)

    @app.put("/api/me/prefs", tags=["auth"])
    def put_my_prefs(body: TreePrefs, user: User = Depends(current_user)):
        """What follows the account from device to device: the language of the interface."""
        if body.language is not None:
            try:
                app.state.user_prefs.set_language(user.name, body.language)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        if body.tree_open is not None:
            app.state.user_prefs.set_tree_open(user.name, body.tree_open)
        return app.state.user_prefs.get(user.name)

    @app.post("/api/me/password", tags=["auth"])
    def change_own_password(body: PasswordChange, request: Request, user: User = Depends(current_user)):
        if not app.state.users.authenticate(user.name, body.old):
            raise HTTPException(status_code=403, detail="wrong password")
        try:
            app.state.users.update(user.name, password=body.new)
        except UserError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "signed_out": app.state.sessions.revoke(user.name, keep_token=session_token(request))}

    def _account_user(user: User):
        account = app.state.users.get(user.name)
        if not account:
            raise HTTPException(status_code=400, detail="only for personal accounts")
        return account

    @app.get("/api/me/sessions", tags=["auth"])
    def my_sessions(request: Request, user: User = Depends(current_user)):
        """The devices this account is signed in on."""
        mine = app.state.sessions.get(session_token(request))
        return [{"id": s.id, "device": describe_agent(s.agent), "agent": s.agent, "ip": s.ip,
                 "created": s.created, "last_seen": s.last_seen, "expires": s.expires,
                 "external": s.external, "mfa": s.mfa, "current": bool(mine and mine.id == s.id)}
                for s in app.state.sessions.for_user(user.name)]

    @app.delete("/api/me/sessions/{session_id}", tags=["auth"])
    def end_session(session_id: str, user: User = Depends(current_user)):
        if not app.state.sessions.revoke(user.name, session_id=session_id):
            raise HTTPException(status_code=404, detail="unknown session")
        return {"ok": True}

    @app.post("/api/me/sessions/others", tags=["auth"])
    def end_other_sessions(request: Request, user: User = Depends(current_user)):
        return {"signed_out": app.state.sessions.revoke(user.name, keep_token=session_token(request))}

    @app.post("/api/me/totp/setup", tags=["auth"])
    def totp_setup(body: PasswordConfirm, user: User = Depends(current_user)):
        account = _account_user(user)
        if not app.state.users.authenticate(user.name, body.password):
            raise HTTPException(status_code=403, detail="wrong password")
        if account.has_totp:
            raise HTTPException(status_code=400, detail="second factor already on")
        secret = app.state.users.totp_begin(user.name)
        uri = otpauth_uri(secret, user.name)
        return {"secret": " ".join(secret[i:i + 4] for i in range(0, len(secret), 4)),
                "uri": uri, "qr": qr_data_uri(uri)}

    @app.post("/api/me/totp/enable", tags=["auth"])
    def totp_enable(body: CodeIn, request: Request, user: User = Depends(current_user)):
        _account_user(user)
        try:
            codes = app.state.users.totp_confirm(user.name, body.code)
        except UserError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        token = session_token(request)
        app.state.sessions.mark_mfa(token)
        # sessions from before the second factor existed must not stay valid
        app.state.sessions.revoke(user.name, keep_token=token)
        app.state.notify.send("notify_2fa_changes", "2fa.on.title", "2fa.on.body", tags="key", user=user.name)
        return {"recovery_codes": codes}

    @app.post("/api/me/totp/disable", tags=["auth"])
    def totp_disable(body: TotpOff, user: User = Depends(current_user)):
        _account_user(user)
        if not app.state.users.authenticate(user.name, body.password):
            raise HTTPException(status_code=403, detail="wrong password")
        if not app.state.users.second_factor(user.name, body.code):
            raise HTTPException(status_code=400, detail="invalid code")
        app.state.users.totp_off(user.name)
        app.state.notify.send("notify_2fa_changes", "2fa.off.title", "2fa.off.body", priority=4,
                              tags="warning", user=user.name, by=user.name)
        return {"ok": True}

    @app.post("/api/me/totp/recovery", tags=["auth"])
    def totp_recovery(body: CodeIn, user: User = Depends(current_user)):
        _account_user(user)
        if app.state.users.second_factor(user.name, body.code) != "totp":
            raise HTTPException(status_code=400, detail="invalid code")
        return {"recovery_codes": app.state.users.new_recovery(user.name)}

    @app.get("/api/security", tags=["security"])
    def get_security(request: Request, user: User = Depends(require_admin)):
        accounts = app.state.users.all()
        return {**app.state.security.load().public(),
                "without_2fa": [a.name for a in accounts if not a.has_totp],
                "networks_default": settings.internal_networks or ", ".join(DEFAULT_INTERNAL),
                "client_ip": client_ip(request)}

    @app.put("/api/security", tags=["security"])
    def put_security(body: SecurityChange, request: Request, user: User = Depends(require_admin)):
        changes = body.model_dump(exclude_unset=True)
        chosen = app.state.user_prefs.get(user.name) if user.name != "api" else {}
        if chosen.get("language") in LANGUAGES:             # notifications speak the language of whoever set them up
            changes["language"] = chosen["language"]
        if changes.get("require_2fa_external"):
            account = app.state.users.get(user.name)
            if account and not account.has_totp:
                raise HTTPException(status_code=400, detail="set up your own second factor first")
        if "internal_networks" in changes and changes["internal_networks"] is not None:
            try:
                spec = normalise_networks(changes["internal_networks"])
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            here = client_ip(request)
            # whoever saves from home must stay at home, or the next sign-in suddenly asks for a code
            if app.state.networks.is_internal(here) and not Networks(spec or settings.internal_networks).is_internal(here):
                raise HTTPException(status_code=400, detail=f"your own address {here} would no longer count as home")
        try:
            config = app.state.security.update(changes)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        app.state.networks = Networks(config.internal_networks or settings.internal_networks)
        return get_security(request, user)

    @app.get("/api/security/api-token", tags=["security"])
    def get_api_token(user: User = Depends(require_admin)):
        """The API key for scripts (empty: API only with a signed-in browser)."""
        return {"api_token": app.state.security.load().api_token}

    @app.post("/api/security/api-token", tags=["security"])
    def new_api_token(user: User = Depends(require_admin)):
        """Create a new API key; the old one stops working at once."""
        token = secrets.token_urlsafe(32)
        app.state.security.set_api_token(token)
        return {"api_token": token}

    @app.delete("/api/security/api-token", tags=["security"])
    def delete_api_token(user: User = Depends(require_admin)):
        app.state.security.set_api_token("")
        return {"api_token": ""}

    @app.post("/api/security/test", tags=["security"])
    def test_security(user: User = Depends(require_admin)):
        error = app.state.notify.send("", "test.title", "test.body", wait=True, tags="white_check_mark")
        if error:
            raise HTTPException(status_code=400, detail=error)
        return {"ok": True}

    @app.get("/api/status", tags=["system"])
    def status(user: User = Depends(current_user)):
        return {"version": __version__, "photos": index.count(), "scan": dict(index.progress),
                "readonly": settings.readonly, "support": SUPPORT_URL}

    @app.post("/api/rescan", tags=["system"])
    def rescan(user: User = Depends(require_admin)):
        app.state.wake.set()
        return {"ok": True}

    # ------------------------------------------------------------ photos

    def filters(folder: str = Query("", max_length=2000), recursive: bool = True, q: str = Query("", max_length=200),
                camera: str = Query("", max_length=200), creator: str = Query("", max_length=100),
                no_place: bool = False, no_date: bool = False,
                kind: str = Query("", pattern="^(image|video|)$"), misfit: bool = False,
                album: str = Query("", max_length=2000), person: str = Query("", max_length=100),
                country: str = Query("", max_length=200), city: str = Query("", max_length=200),
                auto: str = Query("", max_length=20),
                since: str = Query("", pattern=r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)?$"),
                until: str = Query("", pattern=r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)?$"),
                keys: str = Query("", max_length=2000, pattern=r"^[0-9a-f,-]*$")) -> dict:
        return {"folder": folder.strip("/"), "recursive": recursive, "q": q, "camera": camera, "creator": creator, "auto": auto,
                "since": since, "until": until, "keys": [k for k in keys.split(",") if k] if keys else None,
                "no_place": no_place, "no_date": no_date, "kind": kind, "misfit": misfit, "album": album.strip("/"),
                "person": person.strip(), "country": country, "city": city}

    @app.get("/api/areas", tags=["photos"])
    def areas(user: User = Depends(current_user)):
        options = area_options()
        arrives = app.state.inbox.areas()       # where phones upload to: the inbox
        return [{**a, **options.get(a["name"], {"places": True, "dates": True}), "inbox": a["name"] in arrives}
                for a in index.areas() if user.can_read(a["name"])]

    def known_area(name: str) -> dict:
        found = next((a for a in index.areas() if a["name"] == name), None)
        if not found or "/" in name or name.startswith((".", "@")):
            raise HTTPException(status_code=404, detail="unknown area")
        return found

    @app.post("/api/areas", tags=["photos"], status_code=201)
    def area_create(body: AreaIn, user: User = Depends(require_admin)):
        """A new area: a folder directly below the photos that belongs to nobody yet. Who may see it
        or write there is set per user."""
        name = body.name.strip()
        if not AREA_RE.fullmatch(name) or name.endswith((".", " ")):
            raise HTTPException(status_code=400, detail="invalid area name")
        if settings.readonly:
            raise HTTPException(status_code=400, detail="read-only")
        if any(a["name"].lower() == name.lower() for a in index.areas()) or (settings.photos / name).exists():
            raise HTTPException(status_code=409, detail="area exists")
        try:
            (settings.photos / name).mkdir()
            os.chmod(settings.photos / name, 0o775)
        except OSError as exc:
            raise HTTPException(status_code=400, detail=f"cannot create the folder: {exc.strerror}") from exc
        if not (body.places and body.dates):
            prefs.save(areas={**prefs.load()["areas"], name: {"places": body.places, "dates": body.dates}})
            apply_area_options()
        return {"name": name, "count": 0, "places": body.places, "dates": body.dates}

    @app.patch("/api/areas/{name}", tags=["photos"])
    def area_change(name: str, body: AreaOptions, user: User = Depends(require_admin)):
        """Whether the pictures of an area need a place and a date. Changes no file."""
        known_area(name)
        stored = dict(prefs.load()["areas"])
        if body.places and body.dates:
            stored.pop(name, None)
        else:
            stored[name] = {"places": body.places, "dates": body.dates}
        prefs.save(areas=stored)
        apply_area_options()
        index.relocate()        # the place names of this area come or go, without reading a file
        return {"name": name, "places": body.places, "dates": body.dates}

    @app.delete("/api/areas/{name}", tags=["photos"])
    def area_delete(name: str, user: User = Depends(require_admin)):
        """Remove an area – only while there is no file in it, the bin included."""
        if known_area(name)["count"]:
            raise HTTPException(status_code=409, detail="area holds photos")
        if settings.readonly:
            raise HTTPException(status_code=400, detail="read-only")
        target = settings.photos / name
        if any(p.is_file() and ".augenblick" not in p.relative_to(target).parts[:-1] for p in target.rglob("*")):
            raise HTTPException(status_code=409, detail="area holds files")
        try:
            shutil.rmtree(target)
        except OSError as exc:
            raise HTTPException(status_code=400, detail=f"cannot remove the folder: {exc.strerror}") from exc
        stored = dict(prefs.load()["areas"])
        if stored.pop(name, None) is not None:
            prefs.save(areas=stored)
            apply_area_options()
        return {"ok": True}

    @app.get("/api/tree", tags=["photos"])
    def tree(areas: str | None = Query(None, description="comma-separated areas"),
             user: User = Depends(current_user)):
        return index.tree(pick_areas(user, areas))

    @app.get("/api/albums", tags=["photos"])
    def albums(areas: str | None = Query(None), user: User = Depends(current_user)):
        """The albums – they are written in the photos themselves, this is only the overview."""
        return index.album_tree(pick_areas(user, areas))

    @app.get("/api/auto", tags=["photos"])
    def auto_albums(areas: str | None = Query(None), user: User = Depends(current_user)):
        """The albums that fill themselves: films, Live Photos, panoramas, favourites, this day in other years."""
        return index.auto_albums(pick_areas(user, areas))

    @app.get("/api/places", tags=["photos"])
    def places(areas: str | None = Query(None), chosen: dict = Depends(filters), user: User = Depends(current_user)):
        """For the map: the photos with a place, as short rows [key, lat, lon, month, orientation]."""
        return index.places(pick_areas(user, areas), **chosen)

    @app.get("/api/placenames", tags=["photos"])
    def place_names(areas: str | None = Query(None), user: User = Depends(current_user)):
        """The countries and towns the photos were taken in – worked out from the coordinates on this machine."""
        return index.place_names(pick_areas(user, areas))

    @app.get("/api/creators", tags=["photos"])
    def creators(areas: str | None = Query(None), user: User = Depends(current_user)):
        """Who took the pictures, as the files say it – with how many each."""
        return index.creators(pick_areas(user, areas))

    @app.get("/api/cameras", tags=["photos"])
    def cameras(areas: str | None = Query(None), user: User = Depends(current_user)):
        return index.cameras(pick_areas(user, areas))

    @app.get("/api/timeline", tags=["photos"])
    def timeline(areas: str | None = Query(None), chosen: dict = Depends(filters),
                 user: User = Depends(current_user)):
        """The months that have photos, newest first, with how many."""
        return index.months(pick_areas(user, areas), **chosen)

    @app.get("/api/photos", tags=["photos"])
    def photos(month: str = Query(..., description="YYYY-MM"), areas: str | None = Query(None),
               chosen: dict = Depends(filters), user: User = Depends(current_user)):
        if not MONTH_RE.match(month):
            raise HTTPException(status_code=400, detail="month must be YYYY-MM")
        return index.month(pick_areas(user, areas), month, **chosen)

    def photo_or_404(key: str, user: User) -> dict:
        photo = index.photo(key, allowed_areas(user))
        if not photo:
            raise HTTPException(status_code=404, detail="photo not found")
        return photo

    def file_of(photo: dict) -> Path:
        root = settings.photos.resolve()
        path = (root / photo["path"]).resolve()
        if root not in path.parents or not path.is_file():
            raise HTTPException(status_code=404, detail="file not found")
        return path

    @app.get("/api/photos/{key}", tags=["photos"])
    def photo(key: str, user: User = Depends(current_user)):
        return public(photo_or_404(key, user), user)

    def public(data: dict, user: User) -> dict:
        data = dict(data)
        data.pop("hash")
        data.pop("regions", None)       # shown through /faces, in the upright picture
        film = index.film_of(data)
        data.pop("live", None)
        data["film"] = film["key"] if film else None       # a Live Photo: the key of its short film
        original = index.original_of(data)
        data.pop("source", None)
        data["original"] = original["key"] if original else None      # worked on here: the key of what it was made from
        if not original:
            data["recipe"] = {}
        data["can_delete"] = not settings.readonly and user.can_write(data["area"])
        data["can_write"] = data["can_delete"] and data["kind"] == "image" and data["ext"] not in RAW_EXT
        data["can_turn"] = data["can_write"] and data["ext"] in ("jpg", "jpeg", "heic", "heif")
        data["can_develop"] = data["can_write"] and data["ext"] != "gif" and not data["error"]
        data["inbox"] = data["area"] in app.state.inbox.areas()       # sent by the phone, not sorted yet
        data["proposal"] = proposals.get(data["path"]) if data["inbox"] else None
        return data

    def rendered(photo: dict, cache: Path, edge: int) -> Response:
        if photo["kind"] == "video":        # one picture out of the film, made when the film was read
            still = cache_path(settings.thumbs, photo["hash"], 1)
            if not still.exists():
                raise HTTPException(status_code=404, detail="no preview")
            return FileResponse(still, media_type="image/jpeg", headers=IMMUTABLE)
        if photo["kind"] != "image" or photo["error"]:
            raise HTTPException(status_code=404, detail="no preview")
        target = cache_path(cache, photo["hash"], photo["orientation"] or 1)
        if not target.exists():
            try:
                image, _ = read_image(file_of(photo).read_bytes())
                store(target, render(image, edge))
            except HTTPException:
                raise
            except Exception as exc:
                raise HTTPException(status_code=404, detail="no preview") from exc
        return FileResponse(target, media_type="image/jpeg", headers=IMMUTABLE)

    @app.get("/api/photos/{key}/thumb", tags=["photos"])
    def photo_thumb(key: str, user: User = Depends(current_user)):
        return rendered(photo_or_404(key, user), settings.thumbs, THUMB_EDGE)

    @app.get("/api/photos/{key}/preview", tags=["photos"])
    def photo_preview(key: str, user: User = Depends(current_user)):
        return rendered(photo_or_404(key, user), settings.previews, PREVIEW_EDGE)

    @app.get("/api/photos/{key}/file", tags=["photos"])
    def photo_file(key: str, download: bool = False, user: User = Depends(current_user)):
        """The original, untouched."""
        data = photo_or_404(key, user)
        return FileResponse(file_of(data), media_type=MEDIA_TYPES.get(data["ext"], "application/octet-stream"),
                            filename=data["name"],
                            content_disposition_type="attachment" if download else "inline")

    # ----------------------------------------------------------- changes

    def writable(photo: dict, user: User) -> dict:
        if settings.readonly:
            raise HTTPException(status_code=403, detail="read-only")
        if not user.can_write(photo["area"]):
            raise HTTPException(status_code=403, detail="no write access to this area")
        return photo

    def one(action, key: str, user: User) -> dict:
        try:
            return public(action(writable(photo_or_404(key, user), user)), user)
        except EditError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    def many(action, keys: list[str], user: User) -> list[dict]:
        results = []
        for key in keys:
            try:
                results.append({"key": key, "ok": True, "photo": one(action, key, user)})
            except HTTPException as exc:
                results.append({"key": key, "ok": False, "error": exc.detail})
            except Exception as exc:        # one broken file must not stop the rest
                log.exception("change failed for %s", key)
                results.append({"key": key, "ok": False, "error": f"{type(exc).__name__}: {exc}"[:200]})
        return results

    @app.patch("/api/photos/{key}", tags=["changes"])
    def change_photo(key: str, body: Change, user: User = Depends(current_user)):
        """Write date, place, description, keywords or rating into the photo itself."""
        return one(lambda p: changed(p, body.model_dump(), user.name, None), key, user)

    def changed(photo: dict, change: dict, name: str, batch: str | None) -> dict:
        """Change a photo; where the area says so, put it where its new date belongs right away."""
        record = editor.change(photo, change, name, batch)
        if record["misfit"] and prefs.load()["sort_modes"].get(record["area"]) == "auto":
            try:
                record = sorter.move(record, name, batch)
            except SortError as exc:
                log.warning("cannot sort %s: %s", record["path"], exc)
        return record

    @app.post("/api/photos/{key}/develop/preview", tags=["changes"])
    def develop_preview(key: str, body: Recipe, user: User = Depends(current_user)):
        """How the picture would look – worked out from the preview of the original, not cut yet, so
        the cut can be shown on it. Nothing is written."""
        photo = photo_or_404(key, user)
        base = index.original_of(photo) or photo
        if base["kind"] != "image" or base["error"]:
            raise HTTPException(status_code=404, detail="no preview")
        rendered(base, settings.previews, PREVIEW_EDGE)
        try:
            recipe = clean_recipe(body.model_dump())
            with Image.open(cache_path(settings.previews, base["hash"], base["orientation"] or 1)) as small:
                profile = small.info.get("icc_profile")
                shown = develop(small, recipe, crop=False)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        out = io.BytesIO()
        shown.save(out, "JPEG", quality=85, **({"icc_profile": profile} if profile else {}))
        return Response(out.getvalue(), media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    @app.post("/api/photos/{key}/develop", tags=["changes"])
    def develop_photo(key: str, body: Recipe, user: User = Depends(current_user)):
        """Cut, set straight, light and colours: saved as a new picture; the original stays, behind it."""
        return one(lambda photo: editor.develop(photo, body.model_dump(), user.name), key, user)

    def undevelop(photo: dict, user: User) -> dict:
        original = index.original_of(photo)
        if not original:
            raise EditError("this picture was not worked on here")
        try:
            trash.put(photo, user.name, alone=True)         # the picture made goes to the trash, the original stays
        except TrashError as exc:
            raise EditError(str(exc)) from exc
        return editor.step_forward(original)

    @app.delete("/api/photos/{key}/develop", tags=["changes"])
    def develop_discard(key: str, user: User = Depends(current_user)):
        """Back to the original: the picture made from it goes to the trash."""
        return one(lambda photo: undevelop(photo, user), key, user)

    @app.post("/api/photos/{key}/rotate", tags=["changes"])
    def turn_photo(key: str, body: Turn, user: User = Depends(current_user)):
        """Turn clockwise – without loss where the picture allows it, otherwise by the orientation tag."""
        try:
            return one(lambda p: editor.rotate(p, body.degrees, user.name), key, user)
        finally:
            app.state.faces_wake.set()      # a turned picture is a new picture: its faces are looked for again

    @app.post("/api/edit", tags=["changes"])
    def change_many(body: ManyChange, user: User = Depends(current_user)):
        change = body.change.model_dump()
        return many(lambda p: changed(p, change, user.name, body.batch), body.keys, user)

    @app.post("/api/rotate", tags=["changes"])
    def turn_many(body: ManyTurn, user: User = Depends(current_user)):
        try:
            return many(lambda p: editor.rotate(p, body.degrees, user.name, body.batch), body.keys, user)
        finally:
            app.state.faces_wake.set()

    @app.get("/api/journal", tags=["changes"])
    def journal_list(limit: int = Query(200, ge=1, le=1000), before: int | None = None,
                     user: User = Depends(current_user)):
        """What was changed, newest first."""
        entries = journal.list(allowed_areas(user), limit, before)
        paths = {}
        for entry in entries:
            entry.pop("tags")
            entry.pop("sha_after")
            if entry["path"] not in paths:
                current = index.by_path(entry["path"])
                paths[entry["path"]] = current["key"] if current else None
            entry["key"] = paths[entry["path"]]
        return entries

    @app.post("/api/journal/{entry}/undo", tags=["changes"])
    def journal_undo(entry: int, user: User = Depends(current_user)):
        """Take a change back – possible while the file is still what that change left behind."""
        found = journal.get(entry)
        if not found or not user.can_read(found["area"]):
            raise HTTPException(status_code=404, detail="entry not found")
        writable(found, user)
        try:
            if found["action"] == "develop":        # back to how it was before: the earlier recipe, or the original
                photo = index.by_path(found["path"])
                if not photo:
                    raise EditError("file not found")
                earlier = found["before"].get("recipe")
                done = editor.develop(photo, earlier, user.name) if earlier else undevelop(photo, user)
                journal.mark_undone(found["id"], user.name)
                return public(done, user)
            if found["action"] == "move":
                back = found["before"]["path"].split("/", 1)[0]     # it may have come from another area
                if not user.can_write(back):
                    raise HTTPException(status_code=403, detail="no write access to this area")
                return public(sorter.undo(found, user.name), user)
            return public(editor.undo(found, user.name), user)
        except (EditError, SortError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    # ------------------------------------------------------------ people

    def faces_state(user: User) -> dict:
        on = face_areas()
        return {"enabled": bool(on), "areas": [{"name": name, "enabled": name in on} for name in allowed_areas(user)],
                **people.state()}

    def face_scope(areas: list[str]) -> list[str]:
        """Found faces are shown and compared only where the search is switched on."""
        on = face_areas()
        return [name for name in areas if name in on]

    @app.get("/api/people", tags=["people"])
    def people_list(areas: str | None = Query(None), user: User = Depends(current_user)):
        """Who is named on the photos – the names stand in the photos themselves, this is the overview.
        `face` is the found face that stands for the person in lists (its picture: /api/faces/{face}/thumb)."""
        chosen = pick_areas(user, areas)
        faces = people.covers(face_scope(chosen), prefs.load()["covers"])
        return [dict(person, face=faces.get(person["name"])) for person in index.people(chosen)]

    @app.put("/api/people/cover", tags=["people"])
    def person_cover(body: Cover, user: User = Depends(current_user)):
        """Choose the face that stands for a person in the list. Nothing is written to a photo."""
        face = people.face(body.face)
        if not face or not index.with_hash(face["hash"], [a for a in allowed_areas(user) if user.can_write(a)]):
            raise HTTPException(status_code=404, detail="face not found")
        name = clean_name(body.name)
        if not name:
            raise HTTPException(status_code=422, detail="no name")
        covers = dict(prefs.load()["covers"])
        covers[name] = {"hash": face["hash"], "box": [face["x"], face["y"], face["w"], face["h"]]}
        prefs.save(covers=covers)
        return {"name": name, "face": body.face}

    @app.post("/api/people/rename", tags=["people"], status_code=202)
    def rename_person(body: PersonRename, user: User = Depends(current_user)):
        """Give somebody another name in every photo – with a name that exists, the two become one.

        Runs as a long job; every photo changed stands in the journal and can be taken back.
        """
        name, to = clean_name(body.name), clean_name(body.to)
        if settings.readonly:
            raise HTTPException(status_code=403, detail="read-only")
        if not name or not to or name == to:
            raise HTTPException(status_code=400, detail="choose another name")
        mine = [a for a in allowed_areas(user) if user.can_write(a)]
        items = index.with_person(mine, name)
        skipped = len(index.with_person(allowed_areas(user), name)) - len(items)
        if not items:
            raise HTTPException(status_code=404 if not skipped else 403,
                                detail="person not found" if not skipped else "no write access to this area")
        batch = secrets.token_hex(6)

        def work(photo: dict) -> None:
            turn = photo["orientation"] or 1
            edits = [dict(zip("xywh", turn_box((r["x"], r["y"], r["w"], r["h"]), turn)), name=to)
                     for r in photo["regions"] if is_face(r["type"]) and r["name"] == name]
            if edits:
                editor.faces(photo, edits, user.name, batch)

        start_job("person", f"{name} → {to}", items, work)
        return {"total": len(items), "skipped": skipped}

    app.state.immich = None     # the last preview: what to ask Immich for, and for whom it was made

    @app.post("/api/immich/preview", tags=["people"])
    def immich_preview(body: ImmichSource, user: User = Depends(require_admin)):
        """Ask Immich whom it knows by name and on which pictures, and find those pictures here.
        Nothing is written, here or there; address and key are kept in memory only, for the import."""
        if settings.readonly:
            raise HTTPException(status_code=403, detail="read-only")
        mine = [a for a in allowed_areas(user) if user.can_write(a)]
        try:
            source = Immich(body.url, body.key)
            found = immich_plan(source, index.by_name(mine), settings.photos)
        except ImmichError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        token = secrets.token_hex(6)
        app.state.immich = {"token": token, "source": source, "items": found["items"], "user": user.name}
        return {"token": token, "people": found["people"], "missed": found["missed"], "photos": len(found["items"])}

    @app.post("/api/immich/import", tags=["people"], status_code=202)
    def immich_import(body: ImmichImport, user: User = Depends(require_admin)):
        """Write what the preview showed: each face Immich has a name for becomes a named region in
        the photo. A long job; every photo stands in the journal and can be taken back."""
        if settings.readonly:
            raise HTTPException(status_code=403, detail="read-only")
        kept = app.state.immich
        if not kept or kept["token"] != body.token or kept["user"] != user.name:
            raise HTTPException(status_code=409, detail="preview is out of date")
        wanted = set(body.people) if body.people is not None else None
        items = [item for item in kept["items"] if wanted is None or wanted & set(item["names"])]
        source: Immich = kept["source"]
        batch = secrets.token_hex(6)

        def work(item: dict) -> None:
            photo = index.by_path(item["path"])
            if not photo or not user.can_write(photo["area"]):
                raise EditError("photo not found")
            names = set(item["names"]) if wanted is None else wanted & set(item["names"])
            edits = edits_for(source.faces(item["asset"]), photo, names)
            if edits:
                editor.faces(photo, edits, user.name, batch)

        start_job("immich", "Immich", items, work)
        app.state.immich = None
        return {"total": len(items)}

    @app.get("/api/photos/{key}/faces", tags=["people"])
    def photo_faces(key: str, user: User = Depends(current_user)):
        """The faces on a photo: those marked in the file and those found beside them, with a proposed
        name where one looks like somebody already named. Boxes are centre, width and height as
        fractions of the upright picture."""
        return people.on_photo(photo_or_404(key, user), face_scope(allowed_areas(user)))

    @app.put("/api/photos/{key}/faces", tags=["people"])
    def name_face(key: str, body: FaceName, user: User = Depends(current_user)):
        """Write a name on a face into the photo itself – or take it away with an empty name.
        A box where no face is marked yet makes a new mark: that is how a face is drawn by hand."""
        return one(lambda p: editor.faces(p, [body.model_dump()], user.name), key, user)

    @app.get("/api/faces", tags=["people"])
    def faces_status(user: User = Depends(current_user)):
        """Where the search for faces is on, and how far it is."""
        return faces_state(user)

    @app.put("/api/faces", tags=["people"])
    def faces_switch(body: FaceSearch, user: User = Depends(require_admin)):
        """Switch the search for faces on or off, area by area. It runs in the background and only
        reads. What was found in an area switched off is forgotten; names in the photos stay."""
        on = set(face_areas())
        for name, enabled in body.areas.items():
            (on.add if enabled else on.discard)(name)
        prefs.save(faces=sorted(on & {a["name"] for a in index.areas()}))
        app.state.faces_wake.set()
        return faces_state(user)

    @app.get("/api/faces/groups", tags=["people"])
    def face_groups(areas: str | None = Query(None), limit: int = Query(40, ge=1, le=200),
                    offset: int = Query(0, ge=0), shown: int = Query(60, ge=0, le=200),
                    stale: float = Query(0, ge=0, le=900), user: User = Depends(current_user)):
        """Faces without a name, bundled by likeness, with a proposed name where there is one.
        `stale`: an answer worked out up to so many seconds ago will do – quick while names are being written."""
        return people.groups(face_scope(pick_areas(user, areas)), limit, offset, shown, stale)

    @app.get("/api/faces/twins", tags=["people"])
    def face_twins(areas: str | None = Query(None), stale: float = Query(0, ge=0, le=900),
                   user: User = Depends(current_user)):
        """Faces without a name in a second copy of a photo whose first copy names them."""
        return {"faces": people.twins(face_scope(pick_areas(user, areas)), stale)}

    @app.get("/api/faces/doubles", tags=["people"])
    def face_doubles(areas: str | None = Query(None), stale: float = Query(0, ge=0, le=900),
                     user: User = Depends(current_user)):
        """Named faces whose name stands twice in their picture – one of the two is wrong."""
        return {"faces": people.doubles(face_scope(pick_areas(user, areas)), stale)}

    @app.get("/api/faces/groups/{group}", tags=["people"])
    def face_group(group: int, areas: str | None = Query(None), limit: int = Query(120, ge=1, le=5000),
                   stale: float = Query(0, ge=0, le=900), user: User = Depends(current_user)):
        """One bundle of faces without a name, the first `limit` of them."""
        found = people.bundle(face_scope(pick_areas(user, areas)), group, limit, stale)
        if found is None:
            raise HTTPException(status_code=404, detail="group not found")
        return found

    def face_and_photos(face_id: int, user: User, areas: list[str]) -> tuple[dict, list[dict]]:
        face = people.face(face_id)
        photos = index.with_hash(face["hash"], areas) if face else []
        if not photos:
            raise HTTPException(status_code=404, detail="face not found")
        return face, photos

    @app.get("/api/faces/{entry}/thumb", tags=["people"])
    def face_thumb(entry: int, user: User = Depends(current_user)):
        face, photos = face_and_photos(entry, user, allowed_areas(user))
        try:
            return FileResponse(people.thumb(face, photos[0]), media_type="image/jpeg", headers=IMMUTABLE)
        except Exception as exc:
            raise HTTPException(status_code=404, detail="no preview") from exc

    @app.post("/api/faces/name", tags=["people"])
    def name_faces(body: FacesNamed, user: User = Depends(current_user)):
        """Confirm a name for found faces: it is written into every photo that holds one of them."""
        return write_names(body.faces, body.name, body.batch, user)

    @app.post("/api/faces/unname", tags=["people"])
    def unname_faces(body: FacesUnnamed, user: User = Depends(current_user)):
        """Take the name off found faces again – in the photo itself, to be taken back like every change.
        The faces stand among the unnamed ones afterwards."""
        return write_names(body.faces, "", body.batch, user)

    def write_names(faces: list[int], name: str, batch: str | None, user: User) -> list[dict]:
        if settings.readonly:
            raise HTTPException(status_code=403, detail="read-only")
        mine = [a for a in allowed_areas(user) if user.can_write(a)]
        results = []
        for face_id in faces:
            try:
                face, photos = face_and_photos(face_id, user, mine)
                for photo in photos:
                    box = turn_box((face["x"], face["y"], face["w"], face["h"]), photo["orientation"] or 1)
                    editor.faces(photo, [dict(zip("xywh", box), name=name)], user.name, batch)
                results.append({"face": face_id, "ok": True})
            except HTTPException as exc:
                results.append({"face": face_id, "ok": False, "error": exc.detail})
            except EditError as exc:
                results.append({"face": face_id, "ok": False, "error": str(exc)})
            except Exception as exc:        # one broken file must not stop the rest
                log.exception("naming face %s failed", face_id)
                results.append({"face": face_id, "ok": False, "error": f"{type(exc).__name__}: {exc}"[:200]})
        return results

    @app.post("/api/faces/ignore", tags=["people"])
    def ignore_faces(body: FacesIgnored, user: User = Depends(current_user)):
        """Wave found faces away – strangers, statues, posters – or bring them back. Nothing is
        written to a photo, so this also works while the photos are read-only; it takes the right
        to write in the area all the same."""
        mine = [a for a in allowed_areas(user) if user.can_write(a)]
        done = 0
        for face_id in body.faces:
            face = people.face(face_id)
            if face and index.with_hash(face["hash"], mine):
                people.ignore(face, user.name, body.ignored)
                done += 1
        return {"done": done, "missed": len(body.faces) - done}

    @app.get("/api/faces/ignored", tags=["people"])
    def ignored_faces(areas: str | None = Query(None), limit: int = Query(120, ge=1, le=2000),
                      user: User = Depends(current_user)):
        """The faces waved away, the latest first, each once."""
        found = people.waved_away(face_scope(pick_areas(user, areas)))
        return {"total": len(found), "faces": found[:limit]}

    @app.delete("/api/faces/ignored", tags=["people"])
    def restore_faces(user: User = Depends(require_admin)):
        """Bring every face waved away back among those waiting for a name."""
        return {"restored": people.restore_all()}

    # -------------------------------------------------------- long jobs

    app.state.job = {"kind": "", "running": False, "done": 0, "total": 0, "failed": 0, "errors": [], "what": "",
                     "stop": False, "started": 0.0, "ended": 0.0}

    def start_job(kind: str, what: str, items: list, work) -> None:
        """One long job at a time, in the background; the page asks how far it is."""
        job = app.state.job
        if job["running"]:
            raise HTTPException(status_code=409, detail="another job is running")
        job.update(kind=kind, what=what, running=True, done=0, total=len(items), failed=0, errors=[],
                   stop=False, started=time.time(), ended=0.0)

        def run() -> None:
            try:
                for item in items:
                    if job["stop"]:         # somebody asked to break off; what is done stays done
                        break
                    try:
                        work(item)
                    except Exception as exc:
                        job["failed"] += 1
                        if len(job["errors"]) < 30:
                            job["errors"].append({"path": item.get("path", ""), "error": str(exc)[:200]})
                    job["done"] += 1
            finally:
                job.update(running=False, ended=time.time())

        threading.Thread(target=run, daemon=True).start()

    @app.get("/api/jobs", tags=["system"])
    def jobs(user: User = Depends(current_user)):
        """Everything that works in the background, and how far it is."""
        pictures = index._all("SELECT COUNT(DISTINCT hash) n FROM photos WHERE kind = 'image' AND error = '' AND area IN "
                              f"({', '.join('?' * len(face_areas())) or 'NULL'})", face_areas())[0]["n"]
        return {"scan": {**index.progress, "every_minutes": settings.rescan_minutes, "photos": index.count()},
                "faces": {"enabled": bool(face_areas()), "areas": face_areas(), "wanted": pictures, **people.state()},
                "dupes": {**dupes.state(), "wanted": index._all(
                    "SELECT COUNT(DISTINCT hash) n FROM photos WHERE kind = 'image' AND error = ''")[0]["n"]},
                "job": dict(app.state.job), "readonly": settings.readonly}

    @app.post("/api/faces/search", tags=["people"])
    def search_faces_now(user: User = Depends(require_admin)):
        """Wake the search for faces – it looks at every picture it has not seen yet."""
        if not face_areas():
            raise HTTPException(status_code=400, detail="face search is switched off")
        app.state.faces_wake.set()
        return {"ok": True}

    # -------------------------------------------------------- is it a face at all? (asks the AI)

    FACE_SYSTEM = ("Du sortierst kleine Bildausschnitte, die ein Programm für Gesichter gehalten hat. Du sagst nur, ob "
                   "ein Ausschnitt überhaupt das Gesicht eines Menschen zeigt – nie, wer es ist. Antworte nur mit JSON.")
    PACK = 20

    def face_question(count: int) -> str:
        return (f"Du siehst {count} Ausschnitte, in dieser Reihenfolge nummeriert von 1 bis {count}. Für jeden: Zeigt er das "
                "Gesicht eines echten Menschen? Ja auch bei Profil, Unschärfe, Sonnenbrille, Baby, teilweise verdeckt oder "
                "wenn der Mensch auf einem Plakat oder Foto im Foto zu sehen ist. Nein bei Gegenständen (Rad, Lampe, "
                "Schalter), Tieren, Puppen, Statuen, Masken, Zeichnungen, Mustern oder einem Hinterkopf.\n"
                f'Antworte genau so: {{"gesichter": [true, false, …]}} mit genau {count} Einträgen.')

    def ai_areas(user: User) -> list[str]:
        return [area for area in face_scope(allowed_areas(user)) if ai.allowed_for(area)]

    @app.get("/api/faces/no-face", tags=["people"])
    def no_faces(limit: int = Query(120, ge=1, le=2000), stale: float = Query(0, ge=0, le=900),
                 user: User = Depends(current_user)):
        """What the AI took for no face at all – a proposal for waving away, nothing more – and how
        many faces among the strangers nobody has judged yet."""
        waiting, none = people.verdicts(ai_areas(user), stale)
        return {"total": len(none), "faces": none[:limit], "waiting": len(waiting), "enabled": ai.load().enabled}

    @app.post("/api/faces/no-face/check", tags=["people"], status_code=202)
    def check_faces(body: FaceCheck, user: User = Depends(require_admin)):
        """Ask the AI about the faces among the strangers nobody has judged yet: a face or not?
        Only the small cut-outs are sent, twenty a question. It costs money; nothing is written
        to a photo and nothing is waved away by itself."""
        if not ai.load().enabled:
            raise HTTPException(status_code=400, detail="AI is disabled")
        areas = ai_areas(user)
        waiting, _ = people.verdicts(areas)
        waiting = waiting[:body.limit]
        packs = [{"path": f"Gesichter {at + 1}–{at + len(waiting[at:at + PACK])}", "faces": waiting[at:at + PACK]}
                 for at in range(0, len(waiting), PACK)]

        def work(pack: dict) -> None:
            pictures = []
            for face in pack["faces"]:
                photos = index.with_hash(face["hash"], areas)
                if not photos:
                    raise EditError("a photo is gone")
                x, y, w, h = face["stored"]
                pictures.append(people.thumb({"hash": face["hash"], "x": x, "y": y, "w": w, "h": h}, photos[0]).read_bytes())
            try:
                answer = ai.complete(FACE_SYSTEM, face_question(len(pictures)), max_tokens=1500, images=pictures)
            except AIError as exc:
                if any(word in str(exc) for word in ("limit", "disabled", "API key", "unknown model")):
                    app.state.job["stop"] = True        # asking on would only fail again
                raise
            found = re.search(r"\{.*\}", answer.text or "", re.DOTALL)
            try:
                said = json.loads(found.group(0))["gesichter"] if found else None
            except (ValueError, KeyError, TypeError):
                said = None
            if not isinstance(said, list) or len(said) != len(pictures) or not all(isinstance(v, bool) for v in said):
                raise AIError("the answer was not in the agreed form")
            people.judge(pack["faces"], said)

        start_job("aicheck", f"{len(waiting)} Gesichter", packs, work)
        return {"started": len(waiting), "questions": len(packs)}

    @app.post("/api/faces/no-face/keep", tags=["people"])
    def keep_faces(body: FacesIgnored, user: User = Depends(current_user)):
        """These are faces after all: take them out of the AI's list. Nothing is written to a photo."""
        mine = [a for a in allowed_areas(user) if user.can_write(a)]
        chosen = []
        for face_id in body.faces:
            face = people.face(face_id)
            if face and index.with_hash(face["hash"], mine):
                chosen.append({"hash": face["hash"], "stored": (face["x"], face["y"], face["w"], face["h"])})
        people.judge(chosen, [True] * len(chosen))
        return {"done": len(chosen)}

    # -------------------------------------------------------- pictures that are there twice

    @app.post("/api/dupes/search", tags=["dupes"], status_code=202)
    def search_dupes(user: User = Depends(require_admin)):
        """Look at every picture not looked at yet, so that copies can be found. Only reads."""
        if dupes.progress["running"]:
            raise HTTPException(status_code=409, detail="already running")
        areas = [a["name"] for a in index.areas()]
        threading.Thread(target=lambda: dupes.search(areas), daemon=True).start()
        return {"ok": True}

    def dupe_groups(user: User, areas: str | None, level: str = "copies") -> list[dict]:
        return dupes.groups(pick_areas(user, areas), level)

    @app.get("/api/dupes", tags=["dupes"])
    def list_dupes(areas: str | None = Query(None), level: Literal["copies", "alike"] = "copies",
                   limit: int = Query(30, ge=0, le=200), offset: int = Query(0, ge=0), user: User = Depends(current_user)):
        """Pictures that are there more than once – the same data in two files, or the same picture
        saved again in another size or quality; with level "alike" also shots of the same thing.
        The first file of each bundle is the one worth keeping."""
        found = dupe_groups(user, areas, level)
        may = not settings.readonly
        return {"total": len(found), "files": sum(len(bundle["files"]) for bundle in found),
                "spare": sum(len(bundle["files"]) - 1 for bundle in found), **dupes.state(),
                "sure": sum(1 for bundle in found if bundle["sure"]),
                "sure_spare": sum(len(bundle["files"]) - 1 for bundle in found if bundle["sure"]),
                "groups": [dict(bundle, files=[dict(row, can_delete=may and user.can_write(row["area"]))
                                               for row in bundle["files"]]) for bundle in found[offset:offset + limit]]}

    @app.post("/api/dupes/apart", tags=["dupes"])
    def dupes_apart(body: Apart, user: User = Depends(current_user)):
        """These look alike but are different pictures: do not bundle them again. Nothing is written to a photo."""
        for bundle in dupe_groups(user, None, body.level):
            if bundle["id"] == body.id:
                if not all(user.can_write(row["area"]) for row in bundle["files"]):
                    raise HTTPException(status_code=403, detail="no write access to this area")
                if bundle["exact"]:
                    raise HTTPException(status_code=422, detail="these files hold the very same picture")
                return {"apart": dupes.keep_apart(bundle["hashes"], user.name)}
        raise HTTPException(status_code=404, detail="group not found")

    def settle(keeper: dict, others: list[dict], merge: bool, name: str, batch: str) -> dict:
        """One file stays, the others go to the trash. With `merge`, what only the others know is
        written into the one that stays first; if that fails (EditError), nothing is moved."""
        merged: list[str] = []
        if merge:
            change: dict = {}
            for field, add in (("albums", "add_albums"), ("keywords", "add_keywords")):
                more = list(dict.fromkeys(item for other in others for item in other[field] if item not in keeper[field]))
                if more:
                    change[add] = more
                    merged.append(field)
            for other in others:
                if not keeper.get("description") and other.get("description") and "description" not in change:
                    change["description"] = other["description"]
                    merged.append("description")
                if not keeper.get("creator") and other.get("creator") and "creator" not in change:
                    change["creator"] = other["creator"]
                    merged.append("creator")
                if not keeper.get("rating") and other.get("rating") and "rating" not in change:
                    change["rating"] = other["rating"]
                    merged.append("rating")
                if keeper.get("lat") is None and other.get("lat") is not None and "lat" not in change:
                    change["lat"], change["lon"] = other["lat"], other["lon"]
                    merged.append("place")
            known = {person.casefold() for person in keeper["people"]}
            faces = []
            for other in others:
                for region in other["regions"]:
                    if is_face(region["type"]) and region["name"] and region["name"].casefold() not in known:
                        known.add(region["name"].casefold())
                        box = turn_box((region["x"], region["y"], region["w"], region["h"]), other["orientation"] or 1)
                        faces.append(dict(zip("xywh", box), name=region["name"]))
            if change:
                keeper = editor.change(keeper, change, name, batch)
            if faces:
                editor.faces(index.by_path(keeper["path"]), faces, name, batch)
                merged.append("people")
        removed, failed = [], []
        for other in others:
            try:
                trash.put(other, name)
                removed.append(other["key"])
            except TrashError as exc:
                failed.append({"key": other["key"], "error": str(exc)})
        return {"keep": keeper["key"], "removed": removed, "failed": failed, "merged": merged}

    @app.post("/api/dupes/resolve", tags=["dupes"])
    def resolve_dupes(body: Resolve, user: User = Depends(current_user)):
        """Keep one file of a picture and move the others to the trash. With `merge`, what only the
        others know – albums, keywords, names of people, description, rating, place – is written into
        the one that stays first; if that fails, nothing is moved."""
        keeper = writable(photo_or_404(body.keep, user), user)
        others = [writable(photo_or_404(key, user), user) for key in dict.fromkeys(body.drop) if key != body.keep]
        if not others:
            raise HTTPException(status_code=422, detail="nothing to remove")
        try:
            return settle(keeper, others, body.merge, user.name, secrets.token_hex(6))
        except EditError as exc:
            raise HTTPException(status_code=422, detail=f"could not carry over: {exc}") from exc

    @app.post("/api/dupes/resolve-all", tags=["dupes"], status_code=202)
    def resolve_all_dupes(body: ResolveAll, user: User = Depends(require_admin)):
        """Every copy at once, as a long job: of each picture that is there more than once the
        biggest file stays, the others go to the trash. Only for copies that are sure – the same
        data or another size –, never for shots that are merely alike. A bundle whose details cannot be carried over is left as it is."""
        if settings.readonly:
            raise HTTPException(status_code=403, detail="read-only")
        areas = allowed_areas(user)
        todo = [{"path": bundle["files"][0]["path"], "keys": [row["key"] for row in bundle["files"]]}
                for bundle in dupes.groups(areas, "copies") if bundle["sure"]]
        batch = secrets.token_hex(6)

        def work(item: dict) -> None:
            photos = [index.photo(key, areas) for key in item["keys"]]
            if len(photos) < 2 or not all(photos):
                raise EditError("the files have changed since")
            done = settle(photos[0], photos[1:], body.merge, user.name, batch)
            if done["failed"]:
                raise EditError(done["failed"][0]["error"])

        start_job("dupes", f"{len(todo)} Bilder", todo, work)
        return {"started": len(todo)}

    @app.post("/api/job/stop", tags=["system"])
    def stop_job(user: User = Depends(require_admin)):
        """Break off the long job that is running. What it has done so far stays."""
        app.state.job["stop"] = True
        return {"ok": True}

    @app.get("/api/job", tags=["system"])
    def job_state(user: User = Depends(current_user)):
        return dict(app.state.job)

    @app.post("/api/albums/from-folder", tags=["changes"], status_code=202)
    def folder_to_album(body: FolderToAlbum, user: User = Depends(current_user)):
        """Write into every picture of a folder which folder it lies in – as an album.

        Afterwards the folder is no longer needed to know what belongs together.
        """
        folder = body.folder.strip("/")
        area = folder.split("/", 1)[0]
        if settings.readonly:
            raise HTTPException(status_code=403, detail="read-only")
        if not folder or not user.can_write(area):
            raise HTTPException(status_code=403, detail="no write access to this area")
        if "/" not in folder:
            raise HTTPException(status_code=400, detail="choose a folder inside the area")
        batch = secrets.token_hex(6)

        def work(photo: dict) -> None:
            editor.change(photo, {"add_albums": [photo["folder"].split("/", 1)[1]]}, user.name, batch)

        items = index.in_folder(folder)
        start_job("album", folder, items, work)
        return {"total": len(items)}

    # ----------------------------------------------------------- sorting

    def sorting_state() -> dict:
        stored = prefs.load()
        return {"rules": stored["sort_rules"], "modes": stored["sort_modes"], "blocks": list(TOKENS),
                "misfits": index.count_where([a["name"] for a in index.areas()], misfit=True)}

    @app.get("/api/sorting", tags=["sorting"])
    def get_sorting(user: User = Depends(require_admin)):
        """The rules that say where photos belong and what they are called."""
        return sorting_state()

    @app.put("/api/sorting", tags=["sorting"])
    def put_sorting(body: SortingChange, user: User = Depends(require_admin)):
        try:
            rules = clean_rules(body.rules)
        except SortError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        modes = {area: mode for area, mode in body.modes.items() if mode in MODES}
        prefs.save(sort_rules=rules, sort_modes=modes)
        index.planner = Rules(rules)
        index.replan()
        return sorting_state()

    def sort_one(photo: dict, name: str, batch: str | None) -> dict:
        try:
            return sorter.move(photo, name, batch)
        except SortError as exc:
            raise EditError(str(exc)) from exc

    def folder_of(path: str, user: User) -> Path:
        """A folder path as somebody typed it, checked: inside an area the user may write to, nothing hidden."""
        parts = [part.strip() for part in path.strip("/").split("/")]
        if len(parts) < 2 or len(parts) > 12 or any(
                not part or part.startswith((".", "@")) or part.endswith(".") or len(part) > 100
                or any(ord(sign) < 32 or sign in '\\:*?"<>|' for sign in part) for part in parts):
            raise HTTPException(status_code=400, detail="invalid folder name")
        if settings.readonly:
            raise HTTPException(status_code=403, detail="read-only")
        if parts[0] not in allowed_areas(user) or not user.can_write(parts[0]):
            raise HTTPException(status_code=403, detail="no write access to this area")
        return settings.photos.joinpath(*parts)

    @app.post("/api/folders", tags=["sorting"], status_code=201)
    def folder_create(body: FolderIn, user: User = Depends(current_user)):
        """A new, empty folder inside an area – a place to move photos to."""
        target = folder_of(body.path, user)
        if target.exists():
            raise HTTPException(status_code=409, detail="folder exists")
        try:
            target.mkdir(parents=True)
            for made in [target, *target.parents]:
                if made == settings.photos:
                    break
                os.chmod(made, 0o775)
        except OSError as exc:
            raise HTTPException(status_code=400, detail=f"cannot create the folder: {exc.strerror}") from exc
        index.folders(fresh=True)
        return {"path": str(target.relative_to(settings.photos)).replace(os.sep, "/")}

    @app.delete("/api/folders", tags=["sorting"])
    def folder_delete(path: str = Query(..., max_length=1000), user: User = Depends(current_user)):
        """Remove a folder – only an empty one; the area itself is removed in the settings."""
        target = folder_of(path, user)
        if not target.is_dir():
            raise HTTPException(status_code=404, detail="unknown folder")
        try:
            target.rmdir()                      # refuses unless really empty
        except OSError as exc:
            raise HTTPException(status_code=409, detail="folder is not empty") from exc
        index.folders(fresh=True)
        return {"ok": True}

    @app.post("/api/move", tags=["sorting"])
    def move_many(body: ManyMove, user: User = Depends(current_user)):
        """Hand the chosen photos over to another area – from an inbox into the library, say. They land
        where that area's rule wants them, else in its top folder. Needs write access on both sides."""
        folder = (body.folder or body.area).strip("/")
        area = folder.split("/", 1)[0]
        if not area or area not in allowed_areas(user) or not user.can_write(area):
            raise HTTPException(status_code=403, detail="no write access to this area")

        def move_one(photo: dict) -> dict:
            try:
                return sorter.transfer(photo, folder, user.name, body.batch)
            except SortError as exc:
                raise EditError(str(exc)) from exc

        return many(move_one, body.keys, user)

    @app.post("/api/sort", tags=["sorting"])
    def sort_many(body: Many, user: User = Depends(current_user)):
        """Put the chosen photos where the rules want them."""
        return many(lambda p: sort_one(p, user.name, body.batch), body.keys, user)

    @app.get("/api/sort/preview", tags=["sorting"])
    def sort_preview(areas: str | None = Query(None), chosen: dict = Depends(filters),
                     limit: int = Query(200, ge=1, le=1000), user: User = Depends(current_user)):
        """What sorting would do: old place -> new place, for the photos that do not fit."""
        mine = [a for a in pick_areas(user, areas) if user.can_write(a)]
        chosen = dict(chosen, misfit=True)
        rows = index.misfits(mine, limit, **{k: v for k, v in chosen.items() if k != "misfit"})
        return {"total": index.count_where(mine, **chosen),
                "moves": [{"key": r["key"], "from": r["path"], "to": sorter.plan(r)} for r in rows]}

    @app.post("/api/sort/all", tags=["sorting"])
    def sort_all(areas: str | None = Query(None), chosen: dict = Depends(filters),
                 batch: str | None = Query(None, pattern=r"^[0-9a-f]{6,32}$"),
                 user: User = Depends(current_user)):
        """Sort a portion of everything that does not fit; call again until nothing is left."""
        if settings.readonly:
            raise HTTPException(status_code=403, detail="read-only")
        mine = [a for a in pick_areas(user, areas) if user.can_write(a)]
        chosen = dict(chosen, misfit=True)
        plain = {k: v for k, v in chosen.items() if k != "misfit"}
        moved, errors = 0, []
        for row in index.misfits(mine, 300, **plain):
            try:
                sorter.move(row, user.name, batch)
                moved += 1
            except SortError as exc:
                errors.append({"path": row["path"], "error": str(exc)})
        return {"moved": moved, "errors": errors[:20], "failed": len(errors),
                "left": index.count_where(mine, **chosen)}

    # ---------------------------------------------------------------- AI

    def ai_state(lang: str = "de") -> dict:
        config = ai.load()
        data = config.public()
        offered, default = prompts_of(config, lang)
        data["builtin"] = not config.prompts
        data["prompts"] = offered
        data["default_prompt"] = default
        return data

    @app.get("/api/ai", tags=["ai"])
    def get_ai(user: User = Depends(require_admin), lang: str = Depends(request_lang)):
        return ai_state(lang)

    @app.put("/api/ai", tags=["ai"])
    def put_ai(body: AIChange, user: User = Depends(require_admin), lang: str = Depends(request_lang)):
        try:
            ai.update(body.model_dump(exclude_unset=True))
        except (AIError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return ai_state(lang)

    @app.get("/api/ai/models", tags=["ai"])
    def ai_models(user: User = Depends(require_admin)):
        """The models the chosen provider offers."""
        return ai.list_models()

    @app.post("/api/ai/test", tags=["ai"])
    def ai_test(user: User = Depends(require_admin)):
        """A tiny question to see whether provider, key and model work."""
        try:
            answer = ai.complete("Antworte mit genau einem Wort.", "Sag: bereit", max_tokens=2000, force=True)
        except AIError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"answer": answer.text[:200], "model": answer.model, "seconds": answer.seconds, "cost": answer.cost}

    @app.get("/api/ai/prompts", tags=["ai"])
    def ai_prompts(user: User = Depends(current_user), lang: str = Depends(request_lang)):
        """What "KI fragen" offers: is it on, which instructions are there."""
        config = ai.load()
        offered, default = prompts_of(config, lang)
        return {"enabled": config.enabled, "prompts": [p["name"] for p in offered], "default": default,
                "excluded_areas": config.excluded_areas, "memories": config.enabled and config.memories,
                "inbox_auto": config.enabled and config.inbox_auto}

    # ---------------------------------------------------------- memories

    def shown_memory(memory: dict) -> dict:
        return {**{k: memory[k] for k in ("id", "kind", "sub", "since", "until", "days", "count", "cover", "picks", "text")},
                "title": memory["named"] or memory["title"], "found": memory["title"], "by_ai": bool(memory["named"])}

    @app.get("/api/memories", tags=["photos"])
    def list_memories(areas: str | None = Query(None), user: User = Depends(current_user),
                      lang: str = Depends(request_lang)):
        """Journeys and looks back, found in the photos: each a stretch of time with a few pictures
        that stand for it. Nothing is stored for them."""
        return [shown_memory(m) for m in memories.list(pick_areas(user, areas), lang=lang)]

    @app.post("/api/memories/{memory}/ai", tags=["ai"])
    def name_memory(memory: str, areas: str | None = Query(None), user: User = Depends(current_user),
                    lang: str = Depends(request_lang)):
        """Let the AI look at the pictures that stand for a memory: it gives a title, a sentence and
        says which picture should be the cover. Kept in the data folder, not in any photo."""
        config = ai.load()
        if not config.enabled or not config.memories:
            raise HTTPException(status_code=422, detail="AI is disabled")
        mine = [a for a in pick_areas(user, areas) if ai.allowed_for(a)]
        found = memories.get(memory, mine, lang)
        if not found:
            raise HTTPException(status_code=404, detail="memory not found")
        shown, pictures = [], []
        for key in found["picks"][:8]:
            photo = index.photo(key, mine)
            if not photo or photo["error"]:
                continue
            try:
                rendered(photo, settings.previews, PREVIEW_EDGE)
                with Image.open(cache_path(settings.previews, photo["hash"], photo["orientation"] or 1)) as small:
                    pictures.append(render(small, 768))
                shown.append(photo)
            except Exception:
                continue
        if not pictures:
            raise HTTPException(status_code=422, detail="the picture cannot be read")
        try:
            answer = ai.complete(in_language(MEMORY_SYSTEM, lang), memory_prompt(found, shown), max_tokens=1500, images=pictures)
            note = parse_memory(answer.text, [p["key"] for p in shown])
        except AIError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        memories.note(memory, {**note, "model": answer.model})
        return {**shown_memory(memories.get(memory, mine, lang)), "cost": answer.cost}

    @app.delete("/api/memories/{memory}/ai", tags=["ai"])
    def unname_memory(memory: str, areas: str | None = Query(None), user: User = Depends(current_user),
                      lang: str = Depends(request_lang)):
        """Forget what the AI said about a memory: it is called what was found again."""
        if settings.readonly:
            raise HTTPException(status_code=403, detail="read-only")
        memories.note(memory, None)
        found = memories.get(memory, pick_areas(user, areas), lang)
        if not found:
            raise HTTPException(status_code=404, detail="memory not found")
        return shown_memory(found)

    @app.post("/api/photos/{key}/ai", tags=["ai"])
    def ask_ai(key: str, body: AskAI, user: User = Depends(current_user), lang: str = Depends(request_lang)):
        """Let the AI look at the photo and propose description, keywords and – for undated ones – a date.

        Nothing is written; the proposal goes back to the person, who decides.
        """
        photo = writable(photo_or_404(key, user), user)
        if photo["kind"] != "image" or photo["error"]:
            raise HTTPException(status_code=422, detail="only pictures can be shown to the AI")
        config = ai.load()
        if not config.enabled:
            raise HTTPException(status_code=422, detail="AI is disabled")
        if not ai.allowed_for(photo["area"]):
            raise HTTPException(status_code=403, detail="AI is not allowed for this area")
        task = (body.instructions or "").strip()
        if not task:
            offered, default = prompts_of(config, lang)
            wanted = (body.prompt or default).casefold()
            task = next((p["text"] for p in offered if p["name"].casefold() == wanted), "")
            if not task:
                raise HTTPException(status_code=404, detail="unknown instruction")
        try:
            answer = look_at(photo, task, lang)
        except AIError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if photo["area"] in app.state.inbox.areas():
            proposals.put(photo["path"], answer)        # kept, so it is still there when the picture is opened again
        return answer

    def look_at(photo: dict, task: str, lang: str, model: str = "") -> dict:
        """Show the picture to the AI with the task; what comes back is a proposal, nothing is written."""
        try:
            image, _ = read_image(file_of(photo).read_bytes())
            picture = render(image, 1568)
        except HTTPException as exc:
            raise AIError("the picture cannot be read") from exc
        except Exception as exc:
            raise AIError("the picture cannot be read") from exc
        prompt = photo_prompt(photo, task, index.common_keywords([photo["area"]]))
        answer = ai.complete(in_language(PHOTO_SYSTEM, lang), prompt, max_tokens=4000, images=[picture], model=model)
        return {**parse_suggestion(answer.text), "model": answer.model, "seconds": answer.seconds, "cost": answer.cost}

    # ------------------------------------------------------------- the pictures that are there already

    # The one place where the AI writes by itself: nobody confirms twenty thousand proposals one
    # by one. It only fills what is empty – keywords where there are none, a description where
    # there is none – and every picture gets its line in the journal, so it can be taken back.
    STOCK_USER = "KI"
    STOCK_STOPS = ("monthly limit reached", "AI is disabled", "API key rejected", "no API key", "unknown model")
    STOCK_COST = 0.0006      # USD per picture with Haiku 5.5, measured on 2026-11-15

    def stock_model(config, wanted: str = "") -> str:
        return wanted or ("claude-haiku-5-5" if config.provider == "anthropic" else config.model)

    def stock_waiting(user: User) -> list[dict]:
        """The pictures nobody has given keywords yet, in the areas the AI may see and the asker may write to."""
        arrives = app.state.inbox.areas()
        areas = [a for a in allowed_areas(user) if a not in arrives and user.can_write(a) and ai.allowed_for(a)]
        if not areas:
            return []
        raw = ", ".join("?" * len(RAW_EXT))
        return [dict(row) for row in index._all(
            f"SELECT path, area FROM photos WHERE kind = 'image' AND error = '' AND keywords = '' AND ext NOT IN ({raw}) "
            f"AND area IN ({', '.join('?' * len(areas))}) ORDER BY taken DESC", [*RAW_EXT, *areas])]

    @app.get("/api/ai/stock", tags=["ai"])
    def ai_stock(model: str = Query("", max_length=100), user: User = Depends(require_admin)):
        """How many pictures still wait for keywords, and roughly what looking at them costs."""
        config = ai.load()
        waiting = stock_waiting(user)
        areas: dict[str, int] = {}
        for item in waiting:
            areas[item["area"]] = areas.get(item["area"], 0) + 1
        chosen = stock_model(config, model)
        price = PRICES.get(chosen) if config.provider == "anthropic" else None
        return {"waiting": len(waiting), "areas": areas, "model": chosen, "enabled": config.enabled,
                "models": [{"id": m, "label": MODEL_LABELS.get(m, m)} for m in PRICES] if config.provider == "anthropic" else [],
                "cost": round(len(waiting) * STOCK_COST * price[0] / PRICES["claude-haiku-5-5"][0], 2) if price else None}

    @app.post("/api/ai/stock", tags=["ai"], status_code=202)
    def ai_stock_run(body: StockRun, user: User = Depends(require_admin), lang: str = Depends(request_lang)):
        """Let the AI look at every picture without keywords and write what it finds into the picture."""
        if settings.readonly:
            raise HTTPException(status_code=403, detail="read-only")
        config = ai.load()
        if not config.enabled:
            raise HTTPException(status_code=422, detail="AI is disabled")
        items = stock_waiting(user)
        if body.areas:
            items = [item for item in items if item["area"] in body.areas]
        items = items[:body.limit] if body.limit else items
        model = stock_model(config, (body.model or "").strip())
        offered, default = prompts_of(config, lang)
        task = next((p["text"] for p in offered if p["name"].casefold() == default.casefold()), offered[0]["text"])
        batch = secrets.token_hex(6)
        job = app.state.job

        def work(item: dict) -> None:
            photo = index.by_path(item["path"])
            if not photo or photo["keywords"] or not ai.allowed_for(photo["area"]):
                return                              # gone, or somebody was quicker
            try:
                answer = look_at(photo, task, lang, model)
            except AIError as exc:
                if any(reason in str(exc) for reason in STOCK_STOPS):
                    job["stop"] = True              # no use asking twenty thousand times
                raise
            change: dict = {}
            if answer["keywords"]:
                change["add_keywords"] = answer["keywords"]
            if answer["description"] and not photo["description"]:
                change["description"] = answer["description"]
            if not change:
                raise AIError("the AI found nothing to say")
            editor.change(photo, change, STOCK_USER, batch)

        start_job("aistock", MODEL_LABELS.get(model, model), items, work)
        return {"total": len(items), "model": model}

    # ------------------------------------------------------------- inbox

    def inbox_pictures() -> list[dict]:
        areas = app.state.inbox.areas()
        return index.pictures(areas) if areas else []

    def propose_round(stop: threading.Event, most: int = 25) -> int:
        """One round of the AI looking at inbox pictures: first those put in line by hand, then – if
        switched on – those nobody has asked about yet. Stops at the monthly limit."""
        pictures = inbox_pictures()
        by_path = {p["path"]: p for p in pictures}
        proposals.tidy(set(by_path))
        config = ai.load()
        if not config.enabled:
            proposals.clear_line()
            return 0
        line = [p for p in proposals.wanted() if p in by_path]
        if config.inbox_auto:
            known = proposals.known()
            line += [p["path"] for p in pictures if p["path"] not in known and p["path"] not in line]
        line = [p for p in line if ai.allowed_for(by_path[p]["area"])]
        if not line:
            return 0
        lang = known_lang(app.state.security.load().language)
        offered, default = prompts_of(config, lang)
        task = next((p["text"] for p in offered if p["name"].casefold() == default.casefold()), offered[0]["text"])
        done = 0
        proposals.progress.update(running=True, done=0, left=len(line[:most]), error="")
        try:
            for path in line[:most]:
                if stop.is_set():
                    break
                current = index.by_path(path)
                if current:
                    try:
                        proposals.put(path, look_at(current, task, lang))
                    except AIError as exc:
                        if "limit" in str(exc) or "disabled" in str(exc):
                            proposals.progress["error"] = str(exc)
                            break               # nothing more this month – or switched off meanwhile
                        proposals.fail(path, str(exc))
                proposals.served(path)
                done += 1
                proposals.progress.update(done=done, left=len(line[:most]) - done)
        finally:
            proposals.progress["running"] = False
        if done:
            log.info("inbox: the AI looked at %s pictures", done)
            if len(line) > most:
                app.state.inbox_wake.set()      # more is waiting
        return done

    app.state.propose_round = propose_round

    @app.get("/api/inbox/proposals", tags=["inbox"])
    def inbox_proposals(user: User = Depends(current_user)):
        """How far the AI is with the inbox: waiting, done, and whether it looks by itself."""
        config = ai.load()
        return {**proposals.progress, "waiting": len(proposals.wanted()),
                "auto": config.enabled and config.inbox_auto}

    @app.post("/api/inbox/proposals", tags=["inbox"], status_code=202)
    def inbox_want(body: InboxPropose, user: User = Depends(current_user)):
        """Put chosen pictures of the inbox in line for the AI; it looks at them shortly after."""
        if not ai.load().enabled:
            raise HTTPException(status_code=422, detail="AI is disabled")
        inbox_areas = app.state.inbox.areas()
        paths = []
        for key in body.keys:
            photo = index.photo(key, allowed_areas(user))
            if photo and photo["area"] in inbox_areas and photo["kind"] == "image" and not photo["error"] \
                    and user.can_write(photo["area"]) and ai.allowed_for(photo["area"]):
                paths.append(photo["path"])
        added = proposals.want(paths)
        app.state.inbox_wake.set()
        return {"queued": added, "skipped": len(body.keys) - len(paths)}

    @app.delete("/api/photos/{key}/proposal", tags=["inbox"])
    def drop_proposal(key: str, user: User = Depends(current_user)):
        """Throw away what the AI proposed for this picture; it is not asked again by itself."""
        photo = writable(photo_or_404(key, user), user)
        proposals.fail(photo["path"], "thrown away")
        return {"ok": True}

    @app.post("/api/inbox/accept", tags=["inbox"])
    def inbox_accept(body: InboxAccept, user: User = Depends(current_user)):
        """Write what the AI proposed into the pictures – the keywords added to those there, the
        description where there is none yet – and hand them on to another area, where its rule wants
        them. For one picture, the person's own choice can be given instead of the stored proposal."""
        folder = (body.folder or body.area).strip("/")
        area = folder.split("/", 1)[0]
        inbox_areas = app.state.inbox.areas()
        if not area or area in inbox_areas or area not in allowed_areas(user) or not user.can_write(area):
            raise HTTPException(status_code=403, detail="no write access to this area")
        single = len(body.keys) == 1 and (body.description is not None or body.keywords is not None
                                          or body.taken is not None)

        def accept_one(photo: dict) -> dict:
            if photo["area"] not in inbox_areas:
                raise EditError("not in the inbox")
            change: dict = {}
            if single:
                if body.description is not None and body.description.strip():
                    change["description"] = body.description.strip()
                if body.keywords:
                    change["add_keywords"] = body.keywords
                if body.taken and photo["taken_src"] != "exif":
                    change["taken"] = body.taken
            elif body.take:
                idea = proposals.get(photo["path"]) or {}
                if idea.get("description") and not (photo.get("description") or "").strip():
                    change["description"] = idea["description"]
                if idea.get("keywords"):
                    change["add_keywords"] = idea["keywords"]
            record = photo
            if change and photo["kind"] == "image" and photo["ext"] not in RAW_EXT and not photo["error"]:
                record = editor.change(photo, Change(**change).model_dump(), user.name, body.batch)
            try:
                moved = sorter.transfer(record, folder, user.name, body.batch)
            except SortError as exc:
                raise EditError(str(exc)) from exc
            proposals.drop(photo["path"])
            return moved

        return many(accept_one, body.keys, user)

    # ------------------------------------------------------------- trash

    def to_trash(key: str, user: User) -> dict:
        photo = writable(photo_or_404(key, user), user)
        try:
            return {"key": key, "ok": True, "entry": trash.put(photo, user.name)}
        except TrashError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.delete("/api/photos/{key}", tags=["trash"])
    def delete_photo(key: str, user: User = Depends(current_user)):
        """Move the photo into the trash of its area."""
        return to_trash(key, user)

    @app.post("/api/delete", tags=["trash"])
    def delete_many(body: Many, user: User = Depends(current_user)):
        results = []
        for key in body.keys:
            try:
                results.append(to_trash(key, user))
            except HTTPException as exc:
                results.append({"key": key, "ok": False, "error": exc.detail})
        return results

    def trash_entry(entry: int, user: User, write: bool = True) -> dict:
        found = trash.get(entry)
        if not found or not user.can_read(found["area"]):
            raise HTTPException(status_code=404, detail="entry not found")
        if write:
            writable(found, user)
        return found

    @app.get("/api/trash", tags=["trash"])
    def trash_list(user: User = Depends(current_user)):
        days = prefs.load()["trash_days"]
        entries = trash.list(allowed_areas(user))
        for entry in entries:
            entry.pop("hash")
            entry.pop("stored")
            entry["with"] = len(Trash.extras(entry))        # what lies there with it: a short film, an original
            for inner in ("film", "film_stored", "extras"):
                entry.pop(inner, None)
            entry["until"] = entry["ts"] + days * 86400 if days else None
        return {"days": days, "entries": entries}

    @app.get("/api/trash/{entry}/thumb", tags=["trash"])
    def trash_thumb(entry: int, user: User = Depends(current_user)):
        found = trash_entry(entry, user, write=False)
        target = cache_path(settings.thumbs, found["hash"], found["orientation"])
        if not target.exists():
            raise HTTPException(status_code=404, detail="no preview")
        return FileResponse(target, media_type="image/jpeg", headers=IMMUTABLE)

    @app.post("/api/trash/{entry}/restore", tags=["trash"])
    def trash_restore(entry: int, user: User = Depends(current_user)):
        """Put the photo back where it was."""
        try:
            return public(trash.restore(trash_entry(entry, user)), user)
        except TrashError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.delete("/api/trash/{entry}", tags=["trash"])
    def trash_destroy(entry: int, user: User = Depends(current_user)):
        """Remove the photo for good. This cannot be taken back."""
        try:
            trash.destroy(trash_entry(entry, user))
        except TrashError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"ok": True}

    @app.post("/api/trash/empty", tags=["trash"])
    def trash_empty(user: User = Depends(current_user)):
        """Remove everything in the trash of the areas this person may write to."""
        if settings.readonly:
            raise HTTPException(status_code=403, detail="read-only")
        done = 0
        for found in trash.list([a for a in allowed_areas(user) if user.can_write(a)]):
            try:
                trash.destroy(found)
                done += 1
            except TrashError:
                pass
        return {"removed": done}

    @app.put("/api/trash/settings", tags=["trash"])
    def trash_settings(body: TrashPrefs, user: User = Depends(require_admin)):
        return {"days": prefs.save(trash_days=body.days)["trash_days"]}

    # ------------------------------------------------------------- users

    @app.get("/api/users", tags=["users"])
    def list_users(user: User = Depends(require_admin)):
        return [u.public() for u in app.state.users.all()]

    @app.post("/api/users", tags=["users"], status_code=201)
    def create_user(body: NewUser, user: User = Depends(require_admin)):
        try:
            return app.state.users.create(**body.model_dump()).public()
        except UserError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.patch("/api/users/{name}", tags=["users"])
    def update_user(name: str, body: UserChange, user: User = Depends(require_admin)):
        if name == user.name and body.is_admin is False:
            raise HTTPException(status_code=400, detail="cannot remove your own admin role")
        changes = body.model_dump(exclude_unset=True)
        reset_totp = changes.pop("reset_totp", False)
        sign_out = changes.pop("sign_out", False)
        try:
            account = app.state.users.update(name, **changes)
            if reset_totp and account.has_totp:
                app.state.users.totp_off(name)
                app.state.notify.send("notify_2fa_changes", "2fa.off.title", "2fa.off.body", priority=4,
                                      tags="warning", user=name, by=user.name)
        except UserError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if (sign_out or changes.get("password") or reset_totp) and name != user.name:
            app.state.sessions.revoke(name)
        return app.state.users.get(name).public()

    @app.delete("/api/users/{name}", tags=["users"])
    def delete_user(name: str, user: User = Depends(require_admin)):
        if name == user.name:
            raise HTTPException(status_code=400, detail="cannot delete yourself")
        try:
            app.state.users.delete(name)
        except UserError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        app.state.user_prefs.forget(name)
        app.state.sessions.revoke(name)
        return {"ok": True}

    # -------------------------------------------------------- middleware

    @app.middleware("http")
    async def same_origin_only(request: Request, call_next):
        """Changes only from pages of this very host (protection against cross-site requests).

        Browsers say where a request comes from in Sec-Fetch-Site; older ones only send
        Origin. Requests without either (scripts, the API token) carry no cookies of a
        victim and pass.
        """
        if request.method not in ("GET", "HEAD", "OPTIONS") and request.url.path.startswith("/api/"):
            site = request.headers.get("sec-fetch-site")
            origin = request.headers.get("origin")
            refused = False
            if site:
                refused = site not in ("same-origin", "none")
            elif origin and origin != "null":
                host = request.headers.get("x-forwarded-host") or request.headers.get("host", "")
                refused = origin.split("://", 1)[-1].rstrip("/") != host.split(",")[0].strip()
            elif origin == "null":
                refused = True
            if refused:
                return JSONResponse({"detail": "cross-site request refused"}, status_code=403)
        return await call_next(request)

    @app.middleware("http")
    async def no_stale_ui(request: Request, call_next):
        """The UI must always be revalidated, otherwise a deploy shows the old version."""
        response = await call_next(request)
        if not request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    # ------------------------------------------------- upload from the phone

    inbox = app.state.inbox = Inbox(settings.data / "upload.json")

    def upload_state() -> dict:
        return {**inbox.public(), "path": "/dav/", "readonly": settings.readonly,
                "areas": [a["name"] for a in index.areas()]}

    @app.get("/api/upload", tags=["upload"])
    def get_upload(user: User = Depends(require_admin)):
        """The WebDAV door for photo backup apps: where uploads land and whether a key exists."""
        return upload_state()

    @app.put("/api/upload", tags=["upload"])
    def put_upload(body: UploadIn, user: User = Depends(require_admin)):
        if body.area and body.area not in [a["name"] for a in index.areas()]:
            raise HTTPException(status_code=404, detail="unknown area")
        inbox.save(area=body.area)
        return upload_state()

    def known_upload_area(area: str) -> str:
        if area and area not in [a["name"] for a in index.areas()]:
            raise HTTPException(status_code=404, detail="unknown area")
        return area

    @app.post("/api/upload/keys", tags=["upload"], status_code=201)
    def add_upload_key(body: UploadKeyIn, user: User = Depends(require_admin)):
        """One more upload key – for another phone or another person. Shown this once, only its hash is
        kept. It opens nothing but the upload door, into the usual area or the one named here."""
        key_id, key = inbox.new_key(body.name or "", known_upload_area(body.area or ""), creator=body.creator or "")
        return {**upload_state(), "key": key, "key_id": key_id}

    @app.patch("/api/upload/keys/{key_id}", tags=["upload"])
    def change_upload_key(key_id: str, body: UploadKeyIn, user: User = Depends(require_admin)):
        """Another name for a key, or another area it leads into ('' = the usual one)."""
        try:
            inbox.change_key(key_id, body.name, None if body.area is None else known_upload_area(body.area),
                             body.creator)
        except KeyError:
            raise HTTPException(status_code=404, detail="unknown key") from None
        return upload_state()

    @app.post("/api/upload/keys/{key_id}/renew", tags=["upload"])
    def renew_upload_key(key_id: str, user: User = Depends(require_admin)):
        """A new key in place of this one, which stops working at once."""
        try:
            key_id, key = inbox.new_key(key_id=key_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="unknown key") from None
        return {**upload_state(), "key": key, "key_id": key_id}

    @app.delete("/api/upload/keys/{key_id}", tags=["upload"])
    def delete_one_upload_key(key_id: str, user: User = Depends(require_admin)):
        inbox.drop_key(key_id)
        return upload_state()

    @app.post("/api/upload/key", tags=["upload"])
    def new_upload_key(user: User = Depends(require_admin)):
        """As it was with one key: a new key in place of the first one, or the first one."""
        keys = inbox.load()["keys"]
        key_id, key = inbox.new_key(key_id=keys[0]["id"]) if keys else inbox.new_key()
        return {**upload_state(), "key": key, "key_id": key_id}

    @app.delete("/api/upload/key", tags=["upload"])
    def delete_upload_key(user: User = Depends(require_admin)):
        """Take every upload key away."""
        inbox.drop_key()
        return upload_state()

    mount_upload(app, inbox, settings.photos, settings.readonly, client_ip, app.state.wake, sign=editor.sign)

    app.mount("/", StaticFiles(directory=STATIC, html=True), name="ui")
    return app
