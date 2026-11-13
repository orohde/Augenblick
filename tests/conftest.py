import io
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from augenblick.app import create_app
from augenblick.config import Settings

PASSWORD = "ein-langes-testpasswort"


def make_jpeg(path: Path, *, size=(64, 48), color=(200, 60, 40), taken: str | None = None, model: str = "",
              gps: tuple[float, float] | None = None, orientation: int | None = None,
              description: str = "") -> Path:
    """A small JPEG with the metadata a camera would write."""
    image = Image.new("RGB", size, color)
    exif = Image.Exif()
    if model:
        exif[0x0110] = model
    if description:
        exif[0x010E] = description
    if orientation:
        exif[0x0112] = orientation
    if taken:
        exif[0x8769] = {0x9003: taken}
    if gps:
        lat, lon = gps
        exif[0x8825] = {1: "N" if lat >= 0 else "S", 2: (abs(lat), 0.0, 0.0),
                        3: "E" if lon >= 0 else "W", 4: (abs(lon), 0.0, 0.0)}
    path.parent.mkdir(parents=True, exist_ok=True)
    out = io.BytesIO()
    image.save(out, "JPEG", exif=exif.tobytes(), quality=90)
    path.write_bytes(out.getvalue())
    return path


@pytest.fixture
def settings(tmp_path) -> Settings:
    photos = tmp_path / "fotos"
    make_jpeg(photos / "Familie/2024/Urlaub/20240714_102030_ab12.jpg", taken="2024:07:14 10:20:30",
              model="TestCam 1", gps=(49.5, 7.75), description="Am Strand")
    make_jpeg(photos / "Familie/2024/IMG_0001.jpg", color=(10, 120, 200), taken="2024:03:02 08:00:00",
              model="TestCam 1", orientation=6)
    make_jpeg(photos / "Familie/Alt/1981-05-11_12-00-00.jpg", color=(90, 90, 90))
    make_jpeg(photos / "Greta/2023/bild.jpg", color=(0, 200, 0), taken="2023:12:24 18:00:00", model="Phone")
    (photos / "Familie/2024/notiz.txt").write_text("kein Bild")
    (photos / "Familie/2024/film.mov").write_bytes(b"kein echter Film")
    os.utime(photos / "Familie/2024/film.mov", (1709629200, 1709629200))     # 5 March 2024
    return Settings(photos=photos, data=tmp_path / "data")


@pytest.fixture
def app(settings):
    application = create_app(settings, background=False)
    application.state.index.scan()
    return application


def browser(app) -> TestClient:
    return TestClient(app, client=("192.168.1.20", 50000))


@pytest.fixture
def anonymous(app) -> TestClient:
    return browser(app)


@pytest.fixture
def admin(app) -> TestClient:
    client = browser(app)
    response = client.post("/api/setup", json={"username": "oliver", "password": PASSWORD})
    assert response.status_code == 201, response.text
    return client


@pytest.fixture
def greta(app, admin) -> TestClient:
    created = admin.post("/api/users", json={"name": "greta", "password": PASSWORD, "areas": {"Greta": "read"}})
    assert created.status_code == 201, created.text
    client = browser(app)
    assert client.post("/api/login", json={"username": "greta", "password": PASSWORD}).status_code == 200
    return client


def who(client) -> list[dict]:
    """The people list without the face that stands for each – most tests only ask who is there."""
    return [{"name": person["name"], "count": person["count"]} for person in client.get("/api/people").json()]
