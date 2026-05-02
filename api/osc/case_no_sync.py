# -*- coding: utf-8 -*-
"""
api/osc/case_no_sync.py — M12：通用案號同步模組。

涵蓋所有案件類型：刑事/民事/行政/家事/消債/非訟/法律顧問。
M10 的 supplement_core.case_no_updater 改為呼叫本模組的 thin wrapper。
M14：加股別偵測（extract_division_from_text / extract_division_from_notice）。
"""
from __future__ import annotations

import re
from typing import Callable

# ── 機關名 regex ───────────────────────────────────────────────────────────────
# 寬鬆匹配各類法院及檢察署
_INSTITUTION_RE = (
    r"(?:臺灣)?[一-鿿]{2,8}"
    r"(?:地方法院|地院|高等法院|高院|最高法院|"
    r"行政法院|智慧財產(?:及商業)?法院|"
    r"地方檢察署|高等檢察署|最高檢察署|"
    r"少年(?:及家事)?法院)"
)

# 字別：訴/上訴/偵/聲/家親聲/家調/司執/消債更/消債清/司執消債更/...
_CHAR_TYPE_RE = r"[一-鿿]{1,12}"

# 完整案號（機關+年度+字別+號碼）
_CASE_NO_FULL_RE = re.compile(
    rf"({_INSTITUTION_RE})?\s*"
    rf"(\d{{2,3}})年度({_CHAR_TYPE_RE})字第(\d{{1,6}})號"
)


def extract_court_case_no(filename: str) -> dict:
    """從檔名抽案號 + 機關 + 字別。

    Returns: {
        "raw_match": str,           # 完整匹配字串
        "case_no": str,             # 「113年度消債更字第695號」標準格式
        "institution": str | None,  # 「新北地方法院」(若有抽到)
        "year_roc": int,            # 113
        "char_type": str,           # 「消債更」
        "seq_no": int,              # 695
    }
    或 {"case_no": "", ...} 表示沒抽到
    """
    m = _CASE_NO_FULL_RE.search(filename)
    if not m:
        return {
            "case_no": "",
            "institution": None,
            "year_roc": 0,
            "char_type": "",
            "seq_no": 0,
            "raw_match": "",
        }
    inst = m.group(1)
    year = m.group(2)
    char_type = m.group(3)
    seq = m.group(4)
    if inst:
        inst = inst.strip()
    case_no = f"{year}年度{char_type}字第{seq}號"
    return {
        "raw_match": m.group(0).strip(),
        "case_no": case_no,
        "institution": inst if inst else None,
        "year_roc": int(year),
        "char_type": char_type,
        "seq_no": int(seq),
    }


# ── M14：股別偵測 ──────────────────────────────────────────────────────────────

_DIVISION_PATTERNS = [
    # Pattern 1: 「股別：X」「股別:X」「股別 X」（需要分隔符，X 可含「股」字尾）
    re.compile(r"股別[\s:：]+([一-鿿A-Z]{1,4}股?)"),
    # Pattern 1b: 「股別A股」「股別B」（僅 Latin 字母可無分隔符，常見於檔名）
    re.compile(r"股別([A-Z]{1,2}股?)"),
    # Pattern 2: 「承辦股：X」
    re.compile(r"承辦股[\s:：]+([一-鿿A-Z]{1,4}股?)"),
    # Pattern 3: 「X股 法官」（單漢字+股，避免抓到「案由乙股」等串聯詞）
    re.compile(r"([一-鿿]{1}股)\s*法官"),
    # Pattern 4: 「X股 書記官」（同上）
    re.compile(r"([一-鿿]{1}股)\s*書記官"),
]


def extract_division_from_text(text: str) -> str:
    """從 OCR 文字抽股別。

    回傳：「A股」「地股」「乙」等；找不到回空字串。
    多 pattern 命中時取第一個。
    """
    if not text or len(text) < 3:
        return ""
    text = text[:5000]  # 截斷防爆量
    for pat in _DIVISION_PATTERNS:
        m = pat.search(text)
        if m:
            div = m.group(1).strip()
            # 標準化：補上「股」字尾（如「A」→「A股」）
            if div and not div.endswith("股") and len(div) <= 2:
                div = div + "股"
            return div
    return ""


def extract_division_from_notice(notice_path: str) -> tuple[str, str]:
    """從一份裁定 PDF 抽股別。

    Returns: (division, source)
        division: 「A股」等，找不到 ""
        source: "filename" / "ocr" / "none"
    """
    import os
    filename = os.path.basename(notice_path)

    # Stage 1: 檔名直接抽
    div = extract_division_from_text(filename)
    if div:
        return (div, "filename")

    # Stage 2: OCR 內容抽（用既有 supplement_core.ruling_text_loader cache）
    try:
        import importlib.util
        import sys
        if "supplement_core.ruling_text_loader" in sys.modules:
            loader_mod = sys.modules["supplement_core.ruling_text_loader"]
        else:
            here = os.path.dirname(os.path.abspath(__file__))
            spec = importlib.util.spec_from_file_location(
                "supplement_core.ruling_text_loader",
                os.path.join(here, "..", "..", "src", "supplement_core", "ruling_text_loader.py"),
            )
            loader_mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(loader_mod)
        result = loader_mod.load_text(notice_path)
        text = result.get("text", "")
        div = extract_division_from_text(text)
        if div:
            return (div, "ocr")
    except Exception:
        pass

    return ("", "none")


_TYPE_KEYWORDS: dict[str, list[str]] = {
    "刑事": ["刑事", "偵", "訴", "上訴", "公訴", "自訴"],
    "民事": ["民事", "訴", "上", "司執", "強執"],
    "行政": ["行政", "訴", "判"],
    "家事": ["家事", "家親聲", "家調", "司家"],
    "消費者債務清理": ["消費者債務清理", "消債", "更生", "清算"],
    "非訟": ["非訟", "司聲", "司聲撤"],
}


def verify_filename_for_case(
    filename: str,
    *,
    party_name: str,
    case_type: str | None = None,
    institution_hint: str | None = None,
) -> dict:
    """三重驗證檔名是否屬於該案件。

    Returns: {
        "name_match": bool,         # 含當事人姓名（外文名取中文部分）
        "type_match": bool,         # 含案件類型關鍵字（None case_type 時跳過 = True）
        "institution_match": bool,  # 機關一致（None hint 時跳過 = True）
        "score": float,             # 0.0-1.0
        "extracted_institution": str | None,
    }
    """
    # ── 1. 姓名 ──────────────────────────────────────────────────────────────
    name_match = False
    if party_name:
        chinese = re.sub(r"[A-Za-z\s.\-]+", "", party_name).strip()
        if chinese and chinese in filename:
            name_match = True
        elif party_name in filename:
            name_match = True

    # ── 2. 程序類型 ───────────────────────────────────────────────────────────
    type_match = True  # 預設 True 若無 case_type
    if case_type:
        keywords = _TYPE_KEYWORDS.get(case_type, [])
        if keywords:
            type_match = any(kw in filename for kw in keywords)

    # ── 3. 機關 ──────────────────────────────────────────────────────────────
    inst_extracted = None
    extracted = extract_court_case_no(filename)
    if extracted["institution"]:
        inst_extracted = extracted["institution"]

    institution_match = True
    if institution_hint and inst_extracted:
        institution_match = (
            (institution_hint in inst_extracted)
            or (inst_extracted in institution_hint)
        )
    elif institution_hint and not inst_extracted:
        # 檔名無機關名但有 hint → 不扣分（檔名常省略）
        institution_match = True

    score = sum([name_match, type_match, institution_match]) / 3.0
    return {
        "name_match": name_match,
        "type_match": type_match,
        "institution_match": institution_match,
        "score": score,
        "extracted_institution": inst_extracted,
    }


def sync_case_no_from_notices(
    case_record: dict,
    notices: list[dict],
    *,
    min_score: float = 0.6,
    dry_run: bool = False,
) -> dict:
    """從 notices PDF 列表中找最新案號並更新 DB。

    case_record: {
        "id": int,                              # cases.id
        "case_dir": str,                        # 用於反查
        "client_name": str,
        "case_type": str | None,
        "current_court_case_no": str | None,
        "current_institution": str | None,
    }
    notices: ruling_picker.list_court_notices() 輸出格式

    Returns: {
        "case_id": int,
        "current_case_no": str,
        "new_case_no": str | None,
        "new_institution": str | None,
        "char_type": str | None,
        "source_pdf": str | None,
        "verification": dict,
        "updated": bool,
        "errors": list[str],
    }
    """
    # 1. 對每筆 notice 抽 + 驗證
    candidates = []
    for n in notices:
        filename = n.get("filename", "")
        if not filename:
            continue
        ext = extract_court_case_no(filename)
        if not ext["case_no"]:
            continue
        v = verify_filename_for_case(
            filename,
            party_name=case_record.get("client_name", ""),
            case_type=case_record.get("case_type"),
            institution_hint=case_record.get("current_institution"),
        )
        if v["score"] >= min_score:
            candidates.append({
                "notice": n,
                "extracted": ext,
                "verification": v,
            })

    if not candidates:
        return {
            "case_id": case_record.get("id"),
            "current_case_no": case_record.get("current_court_case_no", ""),
            "new_case_no": None,
            "new_institution": None,
            "char_type": None,
            "source_pdf": None,
            "verification": {},
            "updated": False,
            "errors": ["no_qualifying_notice"],
        }

    # 2. 取 mtime 最新（程序最新階段的裁定通常時間最近）
    candidates.sort(key=lambda c: c["notice"].get("mtime", 0), reverse=True)
    best = candidates[0]
    new_case_no = best["extracted"]["case_no"]
    new_inst = best["extracted"]["institution"] or best["verification"]["extracted_institution"]
    char_type = best["extracted"]["char_type"]

    # M14: 股別偵測
    division, division_source = extract_division_from_notice(
        best["notice"].get("path", best["notice"].get("filename", ""))
    )

    # 3. 與 DB 既有比較
    current_case_no = case_record.get("current_court_case_no", "") or ""
    if new_case_no == current_case_no:
        return {
            "case_id": case_record.get("id"),
            "current_case_no": current_case_no,
            "new_case_no": new_case_no,
            "new_institution": new_inst,
            "new_division": division,
            "division_source": division_source,
            "char_type": char_type,
            "source_pdf": best["notice"].get("path", best["notice"].get("filename", "")),
            "verification": best["verification"],
            "updated": False,
            "errors": ["already_current"],
        }

    # 4. 更新 DB（dry_run 時跳過）
    updated = False
    errors: list[str] = []
    if not dry_run:
        ok = _update_db(case_record["id"], new_case_no, new_inst, new_division=division)
        updated = ok
        if not ok:
            errors.append("db_update_failed")
    else:
        errors.append("dry_run")

    return {
        "case_id": case_record.get("id"),
        "current_case_no": current_case_no,
        "new_case_no": new_case_no,
        "new_institution": new_inst,
        "new_division": division,
        "division_source": division_source,
        "char_type": char_type,
        "source_pdf": best["notice"].get("path", best["notice"].get("filename", "")),
        "verification": best["verification"],
        "updated": updated,
        "errors": errors,
    }


def _update_db(
    case_id: int,
    new_case_no: str,
    new_inst: str | None,
    new_division: str | None = None,
) -> bool:
    """UPDATE cases SET court_case_number=..., court_case_no=...,
                       court_name=COALESCE(NULLIF(...,''), court_name),
                       court_division=COALESCE(NULLIF(...,''), court_division)
       WHERE id=?
    M14: 加 court_division 更新；若欄位不存在則容忍 schema 差異。
    """
    try:
        from api.osc.drafts import _osc_exec
    except ImportError:
        try:
            import api.server as _srv
            _osc_exec = _srv._osc_exec
        except (ImportError, AttributeError):
            return False

    # 先嘗試含 court_division 的 SQL（M14 欄位）
    try:
        _osc_exec(
            "UPDATE cases SET court_case_number=%s, court_case_no=%s, "
            "court_name=COALESCE(NULLIF(%s,''), court_name), "
            "court_division=COALESCE(NULLIF(%s,''), court_division) "
            "WHERE id=%s",
            (new_case_no, new_case_no, new_inst or "", new_division or "", case_id),
        )
        return True
    except Exception:
        pass

    # Fallback：不含 court_division（schema 沒此欄位時容忍）
    try:
        _osc_exec(
            "UPDATE cases SET court_case_number=%s, court_case_no=%s, "
            "court_name=COALESCE(NULLIF(%s,''), court_name) WHERE id=%s",
            (new_case_no, new_case_no, new_inst or "", case_id),
        )
        return True
    except Exception:
        return False


def sync_all_cases(
    *,
    case_filter: dict | None = None,
    progress_callback: Callable | None = None,
    dry_run: bool = True,
    max_cases: int | None = None,
) -> dict:
    """批次掃所有 cases，逐一 sync。

    case_filter: {"case_type": "刑事"} 等 SQL WHERE 條件
    progress_callback(stage, info)
    dry_run 預設 True（避免一次掃就動 DB）

    Returns: {
        "total": int, "synced": int, "skipped": int, "failed": int,
        "results": [...每件的 sync_case_no_from_notices output],
    }
    """
    # 從 cases 表撈
    try:
        from api.osc.drafts import _osc_exec
    except ImportError:
        return {
            "total": 0,
            "synced": 0,
            "skipped": 0,
            "failed": 0,
            "results": [],
            "errors": ["db_unavailable"],
        }

    sql = (
        "SELECT id, client_name, case_type, court_case_number, court_name, folder_path "
        "FROM cases WHERE folder_path != '' AND folder_path IS NOT NULL"
    )
    params: tuple = ()
    if case_filter and "case_type" in case_filter:
        sql += " AND case_type=%s"
        params = (case_filter["case_type"],)
    if max_cases:
        sql += f" LIMIT {int(max_cases)}"

    rows = _osc_exec(sql, params, fetch="all") or []
    total = len(rows)
    if progress_callback:
        progress_callback("start", {"total": total})

    results = []
    synced = skipped = failed = 0
    for idx, row in enumerate(rows, 1):
        if progress_callback:
            progress_callback(
                "progress",
                {"done": idx, "total": total, "client": row.get("client_name", "")},
            )
        try:
            from supplement_core import parse_case_meta, list_court_notices

            meta = parse_case_meta(row["folder_path"])
            notices = list_court_notices(meta)
            cr = {
                "id": row["id"],
                "case_dir": row["folder_path"],
                "client_name": row.get("client_name", ""),
                "case_type": row.get("case_type"),
                "current_court_case_no": row.get("court_case_number") or "",
                "current_institution": row.get("court_name") or "",
            }
            r = sync_case_no_from_notices(cr, notices, dry_run=dry_run)
            results.append(r)
            if r["updated"]:
                synced += 1
            elif "already_current" in r.get("errors", []):
                skipped += 1
            else:
                failed += 1
        except Exception as e:
            failed += 1
            results.append({"case_id": row.get("id"), "errors": [f"exception: {e}"]})

    if progress_callback:
        progress_callback("end", {})
    return {
        "total": total,
        "synced": synced,
        "skipped": skipped,
        "failed": failed,
        "results": results,
    }
