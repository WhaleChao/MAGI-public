# -*- coding: utf-8 -*-
"""Shared Legal Aid go-live document readiness rules."""

from __future__ import annotations

import os
from typing import Iterable


OPENING_NOTICE_KEYWORDS = (
    "開辦通知書",
    "接案通知書",
    "扶助律師接案通知書",
    "准予扶助證明書",
    "開辦回報單",
    "開辦回報",
    "回報單",
)

OPENING_NOTICE_EXCLUDE_KEYWORDS = (
    "結案",
    "撤回",
    "酬金",
    "附條件",
    "變更審查",
    "補件",
    "補正",
)

STORED_PLEADING_MARKERS = ("存底", "留底", "收狀章", "收文章")

PLEADING_PROOF_KEYWORDS = (
    "狀",
    "答辯",
    "陳報",
    "聲請",
    "申請",
    "上訴",
    "抗告",
    "準備",
    "補正",
    "告訴",
    "起訴",
    "辯護",
    "意見",
    "理由",
)

PROOF_EXCLUDE_KEYWORDS = (
    "範本",
    "模板",
    "草稿",
    "空白",
    "未簽",
    "收據",
    "酬金",
    "繳費",
)

CONSUMER_DEBT_KEYWORDS = (
    "消費者債務清理",
    "消債",
    "更生",
    "清算",
    "前置調解",
    "前置協商",
    "債務清理",
)


def _contains_any(text: str, keywords: Iterable[str]) -> bool:
    return any(k in str(text or "") for k in keywords)


def _path_text(filename: str, full_path: str = "", subdir: str = "") -> str:
    return " ".join(str(x or "") for x in (filename, full_path, subdir))


def is_opening_notice_filename(filename: str, *, full_path: str = "", subdir: str = "") -> bool:
    """Return whether a file should count as prepared go-live notice/evidence.

    01_法扶資料 contains blank portal forms and must not be treated as prepared.
    The caller should only scan 02_開辦資料 for go-live readiness.
    """
    fn = os.path.basename(str(filename or ""))
    if not fn or _contains_any(fn, OPENING_NOTICE_EXCLUDE_KEYWORDS):
        return False
    in_opening_dir = "02_開辦資料" in _path_text(fn, full_path, subdir)
    if _contains_any(fn, OPENING_NOTICE_KEYWORDS):
        return True
    if in_opening_dir and _contains_any(fn, ("開辦資料", "開辦")):
        return not _contains_any(fn, ("委任狀", "酬金", "附條件", "結案", "撤回"))
    return False


def is_stored_pleading_proof(filename: str, *, full_path: str = "", subdir: str = "") -> bool:
    """Return whether a stored pleading can prove a non-consumer-debt go-live.

    A pleading is only accepted when it is stored under 我方歷次書狀 and is
    clearly a filed/stored copy.  This avoids treating evidence, receipts, or
    random file names as opening proof.
    """
    fn = os.path.basename(str(filename or ""))
    if not fn or _contains_any(fn, PROOF_EXCLUDE_KEYWORDS):
        return False
    text = _path_text(fn, full_path, subdir)
    if "04_我方歷次書狀" not in text and "我方歷次書狀" not in text:
        return False
    if not _contains_any(fn, STORED_PLEADING_MARKERS):
        return False
    return _contains_any(fn, PLEADING_PROOF_KEYWORDS)


def is_go_live_receipt_proof(filename: str) -> bool:
    fn = os.path.basename(str(filename or ""))
    if not fn or _contains_any(fn, PROOF_EXCLUDE_KEYWORDS):
        return False
    return _contains_any(fn, ("回執", "收件回執", "郵局回執", "掛號回執"))


def is_consumer_debt_text(*parts: str) -> bool:
    return _contains_any(" ".join(str(x or "") for x in parts), CONSUMER_DEBT_KEYWORDS)


def go_live_proof_files(docs: dict) -> list[str]:
    """Return non-notice files proving actual work started."""
    seen: set[str] = set()
    out: list[str] = []
    for key in ("poa_files", "opening_proof_files", "stored_pleading_files", "receipt_files"):
        for path in docs.get(key) or []:
            p = str(path or "")
            if p and p not in seen:
                seen.add(p)
                out.append(p)
    return out


def go_live_notice_files(docs: dict) -> list[str]:
    return [str(x) for x in (docs.get("opening_notice_files") or []) if str(x or "")]


def is_go_live_ready(docs: dict, *, is_consumer_debt: bool) -> bool:
    if not go_live_notice_files(docs):
        return False
    if is_consumer_debt:
        return True
    return bool(go_live_proof_files(docs))


def go_live_missing_labels(docs: dict, *, is_consumer_debt: bool) -> list[str]:
    missing: list[str] = []
    if not go_live_notice_files(docs):
        missing.append("開辦通知書/接案通知書/回報單")
    if not is_consumer_debt and not go_live_proof_files(docs):
        missing.append("委任狀或書狀存底/回執")
    return missing
