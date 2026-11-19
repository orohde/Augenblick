"""Configuration from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _flag(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    photos: Path      # the photo folders – the truth; each folder directly below is an area
    data: Path        # index, previews, accounts – the index and previews can be rebuilt from the photos
    readonly: bool = True          # never write to the photo folders
    rescan_minutes: int = 15
    scan_workers: int = 2
    internal_networks: str = ""    # comma-separated CIDR ranges that count as "at home"; empty = private ranges
    trusted_proxies: str = ""      # who may tell the client address (X-Forwarded-For); empty = private ranges
    max_upload_mb: int = 100       # largest request body in MB (AUGENBLICK_MAX_UPLOAD_MB)
    dav_max_gb: float = 16          # largest single file over WebDAV in GB (AUGENBLICK_DAV_MAX_GB)
    api_token: str = ""
    models: Path | None = None     # where the face models lie; None = "models" in the data folder
    faces_load: float = 0.5        # share of one processor core the search for faces may take
    geo: Path | None = None        # where the list of towns lies; None = "geo" in the data folder

    @property
    def db_path(self) -> Path:
        return self.data / "augenblick.db"

    @property
    def faces_path(self) -> Path:
        """Where the faces are and how they look – worked out from the photos, may be thrown away."""
        return self.data / "faces.db"

    @property
    def ignored_path(self) -> Path:
        """Which found faces somebody waved away – a decision, so it is kept apart from what can be worked out."""
        return self.data / "faces-ignored.db"

    @property
    def dupes_path(self) -> Path:
        """How each picture looks, for finding copies – worked out from the photos, may be thrown away."""
        return self.data / "dupes.db"

    @property
    def apart_path(self) -> Path:
        """Which look-alikes somebody declared different pictures – a decision, so it is kept."""
        return self.data / "dupes-apart.db"

    @property
    def face_thumbs(self) -> Path:
        return self.data / "faces"

    @property
    def models_path(self) -> Path:
        return self.models or self.data / "models"

    @property
    def geo_path(self) -> Path:
        return self.geo or self.data / "geo"

    @property
    def users_path(self) -> Path:
        return self.data / "users.json"

    @property
    def journal_path(self) -> Path:
        return self.data / "journal.db"

    @property
    def tmp(self) -> Path:
        return self.data / "tmp"

    @property
    def thumbs(self) -> Path:
        return self.data / "thumbs"

    @property
    def previews(self) -> Path:
        return self.data / "previews"


def load_settings() -> Settings:
    data = Path(os.environ.get("AUGENBLICK_DATA", "./data")).resolve()
    data.mkdir(parents=True, exist_ok=True)
    return Settings(
        photos=Path(os.environ.get("AUGENBLICK_PHOTOS", "./fotos")).resolve(),
        data=data,
        readonly=_flag("AUGENBLICK_READONLY", True),
        rescan_minutes=int(os.environ.get("AUGENBLICK_RESCAN_MINUTES", "15")),
        scan_workers=int(os.environ.get("AUGENBLICK_SCAN_WORKERS", str(max(1, min(4, (os.cpu_count() or 2) // 3))))),
        internal_networks=os.environ.get("AUGENBLICK_INTERNAL_NETWORKS", ""),
        trusted_proxies=os.environ.get("AUGENBLICK_TRUSTED_PROXIES", ""),
        max_upload_mb=int(os.environ.get("AUGENBLICK_MAX_UPLOAD_MB", "100")),
        dav_max_gb=float(os.environ.get("AUGENBLICK_DAV_MAX_GB", "16")),
        api_token=os.environ.get("AUGENBLICK_API_TOKEN", ""),
        models=Path(os.environ["AUGENBLICK_MODELS"]).resolve() if os.environ.get("AUGENBLICK_MODELS") else None,
        faces_load=float(os.environ.get("AUGENBLICK_FACES_LOAD", "0.5")),
        geo=Path(os.environ["AUGENBLICK_GEO"]).resolve() if os.environ.get("AUGENBLICK_GEO") else None,
    )
