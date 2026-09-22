import json
import os
import stat

from friday.desktop.config_pull import pull_or_cached


async def test_sentinel_success_overlays_and_caches(make_settings, tmp_path):
    settings = make_settings(GEMINI_API_KEY="env-key")
    cache = tmp_path / "cache.json"

    async def fetch():
        return {"llm.gemini_api_key": "vault-key", "llm.routes.live": "gemini:live-v", "desktop.voice": "Kore"}

    out, source = await pull_or_cached(settings, fetch, cache)
    assert source == "sentinel"
    assert out.gemini_api_key == "vault-key" and out.llm_routes["live"] == "gemini:live-v" and out.friday_voice == "Kore"
    assert settings.gemini_api_key == "env-key"
    assert json.loads(cache.read_text())["values"]["llm.gemini_api_key"] == "vault-key"
    assert stat.S_IMODE(os.stat(cache).st_mode) == 0o600


async def test_failure_falls_back_to_cache_then_env(make_settings, tmp_path):
    settings = make_settings(GEMINI_API_KEY="env-key")
    cache = tmp_path / "cache.json"

    async def fail():
        return None

    out, source = await pull_or_cached(settings, fail, cache)
    assert source == "env" and out.gemini_api_key == "env-key"

    cache.write_text(json.dumps({"values": {"llm.gemini_api_key": "cached-key"}, "fetched_at": 1.0}))
    out, source = await pull_or_cached(settings, fail, cache)
    assert source == "cache" and out.gemini_api_key == "cached-key"

    cache.write_text("{not json")
    out, source = await pull_or_cached(settings, fail, cache)
    assert source == "env" and out.gemini_api_key == "env-key"
