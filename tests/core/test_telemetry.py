import json
import os
from types import SimpleNamespace

import psutil

from friday.core import telemetry
from friday.core.telemetry import collect


def test_collect_on_this_host(tmp_path):
    snap = collect("node", tmp_path)
    assert snap.node_id == "node"
    assert snap.disk_path == str(tmp_path)
    assert snap.mem_total and snap.mem_total > 0
    assert snap.disk_total and snap.disk_total > 0
    assert snap.cpu_percent is not None
    assert snap.uptime_s is not None and snap.uptime_s > 0
    assert isinstance(snap.thermal, dict)
    json.dumps(snap.to_dict())


def test_collect_survives_every_probe_failing(monkeypatch, tmp_path):
    def boom(*args, **kwargs):
        raise RuntimeError("no sensor")

    for name in ("cpu_percent", "virtual_memory", "disk_usage", "boot_time"):
        monkeypatch.setattr(psutil, name, boom)
    monkeypatch.setattr(os, "getloadavg", boom)
    monkeypatch.setattr(telemetry, "_thermal", boom)
    monkeypatch.setattr(telemetry, "_power", boom)

    snap = collect("n", tmp_path)
    assert snap.cpu_percent is None
    assert snap.load_avg is None
    assert snap.mem_total is None and snap.mem_used is None and snap.mem_available is None
    assert snap.disk_total is None and snap.disk_used is None and snap.disk_free is None
    assert snap.uptime_s is None
    assert snap.thermal == {}
    assert snap.power is None
    json.dumps(snap.to_dict())


def test_sysfs_thermal_is_parsed(monkeypatch, tmp_path):
    monkeypatch.setattr(psutil, "sensors_temperatures", lambda *a, **k: {}, raising=False)
    zone = tmp_path / "sys" / "class" / "thermal" / "thermal_zone0"
    zone.mkdir(parents=True)
    (zone / "type").write_text("cpu-thermal\n")
    (zone / "temp").write_text("48312\n")
    broken = tmp_path / "sys" / "class" / "thermal" / "thermal_zone1"
    broken.mkdir()
    (broken / "type").write_text("gpu\n")            # no temp file: skipped
    assert telemetry._thermal(tmp_path) == {"cpu-thermal": 48.312}


def test_psutil_thermal_is_preferred(monkeypatch, tmp_path):
    entry = SimpleNamespace(label="Package id 0", current=55.0)
    unnamed = SimpleNamespace(label="", current=41.0)
    monkeypatch.setattr(psutil, "sensors_temperatures",
                        lambda *a, **k: {"coretemp": [entry, unnamed]}, raising=False)
    assert telemetry._thermal(tmp_path) == {"coretemp/Package id 0": 55.0, "coretemp/1": 41.0}


def test_no_thermal_anywhere_is_empty(monkeypatch, tmp_path):
    monkeypatch.delattr(psutil, "sensors_temperatures", raising=False)
    assert telemetry._thermal(tmp_path) == {}


def test_vcgencmd_is_parsed_when_present(monkeypatch, tmp_path):
    exe = tmp_path / "vcgencmd"
    exe.write_text("#!/bin/sh\necho 'throttled=0x50005'\n")
    exe.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    assert telemetry._vcgencmd_throttled() == 0x50005
    power = telemetry._power()
    assert power is not None
    assert power.throttled_flags == 0x50005
    assert power.under_voltage is True


def test_vcgencmd_absent(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path))
    assert telemetry._vcgencmd_throttled() is None


def test_power_none_without_battery_or_pi(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setattr(psutil, "sensors_battery", lambda: None, raising=False)
    assert telemetry._power() is None
