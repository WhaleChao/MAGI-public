"""Filters for placeholder legal-insight rows that should not enter UI flows."""

from __future__ import annotations

import re


NON_EXTRACTABLE_INSIGHT_MARKERS = (
    "本件無可擷取之實務見解",
    "本判決無可擷取之實務見解",
    "本裁定無可擷取之實務見解",
    "無可擷取之實務見解",
    "無可擷取實務見解",
    "無實務見解",
    "沒有實務見解",
    "未擷取實務見解",
    "不能擷取之實務見解",
    "不可擷取之實務見解",
    "原始資料未提供全文文字",
    "已存原始 JSON",
    "請提供您需要我摘要的判決書全文",
    "請您提供需要我處理的判決書全文",
    "請您提供需要分析的判決書全文",
    "請您提供原始的判決書片段",
    "請您提供判決書全文",
    "請您提供判決書",
    "請提供完整的判決書",
    "請提供您需要我摘要的判決書全文",
    "請將判決書貼於此",
    "請您將判決書貼於下方",
    "請您現在貼上判決書",
    "判決書貼於下方",
    "我將立即為您執行",
    "我將為您執行",
    "作為MAGI系統的AI助理",
    "作為 MAGI 系統的 AI 助理",
    "MAGI系統的AI助理",
    "MAGI 系統的 AI 助理",
    "我已理解您的需求",
    "我已理解您的要求",
    "我已理解您的指示",
    "我將會按照以下標準",
    "我將會嚴格遵守",
    "我將嚴格依照",
    "嚴格按照您要求的格式輸出",
    "輸出內容：嚴格依照",
    "語言規範：全程使用",
    "請直接輸出校正",
    "而非創設新的法律見解",
    "而非闡述某個具有高度爭議性",
    "若需擷取量刑考量因素",
)

NO_INSIGHT_MARKERS = (
    "無實務見解",
    "無可擷取",
    "不能擷取",
    "不可擷取",
    "未擷取",
)

PROMPT_ECHO_MARKERS = (
    "請您現在貼上",
    "請將判決書貼",
    "判決書貼於下方",
    "我已理解",
    "我將會",
    "我將立即",
    "我將為您",
    "AI助理",
    "AI 助理",
    "作為MAGI",
    "作為 MAGI",
    "MAGI系統",
    "MAGI 系統",
)

PROMPT_ECHO_CONTEXT_MARKERS = (
    "判決書",
    "實務見解",
    "引用裁判",
    "適用法條",
    "逐字擷取",
    "嚴格依照",
    "輸出格式",
)


def normalize_insight_marker_text(value: object) -> str:
    return re.sub(r"\s+", "", str(value or ""))


def is_non_extractable_legal_insight(*values: object) -> bool:
    combined = normalize_insight_marker_text(" ".join(str(v or "") for v in values))
    if not combined:
        return True

    has_marker = any(marker in combined for marker in NON_EXTRACTABLE_INSIGHT_MARKERS)
    has_no_insight_text = any(marker in combined for marker in NO_INSIGHT_MARKERS)
    is_procedural_placeholder = "程序性文書" in combined and has_no_insight_text
    is_prompt_echo = (
        any(marker in combined for marker in PROMPT_ECHO_MARKERS)
        and any(marker in combined for marker in PROMPT_ECHO_CONTEXT_MARKERS)
    )
    is_empty_raw_placeholder = "原始資料未提供全文文字" in combined or "已存原始JSON" in combined
    return has_marker or is_procedural_placeholder or is_prompt_echo or is_empty_raw_placeholder


def displayable_insight_item(item: dict | None) -> bool:
    if not isinstance(item, dict):
        return False
    return not is_non_extractable_legal_insight(
        item.get("title"),
        item.get("summary"),
        item.get("insight_text"),
        item.get("full_text"),
        item.get("case_reason"),
        item.get("court"),
        item.get("source"),
    )


def non_extractable_legal_insight_sql_where(normalized_expr: str) -> tuple[str, tuple[str, ...]]:
    marker_conditions = [f"{normalized_expr} LIKE %s" for _ in NON_EXTRACTABLE_INSIGHT_MARKERS]
    no_insight_conditions = [f"{normalized_expr} LIKE %s" for _ in NO_INSIGHT_MARKERS]
    prompt_echo_conditions = [f"{normalized_expr} LIKE %s" for _ in PROMPT_ECHO_MARKERS]
    prompt_echo_context_conditions = [f"{normalized_expr} LIKE %s" for _ in PROMPT_ECHO_CONTEXT_MARKERS]
    where = (
        f"({' OR '.join(marker_conditions)}) "
        f"OR ({normalized_expr} LIKE %s AND ({' OR '.join(no_insight_conditions)})) "
        f"OR (({' OR '.join(prompt_echo_conditions)}) "
        f"AND ({' OR '.join(prompt_echo_context_conditions)}))"
    )
    params = (
        tuple(f"%{marker}%" for marker in NON_EXTRACTABLE_INSIGHT_MARKERS)
        + ("%程序性文書%",)
        + tuple(f"%{marker}%" for marker in NO_INSIGHT_MARKERS)
        + tuple(f"%{marker}%" for marker in PROMPT_ECHO_MARKERS)
        + tuple(f"%{marker}%" for marker in PROMPT_ECHO_CONTEXT_MARKERS)
    )
    return where, params
