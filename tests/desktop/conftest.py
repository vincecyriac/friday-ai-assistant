"""One temporary data dir for every desktop test: the desktop modules bake paths
in at import time, so the environment must be set before the first import."""
import pytest


@pytest.fixture(scope="session", autouse=True)
def desktop_data_dir(tmp_path_factory):
    from friday.core import config

    data_dir = tmp_path_factory.mktemp("desktop-data").resolve()
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("FRIDAY_DATA_DIR", str(data_dir))
        config.get_settings.cache_clear()
        yield data_dir
    config.get_settings.cache_clear()
