# -*- coding: utf-8 -*-
"""
folder_writer.py — M5 模組 2：建立書狀資料夾、寫 docx、複製改名附件

公開 API:
    write_brief_folder(case_meta, extracted, matched, *, procedure, brief_seq) -> dict
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Optional

from .docx_builder import build_supplement_docx, _int_to_chinese

logger = logging.getLogger("folder_writer")

# 檔名非法字元（macOS / Windows 兩者都排除）
_ILLEGAL_CHARS_RE = re.compile(r'[<>:"|?*\\/\x00-\x1f]')


def _sanitize_filename(name: str) -> str:
    """移除檔名非法字元，並去除前後空白。"""
    return _ILLEGAL_CHARS_RE.sub("", name).strip()


def _nfc(s: str) -> str:
    return unicodedata.normalize("NFC", s)


def _make_folder_name(procedure: str, brief_seq_cn: str, date_str: str) -> str:
    """外層資料夾名稱：YYYYMMDD 消費者債務清理{更生|清算}陳報（{中文書狀號}）狀"""
    return f"{date_str} 消費者債務清理{procedure}陳報（{brief_seq_cn}）狀"


def _make_docx_name(procedure: str, party_name: str, date_str: str) -> str:
    """內層 docx 檔名：YYYYMMDD_{更生|清算}陳報狀（{當事人}）.docx"""
    safe_party = _sanitize_filename(party_name) or "聲請人"
    return f"{date_str}_{procedure}陳報狀（{safe_party}）.docx"


def _make_attachment_name(n: int, category: str, ext: str) -> str:
    """附件{N}_{category_sanitized}.{ext}"""
    safe_cat = _sanitize_filename(category) or f"附件{n}"
    return f"附件{n}_{safe_cat}{ext}"


def write_brief_folder(
    case_meta: dict,
    extracted: dict,
    matched: list[dict],
    *,
    procedure: str,
    brief_seq: int,
    template_path: Optional[str] = None,
) -> dict:
    """建立書狀資料夾、寫 docx、複製改名附件。

    路徑：
        <case_dir>/<subfolder_briefs>/YYYYMMDD <書狀完整名稱>/
            YYYYMMDD_<更生|清算>陳報狀（<當事人>）.docx
            附件1_<category>.pdf
            附件2_<category>.pdf
            ...

    Returns:
        {
            "folder_path": str,
            "docx_path": str,
            "attachments_copied": [{"src": str, "dst": str, "item_id": int}],
            "attachments_skipped": [{"reason": str, "item_id": int}],
        }
    """
    case_dir = _nfc(case_meta.get("case_dir", ""))
    subfolder_briefs = case_meta.get("subfolder_briefs", "04_我方歷次書狀")
    parties = case_meta.get("parties", [])
    party_name = parties[0] if parties else ""

    today = datetime.today()
    date_str = today.strftime("%Y%m%d")
    brief_seq_cn = _int_to_chinese(brief_seq)

    # ── 1. 決定外層資料夾路徑（撞名處理） ────────────────────────────────────
    briefs_root = os.path.join(case_dir, subfolder_briefs)
    os.makedirs(briefs_root, exist_ok=True)

    folder_name_base = _make_folder_name(procedure, brief_seq_cn, date_str)
    folder_path = os.path.join(briefs_root, folder_name_base)

    version = 1
    while os.path.exists(folder_path):
        version += 1
        folder_path = os.path.join(briefs_root, f"{folder_name_base}_v{version}")

    os.makedirs(folder_path, exist_ok=True)
    logger.info("書狀資料夾：%s", folder_path)

    # ── 2. 產生 docx ─────────────────────────────────────────────────────────
    docx_name = _make_docx_name(procedure, party_name, date_str)
    docx_path = os.path.join(folder_path, docx_name)

    build_result = build_supplement_docx(
        case_meta,
        extracted,
        matched,
        procedure=procedure,
        brief_seq=brief_seq,
        output_path=docx_path,
        template_path=template_path,
    )
    logger.info("docx 產生完成：%s", docx_path)

    # ── 3. 複製附件 ──────────────────────────────────────────────────────────
    items = extracted.get("items", [])
    attachments_copied: list[dict] = []
    attachments_skipped: list[dict] = []

    for idx, item in enumerate(items):
        item_id = item.get("item_id", idx + 1)
        category = item.get("category", f"項目{idx+1}")
        m = matched[idx] if idx < len(matched) else None

        # 決定來源
        src: Optional[str] = None
        if m:
            if m.get("selected"):
                src = m["selected"]
            elif m.get("candidates"):
                src = m["candidates"][0]["path"]

        if src is None or not os.path.isfile(src):
            attachments_skipped.append({"reason": "missing", "item_id": item_id})
            logger.debug("附件 missing：item_id=%s category=%s", item_id, category)
            continue

        ext = os.path.splitext(src)[1].lower()
        attach_name = _make_attachment_name(idx + 1, category, ext)
        dst = os.path.join(folder_path, attach_name)

        try:
            shutil.copy2(src, dst)
            attachments_copied.append({"src": src, "dst": dst, "item_id": item_id})
            logger.info("附件複製：%s → %s", src, dst)
        except OSError as exc:
            attachments_skipped.append({"reason": str(exc), "item_id": item_id})
            logger.warning("附件複製失敗：%s → %s：%s", src, dst, exc)

    return {
        "folder_path": folder_path,
        "docx_path": docx_path,
        "attachments_copied": attachments_copied,
        "attachments_skipped": attachments_skipped,
    }
