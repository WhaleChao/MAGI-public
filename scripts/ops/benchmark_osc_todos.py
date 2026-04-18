#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
benchmark_osc_todos.py
========================
Live benchmark for OSC todo deadline extraction.

Tests synthetic filenames covering all 5 deadline categories:
  補正 / 上訴 / 陳述意見 / 繳費 / 閱卷期限

Metric: deadline_extraction_rate (% of test cases where deadline is extracted)
Exit 1 if deadline_extraction_rate < 0.90.
"""
import os
import sys
import json
import time

MAGI_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, MAGI_ROOT)
sys.path.insert(0, os.path.join(MAGI_ROOT, "skills", "osc-orchestrator"))

OUTPUT_PATH = os.path.join(MAGI_ROOT, ".runtime", "benchmark_osc_todos_latest.json")
THRESHOLD = 0.90

# (filename_containing_deadline_text, expected_type, expected_days)
TEST_CASES = [
    # 補正 patterns
    ("20240305 裁定（王大明；應於本裁定送達後20日內補正）.pdf", "補正", 20),
    ("20240305 函文（李小花；請於文到10日內補正）.pdf", "補正", 10),
    ("20241015 裁定（陳大山；文到7日內補正委任狀）.pdf", "補正", 7),
    ("20241015 裁定（陳大山；應於15日內補正）.pdf", "補正", 15),
    # 上訴 patterns
    ("20240305 判決（王大明；如不服本判決得於20日內提起上訴）.pdf", "上訴", 20),
    ("20240305 判決（王大明；應於判決送達後14日內提起上訴）.pdf", "上訴", 14),
    # 陳述意見
    ("20240305 函文（李小花；應於文到20日內陳述意見）.pdf", "陳述意見", 20),
    ("20240305 函文（李小花；限於14日內陳述意見）.pdf", "陳述意見", 14),
    # 繳費
    ("20241015 函文（陳大山；應於文到30日內繳納規費）.pdf", "繳費", 30),
    # 閱卷期限
    ("20241015 函文（陳大山；應於20日內閱卷）.pdf", "閱卷期限", 20),
]


def main():
    try:
        from osc_headless.todos import extract_todos_from_filename
    except ImportError:
        sys.path.insert(0, os.path.join(MAGI_ROOT, "skills", "osc-orchestrator"))
        from osc_headless.todos import extract_todos_from_filename

    total = len(TEST_CASES)
    hits = 0
    results = []

    for filename, expected_type, expected_days in TEST_CASES:
        todos = extract_todos_from_filename(filename)
        matched = [t for t in todos if t.get("type") == expected_type]
        ok = bool(matched)
        hits += int(ok)
        results.append({
            "filename": filename,
            "expected_type": expected_type,
            "expected_days": expected_days,
            "matched": ok,
            "todos": todos,
        })
        status = "✅" if ok else "❌"
        print(f"  {status} {expected_type:10s} {filename[:60]}")

    rate = hits / total if total else 0.0
    summary = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "total": total,
        "hits": hits,
        "deadline_extraction_rate": round(rate, 3),
        "threshold": THRESHOLD,
        "results": results,
    }

    import datetime as _dt

    def _default_serial(obj):
        if isinstance(obj, (_dt.datetime, _dt.date)):
            return obj.isoformat()
        raise TypeError(f"Not serializable: {type(obj)}")

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2, default=_default_serial)

    print(f"\n[benchmark] deadline_extraction_rate={rate:.1%} (threshold={THRESHOLD:.0%})")
    if rate < THRESHOLD:
        print(f"[FAIL] {hits}/{total} matched, below threshold.")
        sys.exit(1)
    else:
        print(f"[PASS] {hits}/{total} matched.")


if __name__ == "__main__":
    main()
