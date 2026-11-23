"""Runtime settings, read from environment variables (see README)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


@dataclass
class Settings:
    db_path: Path = BASE_DIR / "data" / "lumen.db"
    # Shared secret the checkpoints send as "Authorization: Bearer <token>".
    api_token: str = "change-me"
    # If a password is set, the website asks for this user name and password on its login screen.
    admin_user: str = "admin"
    admin_password: str = ""
    # Key that signs login cookies. Empty = generated once and kept next to the database.
    session_secret: str = ""
    # A person counts as "away" once no checkpoint has seen their tag for this long.
    absence_timeout_s: int = 180
    # Sightings weaker than this are ignored (tag too far away / outside the office).
    min_rssi: int = -90
    # Raw sightings older than this are deleted; sessions are kept forever.
    retention_days: int = 30
    # A checkpoint is shown offline if it has not reported for this long.
    checkpoint_offline_s: int = 60
    # How long "scan for new tags" stays enabled on the checkpoints.
    discover_window_s: int = 600

    @classmethod
    def from_env(cls) -> "Settings":
        s = cls()
        env = os.environ.get
        if env("LUMEN_DB"):
            s.db_path = Path(env("LUMEN_DB"))
        s.api_token = env("LUMEN_API_TOKEN", s.api_token)
        s.admin_user = env("LUMEN_ADMIN_USER", s.admin_user)
        s.admin_password = env("LUMEN_ADMIN_PASSWORD", s.admin_password)
        s.session_secret = env("LUMEN_SESSION_SECRET", s.session_secret)
        s.absence_timeout_s = int(env("LUMEN_ABSENCE_TIMEOUT", s.absence_timeout_s))
        s.min_rssi = int(env("LUMEN_MIN_RSSI", s.min_rssi))
        s.retention_days = int(env("LUMEN_RETENTION_DAYS", s.retention_days))
        return s
