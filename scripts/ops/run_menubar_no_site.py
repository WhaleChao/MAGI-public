#!/usr/bin/env python3
"""Launch the MAGI menubar from the runtime copy without importing user site.

launchd can hang while resolving Desktop-hosted virtualenv paths.  The menubar
is small enough to bootstrap explicitly: add the runtime root and venv
site-packages, then execute the GUI module.
"""

from __future__ import annotations

import os
import runpy
import site
import sys
from pathlib import Path


ROOT = Path(os.environ.get("MAGI_ROOT") or Path(__file__).resolve().parents[2]).resolve()
VENV = ROOT / "venv"
PYVER = f"python{sys.version_info.major}.{sys.version_info.minor}"
SITE_PACKAGES = VENV / "lib" / PYVER / "site-packages"

os.environ["MAGI_ROOT"] = str(ROOT)
os.environ["MAGI_ROOT_DIR"] = str(ROOT)
os.environ.setdefault("MAGI_MYSQL_USE_PURE", "1")
os.environ.setdefault("MYSQL_USE_PURE", "1")

for path in (ROOT, SITE_PACKAGES):
    if path.exists() and str(path) not in sys.path:
        sys.path.insert(0, str(path))

if SITE_PACKAGES.exists():
    site.addsitedir(str(SITE_PACKAGES))

try:
    from api.mysql_connector_guard import install_mysql_cext_blocker

    install_mysql_cext_blocker()
except Exception:
    pass

script = ROOT / "gui" / "magi_menubar.py"
sys.argv = [str(script)]
runpy.run_path(str(script), run_name="__main__")
