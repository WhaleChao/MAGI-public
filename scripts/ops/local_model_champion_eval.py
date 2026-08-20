#!/usr/bin/env python3
"""Offline-only champion/challenger evaluation contract for local deep models.

It evaluates pre-recorded JSONL outputs.  It never downloads, loads, or
contacts a model provider; production activation remains a separate approval.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

REQUIRED_AREAS = frozenset({"zh_legal", "tool_json", "refusal", "latency_memory", "crash"})
# 24GB M4 guardrails: the challenger must leave recovery headroom and cannot
# promote after any process crash.  Product teams may tighten these in the
# recorded fixture; the runner never loosens them dynamically.
MAX_P95_LATENCY_SEC = 90.0
MAX_PEAK_MEMORY_GB = 22.0


def score(rows: list[dict[str, Any]]) -> dict[str, Any]:
    areas = {str(row.get("area") or "") for row in rows}
    missing = sorted(REQUIRED_AREAS - areas)
    failures = [row for row in rows if not bool(row.get("passed"))]
    for row in rows:
        if row.get("area") == "latency_memory" and (
            float(row.get("p95_latency_sec", 0) or 0) > MAX_P95_LATENCY_SEC
            or float(row.get("peak_memory_gb", 0) or 0) > MAX_PEAK_MEMORY_GB
        ):
            failures.append(row)
        if row.get("area") == "crash" and int(row.get("crash_count", 0) or 0) != 0:
            failures.append(row)
    crash = [row for row in rows if row.get("area") == "crash" and not bool(row.get("passed"))]
    return {"ok": not missing and not failures and not crash, "missing_areas": missing, "failed": len(failures), "total": len(rows), "thresholds": {"max_p95_latency_sec": MAX_P95_LATENCY_SEC, "max_peak_memory_gb": MAX_PEAK_MEMORY_GB, "max_crash_count": 0}}


def load(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="score offline local model evaluation JSONL")
    parser.add_argument("--champion", required=True, type=Path, help="existing 26B recorded results")
    parser.add_argument("--challenger", required=True, type=Path, help="configured gpt-oss-20b recorded results")
    args = parser.parse_args(argv)
    report = {"champion": score(load(args.champion)), "challenger": score(load(args.challenger)), "offline_only": True}
    report["promotion_allowed"] = bool(report["challenger"]["ok"])
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report["promotion_allowed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
