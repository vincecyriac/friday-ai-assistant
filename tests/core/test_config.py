import pytest

from friday.core.config import DEFAULT_LLM_ROUTES, ConfigError, Settings, load_settings


def test_defaults(make_settings, tmp_path):
    s = make_settings()
    assert isinstance(s, Settings)
    assert s.data_dir == (tmp_path / "data").resolve()
    assert s.data_dir.is_dir()
    assert (s.sentinel_bind_host, s.sentinel_bind_port) == ("127.0.0.1", 8770)
    assert s.sentinel_url is None and s.sentinel_token is None
    assert s.db_synchronous == "FULL"
    assert s.telemetry_interval_s == 15.0
    assert s.heartbeat_interval_s == 30.0
    assert s.retention_days == 14
    assert s.log_level == "INFO"
    assert s.bridges == ()
    assert dict(s.llm_routes) == dict(DEFAULT_LLM_ROUTES)
    assert s.gemini_api_key is None and s.gemini_model is None
    assert s.friday_voice == "Aoede"
    assert s.tripo_api_key is None
    assert s.node_id


def test_overrides(make_settings):
    s = make_settings(
        FRIDAY_NODE_ID="pi",
        FRIDAY_SENTINEL_BIND="0.0.0.0:9000",
        FRIDAY_SENTINEL_URL="https://x.ts.net/sentinel",
        FRIDAY_SENTINEL_TOKEN="t",
        FRIDAY_DB_SYNCHRONOUS="normal",
        FRIDAY_TELEMETRY_INTERVAL="5",
        FRIDAY_HEARTBEAT_INTERVAL="7.5",
        FRIDAY_RETENTION_DAYS="3",
        FRIDAY_LOG_LEVEL="debug",
        FRIDAY_BRIDGES="a.B, c.D,",
        FRIDAY_LLM_WIDGET="gemini:custom",
        FRIDAY_VOICE="Kore",
        GEMINI_API_KEY="k",
        TRIPO_API_KEY="tp",
    )
    assert s.node_id == "pi"
    assert (s.sentinel_bind_host, s.sentinel_bind_port) == ("0.0.0.0", 9000)
    assert s.sentinel_url == "https://x.ts.net/sentinel"
    assert s.sentinel_token == "t"
    assert s.db_synchronous == "NORMAL"
    assert s.telemetry_interval_s == 5.0
    assert s.heartbeat_interval_s == 7.5
    assert s.retention_days == 3
    assert s.log_level == "DEBUG"
    assert s.bridges == ("a.B", "c.D")
    assert s.llm_routes["widget"] == "gemini:custom"
    assert s.llm_routes["live"] == DEFAULT_LLM_ROUTES["live"]
    assert s.friday_voice == "Kore"
    assert s.gemini_api_key == "k"
    assert s.tripo_api_key == "tp"


def test_env_file_is_read_but_process_env_wins(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("FRIDAY_NODE_ID=from-file\nFRIDAY_LOG_LEVEL=WARNING\n")
    s = load_settings(
        env={"FRIDAY_DATA_DIR": str(tmp_path / "d"), "FRIDAY_NODE_ID": "from-env"},
        env_file=env_file,
    )
    assert s.node_id == "from-env"
    assert s.log_level == "WARNING"


def test_missing_env_file_is_fine(tmp_path):
    s = load_settings(env={"FRIDAY_DATA_DIR": str(tmp_path / "d")}, env_file=tmp_path / "nope.env")
    assert s.data_dir.is_dir()


def test_gemini_model_alias_feeds_live_route(make_settings):
    s = make_settings(GEMINI_MODEL="gemini-x-live")
    assert s.gemini_model == "gemini-x-live"
    assert s.llm_routes["live"] == "gemini:gemini-x-live"


def test_explicit_live_route_beats_alias(make_settings):
    s = make_settings(GEMINI_MODEL="old", FRIDAY_LLM_LIVE="gemini:new")
    assert s.llm_routes["live"] == "gemini:new"


@pytest.mark.parametrize(
    "key,value",
    [
        ("FRIDAY_SENTINEL_BIND", "nocolon"),
        ("FRIDAY_SENTINEL_BIND", ":8770"),
        ("FRIDAY_SENTINEL_BIND", "host:notaport"),
        ("FRIDAY_DB_SYNCHRONOUS", "OFF"),
        ("FRIDAY_TELEMETRY_INTERVAL", "fast"),
        ("FRIDAY_HEARTBEAT_INTERVAL", "-1"),
        ("FRIDAY_RETENTION_DAYS", "2.5"),
    ],
)
def test_invalid_values_name_the_variable(make_settings, key, value):
    with pytest.raises(ConfigError) as excinfo:
        make_settings(**{key: value})
    assert key in str(excinfo.value)


def test_get_settings_is_cached(monkeypatch, tmp_path):
    from friday.core import config

    monkeypatch.setenv("FRIDAY_DATA_DIR", str(tmp_path / "d"))
    config.get_settings.cache_clear()
    try:
        assert config.get_settings() is config.get_settings()
        assert config.get_settings().data_dir == (tmp_path / "d").resolve()
    finally:
        config.get_settings.cache_clear()
