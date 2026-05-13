#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Day2 autopilot monitor (rolling window report).

Purpose:
- Measure tick/nightly stability over the last N hours.
- Aggregate blocker reasons.
- Export a concise TXT report for legal/ops review.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List

MAGI_ROOT = Path(os.environ.get("MAGI_ROOT_DIR", str(Path(__file__).resolve().parents[2])))
if str(MAGI_ROOT) not in sys.path:
    sys.path.insert(0, str(MAGI_ROOT))

from api.runtime_paths import get_autopilot_runs_dir

RUNS_DIR = Path(os.environ.get("MAGI_AUTOPILOT_RUNS_DIR", str(get_autopilot_runs_dir())))
EXPORT_MOD = MAGI_ROOT / "skills" / "ops" / "export_text.py"


@dataclass
class Row:
    ts: datetime
    task: str
    ok: bool
    blockers: List[str]
    path: str


def _load_json(path: Path) -> Dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _parse_ts(v: str) -> datetime | None:
    s = (v or "").strip()
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


def collect(hours: int = 24) -> Dict[str, Any]:
    now = datetime.now()
    start = now - timedelta(hours=max(1, int(hours)))
    rows: List[Row] = []

    if RUNS_DIR.exists():
        for p in RUNS_DIR.glob("*/report.json"):
            obj = _load_json(p)
            ts = _parse_ts(str(obj.get("ts") or ""))
            if not ts or ts < start:
                continue
            task = str(obj.get("task") or "").strip()
            if task not in {"tick", "nightly", "self_test"}:
                continue
            details = obj.get("details") if isinstance(obj.get("details"), dict) else {}
            blockers = details.get("blockers") if isinstance(details, dict) else []
            if not isinstance(blockers, list):
                blockers = []
            rows.append(
                Row(
                    ts=ts,
                    task=task,
                    ok=bool(obj.get("ok")),
                    blockers=[str(x) for x in blockers if str(x).strip()],
                    path=str(p),
                )
            )

    rows.sort(key=lambda x: x.ts)
    total = len(rows)
    ok_count = sum(1 for r in rows if r.ok)
    fail_count = total - ok_count
    success_rate = (ok_count / total * 100.0) if total else 0.0

    by_task: Dict[str, Dict[str, Any]] = {}
    for t in ("tick", "nightly", "self_test"):
        task_rows = [r for r in rows if r.task == t]
        t_total = len(task_rows)
        t_ok = sum(1 for r in task_rows if r.ok)
        by_task[t] = {
            "total": t_total,
            "ok": t_ok,
            "fail": t_total - t_ok,
            "success_rate": round((t_ok / t_total * 100.0), 2) if t_total else 0.0,
        }

    blocker_counter = Counter()
    for r in rows:
        for b in r.blockers:
            blocker_counter[b] += 1

    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "window_hours": int(hours),
        "window_start": start.isoformat(timespec="seconds"),
        "window_end": now.isoformat(timespec="seconds"),
        "total": total,
        "ok": ok_count,
        "fail": fail_count,
        "success_rate": round(success_rate, 2),
        "by_task": by_task,
        "top_blockers": blocker_counter.most_common(12),
        "recent_runs": [
            {
                "ts": r.ts.isoformat(timespec="seconds"),
                "task": r.task,
                "ok": r.ok,
                "blockers": r.blockers,
                "path": r.path,
            }
            for r in rows[-20:]
        ],
    }


def _load_export_txt():
    import importlib.util

    spec = importlib.util.spec_from_file_location("export_text", str(EXPORT_MOD))
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod.export_txt


def render_txt(rep: Dict[str, Any]) -> str:
    lines: List[str] = []
    lines.append("MAGI Day2 自動排程穩定度報告")
    lines.append(f"生成時間: {rep.get('generated_at')}")
    lines.append(
        f"觀測區間: {rep.get('window_start')} ~ {rep.get('window_end')} (近 {rep.get('window_hours')}h)"
    )
    lines.append("")
    lines.append("一、整體")
    lines.append(
        f"- 總執行: {rep.get('total')}；成功: {rep.get('ok')}；失敗: {rep.get('fail')}；成功率: {rep.get('success_rate')}%"
    )
    lines.append("")
    lines.append("二、任務別")
    for t in ("tick", "nightly", "self_test"):
        row = (rep.get("by_task") or {}).get(t) or {}
        lines.append(
            f"- {t}: total={row.get('total', 0)}, ok={row.get('ok', 0)}, fail={row.get('fail', 0)}, success_rate={row.get('success_rate', 0)}%"
        )
    lines.append("")
    lines.append("三、主要阻塞原因")
    top = rep.get("top_blockers") or []
    if not top:
        lines.append("- 無")
    else:
        for b, c in top:
            lines.append(f"- {b}: {c}")
    lines.append("")
    lines.append("四、最近執行（末20筆）")
    for r in rep.get("recent_runs") or []:
        lines.append(f"- {r.get('ts')} {r.get('task')} ok={r.get('ok')} blockers={';'.join(r.get('blockers') or []) or '-'}")
    return "\n".join(lines).strip()


def main() -> int:
    ap = argparse.ArgumentParser(description="day2_autopilot_monitor")
    ap.add_argument("--hours", type=int, default=24, help="rolling window hours")
    args = ap.parse_args()

    rep = collect(hours=max(1, int(args.hours)))
    txt = render_txt(rep)

    export_txt = _load_export_txt()
    out = export_txt(txt, prefix="qa_day2_autopilot_monitor")

    payload = {
        "success": True,
        "report": rep,
        "txt_export": out,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
