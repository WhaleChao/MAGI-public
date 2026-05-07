"""
llm_edit_planner.py — 用 LLM 根據律師指令產生 EditInput list

Phase 3 of docx-editor skill.

Usage:
    from lib.llm_edit_planner import plan_edits_with_llm
"""

import json
import logging
import os
import sys
from typing import List, Tuple

logger = logging.getLogger(__name__)

# Import EditInput + find_unique_anchor — support both package and direct-import modes
try:
    from .tracked_edits import EditInput
    from .anchor_matcher import find_unique_anchor
except ImportError:
    # Fallback for when lib/ is added directly to sys.path (e.g., from action.py)
    _SKILL_DIR = os.path.dirname(os.path.abspath(__file__))
    _SKILL_PARENT = os.path.dirname(_SKILL_DIR)
    if _SKILL_PARENT not in sys.path:
        sys.path.insert(0, _SKILL_PARENT)
    if _SKILL_DIR not in sys.path:
        sys.path.insert(0, _SKILL_DIR)
    import importlib as _il
    _te = _il.import_module("tracked_edits")
    _am = _il.import_module("anchor_matcher")
    EditInput = _te.EditInput
    find_unique_anchor = _am.find_unique_anchor


_EDIT_PLANNER_SYSTEM_PROMPT = """你是法律文書編輯助手。律師會給你一份書狀全文與一條編輯指令，
你必須回傳 JSON array，每筆物件含：
  - find: 原文中要被替換的精確字串（必須在文檔中 verbatim 出現）
  - replace: 替換後的字串（純刪除時設空字串 ""）
  - context_before: find 前的 5-15 字 anchor（讓位置唯一）
  - context_after: find 後的 5-15 字 anchor
  - reason: 為什麼要這樣改（一句話）

紅線：
- 不要編造原文不存在的內容
- 不要做超出指令範圍的改動
- 若指令需要大幅改寫（不適合 anchored edit），回 [] 並在 reason 寫「指令超出 anchored edit 範圍」
- 最多回 20 筆
- 用繁體中文（律師慣用）

只回 JSON array，不要任何 markdown 圍欄、解釋文字、prefix。"""

_MAX_DOC_CHARS = 8000  # 超過時截斷並警告
_HEAVY_THRESHOLD = 4000  # 超過此長度用 heavy mode (NIM 405B)


def plan_edits_with_llm(
    docx_text: str,
    user_instruction: str,
    *,
    model: str = "",
    max_edits: int = 20,
    timeout_sec: int = 60,
) -> Tuple[List[EditInput], List[str]]:
    """LLM 根據律師指令產生 EditInput list。

    Args:
        docx_text: cmd_extract 的純文字輸出
        user_instruction: 律師指令，例如「把所有『甲方』改成『乙方』」
        model: 預設讀 MAGI_TEXT_PRIMARY_MODEL，空則讓 inference_gateway 自選
        max_edits: 最多回傳幾筆 edits（預設 20）
        timeout_sec: LLM 呼叫 timeout

    Returns:
        (edits, warnings)
        edits: 通過 anchor 預檢的 EditInput list
        warnings: 預檢失敗 / parse 問題 / 截斷警告
    """
    warnings_list = []

    # --- 截斷過長的文件 ---
    text_to_send = docx_text
    if len(docx_text) > _MAX_DOC_CHARS:
        text_to_send = docx_text[:_MAX_DOC_CHARS]
        warnings_list.append(
            f"文件過長（{len(docx_text)} chars），已截斷至 {_MAX_DOC_CHARS} chars 給 LLM；"
            "若 edit 找不到 anchor，可能是截斷導致"
        )

    heavy = len(text_to_send) > _HEAVY_THRESHOLD

    # --- 組 prompt ---
    prompt = (
        f"【書狀全文】\n{text_to_send}\n\n"
        f"【編輯指令】\n{user_instruction}\n\n"
        f"最多回 {max_edits} 筆。只回 JSON array。"
    )

    # --- 呼叫 LLM ---
    raw_json = _call_llm(prompt, heavy=heavy, timeout_sec=timeout_sec, model=model)
    if raw_json is None:
        warnings_list.append("LLM 呼叫失敗，回傳空 edits")
        return [], warnings_list

    # --- Parse JSON ---
    try:
        raw_list = _parse_json_response(raw_json)
    except ValueError as e:
        warnings_list.append(f"LLM 回應 JSON parse 失敗: {e}；原文: {raw_json[:200]!r}")
        return [], warnings_list

    if not isinstance(raw_list, list):
        warnings_list.append("LLM 回應不是 JSON array")
        return [], warnings_list

    # --- Convert to EditInput + anchor pre-check ---
    edits = []
    for i, item in enumerate(raw_list[:max_edits]):
        if not isinstance(item, dict):
            warnings_list.append(f"edit[{i}] 不是物件，已跳過")
            continue

        find = item.get("find", "")
        replace = item.get("replace", "")
        context_before = item.get("context_before", "")
        context_after = item.get("context_after", "")
        reason = item.get("reason", "")

        # 檢查 LLM 是否表示指令超出 anchored edit 範圍
        if not find and "超出" in reason:
            warnings_list.append(f"LLM 判定指令超出 anchored edit 範圍: {reason}")
            continue

        # Anchor 預檢：find 必須在 docx_text 中唯一可定位
        _offset, status = find_unique_anchor(
            docx_text,
            context_before=context_before,
            find=find,
            context_after=context_after,
        )

        if status == "ok":
            edits.append(EditInput(
                find=find,
                replace=replace,
                context_before=context_before,
                context_after=context_after,
                reason=reason,
            ))
        elif status == "not_found":
            warnings_list.append(
                f"edit[{i}] anchor 預檢失敗（not_found）: find={find!r} "
                f"context_before={context_before!r} context_after={context_after!r}"
            )
        elif status == "ambiguous":
            warnings_list.append(
                f"edit[{i}] anchor 預檢失敗（ambiguous）: find={find!r} 在文件中多次出現，"
                "需要更精確的 context_before / context_after"
            )
        else:
            warnings_list.append(f"edit[{i}] anchor 預檢失敗（{status}）: find={find!r}")

    return edits, warnings_list


def _call_llm(prompt: str, heavy: bool, timeout_sec: int, model: str) -> str:
    """呼叫 inference_gateway。回傳 LLM 文字回應，失敗回 None。"""
    try:
        from skills.bridge.inference_gateway import InferenceGateway
        gw = InferenceGateway()
        result = gw.chat(
            prompt=prompt,
            task_type="general",
            timeout=timeout_sec,
            model=model or "",
            system=_EDIT_PLANNER_SYSTEM_PROMPT,
            heavy=heavy,
        )
        if result.get("success"):
            return result.get("response") or result.get("text") or ""
        else:
            logger.warning(f"llm_edit_planner LLM call failed: {result.get('error')}")
            return None
    except Exception as e:
        logger.warning(f"llm_edit_planner LLM call exception: {e}")
        return None


def _parse_json_response(raw: str) -> list:
    """Parse LLM response as JSON. Strip markdown fences if present."""
    raw = raw.strip()
    # Strip markdown code fences
    if raw.startswith("```"):
        lines = raw.split("\n")
        # Remove first and last ``` lines
        inner = []
        in_block = False
        for line in lines:
            if line.startswith("```") and not in_block:
                in_block = True
                continue
            if line.startswith("```") and in_block:
                break
            if in_block:
                inner.append(line)
        raw = "\n".join(inner).strip()

    return json.loads(raw)
