#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Compatibility CLI wrapper for `skills/iron-dome/action.py`."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def main() -> int:
    src = Path(__file__).resolve().parent.parent / "iron-dome" / "action.py"
    cmd = [sys.executable, str(src), *sys.argv[1:]]
    p = subprocess.run(cmd)
    return int(p.returncode)


if __name__ == "__main__":
    raise SystemExit(main())

