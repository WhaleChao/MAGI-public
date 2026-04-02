#!/usr/bin/env python3
import argparse
import importlib.util
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlparse


LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "0.0.0.0"}
LOCAL_PRIMARY_PROVIDERS = {"omlx", "ollama"}

MAGI_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OPENCLAW_DIR = Path.home() / ".openclaw"
DEFAULT_CONFIG_PATH = DEFAULT_OPENCLAW_DIR / "openclaw.json"
DEFAULT_AUTH_PATH = DEFAULT_OPENCLAW_DIR / "agents" / "main" / "agent" / "auth-profiles.json"
DEFAULT_QUOTA_STATE_PATH = MAGI_ROOT / ".agent" / "openclaw_codex_quota_state.json"
AUTH_MODE_SCRIPT_PATH = Path(__file__).with_name("check_openclaw_auth_mode.py")


def env_path(name: str, default: Path) -> Path:
    return Path(os.environ.get(name, str(default)))


def load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return int(default)


def ms_to_iso(ts_ms: int) -> str:
    if not ts_ms:
        return ""
    try:
        return datetime.fromtimestamp(ts_ms / 1000.0).isoformat()
    except Exception:
        return ""


def is_local_base_url(url: str) -> bool:
    if not url:
        return False
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    return (parsed.hostname or "").lower() in LOCAL_HOSTS


def model_chain_from_node(model_node: Any) -> tuple[str, list[str]]:
    if isinstance(model_node, str):
        primary = str(model_node or "").strip()
        if primary and "/" not in primary:
            primary = f"ollama/{primary}"
        return primary, []
    if isinstance(model_node, dict):
        primary = str(model_node.get("primary") or "").strip()
        if primary and "/" not in primary:
            primary = f"ollama/{primary}"
        fallbacks = []
        for raw in (model_node.get("fallbacks") or []):
            item = str(raw or "").strip()
            if not item:
                continue
            fallbacks.append(item if "/" in item else f"ollama/{item}")
        return primary, fallbacks
    return "", []


def load_auth_mode_report() -> dict:
    if not AUTH_MODE_SCRIPT_PATH.exists():
        return {}
    try:
        spec = importlib.util.spec_from_file_location("check_openclaw_auth_mode_module", str(AUTH_MODE_SCRIPT_PATH))
        if spec is None or spec.loader is None:
            return {}
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        build_report = getattr(module, "build_report", None)
        if callable(build_report):
            result = build_report()
            return result if isinstance(result, dict) else {}
    except Exception:
        return {}
    return {}


def pick_profile_failure_reason(stats: dict) -> str:
    reason = str((stats or {}).get("disabledReason") or "").strip()
    if reason in {"billing", "rate_limit"}:
        return reason
    failure_counts = (stats or {}).get("failureCounts") or {}
    if not isinstance(failure_counts, dict):
        return ""
    if safe_int(failure_counts.get("billing"), 0) > 0:
        return "billing"
    if safe_int(failure_counts.get("rate_limit"), 0) > 0:
        return "rate_limit"
    return ""


def load_codex_usage_snapshot(auth_path: Path, now_ms: int) -> dict:
    data = load_json(auth_path)
    profiles = data.get("profiles") if isinstance(data.get("profiles"), dict) else {}
    usage_stats = data.get("usageStats") if isinstance(data.get("usageStats"), dict) else {}

    entries = []
    for profile_id, profile in profiles.items():
        if not isinstance(profile, dict):
            continue
        if str(profile.get("provider") or "").strip() != "openai-codex":
            continue
        stats = usage_stats.get(profile_id) if isinstance(usage_stats.get(profile_id), dict) else {}
        cooldown_until = safe_int(stats.get("cooldownUntil"), 0)
        disabled_until = safe_int(stats.get("disabledUntil"), 0)
        unusable_until = max(cooldown_until, disabled_until)
        reason = pick_profile_failure_reason(stats)
        entries.append(
            {
                "profile_id": profile_id,
                "reason": reason,
                "active": bool(unusable_until > now_ms and reason in {"billing", "rate_limit"}),
                "cooldown_until_ms": cooldown_until,
                "disabled_until_ms": disabled_until,
                "unusable_until_ms": unusable_until,
                "unusable_until_iso": ms_to_iso(unusable_until),
                "last_failure_at_ms": safe_int(stats.get("lastFailureAt"), 0),
                "last_failure_at_iso": ms_to_iso(safe_int(stats.get("lastFailureAt"), 0)),
                "last_used_ms": safe_int(stats.get("lastUsed"), 0),
                "last_used_iso": ms_to_iso(safe_int(stats.get("lastUsed"), 0)),
                "failure_counts": stats.get("failureCounts") if isinstance(stats.get("failureCounts"), dict) else {},
                "disabled_reason": str(stats.get("disabledReason") or "").strip(),
            }
        )

    def reason_rank(item: dict) -> tuple[int, int]:
        reason = str(item.get("reason") or "")
        severity = 2 if reason == "billing" else 1 if reason == "rate_limit" else 0
        return severity, safe_int(item.get("unusable_until_ms"), 0)

    active_entries = [item for item in entries if item.get("active")]
    selected = max(active_entries, key=reason_rank) if active_entries else None
    return {
        "profiles": entries,
        "active": bool(selected),
        "active_reason": str((selected or {}).get("reason") or "").strip(),
        "active_until_ms": safe_int((selected or {}).get("unusable_until_ms"), 0),
        "active_until_iso": str((selected or {}).get("unusable_until_iso") or ""),
    }


def classify_mode(
    *,
    primary_model: str,
    config: dict,
    quota_state: dict,
    usage: dict,
    now_ms: int,
) -> tuple[str, str, str, str]:
    provider_name = primary_model.split("/", 1)[0] if "/" in primary_model else primary_model
    provider_cfg = ((config.get("models") or {}).get("providers") or {}).get(provider_name) or {}
    primary_is_codex = primary_model.startswith("openai-codex/")
    primary_is_local = provider_name in LOCAL_PRIMARY_PROVIDERS and is_local_base_url(str(provider_cfg.get("baseUrl") or "").strip())

    auto_switched = bool(quota_state.get("auto_switched")) and str(quota_state.get("mode") or "") == "local_fallback"
    hold_until_ms = safe_int(quota_state.get("hold_until_ms"), 0)
    recovery_ready = bool(auto_switched and hold_until_ms > 0 and hold_until_ms <= now_ms)

    if primary_is_local and auto_switched and hold_until_ms > now_ms:
        return (
            "LOCAL_FALLBACK_ACTIVE",
            "本地降級",
            "Codex 因 quota/rate-limit 暫時不可用，已由本地主鏈接手。",
            f"等待 hold 視窗到 {ms_to_iso(hold_until_ms) or '未知時間'} 後再自動檢查是否回切。",
        )
    if primary_is_local and recovery_ready:
        return (
            "WAITING_RECOVERY",
            "等待恢復",
            "本地降級視窗已到期，正在等待下一次 preflight 自動回切 Codex。",
            "可直接跑一次 autopilot tick/self_test 提前觸發恢復檢查。",
        )
    if primary_is_codex and usage.get("active"):
        return (
            "CODEX_PENDING_FALLBACK",
            "Codex",
            "目前仍是 Codex 主路徑，但已偵測到 quota/rate-limit 不可用訊號。",
            "下一次 autopilot preflight 會自動切到本地主鏈。",
        )
    if primary_is_codex:
        return (
            "CODEX_ACTIVE",
            "Codex",
            "目前走 Codex 主路徑。",
            "不需要額外動作。",
        )
    if primary_is_local:
        return (
            "LOCAL_MANUAL",
            "本地降級",
            "目前是本地模型主路徑，但不是由 Codex quota guard 自動切換。",
            "若這是手動回切，保持現狀即可；若不是，建議檢查 openclaw.json。",
        )
    return (
        "UNKNOWN",
        "未知",
        "目前無法準確判定是 Codex 還是本地主鏈。",
        "請檢查 openclaw.json 與 quota state。",
    )


def build_report() -> dict:
    now_ms = int(time.time() * 1000)
    config_path = env_path("MAGI_OPENCLAW_CONFIG_PATH", DEFAULT_CONFIG_PATH)
    auth_path = env_path("MAGI_OPENCLAW_AUTH_PROFILES_PATH", DEFAULT_AUTH_PATH)
    quota_state_path = env_path("MAGI_OPENCLAW_CODEX_QUOTA_STATE_PATH", DEFAULT_QUOTA_STATE_PATH)

    config = load_json(config_path)
    primary_model, fallback_models = model_chain_from_node(
        (((config.get("agents") or {}).get("defaults") or {}).get("model") or {})
    )
    quota_state = load_json(quota_state_path)
    usage = load_codex_usage_snapshot(auth_path, now_ms=now_ms)
    auth_mode_report = load_auth_mode_report()

    status_code, mode_label, summary, next_action = classify_mode(
        primary_model=primary_model,
        config=config,
        quota_state=quota_state,
        usage=usage,
        now_ms=now_ms,
    )

    hold_until_ms = safe_int(quota_state.get("hold_until_ms"), 0)
    report = {
        "status_code": status_code,
        "mode": mode_label,
        "summary": summary,
        "next_action": next_action,
        "current_primary_model": primary_model,
        "current_fallback_models": fallback_models,
        "auth_mode_status": str((auth_mode_report or {}).get("status") or ""),
        "quota_reason": str(quota_state.get("reason") or usage.get("active_reason") or ""),
        "quota_state_present": quota_state_path.exists(),
        "quota_state_path": str(quota_state_path),
        "quota_state": quota_state,
        "hold_until_ms": hold_until_ms,
        "hold_until_iso": ms_to_iso(hold_until_ms),
        "recovery_ready": bool(hold_until_ms > 0 and hold_until_ms <= now_ms),
        "usage_active": bool(usage.get("active")),
        "usage_active_reason": str(usage.get("active_reason") or ""),
        "usage_active_until_iso": str(usage.get("active_until_iso") or ""),
        "config_path": str(config_path),
        "auth_profiles_path": str(auth_path),
        "auth_mode_report": auth_mode_report,
        "generated_at": datetime.now().isoformat(),
    }
    return report


def print_human(report: dict) -> None:
    print("OpenClaw Runtime Mode")
    print(f"mode: {report['mode']}")
    print(f"status_code: {report['status_code']}")
    print(f"primary model: {report['current_primary_model'] or '(missing)'}")
    if report.get("current_fallback_models"):
        print("fallback models:")
        for item in report["current_fallback_models"]:
            print(f"  - {item}")
    else:
        print("fallback models: none")
    print(f"auth mode: {report.get('auth_mode_status') or '(unknown)'}")
    print(f"summary: {report.get('summary') or ''}")
    if report.get("quota_reason"):
        print(f"quota reason: {report['quota_reason']}")
    if report.get("hold_until_iso"):
        print(f"hold until: {report['hold_until_iso']}")
    if report.get("usage_active"):
        print(f"provider unusable until: {report.get('usage_active_until_iso') or '(unknown)'}")
    print(f"next action: {report.get('next_action') or ''}")
    print(f"quota state file: {report['quota_state_path']}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Show whether OpenClaw is currently on Codex, local fallback, or waiting recovery."
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON report")
    args = parser.parse_args()

    report = build_report()
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print_human(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
