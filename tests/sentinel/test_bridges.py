import pytest

from friday.core.config import ConfigError
from friday.sentinel.bridges import load_bridges


class FakeBridge:
    name = "fake"
    instances: list = []

    def __init__(self):
        self.started_with = None
        self.stopped = False
        FakeBridge.instances.append(self)

    async def start(self, bus, ctx):
        self.started_with = (bus, ctx)

    async def stop(self):
        self.stopped = True


def test_no_bridges_by_default(make_settings):
    assert load_bridges(make_settings()) == []


def test_loads_dotted_class_paths(make_settings):
    FakeBridge.instances.clear()
    bridges = load_bridges(make_settings(FRIDAY_BRIDGES=f"{__name__}.FakeBridge, {__name__}.FakeBridge"))
    assert len(bridges) == 2 and all(isinstance(b, FakeBridge) for b in bridges)
    assert bridges[0] is not bridges[1]


@pytest.mark.parametrize("path", ["nodots", f"{__name__}.Missing", "no.such.module.Cls", f"{__name__}.pytest"])
def test_bad_paths_are_config_errors(make_settings, path):
    with pytest.raises(ConfigError) as excinfo:
        load_bridges(make_settings(FRIDAY_BRIDGES=path))
    assert "FRIDAY_BRIDGES" in str(excinfo.value)
