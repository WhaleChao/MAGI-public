#!/usr/bin/env python3
"""
Launch MAGI daemon from launchd without Python's automatic site initialization.

Homebrew Python 3.14 can intermittently stall under launchd while processing
venv/site hooks.  launchd runs this file with `python -S`; this bootstrap then
adds only the paths MAGI needs and installs the runtime guards explicitly.
"""

from __future__ import annotations

import os
import runpy
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
VENV_SITE = ROOT / "venv" / "lib" / "python3.14" / "site-packages"

for path in (str(ROOT), str(VENV_SITE)):
    if path not in sys.path:
        sys.path.insert(0, path)

os.chdir(ROOT)
os.environ.setdefault("HOME", str(Path.home()))
os.environ.setdefault("MAGI_ROOT", str(ROOT))
os.environ.setdefault("MAGI_ROOT_DIR", str(ROOT))
os.environ.setdefault("MAGI_MYSQL_USE_PURE", "1")
os.environ.setdefault("MYSQL_USE_PURE", "1")
os.environ.setdefault("MAGI_AVOID_DISTRIBUTED", "1")

try:
    from api.mysql_connector_guard import install_mysql_cext_blocker

    install_mysql_cext_blocker()
except Exception:
    pass

runpy.run_path(str(ROOT / "daemon.py"), run_name="__main__")
