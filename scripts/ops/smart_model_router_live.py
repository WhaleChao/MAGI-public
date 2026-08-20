#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Live gate for MAGI smart model routing.

This gate does not force a 26B profile switch. It proves the important safety
property instead: MAGI will choose a better local model only when it is live and
resource gates are safe, otherwise it falls back to the stable E4B path.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

MAGI_ROOT = Path(__file__).resolve().parents[2]
_RUNTIME_OVERRIDE = os.environ.get("MAGI_RUNTIME_DIR", "").strip()
RUNTIME_DIR = Path(_RUNTIME_OVERRIDE or MAGI_ROOT / ".runtime").expanduser()
if str(MAGI_ROOT) not in sys.path:
    sys.path.insert(0, str(MAGI_ROOT))

from api.model_config import is_disallowed_model  # noqa: E402
from api.model_router import choose_model_for_request, get_runtime_state  # noqa: E402


def _case(name: str, **kwargs: Any) -> dict[str, Any]:
    decision = choose_model_for_request(**kwargs)
    payload = {
        "name": name,
        "ok": bool(decision.selected_model) and not is_disallowed_model(decision.selected_model),
        "decision": decision.to_dict(),
    }
    if is_disallowed_model(decision.selected_model):
        payload["ok"] = False
        payload["error"] = "disallowed_model_selected"
    return payload


def _chat_probe(timeout: int) -> dict[str, Any]:
    from skills.bridge.inference_gateway import InferenceGateway

    prompt = "請只用一句台灣繁體中文回答：MAGI 模型路由健康檢查通過。"
    try:
        result = InferenceGateway().chat(
            prompt,
            task_type="general",
            timeout=timeout,
            allow_synthetic_fallback=False,
        )
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    text = str(result.get("response") or result.get("text") or "")
    return {
        "ok": bool(result.get("success")) and bool(text.strip()) and not result.get("synthetic_fallback"),
        "route": result.get("route", ""),
        "model": result.get("model", ""),
        "duration_ms": result.get("duration_ms"),
        "error": result.get("error", ""),
        "decision": result.get("model_route_decision") or {},
        "response_head": text[:120],
    }


def build_report(*, chat_probe: bool, chat_timeout: int) -> dict[str, Any]:
    active_models, resource = get_runtime_state()
    cases = [
        _case(
            "routine_general_uses_stable",
            task_type="general",
            prompt="你好，請回覆今天 MAGI 的狀態。",
            active_models=active_models,
            resource=resource,
        ),
        _case(
            "quality_legal_prefers_26b_only_when_safe",
            task_type="legal_analysis",
            prompt=("請分析最高法院判決中的通譯品質爭點。" * 400),
            force_quality=True,
            active_models=active_models,
            resource=resource,
        ),
        _case(
            "translation_quality_route",
            task_type="translate",
            prompt=("請翻譯這份法律文獻並保留專有名詞原文。" * 300),
            force_quality=True,
            active_models=active_models,
            resource=resource,
        ),
        _case(
            "embedding_model_is_preserved",
            task_type="embedding",
            prompt="向量檢索",
            active_models=active_models,
            resource=resource,
        ),
    ]
    chat = _chat_probe(chat_timeout) if chat_probe else {"ok": True, "skipped": True}
    blocked_26b = []
    for case in cases:
        decision = case.get("decision") or {}
        if decision.get("preferred_model") and "26b" in str(decision.get("preferred_model")).lower():
            blocked_26b.extend(decision.get("blocked_reasons") or [])
    report = {
        "ok": all(bool(case.get("ok")) for case in cases) and bool(chat.get("ok")),
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "active_models": list(active_models),
        "resource": {
            "ok": resource.ok,
            "level": resource.level,
            "disk_free_gb": resource.disk_free_gb,
            "swap_used_gb": resource.swap_used_gb,
            "free_plus_inactive_gb": resource.free_plus_inactive_gb,
            "memory_free_percent": resource.memory_free_percent,
            "reasons": list(resource.reasons),
        },
        "cases": cases,
        "chat_probe": chat,
        "26b_blocked_reasons": sorted(set(str(x) for x in blocked_26b)),
        "interpretation": "",
    }
    if any("26b" in str(model).lower() for model in active_models) and not blocked_26b:
        report["interpretation"] = "26B-A4B is live and router can use it for quality tasks."
    elif blocked_26b:
        report["interpretation"] = "26B-A4B is protected by safety gates; stable E4B remains active to avoid OOM."
    else:
        report["interpretation"] = "Stable local model is active; 26B-A4B is not live for this profile."
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check MAGI smart model router and no-OOM gates.")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--json-out", default=str(RUNTIME_DIR / "smart_model_router_live_latest.json"))
    parser.add_argument("--chat-probe", action="store_true", help="send one live local chat request")
    parser.add_argument("--chat-timeout", type=int, default=30)
    args = parser.parse_args(argv)

    report = build_report(chat_probe=bool(args.chat_probe), chat_timeout=int(args.chat_timeout))
    out = Path(args.json_out)
    if _RUNTIME_OVERRIDE and not out.is_absolute():
        relative = Path(*out.parts[1:]) if out.parts and out.parts[0] == ".runtime" else out
        out = RUNTIME_DIR / relative
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2) if args.json else f"smart_model_router_live ok={report['ok']} interpretation={report['interpretation']}")
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
