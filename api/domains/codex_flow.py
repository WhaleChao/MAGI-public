"""
Codex distributed / sidecar operations extracted from Orchestrator.

All functions accept an ``orch`` parameter (the Orchestrator instance)
instead of ``self``, keeping the same logic but as standalone functions.
"""
from __future__ import annotations

import logging
import re

logger = logging.getLogger("Orchestrator")


def parse_codex_distributed_features(message: str) -> dict:
    aliases = {
        "summary": "summary", "summarize": "summary", "摘要": "summary", "總結": "summary",
        "translate": "translate", "translation": "translate", "翻譯": "translate",
        "vision": "vision", "ocr": "vision", "image": "vision",
        "圖像": "vision", "影像": "vision", "視覺": "vision", "辨識": "vision",
        "intent": "intent", "route": "intent", "router": "intent",
        "routing": "intent", "意圖": "intent", "路由": "intent",
        "transcript": "transcript", "transcribe": "transcript", "stt": "transcript",
        "audio": "transcript", "逐字稿": "transcript", "逐字": "transcript",
        "聽打": "transcript", "轉錄": "transcript",
    }
    found = {}
    for token in re.findall(r"[\w\u4e00-\u9fff-]+", str(message or "").lower()):
        name = aliases.get(token)
        if name:
            found[name] = True
    return found


def format_codex_distributed_status(report: dict) -> str:
    labels = {
        "summary": "摘要", "translate": "翻譯", "vision": "視覺/OCR",
        "intent": "意圖路由", "transcript": "逐字稿",
    }
    features = report.get("features") if isinstance(report.get("features"), dict) else {}
    enabled_list = [labels.get(name, name) for name, on in features.items() if on]
    disabled_list = [labels.get(name, name) for name, on in features.items() if not on]
    runtime_line = "ready"
    if not report.get("runtime_ready"):
        runtime_line = f"cooldown {int(report.get('runtime_cooldown_remaining_sec') or 0)}s"
    lines = [
        "🧠 Codex Sidecar 狀態",
        f"- 模式：{report.get('mode_label') or '-'} ({report.get('mode_code') or '-'})",
        f"- 全域開關：{'開啟' if report.get('enabled') else '關閉'}",
        f"- 功能：{', '.join(enabled_list) if enabled_list else '無'}",
    ]
    if disabled_list:
        lines.append(f"- 已停用：{', '.join(disabled_list)}")
    lines.append(f"- Runtime：{runtime_line}")
    lines.append(f"- OAuth：{'可用' if report.get('oauth_ready') else '不可用'}")
    if report.get("last_success_at"):
        lines.append(f"- 最近成功：{report.get('last_success_at')}")
    if report.get("last_feature"):
        lines.append(f"- 最近功能：{labels.get(str(report.get('last_feature')), str(report.get('last_feature')))}")
    if report.get("last_error"):
        lines.append(f"- 最近錯誤：{str(report.get('last_error'))[:180]}")
    if report.get("cooldown_reason"):
        lines.append(f"- 冷卻原因：{str(report.get('cooldown_reason'))[:180]}")
    lines.append("")
    lines.append("可用指令：codex 狀態 / codex 開啟 / codex 關閉 / codex 開啟 摘要 翻譯")
    return "\n".join(lines)


def handle_codex_distributed_command(orch, message: str, role: str):
    from skills.bridge.openclaw_codex_bridge import apply_manual_command, public_status_report

    msg = str(message or "").strip()
    msg_lower = msg.lower()
    if "codex" not in msg_lower and "sidecar" not in msg_lower and "分散式" not in msg:
        return False, None

    command = None
    if any(kw in msg_lower for kw in [" status", "codex status", "狀態", "mode", "模式", "health", "查看"]):
        command = "status"
    if any(kw in msg_lower for kw in ["開啟", "啟用", "打開", "全開", "on", "enable", "啟動"]):
        command = "on"
    if any(kw in msg_lower for kw in ["關閉", "停用", "關掉", "off", "disable", "本地", "切回本地", "退回本地"]):
        command = "off"
    if any(kw in msg_lower for kw in ["help", "幫助", "怎麼用", "指令"]) and "codex" in msg_lower:
        command = "help"

    if not command:
        return False, None
    if role != "admin":
        return True, "⛔ 抱歉，只有管理員可以切換 Codex sidecar。"
    if command == "help":
        report = public_status_report(can_toggle=True)
        return True, format_codex_distributed_status(report)

    features = parse_codex_distributed_features(msg)
    try:
        apply_manual_command(command, features=features or None)
        report = public_status_report(can_toggle=True)
        return True, format_codex_distributed_status(report)
    except Exception as e:
        logger.warning(f"Codex sidecar command failed: {e}")
        return True, f"❌ Codex sidecar 切換失敗：{e}"
