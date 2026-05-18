#!/usr/bin/env python3
"""Live-safe @heavy and fallback checks for MAGI inference.

This check intentionally uses only synthetic prompts. It verifies:
- @heavy / @重型 reaches the heavy path and produces a usable answer.
- If NVIDIA NIM fails, MAGI falls back to an available local route without
  returning the synthetic "busy" placeholder as a successful legal answer.
- In strict heavy mode, MAGI can fail closed instead of silently downgrading.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


BAD_FALLBACK_MARKERS = (
    "系統降級回覆",
    "本機模型逾時",
    "請稍後重試",
    "目前模型忙碌中",
)


def _load_env() -> None:
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


@contextmanager
def _temporary_env(values: dict[str, str]):
    old: dict[str, str | None] = {key: os.environ.get(key) for key in values}
    try:
        for key, value in values.items():
            os.environ[key] = value
        yield
    finally:
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _text_of(result: dict) -> str:
    return str(
        result.get("response")
        or result.get("analysis")
        or result.get("summary")
        or result.get("text")
        or ""
    ).strip()


def _compact_result(result: dict) -> dict:
    text = _text_of(result)
    return {
        "success": bool(result.get("success")),
        "route": str(result.get("route") or ""),
        "model": str(result.get("model") or ""),
        "provider": str(result.get("provider") or ""),
        "degraded": bool(result.get("degraded")),
        "heavy_fast_path": bool(result.get("heavy_fast_path")),
        "synthetic_fallback": bool(result.get("synthetic_fallback")),
        "duration_ms": int(result.get("duration_ms") or 0),
        "text_len": len(text),
        "text_preview": text[:120],
        "error": str(result.get("error") or "")[:240],
    }


def _assert_usable_success(name: str, result: dict) -> tuple[bool, str]:
    text = _text_of(result)
    if not result.get("success"):
        return False, f"{name}: gateway returned failure route={result.get('route')} error={result.get('error')}"
    if not text:
        return False, f"{name}: empty response"
    if any(marker in text for marker in BAD_FALLBACK_MARKERS):
        return False, f"{name}: returned degraded placeholder instead of usable output"
    if "@heavy" in text.lower() or "@重型" in text:
        return False, f"{name}: command prefix leaked into output"
    return True, "ok"


def run_checks(*, run_live_nim: bool) -> dict:
    from skills.bridge.inference_gateway import InferenceGateway

    checks: list[dict] = []
    gateway = InferenceGateway()

    if run_live_nim:
        started = time.monotonic()
        result = gateway.chat(
            "@heavy 請用繁體中文列出民法第184條侵權行為的三個構成要件，每點不超過20字。",
            task_type="legal_analysis",
            timeout=90,
            allow_synthetic_fallback=False,
        )
        ok, detail = _assert_usable_success("heavy_prefix_live", result)
        checks.append({
            "name": "heavy_prefix_live",
            "ok": ok,
            "detail": detail,
            "elapsed_sec": round(time.monotonic() - started, 2),
            "result": _compact_result(result),
        })

    forced_fail = {"success": False, "error": "forced_nim_failure_for_fallback_check", "response": ""}
    with _temporary_env({
        "NVIDIA_NIM_ENABLE": "1",
        "MAGI_HEAVY_STRICT_NIM": "0",
        "INFERENCE_ALLOW_TEXT_FALLBACK": "0",
    }):
        with patch("skills.bridge.nim_heavy.run_nim_chat", return_value=forced_fail):
            started = time.monotonic()
            result = gateway.chat(
                "@heavy 請用繁體中文說明民法第184條侵權行為，限三點。",
                task_type="legal_analysis",
                timeout=60,
                allow_synthetic_fallback=False,
            )
    ok, detail = _assert_usable_success("nim_failure_local_fallback", result)
    if ok and result.get("route") == "nvidia_nim":
        ok = False
        detail = "fallback did not leave nvidia_nim route after forced NIM failure"
    checks.append({
        "name": "nim_failure_local_fallback",
        "ok": ok,
        "detail": detail,
        "elapsed_sec": round(time.monotonic() - started, 2),
        "result": _compact_result(result),
    })

    with _temporary_env({
        "NVIDIA_NIM_ENABLE": "1",
        "MAGI_HEAVY_STRICT_NIM": "1",
        "MAGI_HEAVY_STRICT_NIM_RETRIES": "0",
        "MAGI_HEAVY_STRICT_NIM_ALLOW_FALLBACK": "0",
        "INFERENCE_ALLOW_TEXT_FALLBACK": "0",
    }):
        with patch("skills.bridge.nim_heavy.run_nim_chat", return_value=forced_fail):
            started = time.monotonic()
            result = gateway.chat(
                "@heavy 請用繁體中文說明民法第184條侵權行為，限三點。",
                task_type="legal_analysis",
                timeout=30,
                allow_synthetic_fallback=False,
            )
    ok = (
        result.get("success") is False
        and result.get("route") == "nvidia_nim_strict_failed"
        and not result.get("synthetic_fallback")
    )
    detail = "ok" if ok else "strict mode should fail closed without synthetic fallback"
    checks.append({
        "name": "strict_heavy_fail_closed",
        "ok": bool(ok),
        "detail": detail,
        "elapsed_sec": round(time.monotonic() - started, 2),
        "result": _compact_result(result),
    })

    return {
        "success": all(item["ok"] for item in checks),
        "run_live_nim": bool(run_live_nim),
        "checks": checks,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-live-nim", action="store_true", help="Skip the real @heavy NIM/live call.")
    parser.add_argument(
        "--json-out",
        default=str(ROOT / ".runtime" / "heavy_fallback_live_latest.json"),
        help="Where to write the JSON result.",
    )
    args = parser.parse_args()

    _load_env()
    result = run_checks(run_live_nim=not args.skip_live_nim)

    out_path = Path(args.json_out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    for item in result["checks"]:
        status = "✅" if item["ok"] else "❌"
        route = item["result"].get("route", "")
        print(f"{status} {item['name']} route={route} {item['detail']}")
    print(f"JSON: {out_path}")
    return 0 if result["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
