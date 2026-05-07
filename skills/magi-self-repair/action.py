#!/usr/bin/env python3
"""
Compatibility self-repair wrapper for legacy callers.

The dashboard and older automation paths still look for
`skills/magi-self-repair/action.py` and expect a `repair_targets()`
function. The actual repair logic now lives in `skills/magi-doctor`.
This shim preserves the old contract and delegates to MAGI Doctor.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any


_HERE = Path(__file__).resolve()
_MAGI_ROOT = _HERE.parents[2]
_DOCTOR_PATH = _MAGI_ROOT / "skills" / "magi-doctor" / "action.py"


def _load_doctor_module():
    os.environ.setdefault("MAGI_ROOT_DIR", str(_MAGI_ROOT))
    spec = importlib.util.spec_from_file_location("magi_doctor_action", _DOCTOR_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load MAGI Doctor from {_DOCTOR_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _normalize_targets(targets: Any) -> list[str]:
    if not targets:
        return []
    out: list[str] = []
    for item in targets:
        if isinstance(item, str):
            value = item.strip()
        elif isinstance(item, dict):
            value = str(item.get("id") or "").strip()
        else:
            value = str(item).strip()
        if value:
            out.append(value)
    return out


def repair_targets(targets: Any = None) -> dict[str, Any]:
    doctor = _load_doctor_module()
    normalized = _normalize_targets(targets)

    if normalized:
        repairs = doctor.heal(normalized)
        return {
            "timestamp": datetime.now().isoformat(),
            "total_targets": len(normalized),
            "repaired": sum(1 for r in repairs if r.get("repaired")),
            "failed": sum(1 for r in repairs if not r.get("repaired")),
            "repairs": repairs,
        }

    report = doctor.diagnose()
    return doctor.heal_from_report(report)


def main() -> None:
    parser = argparse.ArgumentParser(description="MAGI legacy self-repair compatibility wrapper")
    parser.add_argument("--targets", default="", help="Comma-separated target ids")
    args = parser.parse_args()
    targets = [t.strip() for t in str(args.targets or "").split(",") if t.strip()]
    print(json.dumps(repair_targets(targets or None), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
