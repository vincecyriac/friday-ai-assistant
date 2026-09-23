"""Browser assets both nodes serve: the FRIDAY orb and its Three.js runtime.

The desktop HUD mounts this at /shared from its GUI server; the sentinel mounts
the same directory at /shared from the dashboard app. One orb, one three.js.
"""

from pathlib import Path

WEBASSETS_DIR = Path(__file__).resolve().parent

__all__ = ["WEBASSETS_DIR"]
