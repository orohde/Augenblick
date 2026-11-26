import dataclasses

import pytest

from augenblick.app import create_app
from augenblick.sort import Rules, SortError, check_template, clean_rules, render
from conftest import PASSWORD, browser, make_jpeg

PHOTO = {"taken": "2020-01-28T19:46:45", "taken_src": "exif", "folder": "Familie/Boote/Flo", "name": "IMG_1.JPG",
         "path": "Familie/Boote/Flo/IMG_1.JPG", "model": "DMC-GM5"}


def test_templates():
    assert render("{JJJJMMTT}_{HHMMSS}", PHOTO) == "20200128_194645"
    assert render("{Ordner} - {JJJJ-MM-TT}_{HH-MM-SS} {Kamera} {Alt}", PHOTO) == "Flo - 2020-01-28_19-46-45 DMC-GM5 IMG_1"
    assert check_template(" /{Jahr}/{Monat}/ ", True) == "{Jahr}/{Monat}"
    for bad in ("{Jahrgang}", "{Jahr", "a:b"):
        with pytest.raises(SortError):
            check_template(bad, False)
    with pytest.raises(SortError):
        check_template("../{Jahr}", True)
    with pytest.raises(SortError):
        clean_rules([{"scope": "Familie", "folder": "{Jahr}"}, {"scope": "familie/", "name": "{Alt}"}])


def test_rules_pick_the_closest_and_leave_undated_alone():
    rules = Rules(clean_rules([{"scope": "Familie", "folder": "{Jahr}", "name": "{JJJJMMTT}_{HHMMSS}"},
                               {"scope": "Familie/Boote", "folder": "", "name": ""},
                               {"scope": "Familie/Urlaub", "folder": "", "name": "{JJJJMMTT}_{HHMMSS}"}]))
    assert rules.target(PHOTO) == ""                                             # Boote: hands off
    loose = dict(PHOTO, folder="Familie/Eingang", path="Familie/Eingang/IMG_1.JPG")
    assert rules.target(loose) == "Familie/2020/20200128_194645.JPG"
    trip = dict(PHOTO, folder="Familie/Urlaub/USA", path="Familie/Urlaub/USA/IMG_1.JPG")
    assert rules.target(trip) == "Familie/Urlaub/USA/20200128_194645.JPG"
    assert rules.target(dict(loose, taken_src="file")) == "" and rules.target(dict(loose, folder="Greta/x")) == ""
    assert Rules.fits("Familie/2020/20200128_194645-3.JPG", "Familie/2020/20200128_194645.JPG")
    assert not Rules.fits("Familie/2020/20200128_194645-3.jpg", "Familie/2020/20200128_194645.JPG")
    assert not Rules.fits("Familie/2021/20200128_194645.JPG", "Familie/2020/20200128_194645.JPG")


@pytest.fixture
def rw(settings):
    settings = dataclasses.replace(settings, readonly=False)
    make_jpeg(settings.photos / "Familie/2024/Urlaub/zwilling.jpg", color=(1, 2, 3), taken="2024:07:14 10:20:30")
    (settings.photos / "Familie/2024/Urlaub/notiz.txt").write_text("bleibt")
    make_jpeg(settings.photos / "Familie/Kiste/allein.jpg", color=(9, 9, 9), taken="1999:12:31 23:59:59")
    app = create_app(settings, background=False)
    app.state.index.scan()
    client = browser(app)
    assert client.post("/api/setup", json={"username": "oliver", "password": PASSWORD}).status_code == 201
    client.photos = settings.photos
    return client


RULE = {"rules": [{"scope": "Familie", "folder": "{Jahr}", "name": "{JJJJMMTT}_{HHMMSS}"}]}


def names(client, folder):
    return sorted(p.name for p in (client.photos / folder).iterdir())


def test_rules_mark_what_does_not_fit(rw):
    assert rw.get("/api/me").json()["sorting"] is False
    assert rw.put("/api/sorting", json={"rules": [{"scope": "Familie", "folder": "{Jahrgang}"}]}).status_code == 400
    state = rw.put("/api/sorting", json=RULE).json()
    # five dated photos in Familie, none in place; the undated ones and Greta are no misfits
    assert state["misfits"] == 4 and rw.get("/api/me").json()["sorting"] is True
    months = rw.get("/api/timeline?misfit=true").json()
    assert sum(m["count"] for m in months) == 4
    preview = rw.get("/api/sort/preview").json()
    assert preview["total"] == 4
    moves = {m["from"]: m["to"] for m in preview["moves"]}
    assert moves["Familie/2024/Urlaub/20240714_102030_ab12.jpg"] == "Familie/2024/20240714_102030.jpg"
    assert moves["Familie/Kiste/allein.jpg"] == "Familie/1999/19991231_235959.jpg"
    assert names(rw, "Familie/2024/Urlaub") == ["20240714_102030_ab12.jpg", "notiz.txt", "zwilling.jpg"]   # only looked


def test_sorting_moves_keeps_apart_and_tidies(rw):
    rw.put("/api/sorting", json=RULE)
    key = next(m["key"] for m in rw.get("/api/sort/preview").json()["moves"] if m["from"].endswith("allein.jpg"))
    result = rw.post("/api/sort", json={"keys": [key]}).json()[0]
    assert result["ok"] and result["photo"]["path"] == "Familie/1999/19991231_235959.jpg"
    assert result["photo"]["key"] == key and result["photo"]["misfit"] == 0
    assert not (rw.photos / "Familie/Kiste").exists()                    # empty folder gone
    done = rw.post("/api/sort/all").json()
    assert done == {"moved": 3, "errors": [], "failed": 0, "left": 0}
    assert names(rw, "Familie/2024") == ["20240302_080000.jpg", "20240714_102030-2.jpg", "20240714_102030.jpg",
                                         "Urlaub", "film.mov", "notiz.txt"]
    assert names(rw, "Familie/2024/Urlaub") == ["notiz.txt"]             # a folder with any file stays
    assert rw.get("/api/sorting").json()["misfits"] == 0
    rw.app.state.index.scan()                                            # nothing is read again, nothing lost
    assert rw.get("/api/sorting").json()["misfits"] == 0 and len(rw.get("/api/journal").json()) == 4
    assert rw.post("/api/sort/all").json()["moved"] == 0


def test_move_can_be_taken_back_and_older_changes_too(rw):
    rw.put("/api/sorting", json=RULE)
    key = next(m["key"] for m in rw.get("/api/sort/preview").json()["moves"] if m["from"].endswith("allein.jpg"))
    rw.patch(f"/api/photos/{key}", json={"rating": 4})
    rw.post("/api/sort", json={"keys": [key]})
    move, change = rw.get("/api/journal").json()[:2]
    assert move["action"] == "move" and move["before"] == {"path": "Familie/Kiste/allein.jpg"}
    assert change["path"] == "Familie/1999/19991231_235959.jpg"          # the older entry followed the file
    assert rw.post(f"/api/journal/{change['id']}/undo").json()["rating"] == 0   # still possible after the move
    back = rw.post(f"/api/journal/{move['id']}/undo")
    assert back.status_code == 200 and back.json()["path"] == "Familie/Kiste/allein.jpg" and back.json()["misfit"] == 1
    assert names(rw, "Familie/Kiste") == ["allein.jpg"] and not (rw.photos / "Familie/1999").exists()
    assert {e["path"] for e in rw.get("/api/journal").json()} == {"Familie/Kiste/allein.jpg"}
    assert rw.post(f"/api/journal/{move['id']}/undo").status_code == 422


def test_date_change_asks_or_sorts_right_away(rw):
    rw.put("/api/sorting", json=RULE)
    rw.post("/api/sort/all")
    key = rw.get("/api/photos?month=1999-12").json()[0]["key"]
    asked = rw.patch(f"/api/photos/{key}", json={"taken": "2001-05-06T07:08:09"}).json()
    assert asked["path"] == "Familie/1999/19991231_235959.jpg" and asked["misfit"] == 1
    assert asked["target"] == "Familie/2001/20010506_070809.jpg"
    state = rw.put("/api/sorting", json={**RULE, "modes": {"Familie": "auto", "Greta": "unsinn"}}).json()
    assert state["modes"] == {"Familie": "auto"} and rw.get("/api/me").json()["sort_modes"] == {"Familie": "auto"}
    moved = rw.patch(f"/api/photos/{key}", json={"taken": "2003-01-02T03:04:05"}).json()
    assert moved["path"] == "Familie/2003/20030102_030405.jpg" and moved["misfit"] == 0
    assert not (rw.photos / "Familie/1999").exists() and names(rw, "Familie/2003") == ["20030102_030405.jpg"]
    assert [e["action"] for e in rw.get("/api/journal").json()[:2]] == ["move", "meta"]


def test_sorting_needs_rights(rw):
    rw.put("/api/sorting", json=RULE)
    rw.post("/api/users", json={"name": "gast", "password": PASSWORD, "areas": {"Familie": "read"}})
    guest = browser(rw.app)
    guest.post("/api/login", json={"username": "gast", "password": PASSWORD})
    key = guest.get("/api/photos?month=1999-12").json()[0]["key"]
    assert guest.post("/api/sort", json={"keys": [key]}).json()[0]["ok"] is False
    assert guest.post("/api/sort/all").json()["moved"] == 0 and guest.get("/api/sort/preview").json()["total"] == 0
    assert guest.put("/api/sorting", json=RULE).status_code == 403
    assert names(rw, "Familie/Kiste") == ["allein.jpg"]


def keys_in(client, area):
    months = client.get("/api/timeline", params={"areas": area}).json()
    return {p["key"] for m in months for p in client.get("/api/photos", params={"areas": area, "month": m["month"]}).json()}


def test_photos_move_from_the_inbox_into_another_area(rw):
    rw.put("/api/sorting", json=RULE)
    assert rw.post("/api/areas", json={"name": "Eingang"}).status_code == 201
    make_jpeg(rw.photos / "Eingang/IMG_0007.jpg", color=(40, 50, 60), taken="2026:11:12 10:15:00", gps=(49.5, 7.75))
    make_jpeg(rw.photos / "Eingang/Unter/ohne-datum.jpg", color=(41, 51, 61))
    make_jpeg(rw.photos / "Familie/ohne-datum.jpg", color=(42, 52, 62))             # the name is taken there
    rw.app.state.index.scan()
    dated, undated = sorted(keys_in(rw, "Eingang"), key=lambda k: rw.get(f"/api/photos/{k}").json()["name"])
    assert rw.post("/api/move", json={"keys": [dated], "area": "Nirgends"}).status_code == 403
    moved = rw.post("/api/move", json={"keys": [dated, undated], "area": "Familie"}).json()
    assert [m["ok"] for m in moved] == [True, True], moved
    # the dated one lands where the rule of the new area wants it, the other one in the top folder, beside its namesake
    assert moved[0]["photo"]["path"] == "Familie/2026/20261112_101500.jpg" and moved[0]["photo"]["area"] == "Familie"
    assert moved[0]["photo"]["misfit"] == 0
    assert moved[1]["photo"]["path"] == "Familie/ohne-datum-2.jpg"
    assert not (rw.photos / "Eingang/IMG_0007.jpg").exists() and not (rw.photos / "Eingang/Unter").exists()
    assert (rw.photos / "Eingang").is_dir()                                         # the area itself stays
    assert keys_in(rw, "Eingang") == set() and {dated, undated} <= keys_in(rw, "Familie")
    assert rw.post("/api/move", json={"keys": [dated], "area": "Familie"}).json()[0]["ok"]      # already there: nothing happens
    move = next(e for e in rw.get("/api/journal").json() if e["path"] == "Familie/2026/20261112_101500.jpg")
    assert move["action"] == "move" and move["before"] == {"path": "Eingang/IMG_0007.jpg"}
    back = rw.post(f"/api/journal/{move['id']}/undo")
    assert back.status_code == 200 and back.json()["path"] == "Eingang/IMG_0007.jpg" and back.json()["area"] == "Eingang"
    assert (rw.photos / "Eingang/IMG_0007.jpg").is_file() and not (rw.photos / "Familie/2026").exists()


def test_moving_needs_write_access_on_both_sides(rw):
    rw.post("/api/areas", json={"name": "Eingang"})
    make_jpeg(rw.photos / "Eingang/a.jpg", color=(70, 50, 60))
    rw.app.state.index.scan()
    key = keys_in(rw, "Eingang").pop()
    rw.post("/api/users", json={"name": "greta", "password": PASSWORD, "areas": {"Eingang": "write", "Familie": "read"}})
    greta = browser(rw.app)
    assert greta.post("/api/login", json={"username": "greta", "password": PASSWORD}).status_code == 200
    assert greta.post("/api/move", json={"keys": [key], "area": "Familie"}).status_code == 403
    assert greta.post("/api/move", json={"keys": [key], "area": "Greta"}).status_code == 403
    assert (rw.photos / "Eingang/a.jpg").is_file()


def test_move_across_disks_copies_checks_and_only_then_removes(rw, monkeypatch):
    import errno, os
    rw.post("/api/areas", json={"name": "Eingang"})
    data = make_jpeg(rw.photos / "Eingang/a.jpg", color=(70, 50, 60)).read_bytes()
    rw.app.state.index.scan()
    key = keys_in(rw, "Eingang").pop()
    real = os.rename

    def other_disk(src, dst):
        if "Eingang" in str(src) and "Familie" in str(dst):
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        return real(src, dst)

    monkeypatch.setattr(os, "rename", other_disk)
    assert rw.post("/api/move", json={"keys": [key], "area": "Familie"}).json()[0]["ok"]
    assert (rw.photos / "Familie/a.jpg").read_bytes() == data and not (rw.photos / "Eingang/a.jpg").exists()
    assert [p.name for p in (rw.photos / "Familie").iterdir() if p.name.startswith(".augenblick-")] == []


def test_folders_are_made_shown_and_filled(rw):
    assert rw.post("/api/areas", json={"name": "Bildschirmfotos"}).status_code == 201
    for bad in ("Bildschirmfotos", "Bildschirmfotos/../Familie/x", "Bildschirmfotos/.versteckt", "Nirgends/a", "Bildschirmfotos/a:b"):
        assert rw.post("/api/folders", json={"path": bad}).status_code in (400, 403), bad
    made = rw.post("/api/folders", json={"path": "Bildschirmfotos/Arbeit/2026"})
    assert made.status_code == 201 and made.json() == {"path": "Bildschirmfotos/Arbeit/2026"}
    assert rw.post("/api/folders", json={"path": "Bildschirmfotos/Arbeit"}).status_code == 409
    tree = {n["path"]: n for n in rw.get("/api/tree").json()}
    assert tree["Bildschirmfotos/Arbeit/2026"]["total"] == 0 and tree["Bildschirmfotos/Arbeit"]["depth"] == 1

    # photos go into the folder itself – no rule has a say there
    keys = sorted(keys_in(rw, "Familie"))[:2]
    moved = rw.post("/api/move", json={"keys": keys, "folder": "Bildschirmfotos/Arbeit/2026"}).json()
    assert all(m["ok"] and m["photo"]["folder"] == "Bildschirmfotos/Arbeit/2026" for m in moved), moved
    assert rw.post("/api/move", json={"keys": keys, "folder": "Bildschirmfotos/Arbeit/2026"}).json()[0]["photo"]["path"] == moved[0]["photo"]["path"]
    tree = {n["path"]: n for n in rw.get("/api/tree").json()}
    assert tree["Bildschirmfotos/Arbeit/2026"]["count"] == 2 and tree["Bildschirmfotos"]["total"] == 2
    assert rw.post("/api/move", json={"keys": keys, "folder": "Bildschirmfotos/Gibtsnicht"}).json()[0]["ok"] is False
    # … and within an area from one folder to another
    assert rw.post("/api/folders", json={"path": "Bildschirmfotos/Privat"}).status_code == 201
    again = rw.post("/api/move", json={"keys": keys[:1], "folder": "Bildschirmfotos/Privat"}).json()
    assert again[0]["photo"]["folder"] == "Bildschirmfotos/Privat"

    # only an empty folder can be removed
    assert rw.delete("/api/folders", params={"path": "Bildschirmfotos/Privat"}).status_code == 409
    assert rw.post("/api/folders", json={"path": "Bildschirmfotos/Leer"}).status_code == 201
    assert rw.delete("/api/folders", params={"path": "Bildschirmfotos/Leer"}).status_code == 200
    assert "Bildschirmfotos/Leer" not in {n["path"] for n in rw.get("/api/tree").json()}
    assert rw.delete("/api/folders", params={"path": "Bildschirmfotos"}).status_code == 400      # an area is no folder


def test_a_folder_moves_with_its_photos(rw):
    rw.post("/api/areas", json={"name": "Bildschirmfotos"})
    rw.post("/api/folders", json={"path": "Bildschirmfotos/Arbeit/2026"})
    keys = sorted(keys_in(rw, "Familie"))[:2]
    rw.post("/api/move", json={"keys": keys, "folder": "Bildschirmfotos/Arbeit/2026"})
    first = rw.get(f"/api/photos/{keys[0]}").json()["path"]
    rw.app.state.proposals.put(first, {"description": "vorgeschlagen"})
    # rename within the area
    done = rw.post("/api/folders/move", json={"path": "Bildschirmfotos/Arbeit", "to": "Bildschirmfotos/Büro"})
    assert done.status_code == 200 and done.json() == {"path": "Bildschirmfotos/Büro", "moved": 2}
    assert not (rw.photos / "Bildschirmfotos/Arbeit").exists()
    for key in keys:
        photo = rw.get(f"/api/photos/{key}").json()
        assert photo["folder"] == "Bildschirmfotos/Büro/2026" and (rw.photos / photo["path"]).is_file()
    assert rw.app.state.proposals.get(first.replace("/Arbeit/", "/Büro/"))["description"] == "vorgeschlagen"
    tree = {n["path"]: n for n in rw.get("/api/tree").json()}
    assert tree["Bildschirmfotos/Büro/2026"]["count"] == 2 and "Bildschirmfotos/Arbeit" not in tree
    # into another area, a level deeper
    done = rw.post("/api/folders/move", json={"path": "Bildschirmfotos/Büro", "to": "Familie/Alt/Büro"}).json()
    assert done["moved"] == 2 and rw.get(f"/api/photos/{keys[0]}").json()["area"] == "Familie"
    # a scan finds nothing new: the index followed the files
    assert rw.app.state.index.scan().get("added", 0) == 0
    # refused
    move = lambda a, b: rw.post("/api/folders/move", json={"path": a, "to": b}).status_code
    rw.post("/api/folders", json={"path": "Familie/Ziel"})
    assert move("Familie/Alt", "Familie/Ziel") == 409                 # exists
    assert move("Familie/Alt", "Familie/Alt/Innen") == 400            # into itself
    assert move("Familie/Alt", "Familie/Alt") == 409                  # nothing to do
    assert move("Familie/Fehlt", "Familie/Woanders") == 404
    assert move("Familie", "Bildschirmfotos/Familie") == 400          # an area is no folder
    assert move("Familie/Alt", "Nirgends/Alt") == 403


def test_a_rule_decides_inside_its_folders(rw):
    rw.put("/api/sorting", json=RULE)
    rw.post("/api/areas", json={"name": "Eingang"})
    make_jpeg(rw.photos / "Eingang/neu.jpg", color=(40, 50, 60), taken="2026:11:12 10:15:00")
    rw.app.state.index.scan()
    key = keys_in(rw, "Eingang").pop()
    assert rw.post("/api/folders", json={"path": "Familie/Irgendwo"}).status_code == 201
    moved = rw.post("/api/move", json={"keys": [key], "folder": "Familie/Irgendwo"}).json()[0]["photo"]
    assert moved["path"] == "Familie/2026/20261112_101500.jpg" and moved["misfit"] == 0


def test_folders_need_write_access(rw):
    rw.post("/api/users", json={"name": "greta", "password": PASSWORD, "areas": {"Familie": "read"}})
    greta = browser(rw.app)
    assert greta.post("/api/login", json={"username": "greta", "password": PASSWORD}).status_code == 200
    assert greta.post("/api/folders", json={"path": "Familie/Neu"}).status_code == 403
    assert not (rw.photos / "Familie/Neu").exists()
