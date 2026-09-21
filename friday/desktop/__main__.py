"""``python -m friday.desktop`` / ``friday-desktop``: the macOS window shell."""

import sys

from friday.desktop.app import main

if __name__ == "__main__":
    sys.exit(main())
