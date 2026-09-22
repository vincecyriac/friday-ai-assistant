"""Import smoke for the macOS node. Skipped wherever the desktop extra is absent."""

import importlib

import pytest

pytest.importorskip("Quartz", reason="desktop extra not installed")

MODULES = [
    "friday.desktop.sentry_web", "friday.desktop.sentry_scene", "friday.desktop.sentry_exec",
    "friday.desktop.sentry_vision", "friday.desktop.sentry_action", "friday.desktop.sentry_recognition",
    "friday.desktop.sentry_personal", "friday.desktop.asset_generator", "friday.desktop.agents",
    "friday.desktop.widget_generator", "friday.desktop.hub", "friday.desktop.app",
    "friday.desktop.sentinel_client", "friday.desktop.config_pull",
]


@pytest.fixture(scope="module", autouse=True)
def desktop_data_dir(tmp_path_factory):
    """One data dir for the whole module: the desktop modules bake paths in at import."""
    from friday.core import config

    data_dir = tmp_path_factory.mktemp("desktop-data").resolve()
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("FRIDAY_DATA_DIR", str(data_dir))
        config.get_settings.cache_clear()
        yield data_dir
    config.get_settings.cache_clear()


@pytest.mark.parametrize("module", MODULES)
def test_module_imports(module):
    importlib.import_module(module)


def test_hub_paths_live_under_data_dir(desktop_data_dir):
    from friday.core.config import REPO_ROOT
    from friday.desktop import hub, sentry_recognition, sentry_scene

    data = str(desktop_data_dir)
    assert data != str(REPO_ROOT / "data")
    assert hub.MEMORY_FILE.startswith(data)
    assert hub.HISTORY_LOG_FILE.startswith(data)
    assert hub.ASSETS_DIR.startswith(data)
    assert sentry_scene.SCENES_FILE.startswith(data)
    assert sentry_recognition.PROFILES_FILE.startswith(data)
    assert sentry_recognition.YUNET_MODEL_PATH.endswith("friday/desktop/models/face_detection_yunet.onnx")


def test_hub_models_come_from_routing():
    from friday.core.llm import resolve
    from friday.desktop import agents, hub, widget_generator

    assert hub.MODEL_ID == resolve(hub.settings, "live").model
    assert agents.TIERS["os"]["role"] == "agent_os" and agents.TIERS["spatial"]["role"] == "agent_spatial"
    assert agents.model_for("os") == resolve(hub.settings, "agent_os").model
    assert widget_generator.widget_model() == resolve(hub.settings, "widget").model
    assert str(hub.CONFIG_CACHE_FILE).startswith(str(hub.settings.data_dir))
