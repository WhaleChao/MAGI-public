"""Tool-first factual pipeline policies.

Determines when a query MUST go through a tool before the model can answer,
when a tool is optional, and when no tool is needed.

Usage::

    from api.tools.policies import classify_tool_requirement, ToolRequirement

    req = classify_tool_requirement(message, intent="QUERY")
    if req.level == "required":
        # Must call tool first; if tool fails, say so honestly
        ...
    elif req.level == "optional":
        # Try tool, fall back to LLM if tool fails
        ...
    else:
        # No tool needed, go straight to LLM
        ...
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


@dataclass(slots=True, frozen=True)
class ToolRequirement:
    level: str         # "required" | "optional" | "none"
    tool_hint: str     # suggested tool name or empty
    reason: str        # why this level was chosen


# ---------------------------------------------------------------------------
# Factual intent patterns → tool requirement
# ---------------------------------------------------------------------------

_TOOL_REQUIRED_PATTERNS: list[tuple[re.Pattern, str]] = [
    # Case info
    (re.compile(r"(案件|案號|案件狀態|案件進度|卷證).*?(查|找|目前|現在|進度|狀態)", re.I), "case_query"),
    (re.compile(r"(查|找).*?(案件|案號)", re.I), "case_query"),
    # Schedule / calendar
    (re.compile(r"(行程|日程|開庭|會議|排程|calendar|schedule).*?(查|找|目前|今天|明天|下週|本週)", re.I), "calendar_query"),
    (re.compile(r"(今天|明天|下週|本週).*?(行程|開庭|會議)", re.I), "calendar_query"),
    # Report content
    (re.compile(r"(報告|晨報|日報|週報).*?(內容|查|看|顯示)", re.I), "report_query"),
    # LAF / legal aid
    (re.compile(r"法扶.*(案件|進度|狀態|未開辦|開辦)", re.I), "laf_query"),
    # DB / statistics
    (re.compile(r"(資料庫|DB|統計|筆數|數量).*?(查|找|多少)", re.I), "db_query"),
]

_TOOL_OPTIONAL_PATTERNS: list[tuple[re.Pattern, str]] = [
    # Legal reference (may have from memory but tool is better)
    (re.compile(r"(法條|法律|條文|第\d+條)", re.I), "legal_reference"),
    # Judgment / case law
    (re.compile(r"(判決|裁判|判例|見解|實務)", re.I), "judgment_query"),
    # Memory recall (already handled but tool can supplement)
    (re.compile(r"(記得|記憶|之前說|你有沒有記|回顧)", re.I), "memory_recall"),
    # Web research
    (re.compile(r"(天氣|匯率|股價|最新|新聞|上網查)", re.I), "web_research"),
]


def classify_tool_requirement(
    message: str,
    *,
    intent: str = "",
    has_memory_context: bool = False,
) -> ToolRequirement:
    """Classify whether *message* requires a tool call before answering.

    Returns a ``ToolRequirement`` with level, tool hint, and reason.
    """
    text = (message or "").strip()
    if not text:
        return ToolRequirement(level="none", tool_hint="", reason="empty message")

    # Check required patterns first
    for pattern, tool_hint in _TOOL_REQUIRED_PATTERNS:
        if pattern.search(text):
            return ToolRequirement(
                level="required",
                tool_hint=tool_hint,
                reason=f"factual intent matched: {tool_hint}",
            )

    # Check optional patterns
    for pattern, tool_hint in _TOOL_OPTIONAL_PATTERNS:
        if pattern.search(text):
            # If we already have memory context, tool is optional
            level = "optional" if has_memory_context else "required"
            return ToolRequirement(
                level=level,
                tool_hint=tool_hint,
                reason=f"{'optional' if has_memory_context else 'required'}: {tool_hint}",
            )

    # QUERY intent without matching patterns → optional
    if intent == "QUERY":
        return ToolRequirement(
            level="optional",
            tool_hint="",
            reason="QUERY intent without specific tool match",
        )

    return ToolRequirement(level="none", tool_hint="", reason="no tool pattern matched")


def format_tool_failure_response(tool_hint: str, error: str = "") -> str:
    """Generate an honest failure response when a required tool fails."""
    tool_labels = {
        "case_query": "案件查詢",
        "calendar_query": "行程查詢",
        "report_query": "報告查詢",
        "laf_query": "法扶查詢",
        "db_query": "資料庫查詢",
        "legal_reference": "法條查詢",
        "judgment_query": "判決查詢",
        "memory_recall": "記憶檢索",
        "web_research": "網路搜尋",
    }
    label = tool_labels.get(tool_hint, "查詢工具")
    if error:
        return f"抱歉，{label}目前無法使用（{error}）。請稍後再試，或提供更多資訊讓我嘗試從其他來源回答。"
    return f"抱歉，{label}目前無法使用。我不想在沒有確認資料的情況下猜測答案。"
