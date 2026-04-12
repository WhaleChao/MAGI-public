#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
import sys
from pathlib import Path as _Path

_MAGI_ROOT = Path(__file__).resolve().parents[2]
if str(_MAGI_ROOT) not in sys.path:
    sys.path.insert(0, str(_MAGI_ROOT))

from skills.engine.trajectory_compressor import TrajectoryCompressor
from skills.evolution.skill_improver import build_improvement_plan
from skills.evolution.usage_tracker import UsageTracker


def _fake_messages(count: int) -> list[dict]:
    messages = [{"role": "system", "content": "system"}]
    for i in range(count):
        messages.append({"role": "user" if i % 2 == 0 else "assistant", "content": f"message-{i}"})
    return messages


def main() -> int:
    compressor = TrajectoryCompressor()
    compressed = compressor.compress(_fake_messages(100), max_tokens=2000, max_messages=20)

    with tempfile.NamedTemporaryFile(prefix="magi_usage_", suffix=".jsonl", delete=False) as handle:
        tracker = UsageTracker(handle.name)
        now = datetime.now()
        rows = []
        for idx in range(7):
            rows.append(
                {
                    "timestamp": (now - timedelta(days=idx)).isoformat(),
                    "skill": "statutes-vdb",
                    "success": idx % 3 != 0,
                    "latency_ms": 100 + idx,
                    "intent": "help",
                    "failure_reason": "timeout" if idx % 3 == 0 else "",
                    "auto_repaired": False,
                }
            )
        _Path(handle.name).write_text(
            "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
            encoding="utf-8",
        )
        report = tracker.daily_report(days=7)
        summary = tracker.summarize(days=7)

    plan = build_improvement_plan("statutes-vdb", summary)
    success = (
        len(compressed) <= 20
        and int(summary.get("event_count") or 0) == 7
        and str(summary.get("top_failure_reason") or "") == "timeout"
        and bool(report)
        and bool(plan.get("suggestions"))
    )
    result = {
        "success": success,
        "compressed_count": len(compressed),
        "report": report,
        "summary": summary,
        "improvement_plan": plan,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
