"""What was decided stands in plain words beside the photos and comes back when the data folder is lost."""
import json
import shutil
import sqlite3

import pytest

from augenblick.app import create_app
from augenblick.config import Settings
from conftest import PASSWORD, browser
from test_faces import BLOCK, FakeEngine, brightness, crowd, key_of, needs_tools, rw, shown  # noqa: F401  (the fixtures)


def test_decisions_stand_beside_the_photos_and_come_back(crowd, tmp_path):
    app = crowd.app
    app.state.people.search()
    groups = crowd.get("/api/faces/groups").json()["groups"]
    four = groups[0]["faces"]
    assert crowd.post("/api/faces/ignore", json={"faces": [four[0]["id"]]}).json()["done"] == 1
    assert crowd.post("/api/faces/no-face/keep", json={"faces": [four[1]["id"]]}).json()["done"] == 1
    assert all(r["ok"] for r in crowd.post("/api/faces/name", json={"faces": [four[2]["id"]], "name": "Oma"}).json())
    assert crowd.put("/api/people/cover", json={"name": "Oma", "face": four[2]["id"]}).status_code == 200
    known = app.state.index.files(["Familie"])[0]["hash"]
    app.state.dupes.keep_apart(["aaa", known], "oliver")              # one of the two is no picture here: left out
    assert app.state.decisions.write() >= 1 and app.state.decisions.write() == 0         # only when something changed
    def stored():
        parts = [json.loads(path.read_text(encoding="utf-8")) for path in sorted(crowd.photos.glob("*/.augenblick/entscheidungen.json"))]
        assert all("Abschrift" in part["hinweis"] for part in parts)
        return {key: [item for part in parts for item in (part[key].items() if isinstance(part[key], dict) else part[key])]
                for key in ("ignorierte_gesichter", "ki_gesicht", "ki_kein_gesicht", "schluesselbilder", "keine_duplikate")}

    data = stored()
    assert len(data["ignorierte_gesichter"]) == 1 and len(data["ki_gesicht"]) == 1
    entry = data["ignorierte_gesichter"][0]
    assert entry["von"] == "oliver" and len(entry["rahmen"]) == 4
    assert [name for name, _ in data["schluesselbilder"]] == ["Oma"] and data["keine_duplikate"] == []
    source = next(path for path in crowd.photos.glob("*/.augenblick/entscheidungen.json") if entry["bild"] in path.read_text())
    # the photo is renamed: the decision stays, the file says where the picture lies now
    old = source.parent.parent / entry["datei"]
    old.rename(old.with_name("umbenannt.jpg"))
    app.state.index.scan()
    assert app.state.decisions.write() == 1
    assert stored()["ignorierte_gesichter"][0]["datei"].endswith("umbenannt.jpg")
    assert crowd.get("/api/faces/ignored").json()["total"] == 1
    # the hidden folder is no photo folder
    assert all(".augenblick" not in row["path"] for row in app.state.index.files(["Familie", "Greta"]))

    # the data folder is lost; the photos are not
    again = create_app(Settings(photos=crowd.photos, data=tmp_path / "neu", readonly=False), background=False)
    again.state.index.scan()
    again.state.people.engine = FakeEngine()
    again.state.prefs.save(faces=["Familie", "Greta"])
    again.state.people.search()
    fresh = browser(again)
    assert fresh.post("/api/setup", json={"username": "oliver", "password": PASSWORD}).status_code == 201
    assert fresh.get("/api/faces/ignored").json()["total"] == 1
    assert fresh.get("/api/people").json()[0]["face"] is not None and again.state.prefs.load()["covers"]["Oma"]
    again.state.decisions.write()
    assert stored()["ki_gesicht"]

    # a store that knows something is never overruled by the file
    fresh.delete("/api/faces/ignored")
    kept = shutil.copy(source, tmp_path / "alt.json")
    again.state.decisions.write()
    shutil.copy(kept, source)                                # an old file comes back, say from a backup
    assert again.state.decisions.restore().get("ignored") == 1          # that store is empty now: taken back
    assert again.state.decisions.restore() == {}                         # … and now it is not


@needs_tools
@pytest.mark.parametrize("degrees", [90, 180, 270])
def test_decisions_go_round_with_the_picture(rw, degrees):
    """A picture turned without loss is another picture to the stores; what was decided about it follows."""
    app = rw.app
    path = rw.photos / "Familie/p/block.jpg"
    (rw.photos / "Familie/p/liegt.jpg").unlink()        # the same picture data: it would keep the decisions for itself
    app.state.index.scan()
    old = app.state.index.by_path("Familie/p/block.jpg")["hash"]
    other = app.state.index.by_path("Greta/x.jpg")["hash"]
    app.state.people.ignore({"hash": old, "x": BLOCK[0], "y": BLOCK[1], "w": BLOCK[2], "h": BLOCK[3]}, "oliver")
    app.state.people.judge([{"hash": old, "stored": BLOCK}], [False])
    app.state.dupes.keep_apart([old, other], "oliver")
    app.state.prefs.save(covers={"Oma": {"hash": old, "box": list(BLOCK)}})

    def state():
        db = sqlite3.connect(app.state.settings.ignored_path)
        try:
            ignored = db.execute("SELECT hash, x, y, w, h FROM ignored").fetchall()
            judged = db.execute("SELECT hash, x, y, w, h, face FROM judged").fetchall()
        finally:
            db.close()
        db = sqlite3.connect(app.state.settings.apart_path)
        try:
            apart = db.execute("SELECT a, b FROM apart").fetchall()
        finally:
            db.close()
        return ignored, judged, apart, app.state.prefs.load()["covers"]["Oma"]

    before = state()
    body = rw.post(f"/api/photos/{key_of(rw, 'block.jpg')}/rotate", json={"degrees": degrees}).json()
    new = app.state.index.by_path("Familie/p/block.jpg")["hash"]
    assert new != old and body["orientation"] == 1
    ignored, judged, apart, cover = state()
    picture = shown(path)[0]
    assert [row[0] for row in ignored] == [new] and brightness(picture, ignored[0][1:]) > 220
    assert [(row[0], row[5]) for row in judged] == [(new, 0)] and brightness(picture, judged[0][1:5]) > 220
    assert apart == [tuple(sorted((new, other)))]
    assert cover["hash"] == new and brightness(picture, cover["box"]) > 220
    app.state.decisions.write(force=True)
    told = json.loads((rw.photos / "Familie/.augenblick/entscheidungen.json").read_text(encoding="utf-8"))
    assert [entry["bild"] for entry in told["ignorierte_gesichter"]] == [new]
    # taken back: everything is where it was
    entry = rw.get("/api/journal").json()[0]
    assert rw.post(f"/api/journal/{entry['id']}/undo").status_code == 200
    after = state()
    back = app.state.index.by_path("Familie/p/block.jpg")["hash"]      # packed anew, so not the hash it once had
    assert back != new
    assert [row[0] for row in after[0]] == [back] and [row[0] for row in after[1]] == [back]
    assert after[2] == [tuple(sorted((back, other)))] and after[3]["hash"] == back and len(before[0]) == 1
    for got, was in zip(after[0][0][1:] + after[1][0][1:5] + tuple(after[3]["box"]), BLOCK * 3):
        assert abs(got - was) < 1e-6


@needs_tools
def test_a_copy_left_behind_keeps_its_decisions(rw):
    """Two files hold the same picture; one is turned. The other keeps what was decided, the turned one gets it too."""
    app = rw.app
    shutil.copy(rw.photos / "Familie/p/block.jpg", rw.photos / "Familie/p/zwilling.jpg")
    app.state.index.scan()
    old = app.state.index.by_path("Familie/p/block.jpg")["hash"]
    app.state.people.ignore({"hash": old, "x": BLOCK[0], "y": BLOCK[1], "w": BLOCK[2], "h": BLOCK[3]}, "oliver")
    key = next(row["key"] for row in app.state.index.files(["Familie"]) if row["path"].endswith("block.jpg"))
    assert rw.post(f"/api/photos/{key}/rotate", json={"degrees": 90}).status_code == 200
    new = app.state.index.by_path("Familie/p/block.jpg")["hash"]
    db = sqlite3.connect(app.state.settings.ignored_path)
    try:
        assert sorted(row[0] for row in db.execute("SELECT hash FROM ignored")) == sorted([old, new])
    finally:
        db.close()
