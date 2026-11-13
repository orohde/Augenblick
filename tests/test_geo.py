import zipfile

from augenblick.geo import Geo, build
from augenblick.index import Index
from conftest import make_jpeg


def town(number, name, lat, lon, country, state, people, code="PPL"):
    return "\t".join([str(number), name, name, "", str(lat), str(lon), "P", code, country, "", state, "", "", "",
                      str(people), "", "", "Europe/Berlin", "2024-01-01"])


def source(folder):
    """The GeoNames files in small: a few towns, a quarter of one of them, one country."""
    folder.mkdir()
    towns = [town(1, "Rotbach", 49.900, 7.700, "DE", "08", 2500), town(2, "Bad Kreuznach", 49.841, 7.867, "DE", "08", 53000),
             town(3, "Mainz", 49.984, 8.279, "DE", "08", 220000), town(4, "Mainz-Altstadt", 49.998, 8.272, "DE", "08", 0, "PPLX"),
             town(5, "Kleinkleckersdorf", 50.03, 8.25, "DE", "05", 600),
             town(6, "Testhausen", 49.51, 7.76, "DE", "08", 900)]
    with zipfile.ZipFile(folder / "cities500.zip", "w") as archive:
        archive.writestr("cities500.txt", "\n".join(towns) + "\n")
    (folder / "admin1CodesASCII.txt").write_text("DE.08\tRheinland-Pfalz\tRheinland-Pfalz\t100\nDE.05\tHessen\tHessen\t101\n")
    (folder / "countryInfo.txt").write_text("# comment\n" + "\t".join(
        ["DE", "DEU", "276", "GM", "Germany"] + [""] * 11 + ["200"]) + "\n")
    names = ["1\t200\tde\tBundesrepublik Deutschland\t1\t\t\t", "2\t200\tde\tDeutschland\t1\t1\t\t",
             "3\t200\tde\tBRD\t\t1\t\t", "4\t200\ten\tGermany\t1\t\t\t", "5\t3\tde\tMogontiacum\t\t\t\t1",
             "6\t3\tde\tMainz am Rhein\t\t\t\t"]
    with zipfile.ZipFile(folder / "alternateNamesV2.zip", "w") as archive:
        archive.writestr("alternateNamesV2.txt", "\n".join(names) + "\n")
    return folder


def test_nearest_town(tmp_path):
    assert build(tmp_path / "geo", "de", source(tmp_path / "src")) == 5       # the quarter is no town
    geo = Geo(tmp_path / "geo")
    assert geo.ready and geo.stamp
    assert geo.lookup(49.899, 7.70) == ("Rotbach", "Rheinland-Pfalz", "Deutschland")
    assert geo.lookup(49.845, 7.865)[0] == "Bad Kreuznach"
    assert geo.lookup(49.998, 8.272)[0] == "Mainz am Rhein"       # the name in the language, never the historic one
    # nearer to the village's centre, but well inside the big town
    assert geo.lookup(50.012, 8.262)[0] == "Mainz am Rhein"
    assert geo.lookup(50.031, 8.251) == ("Kleinkleckersdorf", "Hessen", "Deutschland")
    assert geo.lookup(55.5, 4.0) == ("", "", "")                  # at sea: nothing within reach
    assert geo.lookup(None, None) == ("", "", "")


def test_without_the_list(tmp_path):
    geo = Geo(tmp_path / "nichts")
    assert not geo.ready and geo.lookup(49.899, 7.70) == ("", "", "")


def test_index_names_places(tmp_path):
    photos = tmp_path / "fotos"
    make_jpeg(photos / "Familie/a.jpg", taken="2024:07:14 10:20:30", gps=(49.899, 7.70))
    make_jpeg(photos / "Familie/b.jpg", color=(1, 2, 3), taken="2024:07:15 10:20:30", gps=(49.984, 8.279))
    make_jpeg(photos / "Familie/c.jpg", color=(9, 9, 9), taken="2024:07:16 10:20:30")
    index = Index(tmp_path / "data/augenblick.db", photos, tmp_path / "data/thumbs")
    index.scan()                                    # no list yet: no names
    assert index.by_path("Familie/a.jpg")["city"] == ""
    build(tmp_path / "geo", "de", source(tmp_path / "src"))
    index.geo = Geo(tmp_path / "geo")
    index.scan()                                    # the list is new: names without reading a file again
    assert index.by_path("Familie/a.jpg")["city"] == "Rotbach"
    assert index.by_path("Familie/c.jpg")["country"] == ""
    assert index.relocate() == 0                    # nothing to do a second time
    assert index.place_names(["Familie"]) == [{"country": "Deutschland", "count": 2, "cities": [
        {"city": "Mainz am Rhein", "count": 1}, {"city": "Rotbach", "count": 1}]}]
    assert index.count_where(["Familie"], q="rotbach") == 1
    assert index.count_where(["Familie"], q="rheinland") == 2
    assert index.count_where(["Familie"], country="Deutschland", city="Mainz am Rhein") == 1
    make_jpeg(photos / "Familie/d.jpg", color=(50, 60, 70), taken="2024:07:17 10:20:30", gps=(49.845, 7.865))
    index.scan()                                    # a new photo is named as it is read
    assert index.by_path("Familie/d.jpg")["city"] == "Bad Kreuznach"


def test_api(admin, app, tmp_path):
    build(tmp_path / "geo", "", source(tmp_path / "src"))
    app.state.index.geo = Geo(tmp_path / "geo")
    app.state.index.scan()
    names = admin.get("/api/placenames").json()
    assert [entry["country"] for entry in names] == ["Germany"] and names[0]["count"] == 1
    city = names[0]["cities"][0]["city"]
    months = admin.get("/api/timeline", params={"country": "Germany", "city": city}).json()
    assert sum(month["count"] for month in months) == 1
    key = admin.get("/api/photos", params={"month": months[0]["month"], "city": city}).json()[0]["key"]
    assert admin.get(f"/api/photos/{key}").json()["city"] == city


def test_area_without_places(tmp_path):
    photos = tmp_path / "fotos"
    make_jpeg(photos / "Familie/a.jpg", taken="2024:07:14 10:20:30", gps=(49.899, 7.70))
    make_jpeg(photos / "Schirm/b.jpg", color=(1, 2, 3), gps=(49.899, 7.70))
    build(tmp_path / "geo", "de", source(tmp_path / "src"))
    index = Index(tmp_path / "data/augenblick.db", photos, tmp_path / "data/thumbs")
    index.geo = Geo(tmp_path / "geo")
    index.no_places = frozenset({"Schirm"})
    index.scan()
    assert index.by_path("Familie/a.jpg")["city"] == "Rotbach" and index.by_path("Schirm/b.jpg")["city"] == ""
    index.no_places = frozenset()
    assert index.relocate() == 2 and index.by_path("Schirm/b.jpg")["city"] == "Rotbach"
    index.no_places = frozenset({"Schirm"})
    index.relocate()
    assert index.by_path("Schirm/b.jpg")["city"] == ""
