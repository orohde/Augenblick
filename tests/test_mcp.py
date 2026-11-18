"""Named API keys and the MCP endpoint: the photo library as tools, over the same functions as the REST API."""

import base64
import dataclasses
import io
import json
import shutil

import pytest
from PIL import Image

from augenblick.app import create_app
from conftest import PASSWORD, browser

needs_tools = pytest.mark.skipif(not (shutil.which("exiftool") and shutil.which("jpegtran")),
                                 reason="needs exiftool and jpegtran")


def started(settings, **changes):
    app = create_app(dataclasses.replace(settings, **changes), background=False)
    app.state.index.scan()
    return app


@pytest.fixture
def rw(settings):
    """An installation that may write, its administrator signed in, MCP switched on and a key that may write."""
    client = browser(started(settings, readonly=False))
    assert client.post("/api/setup", json={"username": "oliver", "password": PASSWORD}).status_code == 201
    assert client.put("/api/mcp/settings", json={"on": True}).json()["on"] is True
    client.key = {"Authorization": "Bearer " + client.post("/api/security/api-keys", json={"name": "Claude"}).json()["token"]}
    client.reader = {"Authorization": "Bearer " + client.post(
        "/api/security/api-keys", json={"name": "Nur lesen", "right": "read"}).json()["token"]}
    return client


def rpc(client, method: str, params: dict | None = None, headers: dict | None = None, msg_id: int | None = 1):
    message = {"jsonrpc": "2.0", "method": method, "params": params or {}}
    if msg_id is not None:
        message["id"] = msg_id
    return client.post("/api/mcp", json=message, headers=headers if headers is not None else client.key)


def call(client, tool: str, /, headers: dict | None = None, **arguments):
    """(is_error, content blocks parsed where they are JSON)"""
    res = rpc(client, "tools/call", {"name": tool, "arguments": arguments}, headers)
    assert res.status_code == 200, res.text
    result = res.json()["result"]
    blocks = []
    for block in result["content"]:
        if block["type"] != "text":
            blocks.append(block)
            continue
        try:
            blocks.append(json.loads(block["text"]))
        except ValueError:
            blocks.append(block["text"])
    return result["isError"], blocks


def beach(client) -> str:
    _, [found] = call(client, "search_photos", query="Strand")
    return found["photos"][0]["key"]


# ------------------------------------------------------------------ API keys


def test_api_keys(admin, greta):
    assert admin.get("/api/security/api-keys").json() == []
    assert greta.get("/api/security/api-keys").status_code == 403
    made = admin.post("/api/security/api-keys", json={"name": "Claude"}).json()
    token = made["token"]
    assert token.startswith("agb_") and made["right"] == "write" and made["hint"] == token[-4:]
    stored = admin.app.state.apikeys.path.read_text()
    assert "agb_" not in stored and token not in stored                     # only the hash is kept
    assert admin.post("/api/security/api-keys", json={"name": "claude"}).status_code == 409
    assert admin.post("/api/security/api-keys", json={"name": " "}).status_code == 400
    script = browser(admin.app)
    bearer = {"Authorization": f"Bearer {token}"}
    assert script.get("/api/me", headers=bearer).json()["is_admin"] is True
    keys = admin.get("/api/security/api-keys").json()
    assert keys[0]["last"] and "hash" not in keys[0] and "token" not in keys[0]
    assert admin.get("/api/security").json()["api_keys"] == 1
    renewed = admin.post(f"/api/security/api-keys/{made['id']}/renew").json()["token"]
    assert script.get("/api/me", headers=bearer).status_code == 401
    assert script.get("/api/me", headers={"Authorization": f"Bearer {renewed}"}).status_code == 200
    assert admin.patch(f"/api/security/api-keys/{made['id']}", json={"right": "read"}).json()["right"] == "read"
    assert admin.delete(f"/api/security/api-keys/{made['id']}").status_code == 200
    assert admin.delete(f"/api/security/api-keys/{made['id']}").status_code == 404
    assert script.get("/api/me", headers={"Authorization": f"Bearer {renewed}"}).status_code == 401
    # the upload keys are something else and stay as they were
    assert admin.get("/api/upload").json()["keys"] == []


def test_a_reading_key_sees_everything_and_changes_nothing(admin):
    token = admin.post("/api/security/api-keys", json={"name": "Nur lesen", "right": "read"}).json()["token"]
    script = browser(admin.app)
    bearer = {"Authorization": f"Bearer {token}"}
    assert {a["name"] for a in script.get("/api/areas", headers=bearer).json()} == {"Familie", "Greta"}
    me = script.get("/api/me", headers=bearer).json()
    assert me["is_admin"] is False and me["write"] == []
    key = script.get("/api/photos?month=2024-07", headers=bearer).json()[0]["key"]
    assert script.get(f"/api/photos/{key}/thumb", headers=bearer).status_code == 200
    assert script.patch(f"/api/photos/{key}", json={"rating": 5}, headers=bearer).status_code == 403
    assert script.post("/api/edit", json={"keys": [key], "change": {"rating": 5}}, headers=bearer).status_code == 403
    assert script.delete(f"/api/photos/{key}", headers=bearer).status_code == 403
    assert script.post("/api/trash/empty", headers=bearer).status_code == 403
    assert script.put("/api/me/prefs", json={"language": "en"}, headers=bearer).status_code == 403
    assert script.get("/api/security/api-keys", headers=bearer).status_code == 403


def test_the_key_from_before_goes_on_working(settings):
    """An installation with the single key of before: after the update it is a named key that may write."""
    settings.data.mkdir(parents=True, exist_ok=True)
    (settings.data / "security.json").write_text(json.dumps({"api_token": "der-alte-schluessel-1234567890"}))
    app = started(settings)
    keys = app.state.apikeys.all()
    assert [(k.name, k.right) for k in keys] == [("API", "write")]
    assert app.state.security.load().api_token == ""                       # the settings file forgets it
    assert "der-alte" not in (settings.data / "security.json").read_text()
    assert "der-alte" not in (settings.data / "apikeys.json").read_text()
    bearer = {"Authorization": "Bearer der-alte-schluessel-1234567890"}
    assert browser(app).get("/api/me", headers=bearer).json()["is_admin"] is True
    again = started(settings)                                               # a second start changes nothing
    assert len(again.state.apikeys.all()) == 1
    assert browser(again).get("/api/me", headers=bearer).status_code == 200


def test_the_key_from_the_environment_comes_once(settings):
    app = started(settings, api_token="aus-der-umgebung-1234567890")
    bearer = {"Authorization": "Bearer aus-der-umgebung-1234567890"}
    assert browser(app).get("/api/areas", headers=bearer).status_code == 200
    [key] = app.state.apikeys.all()
    app.state.apikeys.delete(key.id)
    again = started(settings, api_token="aus-der-umgebung-1234567890")      # a deleted key does not come back
    assert again.state.apikeys.all() == []
    assert browser(again).get("/api/areas", headers=bearer).status_code == 401


def test_a_key_from_before_does_not_lose_its_name_clash(settings):
    settings.data.mkdir(parents=True, exist_ok=True)
    app = started(settings)
    app.state.apikeys.create("API", "read")
    app.state.security.set_api_token("noch-ein-alter-schluessel")
    again = started(settings)
    assert sorted((k.name, k.right) for k in again.state.apikeys.all()) == [("API", "read"), ("API 2", "write")]
    assert browser(again).get("/api/me", headers={"Authorization": "Bearer noch-ein-alter-schluessel"}).json()["is_admin"]


# ------------------------------------------------------------------ MCP


def test_off_until_switched_on(admin, greta):
    token = admin.post("/api/security/api-keys", json={"name": "Claude"}).json()["token"]
    bearer = {"Authorization": f"Bearer {token}"}
    assert admin.get("/api/mcp/settings").json() == {"on": False, "path": "/api/mcp"}
    assert rpc(admin, "ping", headers=bearer).status_code == 403
    assert greta.put("/api/mcp/settings", json={"on": True}).status_code == 403
    assert rpc(browser(admin.app), "ping", headers={}).status_code == 401         # no key, no door
    assert admin.put("/api/mcp/settings", json={"on": True}).json()["on"] is True
    assert rpc(admin, "ping", headers=bearer).json() == {"jsonrpc": "2.0", "id": 1, "result": {}}
    assert admin.put("/api/mcp/settings", json={"on": False}).json()["on"] is False
    assert rpc(admin, "ping", headers=bearer).status_code == 403


def test_handshake(rw):
    info = rpc(rw, "initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                                  "clientInfo": {"name": "t", "version": "1"}}).json()["result"]
    assert info["protocolVersion"] == "2025-03-26" and info["serverInfo"]["name"] == "augenblick"
    assert info["serverInfo"]["title"] == "Augenblick!" and "Augenblick!" in info["instructions"]
    assert info["capabilities"] == {"tools": {"listChanged": False}}
    assert rpc(rw, "initialize", {"protocolVersion": "1999-01-01"}).json()["result"]["protocolVersion"] == "2025-06-18"
    assert rpc(rw, "notifications/initialized", msg_id=None).status_code == 202
    assert rpc(rw, "resources/list").json()["error"]["code"] == -32601
    assert rw.get("/api/mcp", headers=rw.key).status_code == 405
    assert rw.post("/api/mcp", content=b"{", headers=rw.key).json()["error"]["code"] == -32700
    both = rw.post("/api/mcp", headers=rw.key, json=[
        {"jsonrpc": "2.0", "id": 1, "method": "ping"}, {"jsonrpc": "2.0", "method": "notifications/initialized"}]).json()
    assert both == [{"jsonrpc": "2.0", "id": 1, "result": {}}]


def test_tools_are_listed_by_right(rw):
    tools = rpc(rw, "tools/list").json()["result"]["tools"]
    names = {t["name"] for t in tools}
    assert {"search_photos", "get_photo", "view_photo", "update_photos", "trash_photos", "undo_change"} <= names
    assert not names & {"empty_trash", "delete_photo", "destroy", "list_users"}        # no way to delete for good
    for tool in tools:
        assert tool["description"] and tool["inputSchema"]["type"] == "object"
    by_name = {t["name"]: t["annotations"] for t in tools}
    assert by_name["trash_photos"]["destructiveHint"] is True and by_name["trash_photos"]["readOnlyHint"] is False
    assert by_name["search_photos"]["readOnlyHint"] is True and by_name["update_photos"]["destructiveHint"] is False
    reading = {t["name"] for t in rpc(rw, "tools/list", headers=rw.reader).json()["result"]["tools"]}
    assert "search_photos" in reading and "view_photo" in reading
    assert not reading & {"update_photos", "trash_photos", "add_to_album", "name_face", "undo_change"}
    # and what is not offered cannot be called either
    error, [text] = call(rw, "update_photos", headers=rw.reader, keys=[beach(rw)], rating=5)
    assert error and "only read" in text


def test_reading(rw):
    _, [areas] = call(rw, "list_areas")
    assert {a["name"] for a in areas} == {"Familie", "Greta"}
    _, [folders] = call(rw, "list_folders", area="Familie")
    assert {"folder": "Familie/2024/Urlaub", "photos": 1, "with_subfolders": 1} in folders
    _, [found] = call(rw, "search_photos", area="Familie")
    assert found["total"] == 4 and found["photos"][0]["taken"] >= found["photos"][-1]["taken"]     # newest first
    _, [july] = call(rw, "search_photos", since="2024-07", until="2024-07")
    assert [p["description"] for p in july["photos"]] == ["Am Strand"]
    _, [none] = call(rw, "search_photos", since="2024-07-15", until="2024")
    assert none["total"] == 0
    _, [camera] = call(rw, "search_photos", camera="TestCam 1", limit=1)
    assert camera["total"] == 2 and len(camera["photos"]) == 1
    error, [text] = call(rw, "search_photos", since="Juli")
    assert error and "not a date" in text
    _, [cameras] = call(rw, "list_cameras")
    assert {"model": "TestCam 1", "count": 2} in cameras["cameras"]
    _, [months] = call(rw, "timeline", area="Greta")
    assert months == [{"month": "2023-12", "count": 1}]
    key = beach(rw)
    _, [photo] = call(rw, "get_photo", key=key)
    assert photo["description"] == "Am Strand" and photo["model"] == "TestCam 1" and "hash" not in photo
    _, [faces] = call(rw, "get_photo_faces", key=key)
    assert faces == []
    error, [text] = call(rw, "get_photo", key="gibtsnicht")
    assert error and "not found" in text
    error, [text] = call(rw, "get_photo", key=key, nonsense=1)
    assert error and "unknown argument" in text
    assert call(rw, "list_memories")[0] is False and call(rw, "list_journal")[1] == [[]]
    _, [inbox] = call(rw, "list_inbox")
    assert inbox == {"inbox_areas": [], "total": 0, "photos": []}


def test_view_photo_gives_the_small_preview(rw):
    key = beach(rw)
    error, [meta, image] = call(rw, "view_photo", headers=rw.reader, key=key)
    assert not error and meta["key"] == key
    assert image["type"] == "image" and image["mimeType"] == "image/jpeg"
    with Image.open(io.BytesIO(base64.b64decode(image["data"]))) as shown:
        assert shown.format == "JPEG" and max(shown.size) <= 480


@needs_tools
def test_writing_and_taking_back(rw):
    key = beach(rw)
    error, [done] = call(rw, "update_photos", keys=[key], description="Am Meer", add_keywords=["Sommer"], rating=4)
    assert not error and done[0]["ok"] is True and done[0]["description"] == "Am Meer"
    _, [photo] = call(rw, "get_photo", key=done[0]["key"])
    assert photo["keywords"] == ["Sommer"] and photo["rating"] == 4
    key = done[0]["key"]
    _, [favourites] = call(rw, "search_photos", favorites=True)
    assert [p["key"] for p in favourites["photos"]] == [key]
    error, [text] = call(rw, "update_photos", keys=[key])
    assert error and "nothing to change" in text
    error, [text] = call(rw, "update_photos", keys=[key], taken="gestern")
    assert error and "invalid arguments" in text
    _, [added] = call(rw, "add_to_album", keys=[key], album="Urlaub/Ostsee")
    key = added[0]["key"]
    _, [albums] = call(rw, "list_albums")
    assert [a["album"] for a in albums] == ["Urlaub", "Urlaub/Ostsee"]
    _, [named] = call(rw, "name_face", key=key, x=0.5, y=0.5, w=0.2, h=0.3, name="Greta")
    assert named["people"] == ["Greta"]
    _, [people] = call(rw, "list_people")
    assert people == [{"name": "Greta", "photos": 1}]
    _, [journal] = call(rw, "list_journal")
    assert len(journal) == 3 and journal[0]["user"] == "api"
    _, [back] = call(rw, "undo_change", entry=journal[0]["id"])
    assert "people" not in back
    _, [people] = call(rw, "list_people")
    assert people == []


@needs_tools
def test_trash_and_back(rw):
    key = beach(rw)
    _, [gone] = call(rw, "trash_photos", keys=[key])
    assert gone[0]["ok"] is True
    assert call(rw, "get_photo", key=key)[0] is True
    _, [trash] = call(rw, "list_trash")
    [entry] = trash["entries"]
    _, [back] = call(rw, "restore_from_trash", entry=entry["id"])
    assert back["path"] == "Familie/2024/Urlaub/20240714_102030_ab12.jpg"
    _, [trash] = call(rw, "list_trash")
    assert trash["entries"] == []


def test_a_read_only_installation_offers_no_writing(settings):
    client = browser(started(settings))         # the photos are mounted read-only
    assert client.post("/api/setup", json={"username": "oliver", "password": PASSWORD}).status_code == 201
    client.put("/api/mcp/settings", json={"on": True})
    client.key = {"Authorization": "Bearer " + client.post("/api/security/api-keys", json={"name": "Claude"}).json()["token"]}
    names = {t["name"] for t in rpc(client, "tools/list").json()["result"]["tools"]}
    assert "search_photos" in names and "update_photos" not in names
