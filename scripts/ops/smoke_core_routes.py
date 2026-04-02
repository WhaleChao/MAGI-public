#!/usr/bin/env python3
"""
Smoke test for core MAGI text-routing capabilities.

Usage:
  python3 scripts/ops/smoke_core_routes.py
  python3 scripts/ops/smoke_core_routes.py --with-network
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from pathlib import Path
from dataclasses import dataclass
from typing import Sequence


MAGI_ROOT = os.environ.get("MAGI_ROOT_DIR", str(Path(__file__).resolve().parents[2]))
if MAGI_ROOT not in sys.path:
    sys.path.insert(0, MAGI_ROOT)

# Keep nightly smoke fast and deterministic by default.
os.environ.setdefault("MAGI_DOC_AUTO_INGEST", "0")
os.environ.setdefault("MAGI_JUDGMENT_CHAT_MAX_RESULTS", "2")
os.environ.setdefault("MAGI_JUDGMENT_CHAT_TIMEOUT_SEC", "45")
os.environ.setdefault("MAGI_TW_REVIEW_ENABLED", "0")
os.environ.setdefault("MAGI_AVOID_DISTRIBUTED", "1")

from api.orchestrator import Orchestrator  # noqa: E402


@dataclass
class Case:
    name: str
    message: str
    expect_substring: str | Sequence[str]
    network: bool = False
    heavy: bool = False
    timeout_sec: int = 18


def _cases() -> list[Case]:
    return [
        Case("translate_guide", "你會翻譯嗎？", "我可以幫您翻譯"),
        Case("summary_guide", "你會摘要嗎？", "我可以幫您做摘要"),
        Case("labor_guide", "你會算勞基法嗎？", "我可以幫您計算勞基法"),
        Case("labor_exec", "幫我算勞基法加班費 30000", "請提供月薪金額"),
        Case("judgment_guide", "你會查判決嗎？", "我可以幫您查判決"),
        Case("stock_guide", "你會追蹤股票嗎？", "我可以幫您追蹤股票"),
        Case("stock_list", "追蹤清單", "目前追蹤股票"),
        Case("translate_exec", "請幫我翻譯 Hello world", ("你好世界", "您好世界"), heavy=True, timeout_sec=45),
        Case(
            "summary_exec",
            "請幫我摘要 這是一篇短文。第一點很重要。第二點也很重要。第三點是結論。",
            "摘要結果",
            heavy=True,
            timeout_sec=45,
        ),
        Case("judgment_exec", "查判決 傷害", "判決搜尋完成：傷害", network=True, heavy=True, timeout_sec=90),
    ]


def _run_case(orch: Orchestrator, case: Case) -> str:
    return orch.process_message(
        user_id=f"smoke_core_routes_{case.name}",
        message=case.message,
        platform="Telegram",
        role="user",
    )


class CaseTimeoutError(TimeoutError):
    pass


def _alarm_handler(signum, frame):
    raise CaseTimeoutError


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--with-network", action="store_true", help="include network-dependent route checks")
    ap.add_argument("--with-heavy", action="store_true", help="include slower execute-route checks")
    ap.add_argument("--json-out", default="", help="optional path to save JSON report")
    args = ap.parse_args()

    orch = Orchestrator()
    failed = 0
    total = 0
    case_reports: list[dict[str, object]] = []
    started = time.time()

    print("=== MAGI Core Route Smoke ===")
    for case in _cases():
        if case.network and not args.with_network:
            continue
        if case.heavy and not args.with_heavy:
            continue
        total += 1
        timed_out = False
        previous_handler = signal.signal(signal.SIGALRM, _alarm_handler)
        signal.setitimer(signal.ITIMER_REAL, case.timeout_sec)
        try:
            out = _run_case(orch, case)
        except CaseTimeoutError:
            timed_out = True
            out = f"TIMEOUT after {case.timeout_sec}s"
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous_handler)
        text = str(out or "").strip()
        expected = (
            tuple(case.expect_substring)
            if isinstance(case.expect_substring, (list, tuple))
            else (str(case.expect_substring),)
        )
        ok = any(token in text for token in expected)
        if timed_out:
            ok = False
        mark = "PASS" if ok else "FAIL"
        preview = text.replace("\n", " ")[:180]
        print(f"{mark} {case.name}: {preview}")
        case_reports.append(
            {
                "name": case.name,
                "pass": ok,
                "status": mark,
                "network": case.network,
                "heavy": case.heavy,
                "timeout_sec": case.timeout_sec,
                "preview": preview,
            }
        )
        if not ok:
            failed += 1

    print(f"--- Summary ---\nPASS: {total - failed}\nFAIL: {failed}")
    if args.json_out:
        out_path = Path(args.json_out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            json.dumps(
                {
                    "summary": {
                        "pass": total - failed,
                        "fail": failed,
                        "total": total,
                        "elapsed_sec": round(time.time() - started, 1),
                    },
                    "mode": {
                        "with_network": bool(args.with_network),
                        "with_heavy": bool(args.with_heavy),
                    },
                    "cases": case_reports,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
