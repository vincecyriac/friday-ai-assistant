"""Where am I running? Feature probes only; never an OS name comparison.

Every probe takes ``sys_root`` so tests can point it at a fake filesystem, and
every probe swallows its own errors: a node must never fail to boot because a
sysfs file has an unexpected shape.
"""

from __future__ import annotations

import platform as _platform
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class PlatformInfo:
    system: str          # "Darwin", "Linux", ...
    machine: str         # raw platform.machine()
    arch_family: str     # "arm64" | "x86_64" | "other"
    hostname: str
    python: str
    is_raspberry_pi: bool
    distro: str | None   # PRETTY_NAME from /etc/os-release; None where absent

    @property
    def summary(self) -> str:
        return f"{self.system}/{self.arch_family}"

    def to_dict(self) -> dict:
        return asdict(self)


def normalise_arch(machine: str) -> str:
    m = machine.lower()
    if m in ("aarch64", "arm64"):
        return "arm64"
    if m in ("x86_64", "amd64"):
        return "x86_64"
    return "other"


def detect(sys_root: Path = Path("/")) -> PlatformInfo:
    machine = _safe(_platform.machine, "unknown")
    return PlatformInfo(
        system=_safe(_platform.system, "unknown"),
        machine=machine,
        arch_family=normalise_arch(machine),
        hostname=_safe(_platform.node, "unknown") or "unknown",
        python=_safe(_platform.python_version, "unknown"),
        is_raspberry_pi=_is_raspberry_pi(sys_root),
        distro=_distro(sys_root),
    )


def _safe(fn, default: str) -> str:
    try:
        return fn() or default
    except Exception:
        return default


def _is_raspberry_pi(sys_root: Path) -> bool:
    try:
        text = (sys_root / "proc" / "device-tree" / "model").read_text(errors="ignore")
    except OSError:
        return False
    return "raspberry pi" in text.lower()


def _distro(sys_root: Path) -> str | None:
    try:
        lines = (sys_root / "etc" / "os-release").read_text(errors="ignore").splitlines()
    except OSError:
        return None
    for line in lines:
        if line.startswith("PRETTY_NAME="):
            return line.split("=", 1)[1].strip().strip('"') or None
    return None
