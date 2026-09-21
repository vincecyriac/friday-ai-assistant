"""friday.core and friday.sentinel must import on a headless Linux box with
only the base dependencies. A fresh interpreter imports every submodule with
the macOS/desktop-only modules poisoned; any leak fails here, not on the Pi."""

import os
import pkgutil
import subprocess
import sys

import friday.core
import friday.sentinel

POISON = ("Quartz", "pyaudio", "cv2", "webview", "EventKit", "Foundation", "AppKit", "objc",
          "pyautogui", "termios", "tty", "PIL", "numpy")

SITECUSTOMIZE = """
import sys
POISON = {poison!r}

class _Blocker:
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in POISON:
            raise ImportError(f"{{name}} is not allowed in friday.core / friday.sentinel")
        return None

sys.meta_path.insert(0, _Blocker())
"""

SCRIPT = """
import importlib, sys
failed = []
for name in {modules!r}:
    try:
        importlib.import_module(name)
    except ImportError as e:
        failed.append(f"{{name}}: {{e}}")
print("\\n".join(failed))
sys.exit(1 if failed else 0)
"""


def _modules(package) -> list[str]:
    names = [package.__name__]
    for info in pkgutil.walk_packages(package.__path__, package.__name__ + "."):
        names.append(info.name)
    return names


def test_core_and_sentinel_never_import_platform_modules(tmp_path):
    modules = sorted(set(_modules(friday.core) + _modules(friday.sentinel)))
    assert "friday.core.storage" in modules and "friday.sentinel.daemon" in modules
    (tmp_path / "sitecustomize.py").write_text(SITECUSTOMIZE.format(poison=POISON))
    env = {**os.environ,
           "PYTHONPATH": str(tmp_path) + os.pathsep + os.environ.get("PYTHONPATH", "")}
    result = subprocess.run([sys.executable, "-c", SCRIPT.format(modules=modules)],
                            env=env, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, f"leaked platform imports:\n{result.stdout}\n{result.stderr}"
