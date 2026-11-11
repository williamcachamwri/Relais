#!/usr/bin/env python3
"""
Universal Clipboard for Windows (iPhone <-> Windows Continuity Bridge).
Main entrypoint invoking the CoreDeviceBridge daemon service.
"""
import sys
from pathlib import Path

_root = str(Path(__file__).resolve().parent)
if _root not in sys.path:
    sys.path.insert(0, _root)

from CoreDeviceBridge.bridgeService import main

if __name__ == "__main__":
    sys.exit(main())
