"""
case_meta.py — 消債補件模組 M1：解析案件資料夾路徑 → metadata dict
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional

from .exceptions import CaseNotFoundError, CategoryNotSupportedError

# ── 中文數字轉換 ──────────────────────────────────────────────────────────────

_CN_DIGITS = ["", "一", "二", "三", "四", "五", "六", "七", "八", "九"]
_CN_UNITS = ["", "十", "百"]

_CN_TO_INT_MAP = {
    "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
    "百": 100,
}


def _chinese_to_int(s: str) -> int:
    """將中文數字字串轉為 int（支援 1~99）。
    例：一→1, 十→10, 十一→11, 二十→20, 二十一→21。
    """
    s = s.strip()
    if not s:
        raise ValueError("Empty string")

    # 純「十」 = 10
    if s == "十":
        return 10

    # 以「十」切分
    if "十" in s:
        parts = s.split("十", 1)
        tens_str, ones_str = parts[0], parts[1]
        tens = _CN_TO_INT_MAP.get(tens_str, 1) if tens_str else 1
        ones = _CN_TO_INT_MAP.get(ones_str, 0) if ones_str else 0
        return tens * 10 + ones

    # 個位數
    if s in _CN_TO_INT_MAP:
        return _CN_TO_INT_MAP[s]

    raise ValueError(f"Cannot convert '{s}' to int")


def _int_to_chinese(n: int) -> str:
    """將 int 轉為中文數字字串（支援 1~99）。
    例：1→一, 10→十, 11→十一, 20→二十, 21→二十一。
    """
    if not (1 <= n <= 99):
        raise ValueError(f"n={n} out of range [1, 99]")

    if n < 10:
        return _CN_DIGITS[n]

    tens = n // 10
    ones = n % 10

    if tens == 1:
        prefix = "十"
    else:
        prefix = _CN_DIGITS[tens] + "十"

    if ones == 0:
        return prefix
    return prefix + _CN_DIGITS[ones]


# ── 動態子資料夾工具 ──────────────────────────────────────────────────────────

def _find_subfolder(sub_list: list[str], keyword: str) -> Optional[str]:
    """從子資料夾清單中，找出含 keyword 的第一個項目。"""
    for item in sub_list:
        if keyword in item:
            return item
    return None


# ── CATEGORY_FOLDER_MAP 反查 ──────────────────────────────────────────────────

def _folder_to_category(path_parts: list[str]) -> Optional[str]:
    """從路徑分段中反查 category key（CATEGORY_FOLDER_MAP 的 key）。

    CATEGORY_FOLDER_MAP = {
      "一般案件":     "一般案件",
      "法律扶助案件": "法扶案件",
      "指定辯護案件": "指定辯護案件",
      "無償案件":     "無償案件",
    }
    路徑中出現的是 value（資料夾名），反查回 key（系統內部名稱）。
    """
    try:
        from casper_ecosystem.law_firm_orchestrators.osc.folder_utils import CATEGORY_FOLDER_MAP
    except ImportError:
        # Fallback hardcoded mapping（不動 folder_utils）
        CATEGORY_FOLDER_MAP = {
            "一般案件": "一般案件",
            "法律扶助案件": "法扶案件",
            "指定辯護案件": "指定辯護案件",
            "無償案件": "無償案件",
        }

    # 反查：folder_value → category_key
    reverse_map = {v: k for k, v in CATEGORY_FOLDER_MAP.items()}

    for part in path_parts:
        if part in reverse_map:
            return reverse_map[part]
    return None


# ── brief_seq 掃描 ────────────────────────────────────────────────────────────

# Pattern A: 陳報[(（]中文數字[)）]狀  → 全/半形括號
_BRIEF_PATTERN_A = re.compile(r"陳報[（\(]([一二三四五六七八九十百]+)[）\)]狀")
# Pattern B: 陳報中文數字狀（無括號）
_BRIEF_PATTERN_B = re.compile(r"陳報([一二三四五六七八九十百]+)狀")


def _scan_brief_seq(brief_dir: str) -> set:
    """掃描書狀資料夾，回傳已使用的序號 set（整數）。

    過濾邏輯（依規格）：
    1. 白名單：只保留名稱含「陳報」的子資料夾
    2. 黑名單：排除含「執行」、名稱 == "新增資料夾"、含「存底」的資料夾
    3. 序號抽取（A > B > C 優先）：
       A. 陳報(一)狀 / 陳報（一）狀 → 括號中文數字
       B. 陳報一狀 → 無括號中文數字
       C. 陳報狀（無數字）→ 未編號
    4. 未編號（C）：若已有 A/B 編號，從 max(已編號)+1 往上補；否則從 1 往上補
    """
    if not os.path.isdir(brief_dir):
        return set()

    import unicodedata

    all_entries = os.listdir(brief_dir)

    # 只取子資料夾，不含隱藏目錄
    dirs = [
        unicodedata.normalize("NFC", e)
        for e in all_entries
        if os.path.isdir(os.path.join(brief_dir, e))
        and not e.startswith(".")
    ]

    # 第一道過濾（白名單）：只保留含「陳報」的資料夾
    dirs = [d for d in dirs if "陳報" in d]

    # 第二道過濾（黑名單）
    def _blacklisted(name: str) -> bool:
        if "執行" in name:
            return True
        if name == "新增資料夾":
            return True
        if "存底" in name:
            return True
        return False

    dirs = [d for d in dirs if not _blacklisted(d)]

    if not dirs:
        return set()

    numbered_set: set[int] = set()   # A 或 B 命中的序號
    n_unnumbered = 0                  # C 命中計數

    for name in dirs:
        # Pattern A 優先
        m_a = _BRIEF_PATTERN_A.search(name)
        if m_a:
            try:
                numbered_set.add(_chinese_to_int(m_a.group(1)))
            except ValueError:
                pass
            continue

        # Pattern B
        m_b = _BRIEF_PATTERN_B.search(name)
        if m_b:
            try:
                numbered_set.add(_chinese_to_int(m_b.group(1)))
            except ValueError:
                pass
            continue

        # Pattern C：無括號也無數字，視為未編號
        n_unnumbered += 1

    # 補入未編號序號
    if n_unnumbered > 0:
        base = max(numbered_set) if numbered_set else 0
        for i in range(1, n_unnumbered + 1):
            numbered_set.add(base + i)

    _try_db_merge(brief_dir, numbered_set)
    return numbered_set


def _try_db_merge(brief_dir: str, seq_set: set) -> None:
    """嘗試從 OSC drafts DB 補充序號（失敗靜默，不影響主流程）。"""
    try:
        # 未來 M3 實作，目前 DB 整合尚未建立
        pass
    except Exception:
        pass


# ── 主要 API ──────────────────────────────────────────────────────────────────

# 消費者債務清理案件資料夾名稱 regex
_CASE_DIR_PATTERN = re.compile(
    r"^(\d{4})-(\d{4})-(.+?)-消費者債務清理-(更生|清算)$"
)

# 本 v1 階段僅支援此 category
_SUPPORTED_CATEGORIES = {"法律扶助案件"}

# case_type 從路徑中辨識
_CASE_TYPES = [
    "消費者債務清理", "民事", "刑事", "行政", "法律顧問", "非訟",
]


def parse_case_meta(case_dir: str) -> dict:
    """解析案件資料夾路徑，回傳 metadata dict。

    參數：
        case_dir: 絕對路徑，如
            /.../01_案件/法扶案件/消費者債務清理/2025-0057-李思瑾-消費者債務清理-更生

    回傳 dict 鍵（v0.6 計畫 §4.1）：
        category, case_type, court, case_no,
        case_year_seq, parties, case_dir,
        subfolder_briefs, subfolder_court_notice, subfolder_evidence,
        subfolder_open_case, subfolder_archive,
        brief_seq_existing, brief_seq_next, procedure_default,
        sample_id

    raises:
        CaseNotFoundError: case_dir 不存在或不是目錄
        CategoryNotSupportedError: category 不在支援清單，或 M1 未支援的 category
    """
    import unicodedata

    # NFC 正規化（macOS Finder 使用 NFD）
    case_dir = unicodedata.normalize("NFC", str(case_dir))

    # 1. 驗證路徑存在
    if not os.path.isdir(case_dir):
        raise CaseNotFoundError(f"案件目錄不存在或不是目錄：{case_dir}")

    path_obj = Path(case_dir)
    dir_name = unicodedata.normalize("NFC", path_obj.name)
    path_parts = [unicodedata.normalize("NFC", p) for p in path_obj.parts]

    # 2. 解析 category（從路徑反查）
    category = _folder_to_category(path_parts)
    if category is None:
        raise CategoryNotSupportedError(
            f"路徑中找不到已知 category 資料夾，路徑：{case_dir}"
        )

    if category not in _SUPPORTED_CATEGORIES:
        raise CategoryNotSupportedError(
            f"M1 暫不支援 category='{category}'，目前僅支援 {_SUPPORTED_CATEGORIES}"
        )

    # 3. 取得 SUBFOLDERS
    try:
        from casper_ecosystem.law_firm_orchestrators.osc.folder_utils import SUBFOLDERS
    except ImportError:
        raise CategoryNotSupportedError("無法 import folder_utils.SUBFOLDERS")

    sub = SUBFOLDERS[category]

    # 4. 解析 case_type（從路徑部分辨識）
    case_type = ""
    for ct in _CASE_TYPES:
        if any(ct in part for part in path_parts):
            case_type = ct
            break

    # 5. 解析資料夾名稱
    m = _CASE_DIR_PATTERN.match(dir_name)
    if m:
        year = m.group(1)
        seq = m.group(2)
        parties_raw = m.group(3)
        procedure_default = m.group(4)
        case_year_seq = f"{year}-{seq}"
        parties = [parties_raw]
        sample_id = f"{year}-{seq}-{parties_raw}"
    else:
        # 非消費者債務清理格式，盡量解析
        case_year_seq = ""
        parties = []
        procedure_default = ""
        sample_id = dir_name

    # 6. 子資料夾名稱
    subfolder_briefs = _find_subfolder(sub, "我方歷次書狀") or ""
    subfolder_court_notice = _find_subfolder(sub, "法院通知或程序裁定") or ""
    subfolder_evidence = _find_subfolder(sub, "證據資料") or ""
    subfolder_open_case = _find_subfolder(sub, "開辦資料") or ""
    subfolder_archive = _find_subfolder(sub, "閱卷資料") or ""

    # 7. 書狀序號掃描
    brief_dir = os.path.join(case_dir, subfolder_briefs) if subfolder_briefs else ""
    brief_seq_existing = _scan_brief_seq(brief_dir) if brief_dir else set()
    brief_seq_next = (max(brief_seq_existing) + 1) if brief_seq_existing else 1

    return {
        "category": category,
        "case_type": case_type,
        "court": "",          # M3 由 LLM 從裁定文抽
        "case_no": "",        # M3 由 LLM 從裁定文抽
        "case_year_seq": case_year_seq,
        "parties": parties,
        "case_dir": case_dir,
        "subfolder_briefs": subfolder_briefs,
        "subfolder_court_notice": subfolder_court_notice,
        "subfolder_evidence": subfolder_evidence,
        "subfolder_open_case": subfolder_open_case,
        "subfolder_archive": subfolder_archive,
        "brief_seq_existing": brief_seq_existing,
        "brief_seq_next": brief_seq_next,
        "procedure_default": procedure_default,
        "sample_id": sample_id,
    }
