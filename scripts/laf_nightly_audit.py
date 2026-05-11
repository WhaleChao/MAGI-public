#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
法扶夜間巡檢 (LAF Nightly Audit)
==================================
夜間掃描所有法扶案件，產出早晨通知：

1. 漏填法扶案號的案件 → 嘗試從資料夾 01_法扶資料 補填
2. 未開辦但已逾期的案件
3. 已結案但未報結的案件
4. 已可開辦但尚未回報的案件
5. 可報結但尚未處理的案件

排程：由 casper_night_patrol.py 呼叫，或獨立以 LaunchAgent 於 02:30 執行。
通知：早上 07:00 透過 Telegram red_phone 推送。
"""

import json
import logging
import os
import re
import sys
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# Project root
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, os.path.join(PROJECT_ROOT, "casper_ecosystem", "law_firm_orchestrators"))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "skills", "legal"))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "skills", "osc-orchestrator"))

from api.runtime_paths import get_config_path
from api.case_path_mapper import default_case_roots, preferred_case_roots
from api.product_runtime import get_product_profile, resolve_laf_portal_targets

# 載入 .env
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(PROJECT_ROOT, ".env"))
except ImportError:
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("LAFNightlyAudit")
_SKIP_IMPORT_PROBES = "pytest" in sys.modules or os.getenv("MAGI_SKIP_IMPORT_PROBES") == "1"

# ── DB Failover: 獨立 process 需自行偵測，daemon 的 monitor 不會跑在這裡 ──
if not _SKIP_IMPORT_PROBES:
    try:
        from api.db_failover import probe_remote, _switch_to_local
        if not probe_remote(force=True):
            _switch_to_local()
            logger.info("DB Failover: 遠端 DB 不可達，已切換至本機")
    except Exception as _e:
        logger.warning("DB Failover 初始化跳過: %s", _e)

# ── NAS Mount: 獨立 process 需自行確保掛載 ──
if not _SKIP_IMPORT_PROBES:
    try:
        from api.nas_mount_guard import ensure_nas_mounts
        _nas_status = ensure_nas_mounts()
        logger.info("NAS mount 狀態: %s", _nas_status)
    except Exception as _e:
        logger.warning("NAS mount guard 跳過: %s", _e)

# NAS case root paths (macOS local mount)
_CASE_ROOTS = preferred_case_roots(include_closed=True)
_FALLBACK_CASE_ROOTS = default_case_roots(include_closed=True)
NAS_CASE_ROOT = _CASE_ROOTS[0] if _CASE_ROOTS else (_FALLBACK_CASE_ROOTS[0] if _FALLBACK_CASE_ROOTS else "")
LAF_CASE_ROOT = os.path.join(NAS_CASE_ROOT, "法扶案件")
# Y 槽歸檔路徑（SMB 掛載）
Y_DRIVE_ROOT = _CASE_ROOTS[1] if len(_CASE_ROOTS) > 1 else (_FALLBACK_CASE_ROOTS[1] if len(_FALLBACK_CASE_ROOTS) > 1 else "")
Y_DRIVE_LAF = os.path.join(Y_DRIVE_ROOT, "法扶案件")
REPORT_DIR = os.path.join(PROJECT_ROOT, "reports")
_DRAFT_STATE_FILE = os.path.join(REPORT_DIR, "_portal_draft_state.json")

# LAF case number regex
LAF_NO_RE = re.compile(r"\d{6,8}-[A-Za-z]-\d{3}")
_PRIORITY_LAF_FILENAME_KEYWORDS = ("開辦通知書", "接案通知書", "准予扶助證明書", "委任狀")
_PORTAL_FILE_CATEGORY_RULES = {
    "01_法扶資料": ["接案通知書", "委任狀", "法律扶助申請書", "案件概述單", "資力詢問表", "審查表", "准予扶助證明書", "預付酬金領款單", "結案回報書"],
    "03_結案資料": ["結案酬金領款單"],
    "02_開辦資料": ["附條件第二階段預付酬金領款單"],
}
_STATUS_TEXT_ALIASES = {
    "active": "進行中",
    "open": "進行中",
    "pending": "進行中",
    "processing": "進行中",
    "in_progress": "進行中",
    "進行中": "進行中",
    "處理中": "進行中",
    "辦理中": "進行中",
    "審理中": "進行中",
    "待處理": "進行中",
    "closed": "已結案",
    "completed": "已結案",
    "archived": "已結案",
    "已結案": "已結案",
}
_NAME_FIXES = str.maketrans({"餘": "余"})


# ─── DB Helper ─────────────────────────────────────────────────

def _get_db():
    """Get DatabaseManager instance."""
    try:
        try:
            from osc import DatabaseManager
        except Exception:
            import importlib.util as _ilu
            _osc_path = os.path.join(PROJECT_ROOT, "osc.py")
            _spec = _ilu.spec_from_file_location("magi_root_osc", _osc_path)
            _mod = _ilu.module_from_spec(_spec)
            _spec.loader.exec_module(_mod)
            DatabaseManager = _mod.DatabaseManager
        config_path = get_config_path("config.json")
        with open(config_path, encoding="utf-8") as f:
            config = json.load(f)

        for profile in config.get("mariadb_profiles", []):
            try:
                db = DatabaseManager(profile["config"])
                return db
            except Exception:
                continue

        # fallback: local
        try:
            from osc_headless.db import db_config_from_env
            c = db_config_from_env()
            return DatabaseManager({
                "host": c.host, "port": int(c.port),
                "user": c.user, "password": c.password, "database": c.database,
            })
        except Exception:
            pass
    except Exception as e:
        logger.error("DB connection failed: %s", e)
    return None


def _load_config() -> dict:
    try:
        config_path = get_config_path("config.json")
        with open(config_path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        logger.error("load config failed: %s", e)
        return {}


def _normalize_status_text(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    return _STATUS_TEXT_ALIASES.get(raw.lower(), raw)


def _normalize_person_name(value: str) -> str:
    text = re.sub(r"\s+", "", str(value or "").strip())
    return text.translate(_NAME_FIXES)


def _normalize_file_label(value: str) -> str:
    text = os.path.basename(str(value or "").strip())
    text = re.sub(r"^\d+\.\s*", "", text)
    text = re.sub(r"\.[^.]+$", "", text)
    text = text.replace("（", "(").replace("）", ")")
    text = re.sub(r"[\s_.\-()]+", "", text)
    return text.lower()


def _split_portal_file_labels(values: List[str]) -> List[str]:
    """Split portal attachment labels into real PDF filenames.

    LAF portal rows sometimes collapse an ordered list into one text node, for
    example: ``a.pdf2. b.pdf``.  Treating that as one expected filename creates
    a fake "missing file" alert after one file has already been downloaded.
    """
    filenames: List[str] = []
    seen: set[str] = set()
    for raw in values or []:
        text = re.sub(r"\s+", " ", str(raw or "")).strip()
        if not text:
            continue
        matches = re.findall(r"[^,，;；\n\r]*?\.pdf", text, flags=re.IGNORECASE)
        candidates = matches or [text]
        for item in candidates:
            name = re.sub(r"^\s*\d+\s*[.)、．]\s*", "", item).strip(" 、，;；")
            if not name:
                continue
            key = _normalize_file_label(name)
            if key and key not in seen:
                seen.add(key)
                filenames.append(name)
    return filenames


def _inspect_laf_number_candidates(case: dict) -> dict:
    """Inspect a case folder and return candidate legal-aid numbers."""
    folder = _to_mac_path((case.get("folder_path") or "").strip())
    info = {
        "case_root": folder,
        "priority_numbers": set(),
        "fallback_numbers": set(),
        "candidate_numbers": set(),
        "source_label": "",
    }
    if not folder or not os.path.isdir(folder):
        return info

    laf_data_dir = os.path.join(folder, "01_法扶資料")
    open_data_dir = os.path.join(folder, "02_開辦資料")
    priority_dirs = [p for p in (laf_data_dir, open_data_dir) if os.path.isdir(p)]

    for scan_dir in priority_dirs:
        try:
            for _, _, files in os.walk(scan_dir):
                for fn in files:
                    matches = LAF_NO_RE.findall(fn)
                    if not matches:
                        continue
                    if any(keyword in fn for keyword in _PRIORITY_LAF_FILENAME_KEYWORDS):
                        info["priority_numbers"].update(matches)
                    else:
                        info["fallback_numbers"].update(matches)
        except Exception:
            continue

    if not info["priority_numbers"] and not info["fallback_numbers"]:
        try:
            for _, _, files in os.walk(folder):
                for fn in files:
                    info["fallback_numbers"].update(LAF_NO_RE.findall(fn))
        except Exception:
            pass

    info["candidate_numbers"] = info["priority_numbers"] or info["fallback_numbers"]
    if info["priority_numbers"]:
        info["source_label"] = "開辦通知書/委任狀"
    elif info["fallback_numbers"]:
        info["source_label"] = "案件資料夾"
    return info


def _case_laf_number(case: dict) -> str:
    for key in ("legal_aid_number", "laf_case_no", "application_no"):
        value = str(case.get(key) or "").strip()
        if value:
            return value
    return ""


def _case_label(case: dict) -> str:
    return _case_laf_number(case) or str(case.get("case_number") or "").strip()


def _parse_case_date(value) -> Optional[date]:
    if not value:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d"):
        try:
            return datetime.strptime(text[:10], fmt).date()
        except Exception:
            continue
    return None


def _case_assignment_date(case: dict) -> Optional[date]:
    assigned = _parse_case_date(case.get("start_date"))
    if assigned:
        return assigned
    deadline = _parse_case_date(case.get("legal_aid_startup_deadline"))
    if deadline:
        # 開辦期限通常是派案後約 30 天；沒有派案日欄位時用期限反推，作為保底提醒。
        return deadline - timedelta(days=30)
    return None


def _is_truncated_laf_number(val: str) -> bool:
    """判斷法扶案號是否被截斷（如 '115-E-014' 少了日期部分，正確應為 '1150320-E-014'）"""
    import re
    v = (val or "").strip()
    if not v:
        return False
    # 截斷格式：3碼年-字母-序號（如 115-E-014），正常是 7碼日期-字母-序號
    return bool(re.match(r"^\d{3}-[A-Z]-\d+$", v))


def _update_case_laf_number(db, case: dict, laf_no: str) -> bool:
    case_id = case.get("id")
    if not case_id or not laf_no:
        return False
    # 也修正截斷的案號（如 '115-E-014' → '1150320-E-014'）
    existing = str(case.get("legal_aid_number") or "").strip()
    force_overwrite = _is_truncated_laf_number(existing) and len(laf_no) > len(existing)
    if force_overwrite:
        update_sql = """
            UPDATE `cases`
            SET `legal_aid_number` = %s,
                `laf_case_no` = %s,
                `application_no` = %s
            WHERE `id` = %s
        """
    else:
        update_sql = """
            UPDATE `cases`
            SET `legal_aid_number` = CASE WHEN `legal_aid_number` IS NULL OR `legal_aid_number` = '' THEN %s ELSE `legal_aid_number` END,
                `laf_case_no` = CASE WHEN `laf_case_no` IS NULL OR `laf_case_no` = '' THEN %s ELSE `laf_case_no` END,
                `application_no` = CASE WHEN `application_no` IS NULL OR `application_no` = '' THEN %s ELSE `application_no` END
            WHERE `id` = %s
        """
    try:
        db.execute_write(update_sql, (laf_no, laf_no, laf_no, case_id))
        if force_overwrite:
            _cn = case.get("client_name", "")
            logger.info("🔧 修正截斷案號: %s -> %s (%s)", existing, laf_no, _cn[:1] + "**" if _cn else "?")
        case["legal_aid_number"] = laf_no if (force_overwrite or not existing) else existing
        case["laf_case_no"] = case.get("laf_case_no") or laf_no
        case["application_no"] = case.get("application_no") or laf_no
        return True
    except Exception as e:
        logger.error("direct laf number sync failed for %s: %s", case.get("case_number"), e)
        return False


def _update_laf_status(db, case: dict, new_status: str) -> bool:
    """更新案件的 legal_aid_status。"""
    case_id = case.get("id")
    if not case_id or not new_status:
        return False
    old_status = case.get("legal_aid_status") or "(空)"
    try:
        db.execute_write(
            "UPDATE `cases` SET `legal_aid_status` = %s WHERE `id` = %s",
            (new_status, case_id),
        )
        logger.info("📝 DB 狀態更新: %s %s「%s」→「%s」",
                     case.get("case_number"), case.get("client_name"), old_status, new_status)
        case["legal_aid_status"] = new_status
        return True
    except Exception as e:
        logger.error("DB 狀態更新失敗 %s: %s", case.get("case_number"), e)
        return False


def _local_portal_case_matches(all_cases: List[dict], portal_case: dict) -> List[dict]:
    laf_no = (portal_case.get("case_number") or "").strip()
    portal_name = _normalize_person_name(portal_case.get("client_name") or "")
    portal_type = (portal_case.get("case_type") or "").strip()
    portal_reason = (portal_case.get("case_reason") or "").strip()

    exact = [
        case for case in all_cases
        if _case_laf_number(case) == laf_no
    ]
    if exact:
        return exact

    candidates = [
        case for case in all_cases
        if _normalize_person_name(case.get("client_name") or "") == portal_name
    ]
    if not candidates:
        return []

    typed = [case for case in candidates if (case.get("case_type") or "").strip() == portal_type]
    if typed:
        candidates = typed

    if portal_reason:
        reasoned = []
        for case in candidates:
            case_reason = (case.get("case_reason") or "").strip()
            if portal_reason == case_reason or portal_reason in case_reason or case_reason in portal_reason:
                reasoned.append(case)
        if reasoned:
            candidates = reasoned

    return candidates


def _collect_existing_portal_files(cases: List[dict]) -> List[str]:
    existing_files: List[str] = []
    for case in cases:
        case_root = _to_mac_path((case.get("folder_path") or "").strip())
        if not case_root or not os.path.isdir(case_root):
            continue
        for subfolder in _PORTAL_FILE_CATEGORY_RULES:
            target = os.path.join(case_root, subfolder)
            if not os.path.isdir(target):
                continue
            try:
                existing_files.extend(os.listdir(target))
            except Exception:
                continue
    return existing_files


def _classify_portal_file(filename: str) -> str:
    """根據 _PORTAL_FILE_CATEGORY_RULES 判斷檔案應歸入哪個子資料夾。

    Returns:
        子資料夾名稱（如 "01_法扶資料"），無法分類時回傳 "01_法扶資料" 作為預設。
    """
    for subfolder, keywords in _PORTAL_FILE_CATEGORY_RULES.items():
        if any(kw in filename for kw in keywords):
            return subfolder
    return "01_法扶資料"


def _find_missing_portal_files(expected_files: List[str], existing_files: List[str]) -> List[str]:
    normalized_existing = [_normalize_file_label(name) for name in existing_files]
    missing: List[str] = []

    for expected in _split_portal_file_labels(expected_files):
        expected_name = str(expected or "").strip()
        if not expected_name:
            continue
        keywords = [
            keyword
            for keyword_list in _PORTAL_FILE_CATEGORY_RULES.values()
            for keyword in keyword_list
            if keyword in expected_name
        ]
        expected_base = re.sub(r'_\d{7,8}-[A-Z]-\d{3}_\d+\.pdf$', '', expected_name)
        normalized_expected = _normalize_file_label(expected_base)
        found = False
        for idx, existing in enumerate(existing_files):
            normalized_current = normalized_existing[idx]
            if normalized_expected and (
                normalized_expected in normalized_current or normalized_current in normalized_expected
            ):
                found = True
                break
            if keywords and any(keyword in existing for keyword in keywords):
                found = True
                break
        if not found:
            missing.append(expected_name)
    return missing


def _make_laf_web_automation(*, log_prefix: str = "LAF-AUDIT"):
    from laf_automation_v2 import LAFWebAutomation

    config = _load_config()
    laf_cfg = config.get("laf") if isinstance(config.get("laf"), dict) else {}
    profile = get_product_profile("laf", config=config)
    portal = resolve_laf_portal_targets(config=config, profile=profile)

    username = (os.environ.get("MAGI_LAF_USERNAME") or laf_cfg.get("username") or "").strip()
    password = (os.environ.get("MAGI_LAF_PASSWORD") or laf_cfg.get("password") or "").strip()
    download_folder = str(laf_cfg.get("download_folder") or "./laf_downloads").strip() or "./laf_downloads"
    if not os.path.isabs(download_folder):
        download_folder = os.path.join(PROJECT_ROOT, download_folder)
    browser_profile_dir = str(laf_cfg.get("browser_profile_dir") or "").strip()
    headless = bool(laf_cfg.get("headless", True))
    base_url = str(portal.get("execute_base_url") or laf_cfg.get("base_url") or "").strip()
    mock_mode = bool(portal.get("execute_mock_mode"))

    if not username or not password:
        raise RuntimeError("missing_laf_credentials")

    return LAFWebAutomation(
        username=username,
        password=password,
        download_folder=download_folder,
        headless=headless,
        log_callback=lambda msg: logger.info("[%s] %s", log_prefix, msg),
        base_url=base_url,
        mock_mode=mock_mode,
        browser_profile_dir=browser_profile_dir,
    )


# ─── 1. 掃描缺法扶案號的案件 ──────────────────────────────────

def scan_missing_laf_numbers(db) -> List[dict]:
    """找出 case_category='法律扶助案件' 但 legal_aid_number 為空或被截斷的案件。"""
    query = """
        SELECT `id`, `case_number`, `client_name`, `case_type`, `case_reason`,
               `folder_path`, `legal_aid_status`, `status`,
               `legal_aid_number`, `laf_case_no`, `application_no`
        FROM `cases`
        WHERE (`case_category` = '法律扶助案件'
               OR `case_reason` LIKE '%法扶%'
               OR `case_reason` LIKE '%法律扶助%')
          AND COALESCE(`case_number`, '') <> '0000-0000'
          AND (
              (`legal_aid_number` IS NULL OR `legal_aid_number` = '')
              AND (`laf_case_no` IS NULL OR `laf_case_no` = '')
              AND (`application_no` IS NULL OR `application_no` = '')
              OR `legal_aid_number` REGEXP '^[0-9]{3}-[A-Z]-[0-9]+$'
          )
        ORDER BY `case_number` DESC
    """
    try:
        return db.fetch_all(query, (), as_dict=True) or []
    except Exception as e:
        logger.error("scan_missing_laf_numbers failed: %s", e)
        return []


def try_backfill_laf_number(db, case: dict) -> Optional[str]:
    """
    嘗試從案件資料夾的 01_法扶資料 中提取法扶案號並回填 DB。
    掃描 PDF/圖片檔名中的法扶案號格式。
    """
    inspected = _inspect_laf_number_candidates(case)
    chosen = set(inspected["candidate_numbers"])

    if len(chosen) == 1:
        laf_no = chosen.pop()
        try:
            if not _update_case_laf_number(db, case, laf_no):
                logger.error("backfill DB update returned false for %s -> %s", case["case_number"], laf_no)
                return None
            try:
                db.check_laf_case_exists(
                    laf_case_number=laf_no,
                    client_name=case.get("client_name", ""),
                    case_type=case.get("case_type", ""),
                    case_reason=case.get("case_reason", ""),
                )
            except Exception as index_error:
                logger.warning(
                    "法扶案號已回填，但 legal_aid_cases 輔助索引更新失敗 %s -> %s: %s",
                    case["case_number"], laf_no, index_error,
                )
            logger.info("✅ 補填法扶案號: %s -> %s (%s)%s",
                        case["case_number"], laf_no, case.get("client_name"),
                        f" [from {inspected['source_label']}]" if inspected["source_label"] else "")
            return laf_no
        except Exception as e:
            logger.error("backfill failed for %s: %s", case["case_number"], e)
    elif len(chosen) > 1:
        logger.warning("⚠️ %s 的法扶資料中有多個案號: %s，需人工確認", case["case_number"], chosen)

    return None


# ─── 1b. 接案清冊 Excel 比對補填 ─────────────────────────────

def _parse_case_list_excel(xlsx_path: str) -> List[dict]:
    """解析接案清冊 Excel，回傳 [{applyno, name, branch, procedure, reason}, ...]"""
    try:
        import openpyxl
        wb = openpyxl.load_workbook(xlsx_path, data_only=True)
        ws = wb.active
        rows_out = []
        for i, row in enumerate(ws.iter_rows(min_row=3, values_only=True), start=3):
            if not row or not row[1]:
                continue
            applyno = str(row[1]).strip()
            if not LAF_NO_RE.match(applyno):
                continue
            rows_out.append({
                "applyno": applyno,
                "name": str(row[2] or "").strip(),
                "branch": str(row[0] or "").strip(),
                "procedure": str(row[6] or "").strip(),
                "reason": str(row[7] or "").strip(),
            })
        wb.close()
        return rows_out
    except Exception as e:
        logger.error("解析接案清冊 Excel 失敗: %s", e)
        return []


def backfill_from_case_list(db, missing_cases: List[dict]) -> List[dict]:
    """
    從法扶接案清冊 Excel 比對缺案號的案件，自動補填。

    流程：
    1. 登入法扶 Portal → 匯出接案清冊 Excel
    2. 用當事人姓名比對 missing_cases
    3. 一人一案號 → 直接補填 DB
    4. 一人多案號 → 用案由 (case_reason) 交叉比對，仍無法判斷則記錄需人工確認

    Returns:
        [{"case_number": ..., "client_name": ..., "laf_no": ..., "source": "接案清冊"}, ...]
    """
    if not missing_cases:
        return []

    laf = None
    xlsx_path = None
    try:
        laf = _make_laf_web_automation(log_prefix="LAF-AUDIT-CASELIST")
        if not laf.login():
            logger.error("法扶網站登入失敗，無法匯出接案清冊")
            return []
        xlsx_path = laf.export_case_list_excel()
    except Exception as e:
        logger.error("匯出接案清冊失敗: %s", e)
        return []
    finally:
        if laf:
            try:
                laf.close()
            except Exception:
                pass

    if not xlsx_path:
        return []

    portal_entries = _parse_case_list_excel(xlsx_path)
    if not portal_entries:
        logger.warning("接案清冊 Excel 解析結果為空")
        return []
    logger.info("接案清冊共 %d 筆", len(portal_entries))

    # 建立 name → [entries] 索引
    from collections import defaultdict
    name_index: dict = defaultdict(list)
    for entry in portal_entries:
        name_index[entry["name"]].append(entry)

    backfilled = []
    for case in missing_cases:
        client = (case.get("client_name") or "").strip().translate(_NAME_FIXES)
        if not client:
            continue

        candidates = name_index.get(client, [])
        if not candidates:
            # 嘗試移除空白或異體字
            for pname, plist in name_index.items():
                if pname.replace(" ", "") == client.replace(" ", ""):
                    candidates = plist
                    break
        if not candidates:
            continue

        if len(candidates) == 1:
            chosen_no = candidates[0]["applyno"]
        else:
            # 多筆 → 用案由交叉比對
            case_reason = (case.get("case_reason") or "").strip()
            matched = [
                c for c in candidates
                if case_reason and (case_reason in c["reason"] or case_reason in c["procedure"]
                                    or c["reason"] in case_reason)
            ]
            if len(matched) == 1:
                chosen_no = matched[0]["applyno"]
            else:
                # 仍無法判斷 → 取最新（案號日期最大）
                # 但如果案由完全對不上就跳過
                if matched:
                    chosen_no = max(matched, key=lambda x: x["applyno"])["applyno"]
                else:
                    logger.warning(
                        "⚠️ %s %s 在接案清冊有 %d 筆，案由比對失敗，需人工確認: %s",
                        case["case_number"], client, len(candidates),
                        [c["applyno"] for c in candidates],
                    )
                    continue

        # 補填 DB
        try:
            if not _update_case_laf_number(db, case, chosen_no):
                logger.error("接案清冊補填 DB update returned false %s -> %s", case["case_number"], chosen_no)
                continue
            logger.info(
                "✅ 接案清冊補填: %s %s → %s",
                case["case_number"], client, chosen_no,
            )
            backfilled.append({
                "case_number": case["case_number"],
                "client_name": client,
                "laf_no": chosen_no,
                "source": "接案清冊",
            })
        except Exception as e:
            logger.error("接案清冊補填失敗 %s: %s", case["case_number"], e)

    # 清理暫存 Excel
    try:
        if xlsx_path and os.path.exists(xlsx_path):
            os.remove(xlsx_path)
    except Exception:
        pass

    return backfilled


def _is_placeholder_client_name(name: str) -> bool:
    s = str(name or "").strip()
    if not s:
        return True
    if s.startswith("-"):
        return True
    if any(c in s for c in ")(<>[]{}!@#$%^&*+=|\\;:\"'?/`~"):
        return True
    if any(token in s for token in ("案情", "文件", "卷宗", "附件", "信件", "資料夾")):
        return True
    return len(s) > 30


# ─── 2. 掃描開辦/結案狀態 ─────────────────────────────────────

def scan_laf_reporting_status(db) -> dict:
    """
    掃描所有法扶案件的開辦/結案回報狀態。

    Returns:
        {
            "not_started": [...],     # 未開辦且已逾期
            "can_go_live": [...],     # 有開辦資料，可回報開辦但還沒
            "pending_close": [...],   # 已結案但尚未報結
            "can_close": [...],       # 有判決書，可報結但還沒
            "progress_overdue": [...],# 進行中且派案超過 18 個月，需確認進度回報
            "all_cases": [...],       # 所有法扶案件
        }
    """
    query = """
        SELECT `id`, `case_number`, `client_name`, `case_type`, `case_reason`,
               `status`, `folder_path`, `legal_aid_number`, `laf_case_no`, `application_no`, `legal_aid_status`,
               `legal_aid_startup_deadline`, `start_date`, `end_date`
        FROM `cases`
        WHERE (`case_category` = '法律扶助案件'
               OR `case_reason` LIKE '%法扶%'
               OR `case_reason` LIKE '%法律扶助%')
        ORDER BY `case_number` DESC
    """
    try:
        all_cases = db.fetch_all(query, (), as_dict=True) or []
    except Exception as e:
        logger.error("scan_laf_reporting_status failed: %s", e)
        return {"not_started": [], "can_go_live": [], "pending_close": [], "can_close": [], "progress_overdue": [], "all_cases": []}

    today = date.today()
    not_started = []      # 未開辦且已逾期
    can_go_live = []      # 有開辦資料可回報
    pending_close = []    # DB 狀態=結案 但法扶未報結
    can_close = []        # 有判決書可報結
    progress_overdue = [] # 進行中且派案超過提醒門檻
    try:
        progress_due_days = int(os.environ.get("MAGI_LAF_PROGRESS_DUE_DAYS", "548") or "548")
    except Exception:
        progress_due_days = 548

    for case in all_cases:
        laf_status = _normalize_status_text(case.get("legal_aid_status") or "")
        osc_status = _normalize_status_text(case.get("status") or "")
        folder = (case.get("folder_path") or "").strip()
        deadline_raw = case.get("legal_aid_startup_deadline")
        laf_no = _case_laf_number(case)

        # 轉換路徑
        mac_folder = _to_mac_path(folder)

        has_go_live_notice = False
        has_go_live_poa = False
        if laf_status in ("未開辦", "", None) and mac_folder:
            has_go_live_notice = _folder_has_file(
                mac_folder,
                "02_開辦資料",
                ("開辦通知書", "接案通知書", "准予扶助證明書"),
            )
            has_go_live_poa = _folder_has_file(mac_folder, "02_開辦資料", ("委任狀",))
            if not has_go_live_notice:
                has_go_live_notice = _folder_has_file(
                    mac_folder,
                    "01_法扶資料",
                    ("開辦通知書", "接案通知書", "准予扶助證明書"),
                )
            if not has_go_live_poa:
                has_go_live_poa = _folder_has_file(mac_folder, "01_法扶資料", ("委任狀",))

        # A. 未開辦且已逾期
        if laf_status in ("未開辦", "", None) and laf_no and not (has_go_live_notice and has_go_live_poa):
            if deadline_raw:
                try:
                    dl = deadline_raw if isinstance(deadline_raw, date) else datetime.strptime(str(deadline_raw)[:10], "%Y-%m-%d").date()
                    if dl <= today:
                        not_started.append({
                            **case,
                            "days_overdue": (today - dl).days,
                        })
                except Exception:
                    pass

        # B. 有開辦資料但尚未回報開辦
        if (
            laf_status in ("未開辦", "", None)
            and has_go_live_notice
            and has_go_live_poa
            and not _is_placeholder_client_name(case.get("client_name") or "")
        ):
            can_go_live.append(case)

        # C. OSC 已結案但 DB 法扶狀態未標記已結案（需上法扶網站確認是否已報結）
        _skip_pending = ("已結案", "已報結", "結案", "已結案，待報結", "已結案，待送出", "已報結（待轉入）")
        if osc_status in ("結案", "已結案") and laf_status not in _skip_pending:
            pending_close.append(case)

        # D. 有判決書/處分書，可報結但還沒
        #    包含「已結案，待報結」狀態（DB 標記已結案但尚未向法扶回報）
        _closeable_statuses = ("進行中", "已開辦", "待報結", "已結案，待報結")
        if laf_status in _closeable_statuses and mac_folder:
            has_judgment = _folder_has_any_file(mac_folder, "10_判決書")
            if has_judgment:
                can_close.append(case)

        # E. 進行中案件：派案/建案超過 18 個月仍未結案，應提醒確認進度回報
        if laf_status in ("進行中", "已開辦") and osc_status not in ("結案", "已結案"):
            assigned = _case_assignment_date(case)
            if assigned:
                days_since = (today - assigned).days
                if days_since >= progress_due_days:
                    progress_overdue.append({
                        **case,
                        "assignment_date": assigned.isoformat(),
                        "days_since_assignment": days_since,
                        "progress_due_days": progress_due_days,
                    })

    return {
        "not_started": not_started,
        "can_go_live": can_go_live,
        "pending_close": pending_close,
        "can_close": can_close,
        "progress_overdue": progress_overdue,
        "all_cases": all_cases,
    }


def _is_dir_ok(path: str) -> bool:
    """os.path.isdir + 實際 listdir 測試，防 stale SMB mount 誤判。"""
    try:
        if not os.path.isdir(path):
            return False
        os.listdir(path)
        return True
    except OSError:
        return False


def _to_mac_path(folder: str) -> str:
    """Convert Windows Z:/Y: path to macOS local path.

    若 Z: 對應的 active 路徑不存在（案件已移至結案資料夾），
    自動 fallback 到 Y_DRIVE_ROOT 下相同的相對路徑。
    """
    if not folder:
        return ""
    f = folder.replace("\\", "/")
    if f.startswith("Z:") or f.startswith("z:"):
        # Z:/lumi63181107/01_案件/法扶案件/SomeFolder → NAS_CASE_ROOT/法扶案件/SomeFolder
        parts = f.split("/")
        for i, p in enumerate(parts):
            if p == "01_案件":
                active_path = os.path.join(NAS_CASE_ROOT, *parts[i + 1:])
                if _is_dir_ok(active_path):
                    return active_path
                # 案件已移至結案資料夾：嘗試 Y_DRIVE_ROOT 下同一相對路徑
                if Y_DRIVE_ROOT:
                    closed_path = os.path.join(Y_DRIVE_ROOT, *parts[i + 1:])
                    if _is_dir_ok(closed_path):
                        logger.debug("_to_mac_path: 案件已在結案資料夾: %s", closed_path)
                        return closed_path
                return active_path  # 回傳原路徑供上層判斷（不靜默丟棄）
        return ""
    if f.startswith("Y:") or f.startswith("y:"):
        # Canonical closed-case path -> local closed-case root
        rel = re.sub(r"^[Yy]:/lumi/03_工作資料/10_結案/", "", f)
        return os.path.join(Y_DRIVE_ROOT, rel)
    if _is_dir_ok(f):
        return f
    return ""


def _folder_has_file(mac_folder: str, subfolder: str, keywords: tuple) -> bool:
    """Check if a subfolder contains files matching any keyword."""
    target = os.path.join(mac_folder, subfolder)
    if not os.path.isdir(target):
        return False
    try:
        for fn in os.listdir(target):
            if any(k in fn for k in keywords):
                return True
    except Exception:
        pass
    return False


def _folder_has_any_file(mac_folder: str, subfolder: str) -> bool:
    """Check if a subfolder has any non-hidden files."""
    target = os.path.join(mac_folder, subfolder)
    if not os.path.isdir(target):
        return False
    try:
        for fn in os.listdir(target):
            if not fn.startswith("."):
                return True
    except Exception:
        pass
    return False


# ─── 2a-2. 可報結案件自動暫存 ─────────────────────────────────

def _run_closing_drafts(max_cases: int = 5) -> dict:
    """呼叫 LAFOrchestrator.run_closing_drafts() 自動暫存報結資料。"""
    try:
        from laf_orchestrator import LAFOrchestrator
        orch = LAFOrchestrator(dry_run=False)
        result = orch.run_closing_drafts(max_cases=max_cases)
        logger.info("報結自動暫存: %s", result)
        return result
    except Exception as e:
        logger.error("報結自動暫存失敗: %s", e)
        return {"ok": False, "error": str(e), "scanned": 0, "processed": 0, "items": []}


def _run_go_live_drafts(
    can_go_live_cases: List[dict],
    max_cases: int = 3,
    db=None,
    suppress_notify: bool = False,
) -> dict:
    """對已有開辦通知書+委任狀的案件，自動填寫開辦表單並截圖（不送出）。
    若 portal 找不到開辦表單（已被開辦），自動回寫 DB 為「進行中」。
    """
    try:
        from laf_orchestrator import LAFOrchestrator
        orch = LAFOrchestrator(dry_run=False)
        results = []
        ok_count = 0
        for case in can_go_live_cases[:max_cases]:
            laf_no = str(case.get("legal_aid_number") or "").strip()
            osc_no = str(case.get("case_number") or "").strip()
            client = str(case.get("client_name") or "").strip()
            if not laf_no and not client:
                continue
            display = f"{client}（{laf_no or osc_no}）" if client else (laf_no or osc_no)
            logger.info("📋 Auto go_live draft: %s", display)
            try:
                r = orch.execute_portal_action_draft(
                    action="go_live",
                    laf_case_number=laf_no,
                    case_number=osc_no,
                    client_name=client,
                    suppress_notify=suppress_notify,
                )
                ok = bool(r.get("ok"))
                err = str(r.get("error") or "")
                results.append({
                    "ok": ok,
                    "laf_case_number": laf_no,
                    "osc_case_number": osc_no,
                    "client_name": client,
                    "error": err,
                })
                if ok:
                    ok_count += 1
                elif err == "portal_draft_failed":
                    # Do not infer "already opened" from a generic portal failure.
                    # The same error is used for login timeout, DOM drift, upload
                    # rejection, and missing buttons.  Auto-updating DB here can
                    # hide a real pending go_live case.
                    logger.warning("⚠️ %s 開辦暫存失敗；不自動更新 DB，需人工確認 portal 狀態", display)
            except Exception as e:
                logger.error("go_live draft failed for %s: %s", display, e)
                results.append({
                    "ok": False,
                    "laf_case_number": laf_no,
                    "client_name": client,
                    "error": str(e),
                })
        return {"ok": ok_count > 0, "total": len(can_go_live_cases), "processed": ok_count, "items": results}
    except Exception as e:
        logger.error("開辦自動暫存失敗: %s", e)
        return {"ok": False, "error": str(e), "total": 0, "processed": 0, "items": []}


# ─── 2b. 法扶網站報結狀態驗證 ─────────────────────────────────

def verify_portal_closing_status(pending_cases: List[dict], db=None) -> dict:
    """
    上法扶律師系統 (lawyer.laf.org.tw) 查詢每件「待報結」案件
    在結案清單/撤回清單上的實際狀態。

    若提供 db，會自動回寫 legal_aid_status（已轉入→已報結，待轉入→已報結（待轉入））。

    Returns:
        {
            "drafted":    [{"case": ..., "portal_info": ...}, ...],  # 暫存（MAGI 已處理）
            "approved":   [{"case": ..., "portal_info": ...}, ...],  # 已轉入（法扶已通過）
            "unreported": [{"case": ..., "portal_info": ...}, ...],  # 真正未報結
            "error":      [{"case": ..., "error": ...}, ...],
        }
    """
    result = {"drafted": [], "approved": [], "pending_transfer": [], "unreported": [], "error": []}

    if not pending_cases:
        return result

    # 篩選有法扶案號的案件
    cases_with_no = [c for c in pending_cases if _case_laf_number(c)]
    if not cases_with_no:
        logger.info("待報結案件均無法扶案號，跳過 portal 驗證")
        result["unreported"] = [{"case": c, "portal_info": "缺法扶案號"} for c in pending_cases]
        return result

    laf = None
    try:
        laf = _make_laf_web_automation(log_prefix="LAF-AUDIT-CLOSING")
        if not laf.login():
            logger.error("法扶網站登入失敗，無法驗證報結狀態")
            result["error"] = [{"case": c, "error": "login_failed"} for c in cases_with_no]
            return result

        for case in cases_with_no:
            laf_no = _case_laf_number(case)
            try:
                portal = laf.query_closing_status(laf_no)
                closing = portal.get("closing", {})
                withdrawal = portal.get("withdrawal", {})

                # 判斷狀態：結案或撤回，任一有紀錄就算
                # 優先序：待轉入 > 已轉入 > 暫存 > 有紀錄
                found_status = ""
                found_type = ""
                if closing.get("found"):
                    found_status = closing.get("status", "")
                    found_type = "結案"
                if withdrawal.get("found"):
                    ws = withdrawal.get("status", "")
                    if not found_status or ws in ("已轉入", "待轉入"):
                        found_status = ws
                        found_type = "撤回"

                entry = {
                    "case": case,
                    "portal_info": f"{found_type}: {found_status}" if found_status else "未找到",
                    "closing_status": closing.get("status", ""),
                    "withdrawal_status": withdrawal.get("status", ""),
                }

                if found_status == "已轉入":
                    result["approved"].append(entry)
                    logger.info("✅ %s %s → 已轉入（%s）", laf_no, (case.get("client_name", "")[:1] + "**"), found_type)
                    # 自動回寫 DB：已轉入 → 已報結
                    if db and case.get("id"):
                        _update_laf_status(db, case, "已報結")
                elif found_status == "待轉入":
                    # 已送件，法扶審核處理中，不需再操作
                    result["pending_transfer"].append(entry)
                    logger.info("⏳ %s %s → 待轉入（%s）", laf_no, (case.get("client_name", "")[:1] + "**"), found_type)
                    # 自動回寫 DB：待轉入 → 已報結（待轉入）
                    if db and case.get("id"):
                        _update_laf_status(db, case, "已報結（待轉入）")
                elif found_status == "暫存":
                    result["drafted"].append(entry)
                    logger.info("📝 %s %s → 暫存（%s），需人工確認送出", laf_no, (case.get("client_name", "")[:1] + "**"), found_type)
                    # 自動回寫 DB：暫存 → 已結案，待送出（提醒律師上網確認送出）
                    if db and case.get("id"):
                        _update_laf_status(db, case, "已結案，待送出")
                elif found_status:
                    # "有紀錄" 但狀態不明 — 保守起見列為需確認
                    result["drafted"].append(entry)
                    logger.info("📝 %s %s → %s（%s，需確認）", laf_no, (case.get("client_name", "")[:1] + "**"), found_status, found_type)
                else:
                    result["unreported"].append(entry)
                    logger.info("⚠️ %s %s → 確認未報結", laf_no, (case.get("client_name", "")[:1] + "**"))

            except Exception as e:
                logger.error("查詢 %s 狀態失敗: %s", laf_no, e)
                result["error"].append({"case": case, "error": str(e)})

    except Exception as e:
        logger.error("portal 驗證初始化失敗: %s", e)
        result["error"] = [{"case": c, "error": str(e)} for c in cases_with_no]
    finally:
        if laf:
            try:
                laf.close()
            except Exception:
                pass

    return result


# ─── 2c. 法扶官網新文件掃描 ──────────────────────────────────

def _move_downloaded_to_case_folder(
    downloaded_paths: List[str],
    case_root: str,
) -> Tuple[List[str], List[str]]:
    """將下載的檔案移動到案件對應子資料夾。

    Returns:
        (moved_files, failed_files) — 各自包含檔案名稱
    """
    import shutil
    import zipfile
    moved, failed = [], []

    def _move_regular_file(src_path: str, display_name: str) -> bool:
        subfolder = _classify_portal_file(display_name)
        target_dir = os.path.join(case_root, subfolder)
        os.makedirs(target_dir, exist_ok=True)
        dest = os.path.join(target_dir, display_name)
        if os.path.abspath(src_path) == os.path.abspath(dest):
            logger.info("  ⏭️ 檔案已在正確位置: %s", display_name)
            return True
        if os.path.exists(dest):
            logger.info("  ⏭️ 檔案已存在，跳過: %s", display_name)
            try:
                os.remove(src_path)
            except Exception:
                pass
            return True
        shutil.move(src_path, dest)
        logger.info("  ✅ 已移至 %s/%s", subfolder, display_name)
        return True

    for fpath in downloaded_paths:
        fname = os.path.basename(fpath)
        try:
            if zipfile.is_zipfile(fpath):
                with zipfile.ZipFile(fpath) as zf:
                    for member in zf.infolist():
                        if member.is_dir():
                            continue
                        member_name = os.path.basename(member.filename)
                        if not member_name:
                            continue
                        subfolder = _classify_portal_file(member_name)
                        target_dir = os.path.join(case_root, subfolder)
                        os.makedirs(target_dir, exist_ok=True)
                        dest = os.path.join(target_dir, member_name)
                        if os.path.exists(dest):
                            logger.info("  ⏭️ ZIP 內檔案已存在，跳過: %s", member_name)
                            moved.append(member_name)
                            continue
                        with zf.open(member) as src, open(dest, "wb") as out:
                            shutil.copyfileobj(src, out)
                        moved.append(member_name)
                        logger.info("  ✅ ZIP 展開至 %s/%s", subfolder, member_name)
                _move_regular_file(fpath, fname)
                continue

            if _move_regular_file(fpath, fname):
                moved.append(fname)
        except Exception as e:
            failed.append(fname)
            logger.warning("  ⚠️ 移動失敗 %s: %s", fname, e)
    return moved, failed


def scan_portal_new_files(all_cases: List[dict]) -> List[dict]:
    """
    上法扶律師系統的下載頁面，取得所有待下載案件，
    偵測缺檔後自動下載並歸檔到正確子資料夾。

    Returns:
        [{"case_number": ..., "client_name": ..., "laf_no": ...,
          "file_count": N, "auto_downloaded": M, ...}, ...]
    """
    laf = None
    new_files_found = []

    try:
        laf = _make_laf_web_automation(log_prefix="LAF-AUDIT-DOWNLOAD")
        if not laf.login():
            logger.error("法扶網站登入失敗，無法掃描新文件")
            return []

        # 取得官網下載頁上所有待下載案件
        downloadable = laf.get_downloadable_cases()
        logger.info("法扶官網待下載案件: %d 筆", len(downloadable))

        for dc in downloadable:
            laf_no = (dc.get("case_number") or "").strip()
            client = dc.get("client_name", "")
            file_list = _split_portal_file_labels(dc.get("file_list") or [])

            if not laf_no:
                continue

            matched_cases = _local_portal_case_matches(all_cases, dc)
            existing_files = _collect_existing_portal_files(matched_cases)
            missing_files = _find_missing_portal_files(file_list, existing_files)

            if not missing_files:
                logger.info("📁 %s %s — 官網列出 %d 份，但本地已齊全", laf_no, client, len(file_list))
                continue

            logger.info(
                "📥 %s %s — %d 份文件待下載%s: %s",
                laf_no, client, len(missing_files),
                " (新案件)" if not matched_cases else "",
                ", ".join(str(f) for f in missing_files[:5]),
            )

            # ── 嘗試自動下載 ──────────────────────────────────
            auto_downloaded_count = 0
            still_missing = list(missing_files)

            if matched_cases:
                # 取第一個有效的 case_root 作為歸檔目標
                case_root = ""
                for mc in matched_cases:
                    cr = _to_mac_path((mc.get("folder_path") or "").strip())
                    if cr and os.path.isdir(cr):
                        case_root = cr
                        break

                if case_root:
                    try:
                        logger.info("  🔽 嘗試自動下載 %s 的檔案...", laf_no)
                        downloaded_paths = laf.download_case_files(
                            case_number=laf_no,
                            row_element=dc.get("row_element"),
                        )
                        if downloaded_paths:
                            moved, move_failed = _move_downloaded_to_case_folder(
                                downloaded_paths, case_root,
                            )
                            auto_downloaded_count = len(moved)
                            logger.info(
                                "  📦 %s: 下載 %d 份，歸檔 %d 份，失敗 %d 份",
                                laf_no, len(downloaded_paths), len(moved), len(move_failed),
                            )
                            # 重新比對：下載後哪些仍缺
                            refreshed_existing = _collect_existing_portal_files(matched_cases)
                            still_missing = _find_missing_portal_files(file_list, refreshed_existing)
                        else:
                            logger.warning("  ⚠️ %s: download_case_files 未回傳檔案", laf_no)
                    except Exception as e:
                        logger.error("  ❌ %s 自動下載失敗（不影響巡檢）: %s", laf_no, e)
                else:
                    logger.warning("  ⚠️ %s: 找到配對案件但資料夾不存在，跳過下載", laf_no)
            else:
                logger.info("  ⚠️ %s: 新案件無本地資料夾，跳過下載", laf_no)

            # 只有仍缺檔才加入通知
            if still_missing or auto_downloaded_count > 0:
                new_files_found.append({
                    "case_number": laf_no,
                    "client_name": client,
                    "laf_no": laf_no,
                    "file_count": len(file_list),
                    "new_count": len(still_missing),
                    "auto_downloaded": auto_downloaded_count,
                    "missing_files": still_missing[:10],
                    "is_new_case": not matched_cases,
                })

    except ImportError:
        logger.warning("無法匯入 LAFWebAutomation，跳過官網文件掃描")
    except Exception as e:
        logger.error("法扶官網文件掃描失敗: %s", e)
    finally:
        if laf:
            try:
                laf.close()
            except Exception:
                pass

    return new_files_found


# ─── 2d. Portal 暫存/待處理全清單掃描 ────────────────────────────

def _load_draft_state() -> dict:
    """讀取上次巡檢的 portal 暫存狀態。"""
    try:
        if os.path.exists(_DRAFT_STATE_FILE):
            with open(_DRAFT_STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def _save_draft_state(state: dict):
    """儲存本次巡檢的 portal 暫存狀態。"""
    os.makedirs(REPORT_DIR, exist_ok=True)
    try:
        with open(_DRAFT_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning("儲存 draft state 失敗: %s", e)


def _sanitize_portal_pending_items(items: List[dict], label: str = "") -> List[dict]:
    """Drop Portal form/help rows that do not represent a real LAF case."""
    clean: List[dict] = []
    dropped = 0
    for raw in items or []:
        if not isinstance(raw, dict):
            dropped += 1
            continue
        item = dict(raw)
        applyno = str(item.get("applyno") or "").strip()
        row_text = str(item.get("row_text") or "")
        if not LAF_NO_RE.fullmatch(applyno):
            match = LAF_NO_RE.search(row_text)
            applyno = match.group(0) if match else ""
        if not applyno:
            dropped += 1
            continue
        item["applyno"] = applyno
        clean.append(item)
    if dropped:
        suffix = f"（{label}）" if label else ""
        logger.warning("Portal 暫存掃描%s已忽略 %d 列無案號表單/說明文字", suffix, dropped)
    return clean


def scan_portal_pending_drafts(db=None) -> dict:
    """
    登入法扶 Portal，掃描案件狀態區與既有 workflow 清單頁，
    找出仍有暫存/待處理的案件。

    與上次巡檢結果比對：
    - 上次有、這次消失 → 已送出（auto_resolved），自動回寫 DB
    - 這次仍在 → 繼續提醒

    Returns:
        {
            "closing_drafts":   [{"applyno": ..., "status": ..., "row_text": ...}],
            "case_status_drafts": [...],
            "condition_pending": [...],
            "go_live_pending":   [...],
            "progress_pending":  [...],
            "auto_resolved":     [{"applyno": ..., "workflow": ..., "label": ...}],
            "error": str or None,
        }
    """
    result = {
        "closing_drafts": [],
        "case_status_drafts": [],
        "condition_pending": [],
        "go_live_pending": [],
        "progress_pending": [],
        "auto_resolved": [],
        "error": None,
    }

    prev_state = _load_draft_state()

    laf = None
    try:
        laf = _make_laf_web_automation(log_prefix="LAF-AUDIT-DRAFTS")
        if not laf.login():
            logger.error("法扶網站登入失敗，無法掃描暫存清單")
            result["error"] = "login_failed"
            return result

        portal = laf.query_pending_drafts_all()

        # 案件狀態區：統一來源，專門提醒仍停留在「暫存」的回報。
        result["case_status_drafts"] = [
            it for it in _sanitize_portal_pending_items(portal.get("case_status", []), "案件狀態區")
            if it.get("status") == "暫存"
        ]

        # 結案提醒優先採用「案件狀態區 > 回報狀態=暫存」的結案回報；
        # 若 portal 版本異動導致抓不到，再退回舊的結案清單頁。
        result["closing_drafts"] = [
            it for it in result["case_status_drafts"]
            if it.get("reply_type") == "結案回報"
        ]
        if not result["closing_drafts"]:
            result["closing_drafts"] = [
                it for it in _sanitize_portal_pending_items(portal.get("closing", []), "結案")
                if it.get("status") == "暫存"
            ]
        result["condition_pending"] = _sanitize_portal_pending_items(portal.get("condition", []), "二階段")
        result["go_live_pending"] = _sanitize_portal_pending_items(portal.get("go_live", []), "開辦")
        result["progress_pending"] = _sanitize_portal_pending_items(portal.get("progress", []), "進度")

    except Exception as e:
        logger.error("Portal 暫存掃描失敗: %s", e)
        result["error"] = str(e)
        return result
    finally:
        if laf:
            try:
                laf.close()
            except Exception:
                pass

    # 與上次比對，找出已自動消失（已送出）的案件
    cur_closing = {it["applyno"] for it in result["closing_drafts"] if it.get("applyno")}
    cur_condition = {it["applyno"] for it in result["condition_pending"] if it.get("applyno")}
    cur_go_live = {it["applyno"] for it in result["go_live_pending"] if it.get("applyno")}
    cur_progress = {it["applyno"] for it in result["progress_pending"] if it.get("applyno")}

    for wf, label, cur_set in [
        ("closing", "結案", cur_closing),
        ("condition", "二階段", cur_condition),
        ("go_live", "開辦", cur_go_live),
        ("progress", "進度", cur_progress),
    ]:
        prev_set = set(prev_state.get(wf, []))
        resolved = prev_set - cur_set
        for applyno in sorted(resolved):
            result["auto_resolved"].append({
                "applyno": applyno,
                "workflow": wf,
                "label": label,
            })
            logger.info("✅ %s %s草稿已確認送出（Portal 已無資料）", applyno, label)

    # 自動回寫 DB：已送出的結案案件 → 更新為「已結案」+ 副狀態「待轉入」
    # （案件已送出 portal，法扶正在審核中；deprecated: 舊版寫「已報結」）
    if db and result["auto_resolved"]:
        for item in result["auto_resolved"]:
            if item["workflow"] != "closing":
                continue
            applyno = item["applyno"]
            try:
                row = db.fetch_one(
                    "SELECT `id`, `legal_aid_status` FROM `cases` "
                    "WHERE (`legal_aid_number` = %s OR `case_number` = %s) "
                    "AND `legal_aid_status` IN ('已結案，待送出', '已結案，待報結') LIMIT 1",
                    (applyno, applyno),
                    as_dict=True,
                )
                if row and isinstance(row, dict) and row.get("id"):
                    db.execute(
                        "UPDATE `cases` SET `legal_aid_status` = '已結案', "
                        "`updated_at` = NOW() WHERE `id` = %s",
                        (row["id"],),
                    )
                    logger.info("  DB 更新: %s → 已結案/待轉入", applyno)
            except Exception as e:
                logger.warning("  DB 更新失敗 (%s): %s", applyno, e)

    # 儲存本次狀態供下次比對
    _save_draft_state({
        "date": date.today().isoformat(),
        "closing": sorted(cur_closing),
        "condition": sorted(cur_condition),
        "go_live": sorted(cur_go_live),
        "progress": sorted(cur_progress),
    })

    logger.info(
        "Portal 暫存掃描完成：案件狀態區暫存=%d, 結案暫存=%d, 條件待處理=%d, 開辦待處理=%d, 進度待回報=%d, 自動確認送出=%d",
        len(result["case_status_drafts"]),
        len(result["closing_drafts"]),
        len(result["condition_pending"]),
        len(result["go_live_pending"]),
        len(result["progress_pending"]),
        len(result["auto_resolved"]),
    )
    return result


def _resolve_go_live_cases_from_portal(status: dict, db) -> list[dict]:
    portal = status.get("portal_drafts") or {}
    if portal.get("error"):
        return []
    pending_laf = {
        str(it.get("applyno") or "").strip()
        for it in _sanitize_portal_pending_items(portal.get("go_live_pending", []), "開辦")
        if str(it.get("applyno") or "").strip()
    }
    resolved: list[dict] = []
    remaining: list[dict] = []
    for case in status.get("can_go_live", []) or []:
        laf_no = _case_laf_number(case)
        if not laf_no or laf_no in pending_laf:
            remaining.append(case)
            continue
        try:
            if db and case.get("id"):
                _update_laf_status(db, case, "進行中")
        except Exception as e:
            logger.warning("go_live portal resolve DB update failed for %s: %s", laf_no, e)
            remaining.append(case)
            continue
        resolved.append({
            "laf_case_number": laf_no,
            "osc_case_number": case.get("case_number", ""),
            "client_name": case.get("client_name", ""),
            "portal_status": "already_opened",
            "ok": True,
        })
    if resolved:
        status["can_go_live"] = remaining
        logger.info("Portal 未開辦清單已無資料，DB 自動修正為進行中: %d 件", len(resolved))
    return resolved


# ─── 3. 報告格式化 ────────────────────────────────────────────

def format_audit_report(
    missing_laf: List[dict],
    backfilled: List[dict],
    status: dict,
) -> str:
    """格式化巡檢報告（Telegram friendly）。"""
    lines = ["📋 法扶夜間巡檢報告", f"日期：{date.today().isoformat()}", ""]

    total = len(status.get("all_cases", []))
    lines.append(f"📊 法扶案件總數：{total}")
    lines.append("")

    # 補填結果
    if backfilled:
        lines.append(f"✅ 自動補填法扶案號：{len(backfilled)} 件")
        for b in backfilled[:10]:
            src_label = f"（{b['source']}）" if b.get("source") else ""
            lines.append(f"  • {b['case_number']} {b['client_name']} → {b['laf_no']}{src_label}")
        lines.append("")

    # 仍缺案號
    still_missing = [c for c in missing_laf if c["case_number"] not in {b["case_number"] for b in backfilled}]
    if still_missing:
        lines.append(f"⚠️ 仍待確認法扶案號：{len(still_missing)} 件（已查進行中與結案資料夾）")
        for c in still_missing[:10]:
            inspected = _inspect_laf_number_candidates(c)
            candidate_numbers = sorted(inspected["candidate_numbers"])
            if len(candidate_numbers) == 1:
                lines.append(f"  • {c['case_number']} {c.get('client_name', '?')} — 已找到 {candidate_numbers[0]}，待回填")
            elif len(candidate_numbers) > 1:
                joined = "、".join(candidate_numbers[:3])
                suffix = " 等" if len(candidate_numbers) > 3 else ""
                lines.append(f"  • {c['case_number']} {c.get('client_name', '?')} — 找到多個候選案號：{joined}{suffix}")
            else:
                lines.append(f"  • {c['case_number']} {c.get('client_name', '?')} — 未找到案號")
        if len(still_missing) > 10:
            lines.append(f"  ...及其他 {len(still_missing) - 10} 件")
        lines.append("")

    # 逾期未開辦
    not_started = status.get("not_started", [])
    if not_started:
        lines.append(f"🚨 逾期未開辦：{len(not_started)} 件")
        for c in sorted(not_started, key=lambda x: x.get("days_overdue", 0), reverse=True)[:10]:
            lines.append(f"  • {_case_label(c)} {c.get('client_name', '?')} — 逾期 {c.get('days_overdue', '?')} 天")
        lines.append("")

    # 可開辦但未回報
    can_go_live = status.get("can_go_live", [])
    go_live_result = status.get("go_live_draft_result") or {}
    go_live_auto_fixed = [
        it for it in (go_live_result.get("items") or [])
        if it.get("portal_status") == "already_opened"
    ]
    # 從 can_go_live 去掉已被明確確認已開辦的案件。
    _auto_fixed_lafs = {it.get("laf_case_number") for it in go_live_auto_fixed}
    can_go_live_remaining = [
        c for c in can_go_live
        if str(c.get("legal_aid_number") or "") not in _auto_fixed_lafs
    ]
    if can_go_live_remaining:
        lines.append(f"📤 可回報開辦（資料齊全）：{len(can_go_live_remaining)} 件")
        for c in can_go_live_remaining[:10]:
            lines.append(f"  • {_case_label(c)} {c.get('client_name', '?')}")
        lines.append("")

    if go_live_auto_fixed:
        lines.append(f"🔄 Portal 確認已開辦（DB 已自動修正為「進行中」）：{len(go_live_auto_fixed)} 件")
        for it in go_live_auto_fixed[:10]:
            lines.append(f"  • {it.get('laf_case_number', '?')} {it.get('client_name', '?')}")
        lines.append("")

    # 已結案 — portal 驗證結果
    portal_drafted = status.get("portal_drafted", [])
    portal_approved = status.get("portal_approved", [])
    portal_pending_transfer = status.get("portal_pending_transfer", [])
    portal_unreported = status.get("portal_unreported", [])

    if portal_approved:
        lines.append(f"✅ 法扶已通過報結（已轉入）：{len(portal_approved)} 件")
        for entry in portal_approved[:10]:
            c = entry["case"]
            lines.append(f"  • {_case_label(c)} {c.get('client_name', '?')} — {entry.get('portal_info', '')}")
        lines.append("")

    if portal_pending_transfer:
        # 已送件、法扶審核中 — 不需任何操作
        lines.append(f"⏳ 已送件，法扶審核中（待轉入）：{len(portal_pending_transfer)} 件（不需處理）")
        for entry in portal_pending_transfer[:10]:
            c = entry["case"]
            lines.append(f"  • {_case_label(c)} {c.get('client_name', '?')}")
        lines.append("")

    if portal_drafted:
        lines.append(f"📝 法扶網站已暫存，請上 lawyer.laf.org.tw 確認送出：{len(portal_drafted)} 件")
        for entry in portal_drafted[:10]:
            c = entry["case"]
            lines.append(f"  • {_case_label(c)} {c.get('client_name', '?')} — {entry.get('portal_info', '')}")
        lines.append("  👉 送出後回覆 MAGI「<案號> 已報結」更新狀態")
        lines.append("")

    if portal_unreported:
        lines.append(f"🚨 確認未報結（需處理）：{len(portal_unreported)} 件")
        for entry in portal_unreported[:10]:
            c = entry["case"]
            lines.append(f"  • {_case_label(c)} {c.get('client_name', '?')}")
        lines.append("")

    # fallback：若沒做 portal 驗證（dry_run 或無 pending），仍顯示 pending_close
    pending_close = status.get("pending_close", [])
    any_portal_result = portal_drafted or portal_approved or portal_pending_transfer or portal_unreported
    if pending_close and not any_portal_result:
        lines.append(f"📝 已結案，需確認法扶報結狀態：{len(pending_close)} 件")
        for c in pending_close[:10]:
            lines.append(f"  • {_case_label(c)} {c.get('client_name', '?')}")
        lines.append("")

    # 有判決書可報結
    can_close = status.get("can_close", [])
    if can_close:
        lines.append(f"📄 有判決書可報結：{len(can_close)} 件")
        for c in can_close[:10]:
            lines.append(f"  • {_case_label(c)} {c.get('client_name', '?')}")
        lines.append("")

    # 法扶官網新文件（含自動下載結果）
    portal_new_files = status.get("portal_new_files", [])
    if portal_new_files:
        total_auto = sum(f.get("auto_downloaded", 0) for f in portal_new_files)
        total_still_missing = sum(f.get("new_count", 0) for f in portal_new_files)

        if total_auto > 0:
            lines.append(f"📥 法扶官網文件：已自動下載 {total_auto} 份")
            for f in portal_new_files:
                ad = f.get("auto_downloaded", 0)
                if ad > 0:
                    lines.append(f"  ✅ {f['laf_no']} {f['client_name']} — 已下載 {ad} 份")
            lines.append("")

        if total_still_missing > 0:
            still_missing_cases = [f for f in portal_new_files if f.get("new_count", 0) > 0]
            lines.append(f"⚠️ 法扶官網仍缺文件：{len(still_missing_cases)} 件")
            for f in still_missing_cases[:10]:
                _mf = f.get("missing_files") or []
                _mf_txt = "：" + "、".join(str(x) for x in _mf[:3]) if _mf else ""
                lines.append(f"  • {f['laf_no']} {f['client_name']} — 尚缺 {f['new_count']} 份文件{_mf_txt}")
            lines.append("")
        elif total_auto > 0 and total_still_missing == 0:
            lines.append("  ✅ 所有缺檔已自動下載完成")
            lines.append("")

    # Portal 暫存/待處理全清單掃描結果
    pd = status.get("portal_drafts") or {}
    pd_case_status = _sanitize_portal_pending_items(pd.get("case_status_drafts", []), "報告/案件狀態區")
    pd_closing = _sanitize_portal_pending_items(pd.get("closing_drafts", []), "報告/結案")
    pd_condition = _sanitize_portal_pending_items(pd.get("condition_pending", []), "報告/二階段")
    pd_go_live = _sanitize_portal_pending_items(pd.get("go_live_pending", []), "報告/開辦")
    pd_progress = _sanitize_portal_pending_items(pd.get("progress_pending", []), "報告/進度")
    pd_resolved = pd.get("auto_resolved", [])

    has_portal_pending = bool(pd_case_status or pd_closing or pd_condition or pd_go_live or pd_progress)

    if pd_resolved:
        lines.append(f"✅ 以下案件已確認送出（Portal 已無暫存，不再提醒）：{len(pd_resolved)} 件")
        for it in pd_resolved[:10]:
            lines.append(f"  • {it['applyno']}（{it['label']}）")
        lines.append("")

    other_case_status = [
        it for it in pd_case_status
        if it.get("reply_type") != "結案回報"
    ]
    if other_case_status:
        lines.append(f"📝 案件狀態區仍有暫存回報：{len(other_case_status)} 件")
        for it in other_case_status[:10]:
            lines.append(f"  • {it.get('applyno', '?')} — {it.get('row_text', '')[:80]}")
        lines.append("")

    if pd_closing:
        lines.append(f"📝 結案回報仍為暫存（請上 lawyer.laf.org.tw 送出）：{len(pd_closing)} 件")
        for it in pd_closing[:10]:
            lines.append(f"  • {it.get('applyno', '?')} — {it.get('row_text', '')[:80]}")
        lines.append("")

    if pd_condition:
        lines.append(f"📝 二階段（附條件）待回報：{len(pd_condition)} 件")
        for it in pd_condition[:10]:
            lines.append(f"  • {it.get('applyno', '?')} — {it.get('row_text', '')[:80]}")
        lines.append("")

    if pd_go_live:
        lines.append(f"📝 開辦待送出（Portal 仍有未開辦案件）：{len(pd_go_live)} 件")
        for it in pd_go_live[:10]:
            lines.append(f"  • {it.get('applyno', '?')} — {it.get('row_text', '')[:80]}")
        lines.append("")

    if pd_progress:
        lines.append(f"🚨 法扶官網要求進度回報：{len(pd_progress)} 件")
        for it in pd_progress[:10]:
            lines.append(f"  • {it.get('applyno', '?')} — {it.get('row_text', '')[:80]}")
        lines.append("")

    progress_overdue = status.get("progress_overdue", [])
    if progress_overdue:
        lines.append(f"⚠️ 進行中逾 18 個月，需確認進度回報：{len(progress_overdue)} 件")
        for c in sorted(progress_overdue, key=lambda x: x.get("days_since_assignment", 0), reverse=True)[:10]:
            assigned = c.get("assignment_date") or "日期不明"
            days_since = c.get("days_since_assignment", "?")
            lines.append(f"  • {_case_label(c)} {c.get('client_name', '?')} — 派案/建案 {assigned}，已 {days_since} 天")
        lines.append("")

    # 全部正常
    # portal_pending_transfer 是「已送件等法扶審核」，不算需處理
    # portal_new_files 只有仍缺檔的才算需處理
    portal_still_missing = [f for f in portal_new_files if f.get("new_count", 0) > 0]
    if not (still_missing or not_started or can_go_live or portal_unreported or portal_drafted
            or can_close or progress_overdue or portal_still_missing or has_portal_pending):
        lines.append("✅ 所有法扶案件狀態正常，無需處理。")

    return "\n".join(lines)


# ─── 4. 通知發送 ──────────────────────────────────────────────

def send_report(report_text: str, has_issues: bool = False):
    """透過 red_phone 發送 Telegram 通知。"""
    try:
        sys.path.insert(0, os.path.join(PROJECT_ROOT, "skills", "ops"))
        from red_phone import alert_admin
        severity = "warning" if has_issues else "info"
        result = alert_admin(
            message=report_text,
            severity=severity,
            source="laf_nightly_audit",
            # 如果沒有問題，DC 鏡像會被 red_phone 的 filter 攔截（clean status report）
            # 若未來需要強制靜默，可在 topic_key 傳入 "__SILENT__"
            topic_key="laf",
        )
        logger.info("Telegram notification sent: %s", result.get("telegram", False))
    except Exception as e:
        logger.error("Failed to send notification: %s", e)


def save_report(report_text: str):
    """儲存報告到 reports/ 目錄。"""
    os.makedirs(REPORT_DIR, exist_ok=True)
    filename = f"laf_audit_{date.today().isoformat()}.md"
    filepath = os.path.join(REPORT_DIR, filename)
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(report_text)
    logger.info("Report saved: %s", filepath)


# ─── Main ─────────────────────────────────────────────────────

def run_audit(notify: bool = True, dry_run: bool = False) -> dict:
    """
    執行完整法扶巡檢流程。

    Args:
        notify: 是否發送 Telegram 通知
        dry_run: 若 True，不寫入 DB 也不發送通知

    Returns:
        巡檢結果 dict
    """
    logger.info("🌙 法扶夜間巡檢開始")

    db = _get_db()
    if not db:
        logger.error("無法連接資料庫，巡檢中止")
        return {"ok": False, "error": "db_connection_failed"}

    # 1. 掃描缺法扶案號
    missing_laf = scan_missing_laf_numbers(db)
    logger.info("缺法扶案號案件: %d", len(missing_laf))

    # 2. 嘗試補填
    backfilled = []
    if not dry_run:
        for case in missing_laf:
            laf_no = try_backfill_laf_number(db, case)
            if laf_no:
                backfilled.append({
                    "case_number": case["case_number"],
                    "client_name": case.get("client_name", ""),
                    "laf_no": laf_no,
                    "source": "資料夾",
                })
    logger.info("自動補填成功（資料夾）: %d", len(backfilled))

    # 2b. 仍缺案號 → 從接案清冊 Excel 比對
    still_missing_after_folder = [
        c for c in missing_laf
        if c["case_number"] not in {b["case_number"] for b in backfilled}
    ]
    caselist_backfilled = []
    if still_missing_after_folder and not dry_run:
        caselist_backfilled = backfill_from_case_list(db, still_missing_after_folder)
        backfilled.extend(caselist_backfilled)
        logger.info("自動補填成功（接案清冊）: %d", len(caselist_backfilled))

    # 2c. Placeholder 案件 reconcile：派案 email 不完整建立的臨時案件，
    # 先用已下載的法扶文件/同法扶案號乾淨案件修正，避免夜間巡檢重複留下假資料夾。
    reconcile_result = {}
    if not dry_run:
        try:
            _old_skip_import_probes = os.environ.get("MAGI_SKIP_IMPORT_PROBES")
            os.environ["MAGI_SKIP_IMPORT_PROBES"] = "1"
            try:
                from casper_ecosystem.law_firm_orchestrators.laf_nightly_audit import (  # type: ignore
                    reconcile_placeholder_cases,
                )
            finally:
                if _old_skip_import_probes is None:
                    os.environ.pop("MAGI_SKIP_IMPORT_PROBES", None)
                else:
                    os.environ["MAGI_SKIP_IMPORT_PROBES"] = _old_skip_import_probes

            notifier = None
            if notify:
                try:
                    from line_notifier import LAFNotifier  # type: ignore
                    notifier = LAFNotifier()
                except Exception:
                    notifier = None
            reconcile_result = reconcile_placeholder_cases(db, force=True, notifier=notifier)
            if reconcile_result.get("reconciled"):
                logger.info(
                    "✅ Placeholder reconcile: %d 筆已修正（rename %d, skip %d）",
                    len(reconcile_result.get("reconciled") or []),
                    len(reconcile_result.get("renamed") or []),
                    len(reconcile_result.get("rename_skipped") or []),
                )
            elif reconcile_result.get("error"):
                logger.warning("Placeholder reconcile 失敗: %s", reconcile_result.get("error"))
        except Exception as _rc_e:
            logger.warning("Placeholder reconcile 跳過: %s", _rc_e)

    # 3. 掃描開辦/結案狀態
    status = scan_laf_reporting_status(db)
    logger.info(
        "狀態統計 — 逾期未開辦:%d, 可開辦:%d, 待報結:%d, 可報結:%d",
        len(status["not_started"]),
        len(status["can_go_live"]),
        len(status["pending_close"]),
        len(status["can_close"]),
    )

    # 3b. 上法扶網站驗證「待報結」案件的實際狀態
    portal_status = {}
    if status["pending_close"] and not dry_run:
        portal_status = verify_portal_closing_status(status["pending_close"], db=db)
        # 更新 status dict：依照 portal 結果分類
        status["portal_drafted"] = portal_status.get("drafted", [])              # 暫存
        status["portal_approved"] = portal_status.get("approved", [])            # 已轉入
        status["portal_pending_transfer"] = portal_status.get("pending_transfer", [])  # 待轉入
        status["portal_unreported"] = portal_status.get("unreported", [])        # 未報結
    else:
        status["portal_drafted"] = []
        status["portal_approved"] = []
        status["portal_pending_transfer"] = []
        status["portal_unreported"] = []

    # 3c. 法扶官網新文件掃描
    portal_new_files = []
    if not dry_run:
        portal_new_files = scan_portal_new_files(status.get("all_cases", []))
        status["portal_new_files"] = portal_new_files
    else:
        status["portal_new_files"] = []

    # 3d. 可報結案件自動暫存（呼叫既有報結流程）
    closing_draft_result = {}
    if status["can_close"] and not dry_run:
        closing_draft_result = _run_closing_drafts(max_cases=5)
        status["closing_draft_result"] = closing_draft_result
        logger.info("報結自動暫存結果: processed=%d/%d",
                     closing_draft_result.get("processed", 0),
                     closing_draft_result.get("scanned", 0))
        # 過濾掉已成功暫存的案件，避免通知列表重複顯示
        _drafted_laf_nos = {
            item.get("laf_case_number") or item.get("osc_case_number", "")
            for item in closing_draft_result.get("items", [])
            if item.get("ok")
        }
        if _drafted_laf_nos:
            status["can_close"] = [
                c for c in status["can_close"]
                if (c.get("legal_aid_number") or c.get("case_number", "")) not in _drafted_laf_nos
            ]
            logger.info("已從 can_close 移除 %d 件已暫存案件", len(_drafted_laf_nos))

    # 3e. Portal 暫存/待處理全清單掃描（結案、二階段、開辦）。
    # 先掃 portal，再決定是否需要開辦暫存；避免 DB 未開辦但 portal 已無未開辦列時誤打草稿。
    portal_drafts = {}
    if not dry_run:
        portal_drafts = scan_portal_pending_drafts(db=db)
        status["portal_drafts"] = portal_drafts
    else:
        status["portal_drafts"] = {}

    # 3f. 可開辦案件自動暫存（填寫表單+截圖，不送出）
    go_live_draft_result = {}
    portal_go_live_resolved = _resolve_go_live_cases_from_portal(status, db) if not dry_run else []
    if status["can_go_live"] and not dry_run:
        go_live_draft_result = _run_go_live_drafts(
            status["can_go_live"],
            max_cases=3,
            db=db,
            suppress_notify=not notify,
        )
        logger.info("開辦自動暫存結果: processed=%d/%d",
                     go_live_draft_result.get("processed", 0),
                     go_live_draft_result.get("total", 0))
    if portal_go_live_resolved:
        items = list(go_live_draft_result.get("items") or [])
        items.extend(portal_go_live_resolved)
        go_live_draft_result = {
            **go_live_draft_result,
            "ok": True,
            "items": items,
            "processed": go_live_draft_result.get("processed", 0),
            "total": go_live_draft_result.get("total", 0) + len(portal_go_live_resolved),
            "auto_resolved": len(portal_go_live_resolved),
        }
    status["go_live_draft_result"] = go_live_draft_result

    # 4. 格式化報告
    backfilled_case_numbers = {b["case_number"] for b in backfilled}
    final_missing_laf = [c for c in missing_laf if c["case_number"] not in backfilled_case_numbers]
    report = format_audit_report(missing_laf, backfilled, status)

    # 5. 儲存報告
    save_report(report)

    # 6. 發送通知
    portal_still_missing = [f for f in portal_new_files if f.get("new_count", 0) > 0]
    _pd = status.get("portal_drafts") or {}
    _has_portal_pending = bool(
        _sanitize_portal_pending_items(_pd.get("case_status_drafts", []), "摘要/案件狀態區")
        or _sanitize_portal_pending_items(_pd.get("closing_drafts", []), "摘要/結案")
        or _sanitize_portal_pending_items(_pd.get("condition_pending", []), "摘要/二階段")
        or _sanitize_portal_pending_items(_pd.get("go_live_pending", []), "摘要/開辦")
        or _sanitize_portal_pending_items(_pd.get("progress_pending", []), "摘要/進度")
    )
    has_issues = bool(
        final_missing_laf or status["not_started"] or status["can_go_live"]
        or status.get("portal_unreported") or status.get("portal_drafted")
        or status["can_close"] or status.get("progress_overdue")
        or portal_still_missing or _has_portal_pending
    )
    if notify and not dry_run:
        send_report(report, has_issues=has_issues)

    logger.info("🌙 法扶夜間巡檢完成")
    return {
        "ok": True,
        "missing_laf_count": len(final_missing_laf),
        "initial_missing_laf_count": len(missing_laf),
        "backfilled_count": len(backfilled),
        "not_started_count": len(status["not_started"]),
        "can_go_live_count": len(status["can_go_live"]),
        "pending_close_count": len(status["pending_close"]),
        "progress_overdue_count": len(status.get("progress_overdue", [])),
        "portal_drafted_count": len(status.get("portal_drafted", [])),
        "portal_approved_count": len(status.get("portal_approved", [])),
        "portal_unreported_count": len(status.get("portal_unreported", [])),
        "can_close_count": len(status["can_close"]),
        "closing_draft_processed": closing_draft_result.get("processed", 0),
        "portal_new_files_count": len(portal_new_files),
        "portal_auto_downloaded": sum(f.get("auto_downloaded", 0) for f in portal_new_files),
        "portal_still_missing_count": len(portal_still_missing),
        "portal_pending_case_status_drafts": len(_sanitize_portal_pending_items(_pd.get("case_status_drafts", []))),
        "portal_pending_closing_drafts": len(_sanitize_portal_pending_items(_pd.get("closing_drafts", []))),
        "portal_pending_condition": len(_sanitize_portal_pending_items(_pd.get("condition_pending", []))),
        "portal_pending_go_live": len(_sanitize_portal_pending_items(_pd.get("go_live_pending", []))),
        "portal_pending_progress": len(_sanitize_portal_pending_items(_pd.get("progress_pending", []))),
        "portal_auto_resolved": len(_pd.get("auto_resolved", [])),
        "total_cases": len(status["all_cases"]),
        "report": report,
    }


def run_backfill_only(notify: bool = True) -> dict:
    """
    只執行法扶案號補填（資料夾 + 接案清冊），不跑完整巡檢。
    可由 TG/DC 手動觸發。
    """
    logger.info("🔍 法扶案號補填開始")
    db = _get_db()
    if not db:
        return {"ok": False, "error": "db_connection_failed"}

    missing_laf = scan_missing_laf_numbers(db)
    if not missing_laf:
        msg = "✅ 所有法扶案件都已有案號，無需補填。"
        logger.info(msg)
        if notify:
            send_report(msg, has_issues=False)
        return {"ok": True, "missing": 0, "backfilled": 0, "message": msg}

    # Step 1: 資料夾補填
    backfilled = []
    for case in missing_laf:
        laf_no = try_backfill_laf_number(db, case)
        if laf_no:
            backfilled.append({
                "case_number": case["case_number"],
                "client_name": case.get("client_name", ""),
                "laf_no": laf_no,
                "source": "資料夾",
            })

    # Step 2: 接案清冊補填
    still_missing = [
        c for c in missing_laf
        if c["case_number"] not in {b["case_number"] for b in backfilled}
    ]
    if still_missing:
        caselist_bf = backfill_from_case_list(db, still_missing)
        backfilled.extend(caselist_bf)

    # 組報告
    final_missing = [
        c for c in missing_laf
        if c["case_number"] not in {b["case_number"] for b in backfilled}
    ]
    lines = [f"🔍 法扶案號補填結果（共 {len(missing_laf)} 件缺案號）"]
    if backfilled:
        lines.append(f"✅ 自動補填成功：{len(backfilled)} 件")
        for b in backfilled[:15]:
            src_label = f"（{b['source']}）" if b.get("source") else ""
            lines.append(f"  • {b['case_number']} {b['client_name']} → {b['laf_no']}{src_label}")
    if final_missing:
        lines.append(f"⚠️ 仍缺案號：{len(final_missing)} 件")
        for c in final_missing[:10]:
            lines.append(f"  • {c['case_number']} {c.get('client_name', '?')}")
    if not backfilled and not final_missing:
        lines.append("✅ 無需補填。")

    msg = "\n".join(lines)
    logger.info(msg)
    if notify:
        send_report(msg, has_issues=bool(final_missing))

    return {
        "ok": True,
        "missing": len(missing_laf),
        "backfilled": len(backfilled),
        "still_missing": len(final_missing),
        "items": backfilled,
        "message": msg,
    }


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="法扶夜間巡檢")
    parser.add_argument("--dry-run", action="store_true", help="預覽模式，不寫入 DB 也不發送通知")
    parser.add_argument("--no-notify", action="store_true", help="不發送 Telegram 通知")
    parser.add_argument("--mode", choices=["full", "backfill"], default="full",
                        help="full=完整巡檢, backfill=只補填案號")
    args = parser.parse_args()

    if args.mode == "backfill":
        result = run_backfill_only(notify=not args.no_notify)
    else:
        # Housekeeping: clean up old exports (>30 days)
        try:
            from api.server import cleanup_old_exports
            cleanup_old_exports(days=30)
        except Exception:
            pass
        result = run_audit(notify=not args.no_notify, dry_run=args.dry_run)

    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
