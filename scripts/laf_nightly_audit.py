"""Compatibility entrypoint for the LAF nightly audit.

The source of truth lives in
``casper_ecosystem.law_firm_orchestrators.laf_nightly_audit``. Keeping this
file as a thin wrapper prevents the script path and daemon/import path from
drifting into different business rules.
"""
from __future__ import annotations

import runpy
import sys
from pathlib import Path

MAGI_ROOT = Path(__file__).resolve().parents[1]
if str(MAGI_ROOT) not in sys.path:
    sys.path.insert(0, str(MAGI_ROOT))

from casper_ecosystem.law_firm_orchestrators import laf_nightly_audit as _canonical

for _name in dir(_canonical):
    if not (_name.startswith("__") and _name.endswith("__")):
        globals()[_name] = getattr(_canonical, _name)

__all__ = [
    _name
    for _name in globals()
    if not (_name.startswith("__") and _name.endswith("__"))
]


if __name__ == "__main__":
    runpy.run_path(
        str(MAGI_ROOT / "casper_ecosystem" / "law_firm_orchestrators" / "laf_nightly_audit.py"),
        run_name="__main__",
    )
