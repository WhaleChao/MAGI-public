"""
citation_format.py — Mike citation 格式解析與輸出

Phase 5 of docx-editor skill integration.

Format spec (from Mike SYSTEM_PROMPT DOCUMENT CITATION INSTRUCTIONS):
  In-prose markers: [1], [2], ...
  Citation block at end of response:

  <CITATIONS>
  [
    {"ref": 1, "doc_id": "doc-0", "page": "3", "quote": "verbatim excerpt ≤25 words"},
    {"ref": 2, "doc_id": "doc-1", "page": "41-42", "quote": "another verbatim excerpt"}
  ]
  </CITATIONS>

Usage:
  from skills.bridge.citation_format import parse_citations, render_citations_for_telegram, build_citation_system_prompt
"""

import re
import json
from dataclasses import dataclass, field
from typing import List, Optional


# ── Regex for <CITATIONS>...</CITATIONS> block ──
CITATION_BLOCK_RE = re.compile(r"<CITATIONS>\s*(\[.*?\])\s*</CITATIONS>", re.DOTALL)

# Max words in a quote before we warn
_MAX_QUOTE_WORDS = 25


@dataclass
class Citation:
    ref: int            # inline marker number e.g. 1
    doc_id: str         # document identifier e.g. "doc-0"
    page: str           # page number or range e.g. "3" or "41-42"
    quote: str          # verbatim excerpt (≤25 words)


@dataclass
class ParsedAnswer:
    prose: str                  # answer text with <CITATIONS> block removed
    citations: List[Citation]   # parsed citations (may be empty)
    parse_warnings: List[str]   # any warnings encountered during parsing


def _count_words(text: str) -> int:
    """Count words in text (simple whitespace split)."""
    return len(text.split())


def parse_citations(answer_text: str) -> ParsedAnswer:
    """從 LLM 答覆中解析 <CITATIONS> JSON block。

    容錯策略：
    - 沒有 <CITATIONS> block → citations=[], prose=answer_text（不警告）
    - JSON parse 失敗 → 警告 + citations=[]，prose 仍保留（移除 <CITATIONS> 標記）
    - 缺欄位（ref/doc_id/page/quote）→ 警告，個別 citation 跳過
    - quote > 25 words → 警告，保留但標注
    - prose 中的 [N] marker 沒對應到 citation ref → 警告
    """
    warnings = []
    citations = []

    # --- 嘗試找 <CITATIONS> block ---
    match = CITATION_BLOCK_RE.search(answer_text)
    if not match:
        # No citation block — return as-is, no warning needed
        return ParsedAnswer(prose=answer_text, citations=[], parse_warnings=[])

    json_str = match.group(1).strip()

    # prose = answer with <CITATIONS>...</CITATIONS> removed and stripped
    prose = CITATION_BLOCK_RE.sub("", answer_text).strip()

    # --- Parse JSON ---
    try:
        raw_list = json.loads(json_str)
    except json.JSONDecodeError as e:
        warnings.append(f"<CITATIONS> JSON parse 失敗: {e}")
        return ParsedAnswer(prose=prose, citations=[], parse_warnings=warnings)

    if not isinstance(raw_list, list):
        warnings.append("<CITATIONS> 內容不是 JSON array")
        return ParsedAnswer(prose=prose, citations=[], parse_warnings=warnings)

    # --- Parse individual citations ---
    for i, item in enumerate(raw_list):
        if not isinstance(item, dict):
            warnings.append(f"citation[{i}] 不是物件，已跳過")
            continue

        missing = [f for f in ("ref", "doc_id", "page", "quote") if f not in item]
        if missing:
            warnings.append(f"citation[{i}] 缺欄位 {missing}，已跳過")
            continue

        ref = item["ref"]
        doc_id = str(item["doc_id"])
        page = str(item["page"])
        quote = str(item["quote"])

        if not isinstance(ref, int):
            try:
                ref = int(ref)
            except (ValueError, TypeError):
                warnings.append(f"citation[{i}].ref 不是整數 ({ref!r})，已跳過")
                continue

        word_count = _count_words(quote)
        if word_count > _MAX_QUOTE_WORDS:
            warnings.append(
                f"citation[{i}] quote 超過 {_MAX_QUOTE_WORDS} words ({word_count} words): {quote[:50]!r}..."
            )

        citations.append(Citation(ref=ref, doc_id=doc_id, page=page, quote=quote))

    # --- 檢查 prose 中的 [N] markers 是否都有對應 citation ---
    inline_refs = set(int(m) for m in re.findall(r"\[(\d+)\]", prose))
    citation_refs = set(c.ref for c in citations)
    unmatched = inline_refs - citation_refs
    if unmatched:
        warnings.append(f"prose 中的 {sorted(unmatched)} 在 citations 中找不到對應項目")

    return ParsedAnswer(prose=prose, citations=citations, parse_warnings=warnings)


def render_citations_for_telegram(parsed: ParsedAnswer) -> str:
    """為 TG/Discord/LINE 格式化：保留 prose 的 [N] marker + 附帶 citations footer。

    格式：
    <prose with [N] markers>

    📄 引用：
    [1] doc-0 p.3：「verbatim quote here」
    [2] doc-1 p.41-42：「another quote」
    """
    if not parsed.citations:
        return parsed.prose

    lines = [parsed.prose, "", "📄 引用："]
    for c in parsed.citations:
        lines.append(f"[{c.ref}] {c.doc_id} p.{c.page}：「{c.quote}」")

    return "\n".join(lines)


def build_citation_system_prompt() -> str:
    """回 Mike SYSTEM_PROMPT 的 DOCUMENT CITATION INSTRUCTIONS 繁中版。"""
    return """
DOCUMENT CITATION INSTRUCTIONS（文件引用規範）：

當你引用文件中的內容時，必須使用以下格式：

1. 在答覆正文中，對每個引用插入 [N] 標記（N 從 1 開始遞增）。
   例：「依據[1]，被告應負賠償責任。」

2. 在答覆末尾，加入 <CITATIONS> 區塊（純 JSON array，不含 markdown 圍欄）：

<CITATIONS>
[
  {"ref": 1, "doc_id": "文件識別碼", "page": "頁碼或範圍", "quote": "精確引用，不超過 25 個英文字或 50 個中文字"},
  {"ref": 2, "doc_id": "文件識別碼", "page": "41-42", "quote": "另一段引用"}
]
</CITATIONS>

紅線：
- quote 必須是文件中的原文逐字引用，不得改寫或總結
- quote 不得超過 25 個英文字（約 50 個中文字）
- 若沒有相關文件可引用，不要加 <CITATIONS> 區塊
- 只回應繁體中文
""".strip()
