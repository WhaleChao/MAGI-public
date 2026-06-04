# -*- coding: utf-8 -*-
"""Compatibility entrypoint for simulated LAF notification output.

The source of truth lives in
``casper_ecosystem.law_firm_orchestrators.simulated_line``.  Keep this wrapper
so legacy skill calls do not grow a second notification implementation.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from casper_ecosystem.law_firm_orchestrators.simulated_line import send_line_notify  # noqa: E402

__all__ = ["send_line_notify"]


if __name__ == "__main__":
    if len(sys.argv) > 1:
        send_line_notify(sys.argv[1])
    else:
        print("Usage: python simulated_line.py 'Message Content'")
