#!/usr/bin/env python3
"""
FD Studio launcher.

    python tools/fd_studio.py

Requires PySide6, numpy and pyserial. Flash apps/datalog to the board first:

    .\\tools\\build.ps1 datalog -Flash     # then double-tap RST
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# This machine has several Pythons and `python` does not resolve to the one
# carrying PySide6. Fail with the fix rather than a bare ImportError.
try:
    import PySide6  # noqa: F401
    import numpy  # noqa: F401
    import serial  # noqa: F401
except ModuleNotFoundError as exc:
    print(f"Missing dependency: {exc.name}", file=sys.stderr)
    print(f"Running on Python {sys.version.split()[0]} at {sys.executable}\n",
          file=sys.stderr)
    print("Use the launcher, which finds an interpreter that has everything:",
          file=sys.stderr)
    print("    .\\tools\\fd_studio.ps1\n", file=sys.stderr)
    print("Or pick the interpreter yourself:", file=sys.stderr)
    print("    py -3.13 tools\\fd_studio.py", file=sys.stderr)
    sys.exit(1)

from fd_studio.app import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
