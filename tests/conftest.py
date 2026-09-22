"""Shared fixtures. Everything runs against tmp_path; nothing touches <repo>/data."""
from pathlib import Path

import pytest

from friday.core.config import Settings, load_settings

# One master key for the whole test session; every sentinel fixture inherits it.
TEST_MASTER_KEY = "dGVzdC1tYXN0ZXIta2V5LTMyLWJ5dGVzLWxvbmctISEhIQ=="   # base64 of 32 bytes


@pytest.fixture
def make_settings(tmp_path: Path):
    """Build Settings from an explicit env dict, isolated from the real .env."""

    def _make(**env: str) -> Settings:
        env.setdefault("FRIDAY_DATA_DIR", str(tmp_path / "data"))
        env.setdefault("FRIDAY_MASTER_KEY", TEST_MASTER_KEY)
        return load_settings(env=env, env_file=tmp_path / "absent.env")

    return _make
