"""
[2026-04-25 已停用] OpenClaw Codex bridge — 全面改走 NVIDIA NIM。
此 module 保留純粹是為了避免下游 import 爆炸。所有對外函式皆 stub 化。

原功能：OpenClaw Codex OAuth 介面（session lock、feature_enabled、apply_manual_command、public_status_report）
現狀：全面停用，推理改走 NVIDIA NIM 405B / oMLX（InferenceGateway）。
"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("OpenClawCodexBridge")

MAGI_ROOT = Path(__file__).resolve().parents[2]

# 保留常數供下游 import 相容（不刪除）
DEFAULT_AGENT_ID = "codex-distributed"
DEFAULT_FEATURES = {
    "summary": False,
    "translate": False,
    "vision": False,
    "intent": False,
    "transcript": False,
}
FEATURE_ALIASES = {
    "summarize": "summary", "summary": "summary",
    "translate": "translate", "translation": "translate",
    "vision": "vision", "ocr": "vision", "captcha": "vision", "image": "vision",
    "intent": "intent", "router": "intent", "routing": "intent",
    "transcript": "transcript", "transcribe": "transcript", "stt": "transcript", "audio": "transcript",
}

_STUB_MSG = "Codex 已停用，改用 NVIDIA NIM"
_STUB_STATUS_MSG = "Codex 已停用，目前推理走 NVIDIA NIM 405B / oMLX。"


# ── Stub 函式（保留簽名，呼叫即返回 disabled）────────────────────────────────

def feature_enabled(feature: str) -> bool:
    """[stub] Codex 已停用，永遠回傳 False。"""
    return False


def apply_manual_command(command: str, *, features: Any = None) -> dict:
    """[stub] Codex 已停用，所有指令忽略。"""
    return {"success": False, "message": _STUB_MSG}


def public_status_report(*, can_toggle: Optional[bool] = None) -> Any:
    """[stub] Codex 已停用，回傳停用訊息字串。"""
    return _STUB_STATUS_MSG


def clear_failure_cooldown() -> None:
    """[stub] Codex 已停用，no-op。"""
    return None


def is_session_locked() -> bool:
    """[stub] Codex 已停用，永遠回傳 False。"""
    return False


def normalize_feature_name(feature: str) -> str:
    """保留工具函式供下游相容。"""
    raw = str(feature or "").strip().lower()
    return FEATURE_ALIASES.get(raw, raw)


def _normalize_feature_name(feature: str) -> str:
    return normalize_feature_name(feature)


def load_policy() -> dict:
    """[stub] 回傳停用 policy。"""
    return {"enabled": False, "features": dict(DEFAULT_FEATURES)}


def load_runtime_state() -> dict:
    """[stub] 回傳空 runtime state，llm_direct 等下游有讀此函式。"""
    return {
        "consecutive_failures": 0,
        "cooldown_until_ts": 0,
        "cooldown_reason": "",
        "last_feature": "",
        "last_error": "",
        "last_failure_at": "",
        "last_success_at": "",
        "last_duration_ms": 0,
        "last_provider": "",
        "last_model": "",
        "last_usage_total": 0,
        "last_system_prompt_chars": 0,
        "updated_at": "",
    }


def save_runtime_state(state: dict) -> dict:
    """[stub] Codex 已停用，no-op，回傳傳入的 state。"""
    return state if isinstance(state, dict) else {}


def save_policy(policy: dict) -> dict:
    """[stub] Codex 已停用，no-op，回傳傳入的 policy。"""
    return policy if isinstance(policy, dict) else {}


def status_report() -> dict:
    """[stub] 回傳停用狀態報告。"""
    return {
        "enabled": False,
        "mode_code": "CODEX_DISABLED",
        "mode_label": "Codex disabled (NIM 取代)",
        "message": _STUB_STATUS_MSG,
    }


def run_prompt(*, feature: str, prompt: str, timeout_sec: Optional[int] = None,
               thinking: Optional[str] = None, session_id: Optional[str] = None) -> dict:
    """[stub] Codex 已停用。"""
    feature_name = normalize_feature_name(feature)
    return {
        "success": False,
        "error": "codex_disabled:改用 NVIDIA NIM",
        "feature": feature_name,
        "text": "",
    }


def translate_with_codex(text: str, *, source_lang: str = "auto", target_lang: str = "繁體中文",
                          timeout_sec: Optional[int] = None) -> dict:
    """[stub] Codex 已停用。"""
    return run_prompt(feature="translate", prompt=text, timeout_sec=timeout_sec)


def summarize_with_codex(text: str, *, summary_length: str = "medium",
                          timeout_sec: Optional[int] = None) -> dict:
    """[stub] Codex 已停用。"""
    return run_prompt(feature="summary", prompt=text, timeout_sec=timeout_sec)


def classify_intent_with_codex(text: str, *, timeout_sec: Optional[int] = None) -> dict:
    """[stub] Codex 已停用。"""
    return run_prompt(feature="intent", prompt=text, timeout_sec=timeout_sec)


def analyze_image_with_codex(image_path: str, *, user_prompt: str, task_type: str = "vision",
                               timeout_sec: Optional[int] = None) -> dict:
    """[stub] Codex 已停用。"""
    return run_prompt(feature="vision", prompt=user_prompt, timeout_sec=timeout_sec)


def refine_ocr_with_codex(ocr_text: str, *, user_prompt: str,
                            timeout_sec: Optional[int] = None) -> dict:
    """[stub] Codex 已停用。"""
    return run_prompt(feature="vision", prompt=ocr_text, timeout_sec=timeout_sec)


def polish_transcript_with_codex(text: str, *, timeout_sec: Optional[int] = None) -> dict:
    """[stub] Codex 已停用。"""
    return run_prompt(feature="transcript", prompt=text, timeout_sec=timeout_sec)


def update_policy(*, enabled: Optional[bool] = None, features: Optional[dict] = None) -> dict:
    """[stub] Codex 已停用，no-op。"""
    return load_policy()
