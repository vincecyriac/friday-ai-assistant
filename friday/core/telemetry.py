"""Lightweight system telemetry that never crashes on a missing sensor.

Every probe is wrapped: a failure yields ``None`` (or ``{}``) for that field
and a one-time DEBUG line. macOS has no thermal zones, a Pi has no battery, a
Fedora box has neither vcgencmd nor a battery — all of those are ordinary.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Callable, TypeVar

import psutil

from friday.core.events import PowerInfo, TelemetrySnapshot
from friday.core.platform import PlatformInfo, detect

log = logging.getLogger(__name__)

T = TypeVar("T")
_reported: set[str] = set()


def _probe(name: str, fn: Callable[[], T], default: T = None) -> T:
    try:
        return fn()
    except Exception as e:
        if name not in _reported:
            _reported.add(name)
            log.debug("telemetry probe %s unavailable: %s", name, e)
        return default


def collect(node_id: str, data_dir: Path, *, platform: PlatformInfo | None = None,
            sys_root: Path = Path("/")) -> TelemetrySnapshot:
    info = platform or detect(sys_root)
    vm = _probe("memory", psutil.virtual_memory)
    du = _probe("disk", lambda: psutil.disk_usage(str(data_dir)))
    return TelemetrySnapshot(
        ts=time.time(),
        node_id=node_id,
        platform=info,
        cpu_percent=_probe("cpu", lambda: float(psutil.cpu_percent(interval=None))),
        load_avg=_probe("load", lambda: tuple(float(x) for x in os.getloadavg())),
        mem_total=vm.total if vm else None,
        mem_used=(vm.total - vm.available) if vm else None,
        mem_available=vm.available if vm else None,
        disk_total=du.total if du else None,
        disk_used=du.used if du else None,
        disk_free=du.free if du else None,
        disk_path=str(data_dir),
        uptime_s=_probe("uptime", lambda: max(0.0, time.time() - psutil.boot_time())),
        thermal=_probe("thermal", lambda: _thermal(sys_root), {}) or {},
        power=_probe("power", _power),
    )


def _thermal(sys_root: Path) -> dict[str, float]:
    """psutil first (Linux with lm-sensors); then raw sysfs (Pi, minimal Linux); else {}."""
    out: dict[str, float] = {}
    sensors = getattr(psutil, "sensors_temperatures", None)
    if sensors is not None:
        try:
            for chip, entries in (sensors() or {}).items():
                for i, entry in enumerate(entries):
                    if entry.current is None:
                        continue
                    out[f"{chip}/{entry.label or i}"] = float(entry.current)
        except Exception:
            out = {}
    if out:
        return out
    base = sys_root / "sys" / "class" / "thermal"
    try:
        zones = sorted(base.glob("thermal_zone*"))
    except OSError:
        return out
    for zone in zones:
        try:
            kind = (zone / "type").read_text().strip() or zone.name
            out[kind] = float((zone / "temp").read_text().strip()) / 1000.0
        except (OSError, ValueError):
            continue
    return out


def _power() -> PowerInfo | None:
    battery = None
    sensors_battery = getattr(psutil, "sensors_battery", None)
    if sensors_battery is not None:
        try:
            battery = sensors_battery()
        except Exception:
            battery = None
    flags = _vcgencmd_throttled()
    if battery is None and flags is None:
        return None
    return PowerInfo(
        battery_percent=float(battery.percent) if battery is not None else None,
        on_ac=bool(battery.power_plugged) if battery is not None else None,
        throttled_flags=flags,
        under_voltage=None if flags is None else bool(flags & 0x1),
    )


def _vcgencmd_throttled() -> int | None:
    """Raspberry Pi firmware throttle flags; bit 0 = under-voltage now."""
    exe = shutil.which("vcgencmd")
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "get_throttled"], capture_output=True, text=True,
                             timeout=2.0).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(r"throttled=(0x[0-9a-fA-F]+)", out)
    return int(match.group(1), 16) if match else None
