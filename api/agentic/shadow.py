"""Public-safe shadow observations around the existing MAGI orchestrator."""

from __future__ import annotations

import re
from typing import Any

from api.agentic.telemetry import write_public_agent_status
from api.routing.intent_contract import classify_intent_contract


_CATEGORY_RULES = (
    ("calendar", r"行事曆|日曆|行程|開會|會議|排庭|庭期|提醒"),
    ("laf", r"法扶|法律扶助"),
    ("file_review", r"閱卷|卷證"),
    ("transcript", r"筆錄|逐字稿"),
    ("transcription", r"錄音轉文字|語音轉文字|轉錄|聽打"),
    ("translation", r"翻譯|譯成|中翻|英翻"),
    ("ocr", r"OCR|文字辨識|影像辨識"),
    ("cases", r"案件|案號|當事人|委任人"),
    ("legal", r"法律|法規|法條|判決|裁判|書狀|起訴|答辯"),
    ("files", r"檔案|文件|資料夾|NAS|Drive|雲端"),
    ("accounting", r"帳務|記帳|收入|支出|報價"),
    ("system", r"系統|模型|服務|健康|備份|狀態"),
    ("research", r"研究|查詢|搜尋|分析|比較|整理|摘要"),
    ("automation", r"自動|排程|監控|同步|通知"),
)
_WRITE_RE = re.compile(r"新增|建立|修改|變更|取消|刪除|移除|上傳|同步|送出|提交|回報|產生|製作")
_ERROR_RE = re.compile(r"(?:失敗|錯誤|暫時忙碌|無法|受阻|逾時)")
_WAIT_RE = re.compile(r"(?:等待確認|請回覆[「\"]?確認|確認是否)")


def public_category_for_message(message: str) -> str:
    text = str(message or "")
    for category, pattern in _CATEGORY_RULES:
        if re.search(pattern, text, re.IGNORECASE):
            return category
    return "general"


def public_tool_category(category: str) -> str:
    return {
        "calendar": "calendar",
        "laf": "laf",
        "file_review": "file_review",
        "transcript": "transcript",
        "transcription": "transcription",
        "translation": "translation",
        "ocr": "ocr",
        "cases": "database",
        "accounting": "database",
        "files": "drive",
        "legal": "search",
        "research": "search",
        "system": "system",
        "automation": "system",
    }.get(category, "none")


def observe_start(message: str) -> dict[str, Any]:
    decision = classify_intent_contract(str(message or ""))
    category = public_category_for_message(message)
    snapshot = {
        "status": "running",
        "intent_category": category,
        "confidence": decision.confidence,
        "plan_status": "running",
        "step_counts": {"total": 4, "running": 1, "pending": 3},
        "current_action": "classify",
        "tool_category": public_tool_category(category),
        "side_effect": "reversible_write" if _WRITE_RE.search(str(message or "")) else "read_only",
        "waiting_confirmation": False,
        "verification": "pending",
        "health": "healthy",
        "degraded": False,
        "error_category": "none",
    }
    return write_public_agent_status(snapshot)


def observe_finish(message: str, result: Any, *, failed: bool = False) -> dict[str, Any]:
    decision = classify_intent_contract(str(message or ""))
    category = public_category_for_message(message)
    result_text = _public_result_text(result)
    waiting = bool(_WAIT_RE.search(result_text))
    error = bool(failed or _ERROR_RE.search(result_text))
    snapshot = {
        "status": "blocked" if error else "ready" if waiting else "completed",
        "intent_category": category,
        "confidence": decision.confidence,
        "plan_status": "failed" if error else "awaiting_confirmation" if waiting else "succeeded",
        "step_counts": {
            "total": 4,
            "succeeded": 2 if waiting else 4 if not error else 1,
            "pending": 2 if waiting else 0,
            "failed": 1 if error else 0,
        },
        "current_action": "await_confirmation" if waiting else "verify" if error else "respond",
        "tool_category": public_tool_category(category),
        "side_effect": "reversible_write" if _WRITE_RE.search(str(message or "")) else "read_only",
        "waiting_confirmation": waiting,
        "verification": "failed" if error else "pending" if waiting else "passed",
        "health": "degraded" if error else "healthy",
        "degraded": error,
        "last_success": not error,
        "error_category": "unknown" if error else "none",
    }
    return write_public_agent_status(snapshot)


def _public_result_text(result: Any) -> str:
    if isinstance(result, str):
        return result[:500]
    if isinstance(result, dict):
        return str(result.get("text") or result.get("message") or result.get("error") or "")[:500]
    return ""
