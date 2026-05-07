"""
anchor_matcher.py — context_before + find + context_after 唯一匹配

移植自 Mike docxTrackedChanges.ts::findUniqueAnchor + normalizeWs。

設計：
- 在 full_text（文件全文，段落間以 "\\n" 分隔）中找唯一的 anchor
- 支援空白正規化（連續空白視為等價）
- 大小寫敏感（律師書狀大小寫有意義）
- 回傳 find 在 full_text 中的 0-indexed 起始位置
"""

import re
from typing import Optional, Tuple


# ---------------------------------------------------------------------------
# Pre-normalization (1-to-1 character replacements — length preserved)
# ---------------------------------------------------------------------------

def _pre_normalize(s: str) -> str:
    """取代智慧引號、非斷行空格等，1 對 1 字元替換，保持長度。"""
    # Smart single quotes / apostrophes
    s = s.replace("‘", "'").replace("’", "'").replace("′", "'")
    # Smart double quotes
    s = s.replace("“", '"').replace("”", '"').replace("″", '"')
    # Em/en dashes
    s = s.replace("–", "-").replace("—", "-")
    # Non-breaking space, zero-width space
    s = s.replace(" ", " ").replace("​", " ")
    return s


# ---------------------------------------------------------------------------
# Whitespace normalization with index mapping
# ---------------------------------------------------------------------------

class Normalized:
    """Normalized string + 原始 index 映射。"""

    def __init__(self, norm: str, orig_idx: list):
        self.norm = norm
        self.orig_idx = orig_idx  # orig_idx[i] = index in original string for norm[i]


def normalize_ws(s: str) -> Normalized:
    """
    將連續空白壓縮成單一空格，並記錄每個正規化字元在原始字串中的位置。
    不跨越段落分隔（\\n\\n 視為硬邊界，不壓縮）。

    注意：\\n（段落分隔符）本身也是空白，會被壓縮進去；
    但 \\n\\n 之間的分界語義上不應壓縮，Mike 實作中用 full-doc text
    join with \\n，因此兩段之間的 \\n 也只是單一 \\n。
    """
    pre = _pre_normalize(s)
    norm_chars = []
    orig_idx = []
    prev_space = False
    for i, ch in enumerate(pre):
        if re.match(r"\s", ch):
            if not prev_space:
                norm_chars.append(" ")
                orig_idx.append(i)
                prev_space = True
        else:
            norm_chars.append(ch)
            orig_idx.append(i)
            prev_space = False
    return Normalized("".join(norm_chars), orig_idx)


def map_norm_range_to_original(
    para_norm: Normalized,
    orig_len: int,
    norm_start: int,
    norm_end: int,
) -> Tuple[int, int]:
    """將正規化 [norm_start, norm_end) 映射回原始字串 [orig_start, orig_end)。"""
    if norm_start < len(para_norm.orig_idx):
        orig_start = para_norm.orig_idx[norm_start]
    else:
        orig_start = orig_len

    if norm_end == norm_start:
        orig_end = orig_start
    elif norm_end - 1 < len(para_norm.orig_idx):
        orig_end = para_norm.orig_idx[norm_end - 1] + 1
    else:
        orig_end = orig_len

    return orig_start, orig_end


# ---------------------------------------------------------------------------
# Core anchor matching
# ---------------------------------------------------------------------------

def _find_unique_in_norm(
    hay_norm: str,
    find_norm: str,
    ctx_before_norm: str,
    ctx_after_norm: str,
) -> Optional[Tuple[int, int]]:
    """
    在 hay_norm 中找唯一匹配的 find_norm（前有 ctx_before_norm、後有 ctx_after_norm）。

    Returns:
        (norm_start, norm_end) 若唯一找到；None 若 0 次或 2+ 次。
    """
    def check_ctx(pos: int) -> bool:
        if ctx_before_norm:
            start = pos - len(ctx_before_norm)
            if start < 0:
                return False
            if hay_norm[start:pos] != ctx_before_norm:
                return False
        if ctx_after_norm:
            end = pos + len(find_norm)
            if hay_norm[end:end + len(ctx_after_norm)] != ctx_after_norm:
                return False
        return True

    candidates = []

    if len(find_norm) == 0:
        # Pure insertion: scan every position
        for i in range(len(hay_norm) + 1):
            if check_ctx(i):
                candidates.append(i)
    else:
        from_pos = 0
        while from_pos <= len(hay_norm) - len(find_norm):
            idx = hay_norm.find(find_norm, from_pos)
            if idx < 0:
                break
            if check_ctx(idx):
                candidates.append(idx)
            from_pos = idx + 1

    if len(candidates) != 1:
        return None
    start = candidates[0]
    return (start, start + len(find_norm))


def find_unique_anchor(
    full_text: str,
    context_before: str,
    find: str,
    context_after: str,
) -> Tuple[Optional[int], str]:
    """
    在 full_text 中找 (context_before + find + context_after) 的唯一匹配位置。

    Returns:
        (start_offset, status)
        status = "ok" / "not_found" / "ambiguous" / "find_not_in_anchor"
        start_offset 為 find 在 full_text 中的 0-indexed 起始位置（status="ok" 時）
        其他 status 時 start_offset = None

    匹配規則：
    - 連續空白（\\s+）視為等價（normalize 後比對）
    - 大小寫敏感
    - 整個 anchor 在 full_text 中必須恰好出現 1 次

    特殊：
    - context_before == "" 表示 find 在文檔開頭
    - context_after == "" 表示 find 在文檔結尾
    - find == "" 是純插入（只用 context_before + context_after 定位插入點）
    """
    hay_norm = normalize_ws(full_text)
    find_norm = normalize_ws(find).norm
    ctx_before_norm = normalize_ws(context_before).norm
    ctx_after_norm = normalize_ws(context_after).norm

    # Strategy (mirror Mike's fallback chain):
    # 1. find + full context (strictest)
    # 2. find + context_before only
    # 3. find + context_after only
    # 4. find alone (globally unique)
    attempts = [
        (ctx_before_norm, ctx_after_norm),
        (ctx_before_norm, ""),
        ("", ctx_after_norm),
        ("", ""),
    ]

    saw_multi = False
    for cb, ca in attempts:
        result = _find_unique_in_norm(hay_norm.norm, find_norm, cb, ca)
        if result is not None:
            norm_start, norm_end = result
            # Map norm position back to original
            orig_start, _ = map_norm_range_to_original(
                hay_norm, len(full_text), norm_start, norm_end
            )
            return (orig_start, "ok")
        else:
            # Check if it's ambiguous (> 1) or not found (0)
            # Re-run to count candidates
            count = _count_candidates(hay_norm.norm, find_norm, cb, ca)
            if count > 1:
                saw_multi = True

    if saw_multi:
        return (None, "ambiguous")
    return (None, "not_found")


def _count_candidates(
    hay_norm: str,
    find_norm: str,
    ctx_before_norm: str,
    ctx_after_norm: str,
) -> int:
    """Count how many positions match the given anchor."""
    def check_ctx(pos: int) -> bool:
        if ctx_before_norm:
            start = pos - len(ctx_before_norm)
            if start < 0:
                return False
            if hay_norm[start:pos] != ctx_before_norm:
                return False
        if ctx_after_norm:
            end = pos + len(find_norm)
            if hay_norm[end:end + len(ctx_after_norm)] != ctx_after_norm:
                return False
        return True

    count = 0
    if len(find_norm) == 0:
        for i in range(len(hay_norm) + 1):
            if check_ctx(i):
                count += 1
    else:
        from_pos = 0
        while from_pos <= len(hay_norm) - len(find_norm):
            idx = hay_norm.find(find_norm, from_pos)
            if idx < 0:
                break
            if check_ctx(idx):
                count += 1
            from_pos = idx + 1
    return count


# ---------------------------------------------------------------------------
# Multi-paragraph document matching (used by tracked_edits.py)
# ---------------------------------------------------------------------------

def find_anchor_in_paragraphs(
    para_texts: list,
    context_before: str,
    find: str,
    context_after: str,
) -> Tuple[Optional[int], Optional[int], Optional[int], str]:
    """
    在段落列表中找唯一的 anchor，回傳 (para_idx, orig_start, orig_end, status)。

    實作 Mike 的「逐段搜尋 + fallback 降階」策略：
    1. 每段各自嘗試 full context → half → find-only
    2. 若正好一段中正好一個唯一匹配 → ok
    3. 跨段都找不到 → not_found
    4. 多段都有匹配 → ambiguous

    Returns:
        (para_idx, orig_start, orig_end, status)
        status = "ok" / "not_found" / "ambiguous"
    """
    find_norm = normalize_ws(find).norm
    ctx_before_norm = normalize_ws(context_before).norm
    ctx_after_norm = normalize_ws(context_after).norm

    # Normalize each paragraph
    para_norms = [normalize_ws(t) for t in para_texts]

    attempts = [
        (ctx_before_norm, ctx_after_norm),
        (ctx_before_norm, ""),
        ("", ctx_after_norm),
        ("", ""),
    ]

    saw_ambiguous = False

    for cb, ca in attempts:
        hits = []
        internal_ambiguous = False
        for pi, pn in enumerate(para_norms):
            result = _find_unique_in_norm(pn.norm, find_norm, cb, ca)
            if result is not None:
                hits.append((pi, result[0], result[1]))
            else:
                count = _count_candidates(pn.norm, find_norm, cb, ca)
                if count > 1:
                    internal_ambiguous = True

        if internal_ambiguous or len(hits) > 1:
            saw_ambiguous = True
            continue

        if len(hits) == 1:
            pi, norm_start, norm_end = hits[0]
            orig_len = len(para_texts[pi])
            orig_start, orig_end = map_norm_range_to_original(
                para_norms[pi], orig_len, norm_start, norm_end
            )
            return (pi, orig_start, orig_end, "ok")

    if saw_ambiguous:
        return (None, None, None, "ambiguous")
    return (None, None, None, "not_found")
