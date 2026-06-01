#!/usr/bin/env python3
"""Audit active imports that bypass MAGI's canonical feature modules."""

from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "config" / "single_source_of_truth.json"
EXCLUDE_PARTS = {
    ".git",
    ".pytest_cache",
    "__pycache__",
    ".venv",
    "venv",
    "node_modules",
    "dist",
    "tests",
    ".claude",
    ".claire",
}


def _iter_python_files():
    for path in ROOT.rglob("*.py"):
        rel = path.relative_to(ROOT)
        if any(part in EXCLUDE_PARTS for part in rel.parts):
            continue
        yield path


def audit() -> dict:
    data = json.loads(CONFIG.read_text(encoding="utf-8"))
    failures: list[dict] = []
    for feature, spec in (data.get("features") or {}).items():
        forbidden = list(spec.get("forbidden_imports") or [])
        if not forbidden:
            continue
        for path in _iter_python_files():
            text = path.read_text(encoding="utf-8", errors="ignore")
            for needle in forbidden:
                if needle in text:
                    failures.append(
                        {
                            "feature": feature,
                            "file": str(path.relative_to(ROOT)),
                            "forbidden": needle,
                            "canonical": spec.get("canonical"),
                        }
                    )
    return {"ok": not failures, "failures": failures}


def main() -> int:
    result = audit()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
