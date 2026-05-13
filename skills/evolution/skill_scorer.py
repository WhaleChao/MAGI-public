from __future__ import annotations

from typing import Dict


def score_skill_run(event: Dict[str, object]) -> Dict[str, object]:
    success = bool(event.get("success"))
    latency_ms = int(event.get("latency_ms") or 0)
    repaired = bool(event.get("auto_repaired"))

    score = 100
    if not success:
        score -= 55
    if latency_ms > 10000:
        score -= 15
    elif latency_ms > 4000:
        score -= 8
    if repaired:
        score -= 10
    score = max(0, min(100, score))
    return {
        "skill": str(event.get("skill") or "").strip(),
        "score": score,
        "bucket": "good" if score >= 80 else ("warning" if score >= 60 else "needs_improvement"),
    }
