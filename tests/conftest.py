"""Shared fixtures. Everything runs against tmp_path; nothing touches <repo>/data."""
from pathlib import Path

import pytest

from friday.core.config import Settings, load_settings


@pytest.fixture
def make_settings(tmp_path: Path):
    """Build Settings from an explicit env dict, isolated from the real .env."""

    def _make(**env: str) -> Settings:
        env.setdefault("FRIDAY_DATA_DIR", str(tmp_path / "data"))
        return load_settings(env=env, env_file=tmp_path / "absent.env")

    return _make
