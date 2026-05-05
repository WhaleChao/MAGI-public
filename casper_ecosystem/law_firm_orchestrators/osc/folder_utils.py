"""
OSC 案件資料夾建立工具 — 獨立於 UI，可由桌面版或 Web API 共用。
"""
from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path

SUBFOLDERS = {
    "一般案件": [
        "01_委任契約書", "02_我方歷次書狀", "03_對方歷次書狀",
        "04_閱卷資料", "05_證據資料", "06_筆錄",
        "07_法院通知或程序裁定", "08_判決書", "09_回執", "10_信件往返",
    ],
    "法律扶助案件": [
        "01_法扶資料", "02_開辦資料", "03_結案資料",
        "04_我方歷次書狀", "05_對方歷次書狀", "06_閱卷資料",
        "07_證據資料", "08_筆錄", "09_法院通知或程序裁定",
        "10_判決書", "11_回執", "12_信件往返",
    ],
    "指定辯護案件": [
        "01_我方歷次書狀", "02_對方歷次書狀", "03_結案資料",
        "04_閱卷資料", "05_證據資料", "06_筆錄",
        "07_法院通知或程序裁定", "08_判決書", "09_回執", "10_信件往返",
    ],
    "無償案件": [
        "01_無償委任資料", "02_我方歷次書狀", "03_對方歷次書狀",
        "04_結案資料", "05_閱卷資料", "06_證據資料", "07_筆錄",
        "08_法院通知或程序裁定", "09_判決書", "10_回執", "11_信件往返",
    ],
}

CATEGORY_FOLDER_MAP = {
    "一般案件": "一般案件",
    "法律扶助案件": "法扶案件",
    "指定辯護案件": "指定辯護案件",
    "無償案件": "無償案件",
}

TYPE_FOLDER_MAP = {
    "刑事": "刑事",
    "民事": "民事",
    "行政": "行政",
    "消費者債務清理": "消費者債務清理",
    "法律顧問": "法律顧問",
    "非訟": "非訟",
}

_ILLEGAL_CHARS = re.compile(r'[<>:"|?*\\/]')
_ATTACHED_CIVIL_TOKENS = ("刑事附帶民事", "附帶民事", "附民")


def sanitize_folder_name(name: str) -> str:
    return _ILLEGAL_CHARS.sub("_", name)


def build_case_folder_name(
    case_number: str,
    client_name: str,
    case_type: str = "",
    case_category: str = "",
    case_stage: str = "",
    case_reason: str = "",
) -> str:
    if case_type == "消費者債務清理" or "消費者債務清理" in (case_reason or ""):
        # 消費者債務清理派案，資料夾案由必須帶「更生」
        reason = case_reason or ""
        if "更生" not in reason and "清算" not in reason:
            reason = "更生"
        parts = [case_number, client_name, "消費者債務清理", reason]
    else:
        parts = [case_number, client_name, case_stage or case_type, case_reason]
    return sanitize_folder_name("-".join(filter(None, parts)))


def resolve_type_folder(case_type: str = "", case_stage: str = "", case_reason: str = "") -> str:
    """Resolve the second-level case folder from explicit type first, then safe fallbacks."""
    explicit = (case_type or "").strip()
    text = " ".join(filter(None, [explicit, (case_stage or "").strip(), (case_reason or "").strip()]))

    if any(token in text for token in _ATTACHED_CIVIL_TOKENS):
        return "民事"
    if "消費者債務清理" in explicit:
        return "消費者債務清理"
    if explicit in TYPE_FOLDER_MAP:
        return TYPE_FOLDER_MAP[explicit]
    for token in ("民事", "刑事", "行政", "非訟", "法律顧問"):
        if token in explicit:
            return TYPE_FOLDER_MAP[token]
    if "消費者債務清理" in text:
        return "消費者債務清理"
    return "其他"


def build_full_case_path(
    base_path: str,
    case_number: str,
    client_name: str,
    case_type: str = "",
    case_category: str = "",
    case_stage: str = "",
    case_reason: str = "",
) -> str:
    category_folder = CATEGORY_FOLDER_MAP.get(case_category, "其他案件")
    type_folder = resolve_type_folder(case_type, case_stage, case_reason)
    folder_name = build_case_folder_name(
        case_number,
        client_name,
        case_type,
        case_category,
        case_stage,
        case_reason,
    )
    return os.path.join(base_path, category_folder, type_folder, folder_name)


def create_folder_structure(base_path: str, case_category: str = "一般案件") -> dict:
    try:
        os.makedirs(base_path, exist_ok=True)
        base = Path(base_path)
        folders = SUBFOLDERS.get(case_category, SUBFOLDERS["一般案件"])
        created = []
        for name in folders:
            fp = base / name
            os.makedirs(fp, exist_ok=True)
            gitkeep = fp / ".gitkeep"
            if not gitkeep.exists():
                gitkeep.write_text(
                    f"# {name} - 建立於 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
                    encoding="utf-8",
                )
            created.append(name)
        return {"ok": True, "path": base_path, "subfolders": created}
    except OSError as e:
        return {"ok": False, "error": f"OSError: {e}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}
