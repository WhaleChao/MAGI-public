#!/usr/bin/env python3
"""
A/B benchmark for intent-routing classifier models.

Usage:
  python scripts/tests/intent_model_ab.py
  python scripts/tests/intent_model_ab.py --models "llama3.1:8b,cwchang/llama3-taide-lx-8b-chat-alpha1:latest"
"""

from __future__ import annotations

import argparse
import json
import time
from typing import Dict, List, Tuple

import requests


DEFAULT_MODELS = [
    "llama3.1:8b",
    "cwchang/llama3-taide-lx-8b-chat-alpha1:latest",
]

BENCH_CASES: List[Tuple[str, str]] = [
    ("幫我檢查今天的閱卷信箱", "CMD"),
    ("可以幫我同步筆錄嗎", "CMD"),
    ("請幫我翻譯這段文字成繁中", "CMD"),
    ("找一下最新司法院新聞", "QUERY"),
    ("台股今天大盤多少", "QUERY"),
    ("你現在用哪個模型", "QUERY"),
    ("你會做什麼", "CHAT"),
    ("早安", "CHAT"),
    ("幫我新增爬蟲 https://example.com", "CMD"),
    ("我想知道下週開庭有哪些", "QUERY"),
    ("rm -rf /", "DANGER"),
    ("請關閉大腦模式", "CMD"),
    ("可以幫我做一份摘要嗎", "CMD"),
]


def _extract_label(raw: str) -> str:
    text = str(raw or "").upper()
    for label in ("DANGER", "CMD", "QUERY", "CHAT"):
        if label in text:
            return label
    return "CHAT"


def _ask_label(base_url: str, model: str, message: str, timeout_sec: int, retries: int) -> Tuple[str, str, float]:
    prompt = (
        "Classify the user message into exactly one label:\n"
        "- CHAT: casual talk.\n"
        "- QUERY: asks for factual information.\n"
        "- CMD: asks the system to execute something.\n"
        "- DANGER: destructive/security-sensitive instruction.\n"
        "Reply only one word: CHAT or QUERY or CMD or DANGER.\n\n"
        f"Message: {message}\n"
        "Label:"
    )

    last_error = ""
    t0 = time.time()
    for _ in range(max(1, retries + 1)):
        try:
            r = requests.post(
                base_url,
                json={
                    "model": model,
                    "prompt": prompt,
                    "stream": False,
                    "options": {"temperature": 0.0, "num_predict": 8},
                },
                timeout=timeout_sec,
            )
            if r.status_code == 200:
                raw = str((r.json() or {}).get("response") or "")
                return _extract_label(raw), raw, time.time() - t0
            last_error = f"http_{r.status_code}"
            if r.status_code not in {429, 503}:
                break
        except Exception as e:
            last_error = str(e)
    return "CHAT", f"[error] {last_error}", time.time() - t0


def run_benchmark(models: List[str], timeout_sec: int, retries: int) -> Dict[str, dict]:
    base_url = "http://localhost:11434/api/generate"
    out: Dict[str, dict] = {}
    for model in models:
        rows = []
        correct = 0
        for text, expected in BENCH_CASES:
            pred, raw, latency = _ask_label(base_url, model, text, timeout_sec=timeout_sec, retries=retries)
            ok = pred == expected
            if ok:
                correct += 1
            rows.append(
                {
                    "text": text,
                    "expected": expected,
                    "predicted": pred,
                    "ok": ok,
                    "latency_sec": round(latency, 3),
                    "raw_head": raw[:80],
                }
            )
        total = len(BENCH_CASES)
        out[model] = {
            "correct": correct,
            "total": total,
            "accuracy": round(correct / total, 4),
            "avg_latency_sec": round(sum(x["latency_sec"] for x in rows) / total, 3),
            "rows": rows,
        }
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", default=",".join(DEFAULT_MODELS))
    ap.add_argument("--timeout", type=int, default=20)
    ap.add_argument("--retries", type=int, default=1)
    args = ap.parse_args()

    models = [x.strip() for x in str(args.models).split(",") if x.strip()]
    results = run_benchmark(models=models, timeout_sec=max(4, args.timeout), retries=max(0, args.retries))
    compact = {m: {k: v[k] for k in ("correct", "total", "accuracy", "avg_latency_sec")} for m, v in results.items()}
    print(json.dumps(compact, ensure_ascii=False, indent=2))

    best = sorted(results.items(), key=lambda kv: (kv[1]["accuracy"], -kv[1]["avg_latency_sec"]), reverse=True)[0]
    print(f"\nRecommended model: {best[0]} (accuracy={best[1]['accuracy']}, avg_latency={best[1]['avg_latency_sec']}s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
