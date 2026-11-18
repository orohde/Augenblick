import io
from pathlib import Path

from PIL import Image

from augenblick import __version__
from conftest import PASSWORD, browser, make_jpeg
from fastapi.testclient import TestClient
from augenblick.app import create_app

PUBLIC = {"/api/version", "/api/login", "/api/setup", "/api/logout", "/api/docs", "/api/openapi.json",
          "/api/docs/oauth2-redirect"}


def test_version(anonymous):
    body = anonymous.get("/api/version").json()
    assert body["version"] == __version__ == (Path(__file__).parents[1] / "VERSION").read_text().strip()


def test_start_page(anonymous):
    response = anonymous.get("/")
    assert response.status_code == 200
    assert "Augenblick!" in response.text
    assert response.headers["cache-control"] == "no-cache"


def test_every_api_route_wants_a_login(app, anonymous):
    checked = 0
    for route in app.routes:
        path = getattr(route, "path", "")
        if not path.startswith("/api/") or path in PUBLIC:
            continue
        url = path.replace("{key}", "abc").replace("{name}", "abc").replace("{entry}", "1").replace("{session_id}", "abc")
        for method in route.methods - {"HEAD", "OPTIONS"}:
            response = anonymous.request(method, url, json={})
            assert response.status_code == 401, f"{method} {path} answered {response.status_code}"
            checked += 1
    assert checked >= 15


def test_setup_only_once_and_only_from_home(app, anonymous):
    outside = browser(app)
    outside = type(outside)(app, client=("203.0.113.9", 40000))
    assert outside.get("/api/setup").json() == {"needed": True, "home": False}
    assert outside.post("/api/setup", json={"username": "boese", "password": PASSWORD}).status_code == 403
    assert anonymous.post("/api/setup", json={"username": "oliver", "password": "kurz"}).status_code == 400
    assert anonymous.post("/api/setup", json={"username": "oliver", "password": PASSWORD}).status_code == 201
    assert anonymous.get("/api/me").json()["is_admin"] is True
    assert anonymous.post("/api/setup", json={"username": "zwei", "password": PASSWORD}).status_code == 409


PROXY = ("10.10.20.50", 50000)


def home_says(app, where, forwarded=None) -> bool:
    headers = {"X-Forwarded-For": forwarded} if forwarded else {}
    return TestClient(app, client=where).get("/api/setup", headers=headers).json()["home"]


def test_forwarded_address_only_from_a_trusted_proxy(app):
    """Somebody on the internet must not become "at home" by sending X-Forwarded-For himself."""
    outside = ("203.0.113.9", 40000)
    assert home_says(app, outside) is False
    assert home_says(app, outside, "10.10.30.5") is False          # straight from outside: header ignored
    assert home_says(app, PROXY, "203.0.113.7") is False           # proxy reports a visitor from outside
    assert home_says(app, PROXY, "10.10.30.5") is True             # proxy reports a visitor at home
    # a proxy that appends: the visitor's own claim stands left of the real address
    assert home_says(app, PROXY, "10.10.30.5, 203.0.113.7") is False


def test_trusted_proxies_can_be_narrowed(settings):
    from dataclasses import replace
    app = create_app(replace(settings, trusted_proxies="10.10.20.50"), background=False)
    assert home_says(app, PROXY, "203.0.113.7") is False
    # another machine at home is no proxy any more: its header no longer counts
    assert home_says(app, ("10.10.30.6", 50000), "203.0.113.7") is True


def test_login_and_logout(app, admin):
    client = browser(app)
    assert client.post("/api/login", json={"username": "oliver", "password": "falsch-falsch-falsch"}).status_code == 401
    assert client.post("/api/login", json={"username": "oliver", "password": PASSWORD}).status_code == 200
    assert client.get("/api/me").json()["name"] == "oliver"
    client.post("/api/logout")
    assert client.get("/api/me").status_code == 401


def test_cross_site_requests_are_refused(admin):
    response = admin.post("/api/rescan", headers={"sec-fetch-site": "cross-site"})
    assert response.status_code == 403


def test_timeline_and_month(admin):
    months = admin.get("/api/timeline").json()
    assert months == [{"month": "2024-07", "count": 1}, {"month": "2024-03", "count": 2},
                      {"month": "2023-12", "count": 1}, {"month": "1981-05", "count": 1}]
    july = admin.get("/api/photos", params={"month": "2024-07"}).json()
    assert len(july) == 1 and july[0]["kind"] == "image"
    assert admin.get("/api/photos", params={"month": "juli"}).status_code == 400


def test_filters(admin):
    def total(**params):
        return sum(m["count"] for m in admin.get("/api/timeline", params=params).json())

    assert total() == 5
    assert total(kind="video") == 1
    assert total(folder="Familie/2024") == 3
    assert total(folder="Familie/2024", recursive="false") == 2
    assert total(camera="TestCam 1") == 2
    assert total(no_place="true") == 4
    assert total(no_date="true") == 2      # the one dated by its name, and the film
    assert total(q="strand") == 1
    assert total(q="urlaub") == 1
    assert total(areas="Greta") == 1


def test_places_for_the_map(admin, greta):
    rows = admin.get("/api/places").json()
    assert len(rows) == 1 and rows[0][1:4] == [49.5, 7.75, "2024-07"]
    assert admin.get("/api/photos/" + rows[0][0]).json()["name"] == "20240714_102030_ab12.jpg"
    assert admin.get("/api/places?folder=Familie/Alt").json() == []
    assert greta.get("/api/places").json() == []


def test_tree_counts(admin):
    tree = {n["path"]: n for n in admin.get("/api/tree").json()}
    assert tree["Familie"]["total"] == 4 and tree["Familie"]["count"] == 0
    assert tree["Familie/2024"]["count"] == 2 and tree["Familie/2024"]["total"] == 3
    assert tree["Familie/2024/Urlaub"]["depth"] == 2


def test_photo_details(admin):
    key = admin.get("/api/photos", params={"month": "2024-07"}).json()[0]["key"]
    photo = admin.get(f"/api/photos/{key}").json()
    assert photo["taken"] == "2024-07-14T10:20:30" and photo["taken_src"] == "exif"
    assert photo["model"] == "TestCam 1" and photo["description"] == "Am Strand"
    assert abs(photo["lat"] - 49.5) < 1e-6 and abs(photo["lon"] - 7.75) < 1e-6
    assert photo["folder"] == "Familie/2024/Urlaub" and "hash" not in photo


def test_previews_stand_upright(admin):
    key = admin.get("/api/photos", params={"month": "2024-03", "kind": "image"}).json()[0]["key"]
    photo = admin.get(f"/api/photos/{key}").json()
    assert (photo["width"], photo["height"]) == (48, 64)      # stored 64×48, turned by the tag
    for kind in ("thumb", "preview"):
        response = admin.get(f"/api/photos/{key}/{kind}")
        assert response.status_code == 200 and response.headers["content-type"] == "image/jpeg"
        assert Image.open(io.BytesIO(response.content)).size == (48, 64)


def test_original_is_served_untouched(admin, settings):
    key = admin.get("/api/photos", params={"month": "2024-07"}).json()[0]["key"]
    original = (settings.photos / "Familie/2024/Urlaub/20240714_102030_ab12.jpg").read_bytes()
    assert admin.get(f"/api/photos/{key}/file").content == original


def test_video_is_listed_without_preview(admin):
    videos = [p for m in admin.get("/api/timeline", params={"kind": "video"}).json()
              for p in admin.get("/api/photos", params={"month": m["month"], "kind": "video"}).json()]
    assert len(videos) == 1
    assert admin.get(f"/api/photos/{videos[0]['key']}/thumb").status_code == 404
    assert admin.get(f"/api/photos/{videos[0]['key']}/file").content == b"kein echter Film"


def test_areas_keep_people_apart(admin, greta):
    assert [a["name"] for a in greta.get("/api/areas").json()] == ["Greta"]
    assert [m["month"] for m in greta.get("/api/timeline").json()] == ["2023-12"]
    assert greta.get("/api/timeline", params={"areas": "Familie"}).json() == []
    assert {n["path"].split("/")[0] for n in greta.get("/api/tree").json()} == {"Greta"}
    foreign = admin.get("/api/photos", params={"month": "2024-07"}).json()[0]["key"]
    for suffix in ("", "/thumb", "/preview", "/file"):
        assert greta.get(f"/api/photos/{foreign}{suffix}").status_code == 404
    assert greta.get("/api/users").status_code == 403
    assert greta.post("/api/rescan").status_code == 403


def test_user_management(admin, greta):
    assert admin.patch("/api/users/greta", json={"areas": {"Greta": "read", "Familie": "read"}}).status_code == 200
    assert len(greta.get("/api/areas").json()) == 2
    assert admin.patch("/api/users/oliver", json={"is_admin": False}).status_code == 400
    assert admin.delete("/api/users/oliver").status_code == 400
    assert admin.delete("/api/users/greta").status_code == 200
    assert greta.get("/api/me").status_code == 401


def test_password_change(admin):
    assert admin.post("/api/me/password", json={"old": "falsch", "new": PASSWORD + "2"}).status_code == 403
    assert admin.post("/api/me/password", json={"old": PASSWORD, "new": PASSWORD + "2"}).status_code == 200


def test_second_factor_and_devices(app, admin):
    import base64, hashlib, hmac, struct, time
    assert admin.get("/api/me").json()["totp"] is False
    assert admin.post("/api/me/totp/setup", json={"password": "falsch-falsch-falsch"}).status_code == 403
    setup = admin.post("/api/me/totp/setup", json={"password": PASSWORD}).json()
    assert setup["qr"].startswith("data:image/") and "otpauth://" in setup["uri"]
    key = base64.b32decode(setup["secret"].replace(" ", ""))

    def code(offset=0):
        counter = struct.pack(">Q", int(time.time()) // 30 + offset)
        digest = hmac.new(key, counter, hashlib.sha1).digest()
        at = digest[-1] & 15
        return f"{(struct.unpack('>I', digest[at:at + 4])[0] & 0x7FFFFFFF) % 1000000:06d}"

    assert admin.post("/api/me/totp/enable", json={"code": "000000"}).status_code == 400
    codes = admin.post("/api/me/totp/enable", json={"code": code()}).json()["recovery_codes"]
    assert len(codes) >= 8 and admin.get("/api/me").json()["totp"] is True
    assert admin.get("/api/users").json()[0]["totp"] is True
    other = browser(app)
    assert other.post("/api/login", json={"username": "oliver", "password": PASSWORD}).status_code == 200
    sessions = admin.get("/api/me/sessions").json()
    assert len(sessions) == 2 and sum(s["current"] for s in sessions) == 1
    assert admin.post("/api/me/sessions/others").json() == {"signed_out": 1}
    assert other.get("/api/me").status_code == 401
    assert admin.post("/api/me/totp/disable", json={"password": PASSWORD, "code": codes[0]}).json() == {"ok": True}
    assert admin.get("/api/me").json()["totp"] is False


def test_security_settings_and_api_key(app, admin, greta):
    assert greta.get("/api/security").status_code == 403
    state = admin.get("/api/security").json()
    assert state["without_2fa"] == ["greta", "oliver"] or set(state["without_2fa"]) == {"greta", "oliver"}
    assert admin.put("/api/security", json={"require_2fa_external": True}).status_code == 400     # own factor first
    assert admin.put("/api/security", json={"internal_networks": "10.0.0.0/8"}).status_code == 400  # would lock out
    assert admin.put("/api/security", json={"internal_networks": "192.168.1.0/24"}).json()["internal_networks"]
    made = admin.post("/api/security/api-keys", json={"name": "Skript"}).json()
    script = type(admin)(app, client=("192.168.1.30", 40000))
    assert script.get("/api/areas", headers={"Authorization": f"Bearer {made['token']}"}).status_code == 200
    assert admin.delete(f"/api/security/api-keys/{made['id']}").json() == {"ok": True}
    assert script.get("/api/areas", headers={"Authorization": f"Bearer {made['token']}"}).status_code == 401


def test_jobs_overview(admin, greta):
    state = admin.get("/api/jobs").json()
    assert state["scan"]["photos"] == 5 and state["scan"]["running"] is False and state["scan"]["every_minutes"] == 15
    assert state["faces"]["enabled"] is False and state["faces"]["wanted"] == 0
    assert state["job"]["running"] is False and state["job"]["kind"] == ""
    assert admin.post("/api/faces/search").status_code == 400            # switched off
    assert greta.get("/api/jobs").status_code == 200
    assert greta.post("/api/job/stop").status_code == 403 and greta.post("/api/faces/search").status_code == 403
    assert admin.post("/api/job/stop").json() == {"ok": True}


def test_areas_are_made_and_removed(admin, greta, app, settings):
    object.__setattr__(settings, "readonly", False)
    assert greta.post("/api/areas", json={"name": "Bildschirmfotos"}).status_code == 403
    assert admin.post("/api/areas", json={"name": "../raus"}).status_code == 400
    assert admin.post("/api/areas", json={"name": ".versteckt"}).status_code == 400
    assert admin.post("/api/areas", json={"name": "familie"}).status_code == 409
    made = admin.post("/api/areas", json={"name": "Bildschirmfotos", "places": False, "dates": False})
    assert made.status_code == 201, made.text
    assert (settings.photos / "Bildschirmfotos").is_dir()
    listed = {a["name"]: a for a in admin.get("/api/areas").json()}
    assert listed["Bildschirmfotos"] == {"name": "Bildschirmfotos", "count": 0, "places": False, "dates": False,
                                         "inbox": False}
    assert listed["Familie"]["places"] and listed["Familie"]["dates"]
    # it belongs to nobody at first; rights are given per user
    assert "Bildschirmfotos" not in [a["name"] for a in greta.get("/api/areas").json()]
    assert admin.patch("/api/users/greta", json={"areas": {"Greta": "read", "Bildschirmfotos": "write"}}).status_code == 200
    assert "Bildschirmfotos" in [a["name"] for a in greta.get("/api/areas").json()]

    # a screenshot has neither place nor date – and is not listed as lacking them
    make_jpeg(settings.photos / "Bildschirmfotos/Bild 1.jpg", color=(7, 7, 7), gps=(49.5, 7.75))
    make_jpeg(settings.photos / "Bildschirmfotos/Bild 2.jpg", color=(8, 8, 8))
    app.state.index.scan()
    count = lambda **f: sum(m["count"] for m in admin.get("/api/timeline", params=f).json())
    before = count(no_date="true"), count(no_place="true")
    assert admin.patch("/api/areas/Bildschirmfotos", json={"places": True, "dates": True}).status_code == 200
    assert (count(no_date="true"), count(no_place="true")) == (before[0] + 2, before[1] + 1)
    assert admin.patch("/api/areas/Nirgends", json={}).status_code == 404

    assert admin.delete("/api/areas/Bildschirmfotos").status_code == 409        # not empty
    for name in ("Bild 1.jpg", "Bild 2.jpg"):
        (settings.photos / "Bildschirmfotos" / name).unlink()
    app.state.index.scan()
    assert admin.delete("/api/areas/Bildschirmfotos").status_code == 200
    assert not (settings.photos / "Bildschirmfotos").exists()


def test_no_new_area_when_read_only(admin):
    assert admin.post("/api/areas", json={"name": "Neu"}).status_code == 400


def test_support_line_follows_the_address(admin, monkeypatch):
    assert admin.get("/api/status").json()["support"].startswith("https://worldgate.de/")
    import augenblick.app as module
    monkeypatch.setattr(module, "SUPPORT_URL", "")          # no page, no line
    assert admin.get("/api/status").json()["support"] == ""
