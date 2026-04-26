#!/usr/bin/env python3
"""Benchmark the Apple Translation + LLM post-edit (APE) translator path.

Runs a fixed suite of TW-legal sentences through three configurations:
  1. Google GTX primary (current stable fast path)
  2. Apple Translation baseline only (no LLM polish)
  3. Apple Translation + LLM post-edit (full APE)

Prints per-row provider/degraded/elapsed_ms + simple term hit rate so the
regression cron can fail loudly if APE starts falling back too often.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from skills.engine.apple_translation import is_available as _apple_avail, translate as _apple_translate
from skills.translator._apple_post_edit import translate_with_ape
from skills.translator.action import translate as _translate


def _warmup_omlx(timeout_sec: int = 60) -> bool:
    """Pre-warm the primary oMLX chat server (port 8080) before benchmark.

    With loaded_count=0 the first request can timeout; pre-warming avoids
    a cold-start failure that would register as a hard APE regression.
    Returns True if server is responsive, False if unreachable.
    """
    import requests

    omlx_base = os.environ.get("MAGI_OMLX_CHAT_URL",
                               os.environ.get("MAGI_OMLX_BASE", "http://127.0.0.1:8080")).rstrip("/")
    models_url = f"{omlx_base}/v1/models"
    deadline = time.time() + timeout_sec
    attempt = 0
    while time.time() < deadline:
        attempt += 1
        try:
            r = requests.get(models_url, timeout=5)
            if r.status_code == 200:
                print(f"[warmup] oMLX responsive after attempt {attempt}")
                return True
        except Exception as exc:
            if attempt == 1:
                print(f"[warmup] oMLX not ready ({exc.__class__.__name__}), waiting...",
                      file=sys.stderr)
        time.sleep(3)
    print(f"[warmup] oMLX still unreachable after {timeout_sec}s", file=sys.stderr)
    return False


SUITE = [
    {
        "id": "prayer_for_relief",
        "zh": "原告訴之聲明：被告應給付原告新臺幣200,000元整。",
        "expect_terms_en": ["prayer for relief", "defendant", "plaintiff", "200,000"],
    },
    {
        "id": "criminal_indictment",
        "zh": "被告犯詐欺罪，處有期徒刑六月。",
        "expect_terms_en": ["defendant", "fraud", "imprisonment"],
    },
    {
        "id": "civil_tort",
        "zh": "被告應就原告所受之損害負侵權行為損害賠償責任。",
        "expect_terms_en": ["defendant", "plaintiff", "damages"],
    },
    {
        "id": "case_number",
        "zh": "本院114年度原訴字第000024號案現正審理中。",
        "expect_terms_en": ["114年度原訴字第000024號", "court"],
    },
]


def _term_hit_rate(text: str, expected: list) -> float:
    if not expected:
        return 1.0
    t = (text or "").lower()
    hits = sum(1 for kw in expected if kw.lower() in t)
    return hits / len(expected)


def _bench_gtx(item):
    t0 = time.monotonic()
    r = _translate({
        "text": item["zh"], "source_lang": "zh-Hant", "target_lang": "en",
        "mode": "full", "export": "0", "llm_timeout": 45, "timeout_sec": 90,
    })
    return {
        "id": item["id"], "stage": "gtx_primary",
        "provider": r.get("provider"), "degraded": bool(r.get("degraded")),
        "elapsed_ms": int((time.monotonic() - t0) * 1000),
        "text": r.get("text") or "",
        "term_hit_rate": _term_hit_rate(r.get("text") or "", item["expect_terms_en"]),
    }


def _bench_apple_baseline(item):
    t0 = time.monotonic()
    r = _apple_translate(item["zh"], source_lang="zh-Hant", target_lang="en", timeout_sec=15)
    return {
        "id": item["id"], "stage": "apple_baseline",
        "provider": r.get("provider"), "success": bool(r.get("success")),
        "elapsed_ms": int((time.monotonic() - t0) * 1000),
        "text": r.get("text") or "",
        "term_hit_rate": _term_hit_rate(r.get("text") or "", item["expect_terms_en"]),
    }


def _bench_ape(item):
    t0 = time.monotonic()
    r = translate_with_ape(
        item["zh"], source_lang="zh-Hant", target_lang="en",
        llm_timeout=45, apple_timeout=10.0,
    )
    return {
        "id": item["id"], "stage": "apple_ape",
        "provider": r.get("provider"), "degraded": bool(r.get("degraded")),
        "elapsed_ms": int((time.monotonic() - t0) * 1000),
        "text": r.get("text") or "",
        "validator_reasons": (r.get("validator") or {}).get("reasons"),
        "term_hit_rate": _term_hit_rate(r.get("text") or "", item["expect_terms_en"]),
    }


def _write_static_result(summary: dict) -> None:
    """Write benchmark result to static/ for the web dashboard."""
    import datetime
    static_dir = _ROOT / "static"
    try:
        static_dir.mkdir(parents=True, exist_ok=True)
        out_path = static_dir / "translator_ape_latest.json"
        payload = dict(summary)
        payload["generated_at"] = datetime.datetime.now().isoformat()
        # Keep rows but truncate text fields for the dashboard (don't need full translations)
        if "rows" in payload:
            for row in payload["rows"]:
                if len(row.get("text") or "") > 200:
                    row["text"] = row["text"][:200] + "…"
        with open(out_path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
    except Exception as exc:
        print(f"[warn] failed to write static result: {exc}", file=sys.stderr)


def _send_dc_alert(summary: dict) -> None:
    """Post a DC alert if APE regression detected. Non-fatal on failure."""
    try:
        import requests
        tools_url = os.environ.get("MAGI_TOOLS_API", "http://127.0.0.1:5003")
        ape_hit = summary["avg_term_hit_rate"]["apple_ape"]
        base_hit = summary["avg_term_hit_rate"]["apple_baseline"]
        degraded = summary["ape_degraded_count"]
        msg = (
            f"⚠️ **翻譯 APE 回歸告警**\n"
            f"APE 術語命中率 {ape_hit:.1%} < baseline {base_hit:.1%}，"
            f"退化 {degraded}/{summary['cases']} 筆。請檢查 oMLX / Apple sidecar 狀態。"
        )
        requests.post(
            f"{tools_url}/notify",
            json={"topic": "magi_health", "message": msg},
            timeout=10,
        )
    except Exception:
        pass  # non-fatal


def main() -> int:
    apple_ok, reason = _apple_avail()
    if not apple_ok:
        result = {"success": False, "error": f"apple_unavailable: {reason}"}
        print(json.dumps(result, ensure_ascii=False))
        _write_static_result(result)
        return 2

    # Pre-warm oMLX primary server to avoid cold-start timeout silently failing all calls.
    # GTX path uses melchior → oMLX; APE path uses grounded_ai → oMLX.
    _warmup_omlx(timeout_sec=60)

    os.environ.setdefault("MAGI_TRANSLATOR_APE", "1")
    rows = []
    for item in SUITE:
        rows.append(_bench_gtx(item))
        rows.append(_bench_apple_baseline(item))
        rows.append(_bench_ape(item))

    gtx_hit = sum(r["term_hit_rate"] for r in rows if r["stage"] == "gtx_primary") / len(SUITE)
    base_hit = sum(r["term_hit_rate"] for r in rows if r["stage"] == "apple_baseline") / len(SUITE)
    ape_hit = sum(r["term_hit_rate"] for r in rows if r["stage"] == "apple_ape") / len(SUITE)
    ape_degraded = sum(1 for r in rows if r["stage"] == "apple_ape" and r.get("degraded"))

    summary = {
        "success": True,
        "cases": len(SUITE),
        "avg_term_hit_rate": {
            "gtx_primary": round(gtx_hit, 3),
            "apple_baseline": round(base_hit, 3),
            "apple_ape": round(ape_hit, 3),
        },
        "ape_degraded_count": ape_degraded,
        "ape_beats_baseline": ape_hit >= base_hit,
        "rows": rows,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))

    # Write to static/ for dashboard consumption.
    _write_static_result(summary)

    # Fail cron if APE regressed vs baseline or degraded >50% of the suite.
    regressed = not summary["ape_beats_baseline"] or ape_degraded > len(SUITE) // 2
    if regressed:
        _send_dc_alert(summary)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
