"""
tracked_edits.py — apply_tracked_edits() 主邏輯

移植自 Mike docxTrackedChanges.ts::applyTrackedEdits。

設計：
- 讀 docx bytes
- 逐 edit 找 anchor → 規劃 change → 重建段落 XML → 寫回 ZIP
- 安全保證：全失敗時回傳原 bytes；只改 word/document.xml

Python API：
    from skills.docx_editor.lib.tracked_edits import apply_tracked_edits, EditInput
"""

import uuid
import zipfile
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional

from .anchor_matcher import (
    find_anchor_in_paragraphs,
    normalize_ws,
    map_norm_range_to_original,
)
from .docx_io import (
    read_docx_to_xml,
    write_xml_to_docx,
    find_max_id,
    get_body,
    collect_paragraphs,
    extract_paragraph_text,
)
from .run_splitter import (
    flatten_paragraph,
    collapse_diff,
    rebuild_paragraph_with_edits,
)


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------

@dataclass
class EditInput:
    """單筆 edit 描述。完全對應 Mike EditInput interface。"""
    find: str             # 要被替換的原文（必須在 anchor 範圍內 verbatim 出現）
    replace: str          # 替換後的新文（空字串 = 純刪除）
    context_before: str   # find 前的 anchor 文字（需與 docx 內 verbatim 對得上）
    context_after: str    # find 後的 anchor 文字
    reason: Optional[str] = None  # 編輯理由（律師可見）


@dataclass
class AppliedChange:
    """成功套用的單筆 edit 結果。"""
    id: str               # MAGI 端的 edit id（UUID4 hex 前 12 字元）
    del_id: Optional[str] # docx <w:del w:id> 的值（int as str）
    ins_id: Optional[str] # docx <w:ins w:id> 的值
    deleted_text: str     # 實際刪除的文字
    inserted_text: str    # 實際插入的文字
    context_before: str
    context_after: str
    reason: Optional[str] = None


@dataclass
class EditError:
    """套用失敗的 edit。"""
    index: int            # 在輸入 edits list 中的 index
    reason: str           # 失敗原因


@dataclass
class ApplyTrackedEditsResult:
    bytes: bytes                    # 改完的 docx ZIP bytes
    changes: List[AppliedChange]    # 成功套用的 edits
    errors: List[EditError]         # 失敗的 edits


# ---------------------------------------------------------------------------
# Internal plan
# ---------------------------------------------------------------------------

@dataclass
class _PlannedChange:
    edit_index: int
    para_idx: int
    delete_start: int
    delete_end: int
    deleted_text: str
    inserted_text: str
    context_before: str
    context_after: str
    reason: Optional[str]
    change_id: str
    del_wid: Optional[str]
    ins_wid: Optional[str]


# ---------------------------------------------------------------------------
# Main function
# ---------------------------------------------------------------------------

def apply_tracked_edits(
    docx_bytes: bytes,
    edits: List[EditInput],
    *,
    author: str = "MAGI",
    date: Optional[str] = None,
) -> ApplyTrackedEditsResult:
    """
    對 docx bytes 套 tracked changes，回傳新 bytes + 套用結果。

    安全保證：
    - 任何 edit 失敗 → 進 errors，不影響其他 edits
    - 所有 edits 全部失敗時仍回傳原 bytes（不破壞檔案）
    - 不修改 word/document.xml 以外的任何檔案

    Raises:
        ValueError: docx_bytes 不合法
        RuntimeError: 其他意外錯誤
    """
    if date is None:
        date = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # --- Parse docx ---
    try:
        zf, root = read_docx_to_xml(docx_bytes)
    except ValueError:
        raise
    except Exception as e:
        raise RuntimeError(f"Failed to parse docx: {e}") from e

    try:
        body = get_body(root)
    except ValueError:
        raise

    paragraphs = collect_paragraphs(body)

    # Pre-compute paragraph texts (accepted view)
    para_texts = [extract_paragraph_text(p) for p in paragraphs]

    # Pre-compute flattened paragraphs (with run tracking)
    para_flats = [flatten_paragraph(p) for p in paragraphs]

    # Get next available w:id
    next_wid = find_max_id(root) + 1

    # Plan edits
    plans_per_para: dict = {}  # para_idx -> List[_PlannedChange]
    applied_changes: List[AppliedChange] = []
    errors: List[EditError] = []

    for edit_idx, edit in enumerate(edits):
        find = edit.find or ""
        replace = edit.replace or ""
        ctx_before = edit.context_before or ""
        ctx_after = edit.context_after or ""

        # Validation (mirror Mike)
        if not find and not replace:
            errors.append(EditError(index=edit_idx, reason="Empty edit (both find and replace are empty)."))
            continue

        if not find and not ctx_before and not ctx_after:
            errors.append(EditError(
                index=edit_idx,
                reason="Pure insertion requires context_before or context_after."
            ))
            continue

        # Find anchor across paragraphs
        pi, orig_start, orig_end, status = find_anchor_in_paragraphs(
            para_texts, ctx_before, find, ctx_after
        )

        if status == "not_found":
            errors.append(EditError(
                index=edit_idx,
                reason=f'Could not locate find="{_truncate(find, 80)}" in the document. '
                       f'Re-read the document and copy context verbatim (including punctuation & whitespace).'
            ))
            continue
        elif status == "ambiguous":
            errors.append(EditError(
                index=edit_idx,
                reason=f'Ambiguous match for find="{_truncate(find, 80)}". '
                       f'Add longer context_before / context_after so the anchor is unique.'
            ))
            continue

        # Get the actual original text in that range
        para_text = para_texts[pi]
        original_find = para_text[orig_start:orig_end]

        # Collapse diff to minimize tracked range
        deleted, inserted, leading_eq, trailing_eq = collapse_diff(original_find, replace)
        min_start = orig_start + leading_eq
        min_end = min_start + len(deleted)

        change_id = uuid.uuid4().hex[:12]
        del_wid = str(next_wid) if deleted else None
        if deleted:
            next_wid += 1
        ins_wid = str(next_wid) if inserted else None
        if inserted:
            next_wid += 1

        plan = _PlannedChange(
            edit_index=edit_idx,
            para_idx=pi,
            delete_start=min_start,
            delete_end=min_end,
            deleted_text=deleted,
            inserted_text=inserted,
            context_before=ctx_before,
            context_after=ctx_after,
            reason=edit.reason,
            change_id=change_id,
            del_wid=del_wid,
            ins_wid=ins_wid,
        )

        # Check for overlap with earlier plans in same paragraph
        existing = plans_per_para.get(pi, [])
        overlap = any(
            not (plan.delete_end <= p.delete_start or plan.delete_start >= p.delete_end)
            for p in existing
        )
        if overlap:
            errors.append(EditError(
                index=edit_idx,
                reason="Overlaps a previous edit in the same paragraph."
            ))
            continue

        existing.append(plan)
        existing.sort(key=lambda p: p.delete_start)
        plans_per_para[pi] = existing

        applied_changes.append(AppliedChange(
            id=change_id,
            del_id=plan.del_wid,
            ins_id=plan.ins_wid,
            deleted_text=deleted,
            inserted_text=inserted,
            context_before=ctx_before,
            context_after=ctx_after,
            reason=edit.reason,
        ))

    # If no edits succeeded, return original bytes
    if not applied_changes:
        zf.close()
        return ApplyTrackedEditsResult(
            bytes=docx_bytes,
            changes=[],
            errors=errors,
        )

    # Apply plans per paragraph
    for para_idx, plans in plans_per_para.items():
        p_element = paragraphs[para_idx]
        flat = para_flats[para_idx]
        edit_dicts = [
            {
                "delete_start": plan.delete_start,
                "delete_end": plan.delete_end,
                "deleted_text": plan.deleted_text,
                "inserted_text": plan.inserted_text,
                "del_wid": plan.del_wid,
                "ins_wid": plan.ins_wid,
            }
            for plan in plans
        ]
        rebuild_paragraph_with_edits(p_element, flat, edit_dicts, author, date)

    # Write back to ZIP
    new_bytes = write_xml_to_docx(zf, root)
    zf.close()

    return ApplyTrackedEditsResult(
        bytes=new_bytes,
        changes=applied_changes,
        errors=errors,
    )


def _truncate(s: str, n: int) -> str:
    if not s:
        return ""
    return s[:n] + "…" if len(s) > n else s
