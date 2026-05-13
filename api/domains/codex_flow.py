"""
[2026-04-25 已停用] Codex distributed / sidecar operations.

所有函式已 stub 化 — Codex 全面停用，推理改走 NVIDIA NIM 405B / oMLX。
保留 module 與函式簽名避免下游 import 爆炸。
"""
from __future__ import annotations

import logging

logger = logging.getLogger("Orchestrator")

_STUB_STATUS_MSG = "Codex 已停用，目前推理走 NVIDIA NIM 405B / oMLX。"


def parse_codex_distributed_features(message: str) -> dict:
    """[stub] Codex 已停用，永遠回傳空 dict。"""
    return {}


def format_codex_distributed_status(report: dict = None) -> str:
    """[stub] Codex 已停用，回傳停用訊息。"""
    return _STUB_STATUS_MSG


def handle_codex_distributed_command(orch, message: str, role: str):
    """[stub] Codex 已停用，所有指令忽略並回傳停用訊息。"""
    msg = str(message or "").strip().lower()
    if "codex" not in msg and "sidecar" not in msg and "分散式" not in str(message or ""):
        return False, None
    return True, {"success": False, "message": "Codex 已停用，改用 NVIDIA NIM。指令已忽略。"}
