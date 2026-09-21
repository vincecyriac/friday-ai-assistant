"""``python -m friday.sentinel`` / ``friday-sentinel``."""

from __future__ import annotations

import asyncio
import sqlite3
import sys

from friday.core.config import ConfigError, load_settings
from friday.sentinel.daemon import Sentinel


def main(argv: list[str] | None = None) -> int:
    try:
        settings = load_settings()
        return asyncio.run(Sentinel(settings).run())
    except ConfigError as e:
        print(f"friday-sentinel: configuration error: {e}", file=sys.stderr)
        return 1
    except (OSError, sqlite3.Error) as e:
        print(f"friday-sentinel: cannot start: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
