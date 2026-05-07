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
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
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
        from osc import DatabaseManager
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
            _dir_count = 0
            for root, dirs, files in os.walk(scan_dir):
                _depth = root.replace(scan_dir, "").count(os.sep)
                if _depth >= 2:
                    dirs.clear()
                    continue
                _dir_count += 1
                if _dir_count > 500:
                    dirs.clear()
                    break
                if _dir_count % 50 == 0:
                    import time as _t; _t.sleep(0.05)
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
            _dir_count = 0
            for root, dirs, files in os.walk(folder):
                _depth = root.replace(folder, "").count(os.sep)
                if _depth >= 2:
                    dirs.clear()
                    continue
                _dir_count += 1
                if _dir_count > 500:
                    dirs.clear()
                    break
                if _dir_count % 50 == 0:
                    import time as _t; _t.sleep(0.05)
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


def _case_identity_key(case: dict) -> str:
    """穩定識別同一法扶案件，優先用法扶案號。"""
    if not isinstance(case, dict):
        return ""
    return _case_laf_number(case) or str(case.get("case_number") or "").strip() or str(case.get("id") or "").strip()


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


def _update_laf_status_with_approval(db, case: dict, main_status: str, approval_status: str) -> None:
    """同時更新 legal_aid_status + legal_aid_approval_status + legal_aid_approval_checked_at。

    冪等：相同值不寫，避免 UPDATE 噪音。
    若 legal_aid_approval_status 欄位不存在（schema 尚未 ALTER），退回呼叫 _update_laf_status。
    """
    case_id = case.get("id")
    if not case_id:
        return
    cur_main = (case.get("legal_aid_status") or "").strip()
    cur_approval = (case.get("legal_aid_approval_status") or "").strip()
    if cur_main == main_status and cur_approval == approval_status:
        logger.debug("DB 冪等跳過 case_id=%s: %s/%s 無變化", case_id, main_status, approval_status)
        return
    try:
        db.execute_write(
            "UPDATE `cases` SET `legal_aid_status` = %s, `legal_aid_approval_status` = %s, "
            "`legal_aid_approval_checked_at` = NOW() WHERE `id` = %s",
            (main_status, approval_status, case_id),
        )
        logger.info(
            "📝 DB 狀態更新（主+副）: %s %s「%s/%s」→「%s/%s」",
            case.get("case_number"), case.get("client_name"),
            cur_main, cur_approval, main_status, approval_status,
        )
        case["legal_aid_status"] = main_status
        case["legal_aid_approval_status"] = approval_status
    except Exception as e:
        err_str = str(e)
        if "legal_aid_approval_status" in err_str or "Unknown column" in err_str:
            # Schema 尚未 ALTER — 退回只更新主狀態
            logger.warning(
                "legal_aid_approval_status 欄位不存在，退回更新主狀態。請執行 ALTER TABLE: %s", e
            )
            _update_laf_status(db, case, main_status)
        else:
            logger.error("DB 主+副狀態更新失敗 case_id=%s: %s", case_id, e)


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

    for expected in expected_files:
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
            _update_case_laf_number(db, case, laf_no)
            db.check_laf_case_exists(
                laf_case_number=laf_no,
                client_name=case.get("client_name", ""),
                case_type=case.get("case_type", ""),
                case_reason=case.get("case_reason", ""),
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
            _update_case_laf_number(db, case, chosen_no)
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


# ─── 1.5 Reconcile placeholder LAF 案件（不完整派案 email）──────────

import time as _time

# 客戶名禁用字元（族名 UTAK KUAD 含半形空白與 - 是合法的）
_PLACEHOLDER_INVALID_CHARS = set(")(<>[]{}!@#$%^&*+=|\\;:\"'?/`~")
_PLACEHOLDER_NOISE_TOKENS = ("案情", "文件", "卷宗", "附件", "信件", "資料夾")
_PLACEHOLDER_REASON_TOKENS = {"", "待確認", "未確認"}
_RECONCILE_STATE_FILE = os.path.join(PROJECT_ROOT, "static", "laf_reconcile_state.json")
_RECONCILE_THROTTLE_SEC = 3600  # 1 小時節流


def _is_placeholder_client_name(name: str) -> bool:
    """客戶名是否為不完整 email 解析出的垃圾。

    規則（任一命中即視為 placeholder）：
    - 空字串 / None
    - 含特殊字元（括號、底線、特殊符號）— 半形空白與單個 `-` 視為合法（族名 UTAK KUAD）
    - 連續 `--`
    - 含明顯 placeholder 文字（案情/文件/卷宗等）
    - 長度 > 30 字元（族名也不應超過此值）
    """
    s = str(name or "").strip()
    if not s:
        return True
    if any(c in _PLACEHOLDER_INVALID_CHARS for c in s):
        return True
    if "--" in s:
        return True
    for token in _PLACEHOLDER_NOISE_TOKENS:
        if token in s:
            return True
    if len(s) > 30:
        return True
    return False


def _is_placeholder_case_reason(reason: str) -> bool:
    """case_reason 是否 placeholder（空、'待確認'、'未確認'）。"""
    return str(reason or "").strip() in _PLACEHOLDER_REASON_TOKENS


def _is_folder_open_by_other(folder_path: str) -> Tuple[bool, str]:
    """檢查資料夾是否被其他應用打開（lsof）— 排除自己 process（python3/lsof）。

    Returns (is_open, who) — who 為 process 名稱列表（用 , 分隔）。
    """
    if not folder_path or not os.path.isdir(folder_path):
        return False, ""
    try:
        import subprocess as _subp
        proc = _subp.run(
            ["lsof", "+D", folder_path],
            capture_output=True, text=True, timeout=15,
        )
        out = (proc.stdout or "")
        lines = [ln for ln in out.splitlines() if ln and not ln.startswith("COMMAND")]
        # 第一欄是 process 名稱
        my_pid = str(os.getpid())
        ignored_procs = {"python3", "python", "lsof", "Python"}
        relevant_procs = []
        for ln in lines:
            tokens = ln.split()
            if len(tokens) < 2:
                continue
            pname, pid = tokens[0], tokens[1]
            if pid == my_pid:
                continue
            if pname in ignored_procs:
                continue
            relevant_procs.append(pname)
        if relevant_procs:
            uniq = sorted(set(relevant_procs))
            return True, ",".join(uniq[:3])
    except Exception as e:
        logger.debug("lsof check failed for %s: %s", folder_path, e)
    return False, ""


def _safe_rename_case_folder(old_path: str, new_path: str) -> Tuple[bool, str]:
    """安全 rename 資料夾。Returns (renamed, reason)。

    reason='' 表示成功；否則為跳過原因（folder_open / target_exists / ...）。
    """
    if not old_path:
        return False, "old_path_empty"
    if not os.path.isdir(old_path):
        return False, f"old_not_exist"
    if old_path == new_path:
        return False, "same_path"
    if os.path.exists(new_path):
        return False, "target_exists"
    is_open, who = _is_folder_open_by_other(old_path)
    if is_open:
        return False, f"folder_open:{who}"
    try:
        os.rename(old_path, new_path)
        return True, ""
    except OSError as e:
        return False, f"rename_failed:{e}"


def _replace_folder_basename_canonical(canonical_path: str, new_basename: str) -> str:
    """把 canonical 路徑（Z:\\... 或 Y:\\...）的最後一段 basename 換成新值。

    處理 \\ 與 / 兩種分隔符（Z 路徑通常是 \\）。
    """
    if not canonical_path or not new_basename:
        return canonical_path
    p = canonical_path.rstrip("/\\")
    if "\\" in p:
        sep = "\\"
    else:
        sep = "/"
    parts = p.split(sep)
    if not parts:
        return canonical_path
    parts[-1] = new_basename
    return sep.join(parts)


def _write_reconcile_state():
    try:
        os.makedirs(os.path.dirname(_RECONCILE_STATE_FILE), exist_ok=True)
        with open(_RECONCILE_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(
                {"last_run": _time.time(), "last_run_iso": datetime.now().isoformat()},
                f,
            )
    except Exception as e:
        logger.debug("write reconcile state failed: %s", e)


def _check_reconcile_throttle() -> Tuple[bool, int]:
    """Returns (throttled, remaining_sec)."""
    try:
        if os.path.exists(_RECONCILE_STATE_FILE):
            with open(_RECONCILE_STATE_FILE) as f:
                st = json.load(f)
            last = float(st.get("last_run") or 0)
            elapsed = _time.time() - last
            if elapsed < _RECONCILE_THROTTLE_SEC:
                return True, int(_RECONCILE_THROTTLE_SEC - elapsed)
    except Exception:
        pass
    return False, 0


def reconcile_placeholder_cases(db, *, force: bool = False,
                                 only_laf_no: str = "",
                                 notifier=None) -> dict:
    """修補不完整派案 email 建立的 placeholder 法扶案件。

    從 LAF portal 接案清冊 Excel 抓真實資料：
    - 用 legal_aid_number 直接比對（不靠 client_name，因為它是垃圾）
    - 更新 DB: client_name, case_reason, case_stage
    - 安全 rename 資料夾（lsof 偵測；被開啟則跳過 rename，仍更新 DB）
    - 更新 DB folder_path
    - DC 通知律師每筆修正

    Args:
        db: DatabaseManager 實例
        force: True 跳過 1 小時節流
        only_laf_no: 僅處理特定 LAF 案號（CLI 單筆觸發）
        notifier: optional LAFNotifier；提供時會送 DC 通知

    Returns: dict with placeholder_count, reconciled, renamed, rename_skipped, throttled
    """
    # 節流（單筆觸發或 force 時跳過）
    if not force and not only_laf_no:
        throttled, remaining = _check_reconcile_throttle()
        if throttled:
            return {"throttled": True, "next_run_in_sec": remaining,
                    "placeholder_count": 0, "reconciled": [], "renamed": [], "rename_skipped": []}

    # Step 1: 找 placeholder 案件
    where_clause = """
        WHERE (`case_category` = '法律扶助案件'
               OR `case_reason` LIKE '%法扶%'
               OR `case_reason` LIKE '%法律扶助%')
          AND COALESCE(`legal_aid_number`, '') <> ''
    """
    params: tuple = ()
    if only_laf_no:
        where_clause += " AND `legal_aid_number` = %s"
        params = (only_laf_no,)
    query = f"""
        SELECT `id`, `case_number`, `client_name`, `case_type`, `case_reason`,
               `case_stage`, `case_category`, `folder_path`, `legal_aid_number`
        FROM `cases`
        {where_clause}
        ORDER BY `case_number` DESC
        LIMIT 200
    """
    try:
        all_rows = db.fetch_all(query, params, as_dict=True) or []
    except Exception as e:
        logger.error("reconcile fetch failed: %s", e)
        return {"error": f"fetch_failed: {e}"}

    placeholders = [
        r for r in all_rows
        if _is_placeholder_client_name(r.get("client_name") or "")
        or _is_placeholder_case_reason(r.get("case_reason") or "")
    ]

    if not placeholders:
        if not only_laf_no:
            _write_reconcile_state()
        return {"placeholder_count": 0, "reconciled": [], "renamed": [], "rename_skipped": []}

    logger.info("🔍 Reconcile: 找到 %d 個 placeholder 案件%s",
                len(placeholders), f" (filter laf_no={only_laf_no})" if only_laf_no else "")

    # Step 2: 匯出接案清冊 Excel
    laf = None
    xlsx_path = None
    try:
        laf = _make_laf_web_automation(log_prefix="LAF-RECONCILE")
        if not laf.login():
            logger.error("法扶網站登入失敗，無法匯出接案清冊")
            return {"error": "portal_login_failed", "placeholder_count": len(placeholders)}
        xlsx_path = laf.export_case_list_excel()
    except Exception as e:
        logger.error("匯出接案清冊失敗: %s", e)
        return {"error": f"excel_export_failed: {e}", "placeholder_count": len(placeholders)}
    finally:
        if laf:
            try:
                laf.close()
            except Exception:
                pass

    if not xlsx_path:
        return {"error": "excel_path_empty", "placeholder_count": len(placeholders)}

    portal_entries = _parse_case_list_excel(xlsx_path)
    if not portal_entries:
        logger.warning("接案清冊 Excel 解析結果為空")
        return {"error": "excel_parse_empty", "placeholder_count": len(placeholders)}

    applyno_index = {e["applyno"]: e for e in portal_entries}
    logger.info("接案清冊共 %d 筆，準備比對 %d 個 placeholder", len(portal_entries), len(placeholders))

    # Step 3: 對每個 placeholder 找 portal entry，做修正
    try:
        from laf_folder_builder import LAFFolderBuilder
        folder_builder = LAFFolderBuilder()
    except Exception as fb_e:
        logger.error("import LAFFolderBuilder failed: %s", fb_e)
        return {"error": f"folder_builder_import: {fb_e}", "placeholder_count": len(placeholders)}

    try:
        from api.case_path_mapper import local_synology_path_candidates as _path_cands
    except Exception:
        _path_cands = None

    reconciled = []
    renamed = []
    rename_skipped = []
    not_found_in_portal = []

    for case in placeholders:
        laf_no = str(case.get("legal_aid_number") or "").strip()
        case_id = case.get("id")
        case_number = case.get("case_number") or ""
        old_client_name = case.get("client_name") or ""
        old_case_reason = case.get("case_reason") or ""
        old_folder_path = case.get("folder_path") or ""
        old_case_stage = case.get("case_stage") or ""

        portal = applyno_index.get(laf_no)
        if not portal:
            logger.info("⏭️ %s: 接案清冊找不到 LAF %s", case_number, laf_no)
            not_found_in_portal.append({"case_number": case_number, "laf_no": laf_no})
            continue

        new_client_name = (portal.get("name") or "").strip().translate(_NAME_FIXES)
        new_case_reason = (portal.get("reason") or "").strip()
        new_case_stage = (portal.get("procedure") or "").strip()

        if not new_client_name and not new_case_reason:
            logger.info("⏭️ %s: portal entry name/reason 都空", case_number)
            continue

        # 消費者債務清理特殊處理
        case_type = case.get("case_type") or ""
        case_category = case.get("case_category") or ""
        if case_type == "消費者債務清理" or case_category == "消費者債務清理" or "消費者債務清理" in new_case_reason:
            if "清算" not in new_case_reason:
                new_case_reason = "更生"

        # 若 portal 給的資料和 DB 完全一樣，跳過（雖然不該發生）
        if (new_client_name == old_client_name
                and new_case_reason == old_case_reason
                and new_case_stage == old_case_stage):
            continue

        # Step 4: UPDATE DB（先用 case_id 直接 UPDATE）
        try:
            db.execute_write(
                """UPDATE `cases`
                   SET `client_name` = %s,
                       `case_reason` = %s,
                       `case_stage` = CASE WHEN COALESCE(`case_stage`,'') IN ('','待確認','未確認') THEN %s ELSE `case_stage` END
                   WHERE `id` = %s""",
                (new_client_name, new_case_reason, new_case_stage, case_id),
            )
            logger.info("📝 DB updated %s: name=%s reason=%s stage=%s",
                        case_number, new_client_name, new_case_reason, new_case_stage)
        except Exception as e:
            logger.error("UPDATE DB failed for %s: %s", case_number, e)
            continue

        # Step 5: Rename folder（找到實體舊路徑 + 安全 rename）
        new_folder_info = {
            "case_number": case_number,
            "client_name": new_client_name,
            "case_type": case_type,
            "case_stage": new_case_stage,
            "case_reason": new_case_reason,
        }
        new_basename = folder_builder._build_folder_name(new_folder_info)

        rename_result = {"renamed": False, "old": old_folder_path,
                         "new": "", "reason": "", "new_canonical": ""}

        if old_folder_path:
            old_local = ""
            cands = []
            if _path_cands:
                cands = _path_cands(old_folder_path) or []
            for cand in cands:
                if os.path.isdir(cand):
                    old_local = cand
                    break

            if old_local:
                new_local = os.path.join(os.path.dirname(old_local), new_basename)
                ok, reason = _safe_rename_case_folder(old_local, new_local)
                rename_result["old"] = old_local
                rename_result["new"] = new_local
                rename_result["reason"] = reason
                rename_result["renamed"] = ok

                if ok:
                    new_canonical = _replace_folder_basename_canonical(old_folder_path, new_basename)
                    rename_result["new_canonical"] = new_canonical
                    try:
                        db.execute_write(
                            "UPDATE `cases` SET `folder_path` = %s WHERE `id` = %s",
                            (new_canonical, case_id),
                        )
                        renamed.append({
                            "case_number": case_number,
                            "old_local": old_local,
                            "new_local": new_local,
                            "new_canonical": new_canonical,
                        })
                        logger.info("📁 Renamed %s: %s → %s", case_number,
                                    os.path.basename(old_local), new_basename)
                    except Exception as e:
                        logger.error("UPDATE folder_path failed for %s: %s", case_number, e)
                        rename_result["reason"] = f"db_update_failed_after_rename:{e}"
                else:
                    rename_skipped.append({
                        "case_number": case_number,
                        "old": old_local,
                        "intended_new": new_local,
                        "reason": reason,
                    })
                    logger.info("📁 Rename skipped %s: %s (folder=%s)",
                                case_number, reason, os.path.basename(old_local))
            else:
                rename_skipped.append({
                    "case_number": case_number,
                    "old": old_folder_path,
                    "intended_new": "",
                    "reason": "old_local_not_found",
                })

        reconciled.append({
            "case_number": case_number,
            "laf_no": laf_no,
            "old_client_name": old_client_name,
            "new_client_name": new_client_name,
            "old_case_reason": old_case_reason,
            "new_case_reason": new_case_reason,
            "old_case_stage": old_case_stage,
            "new_case_stage": new_case_stage,
            "rename_result": rename_result,
        })

    # Step 6: 通知律師
    if notifier and reconciled:
        try:
            lines = ["📝 法扶 placeholder 案件已自動修正"]
            for r in reconciled:
                lines.append("")
                lines.append(f"• {r['case_number']} ({r['laf_no']})")
                lines.append(f"  當事人: 「{r['old_client_name']}」 → 「{r['new_client_name']}」")
                lines.append(f"  案由: 「{r['old_case_reason']}」 → 「{r['new_case_reason']}」")
                rr = r["rename_result"]
                if rr["renamed"]:
                    lines.append(f"  📁 資料夾已 rename → {os.path.basename(rr['new'])}")
                elif rr.get("reason", "").startswith("folder_open"):
                    proc = rr["reason"].split(":", 1)[-1] if ":" in rr["reason"] else "?"
                    lines.append(f"  ⚠️ 資料夾正開啟（{proc}），DB 已更新但資料夾未 rename。")
                    lines.append(f"     請關閉相關應用後手動重跑：reconcile_placeholder --laf-no {r['laf_no']}")
                elif rr.get("reason"):
                    lines.append(f"  ⚠️ 資料夾 rename 跳過：{rr['reason']}（DB 已更新）")
            try:
                notifier.notify_admin("\n".join(lines), topic_key="laf")
            except TypeError:
                notifier.notify_admin("\n".join(lines))
        except Exception as e:
            logger.warning("send reconcile notify failed: %s", e)

    # 清理暫存 Excel
    try:
        if xlsx_path and os.path.exists(xlsx_path):
            os.remove(xlsx_path)
    except Exception:
        pass

    if not only_laf_no:
        _write_reconcile_state()

    return {
        "placeholder_count": len(placeholders),
        "reconciled": reconciled,
        "renamed": renamed,
        "rename_skipped": rename_skipped,
        "not_found_in_portal": not_found_in_portal,
        "throttled": False,
    }


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
        return {"not_started": [], "can_go_live": [], "pending_close": [], "can_close": [], "all_cases": []}

    today = date.today()
    not_started = []      # 未開辦且已逾期
    can_go_live = []      # 有開辦資料可回報
    pending_close = []    # DB 狀態=結案 但法扶未報結
    can_close = []        # 有判決書可報結

    for case in all_cases:
        laf_status = _normalize_status_text(case.get("legal_aid_status") or "")
        osc_status = _normalize_status_text(case.get("status") or "")
        folder = (case.get("folder_path") or "").strip()
        deadline_raw = case.get("legal_aid_startup_deadline")
        laf_no = _case_laf_number(case)

        # 轉換路徑
        mac_folder = _to_mac_path(folder)

        # A. 未開辦且已逾期
        if laf_status in ("未開辦", "", None) and laf_no:
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
        if laf_status in ("未開辦", "", None) and mac_folder:
            has_notice = _folder_has_file(mac_folder, "02_開辦資料", ("開辦通知書", "接案通知書", "准予扶助證明書"))
            has_poa = _folder_has_file(mac_folder, "02_開辦資料", ("委任狀",))
            if not has_notice:
                has_notice = _folder_has_file(mac_folder, "01_法扶資料", ("開辦通知書", "接案通知書", "准予扶助證明書"))
            if has_notice and has_poa:
                can_go_live.append(case)

        # C. OSC 已結案但 DB 法扶狀態未標記已結案（需上法扶網站確認是否已報結）
        # deprecated alias：「已報結」/「已報結（待轉入）」保留至 2026-07-26，遷移完成後可移除
        _skip_pending = (
            "已結案", "結案", "已結案，待送出",
            # deprecated alias（遷移完成後移除）：
            "已報結", "已結案，待報結", "已報結（待轉入）",
        )
        if osc_status in ("結案", "已結案") and laf_status not in _skip_pending:
            pending_close.append(case)

        # D. 有判決書/處分書，可報結但還沒
        #    包含「已結案，待報結」狀態（DB 標記已結案但尚未向法扶回報）
        _closeable_statuses = ("進行中", "已開辦", "待報結", "已結案，待報結")
        if laf_status in _closeable_statuses and mac_folder:
            has_judgment = _folder_has_any_file(mac_folder, "10_判決書")
            if has_judgment:
                can_close.append(case)

    return {
        "not_started": not_started,
        "can_go_live": can_go_live,
        "pending_close": pending_close,
        "can_close": can_close,
        "all_cases": all_cases,
    }


def _is_dir_ok(path: str) -> bool:
    """os.path.isdir + 實際 stat 測試，防 stale SMB mount 誤判。"""
    try:
        if not os.path.isdir(path):
            return False
        os.stat(path)
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


def _run_go_live_drafts(can_go_live_cases: List[dict], max_cases: int = 3, db=None) -> dict:
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

    若提供 db，會自動回寫 legal_aid_status + legal_aid_approval_status：
      - 已轉入 → main=「已結案」, approval=「已轉入」
      - 待轉入 → main=「已結案」, approval=「待轉入」
      - 暫存   → main=「已結案，待送出」, approval=「暫存」
    （deprecated: 舊版回寫「已報結」/「已報結（待轉入）」，
      已改為「已結案」；2026-07-26 前保留 deprecated alias 相容性）

    Returns:
        {
            "drafted":          [{"case": ..., "portal_info": ...}, ...],  # 暫存（MAGI 已處理）
            "approved":         [{"case": ..., "portal_info": ...}, ...],  # 已轉入（法扶已通過）
            "pending_transfer": [{"case": ..., "portal_info": ...}, ...],  # 待轉入（法扶審核中）
            "unreported":       [{"case": ..., "portal_info": ...}, ...],  # 真正未報結
            "error":            [{"case": ..., "error": ...}, ...],
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
                    logger.info("✅ %s %s → 已轉入（%s），事務所工作完成", laf_no, (case.get("client_name", "")[:1] + "**"), found_type)
                    # 回寫 DB：已轉入 → 主狀態「已結案」+ 副狀態「已轉入」
                    if db and case.get("id"):
                        _update_laf_status_with_approval(db, case, "已結案", "已轉入")
                elif found_status == "待轉入":
                    # 已送件，法扶審核處理中，事務所端工作已完成
                    result["pending_transfer"].append(entry)
                    logger.info("⏳ %s %s → 待轉入（%s），事務所工作完成（法扶審核中）", laf_no, (case.get("client_name", "")[:1] + "**"), found_type)
                    # 回寫 DB：待轉入 → 主狀態「已結案」+ 副狀態「待轉入」
                    if db and case.get("id"):
                        _update_laf_status_with_approval(db, case, "已結案", "待轉入")
                elif found_status == "暫存":
                    result["drafted"].append(entry)
                    logger.info("📝 %s %s → 暫存（%s），需人工確認送出", laf_no, (case.get("client_name", "")[:1] + "**"), found_type)
                    # 回寫 DB：暫存 → 主狀態「已結案，待送出」+ 副狀態「暫存」
                    if db and case.get("id"):
                        _update_laf_status_with_approval(db, case, "已結案，待送出", "暫存")
                elif found_status:
                    # "有紀錄" 但狀態不明 — 保守不改主狀態，列為需確認
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


def _filter_can_close_by_portal_status(can_close: List[dict], portal_status: dict) -> List[dict]:
    """
    從「有判決書可報結」名單排除 portal 已經存在結案紀錄的案件。
    """
    if not can_close:
        return []

    handled_keys: set[str] = set()
    for bucket in ("drafted", "approved", "pending_transfer"):
        for entry in portal_status.get(bucket, []) or []:
            key = _case_identity_key(entry.get("case") or {})
            if key:
                handled_keys.add(key)

    if not handled_keys:
        return list(can_close)

    filtered = [case for case in can_close if _case_identity_key(case) not in handled_keys]
    removed = len(can_close) - len(filtered)
    if removed:
        logger.info("已從 can_close 排除 %d 件 portal 已有結案紀錄的案件", removed)
    return filtered


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
    moved, failed = [], []
    for fpath in downloaded_paths:
        fname = os.path.basename(fpath)
        subfolder = _classify_portal_file(fname)
        target_dir = os.path.join(case_root, subfolder)
        try:
            os.makedirs(target_dir, exist_ok=True)
            dest = os.path.join(target_dir, fname)
            if os.path.exists(dest):
                # 同名檔案已存在，視為成功
                logger.info("  ⏭️ 檔案已存在，跳過: %s", fname)
                moved.append(fname)
                try:
                    os.remove(fpath)
                except Exception:
                    pass
                continue
            shutil.move(fpath, dest)
            moved.append(fname)
            logger.info("  ✅ 已移至 %s/%s", subfolder, fname)
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
            file_list = dc.get("file_list") or []

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
    """儲存本次巡檢的 portal 暫存狀態（atomic write via tmp + rename）。"""
    os.makedirs(REPORT_DIR, exist_ok=True)
    _tmp_path = _DRAFT_STATE_FILE + ".tmp"
    try:
        with open(_tmp_path, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        os.replace(_tmp_path, _DRAFT_STATE_FILE)
    except Exception as e:
        logger.warning("儲存 draft state 失敗: %s", e)
        # Clean up tmp file on failure
        try:
            os.remove(_tmp_path)
        except OSError:
            pass


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
            "auto_resolved":     [{"applyno": ..., "workflow": ..., "label": ...}],
            "error": str or None,
        }
    """
    result = {
        "closing_drafts": [],
        "case_status_drafts": [],
        "condition_pending": [],
        "go_live_pending": [],
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

        # 案件狀態區：統一來源，專門提醒仍停留在「暫存」的回報
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

    for wf, label, cur_set in [
        ("closing", "結案", cur_closing),
        ("condition", "二階段", cur_condition),
        ("go_live", "開辦", cur_go_live),
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
                    "SELECT `id`, `legal_aid_status`, `legal_aid_approval_status` FROM `cases` "
                    "WHERE (`legal_aid_number` = %s OR `case_number` = %s) "
                    "AND `legal_aid_status` IN ('已結案，待送出', '已結案，待報結') LIMIT 1",
                    (applyno, applyno),
                    as_dict=True,
                )
                if row and isinstance(row, dict) and row.get("id"):
                    _update_laf_status_with_approval(db, row, "已結案", "待轉入")
                    logger.info("  DB 更新: %s → 已結案/待轉入", applyno)
            except Exception as e:
                logger.warning("  DB 更新失敗 (%s): %s", applyno, e)

    # 儲存本次狀態供下次比對
    _save_draft_state({
        "date": date.today().isoformat(),
        "closing": sorted(cur_closing),
        "condition": sorted(cur_condition),
        "go_live": sorted(cur_go_live),
    })

    logger.info(
        "Portal 暫存掃描完成：案件狀態區暫存=%d, 結案暫存=%d, 條件待處理=%d, 開辦待處理=%d, 自動確認送出=%d",
        len(result["case_status_drafts"]),
        len(result["closing_drafts"]),
        len(result["condition_pending"]),
        len(result["go_live_pending"]),
        len(result["auto_resolved"]),
    )
    return result


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
    pd_closing = _sanitize_portal_pending_items(pd.get("closing_drafts", []), "報告/結案")
    pd_condition = _sanitize_portal_pending_items(pd.get("condition_pending", []), "報告/二階段")
    pd_go_live = _sanitize_portal_pending_items(pd.get("go_live_pending", []), "報告/開辦")
    pd_resolved = pd.get("auto_resolved", [])

    has_portal_pending = bool(pd_closing or pd_condition or pd_go_live)

    if pd_resolved:
        lines.append(f"✅ 以下案件已確認送出（Portal 已無暫存，不再提醒）：{len(pd_resolved)} 件")
        for it in pd_resolved[:10]:
            lines.append(f"  • {it['applyno']}（{it['label']}）")
        lines.append("")

    if pd_closing:
        lines.append(f"📝 案件狀態區仍有已暫存結案回報（請確認報結情形）：{len(pd_closing)} 件")
        for it in pd_closing[:10]:
            detail_bits = [it.get("reply_type", "結案回報")]
            if it.get("first_reply_date"):
                detail_bits.append(f"首次 {it['first_reply_date']}")
            if it.get("latest_reply_date"):
                detail_bits.append(f"最新 {it['latest_reply_date']}")
            detail = "｜".join(detail_bits)
            lines.append(f"  • {it.get('applyno', '?')} — {detail}")
        lines.append("  👉 來源：案件狀態區 > 回報狀態=暫存")
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

    # 全部正常
    # portal_pending_transfer 是「已送件等法扶審核」，不算需處理
    # portal_new_files 只有仍缺檔的才算需處理
    portal_still_missing = [f for f in portal_new_files if f.get("new_count", 0) > 0]
    if not (still_missing or not_started or can_go_live or portal_unreported or portal_drafted
            or can_close or portal_still_missing or has_portal_pending):
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

    # 2c. Placeholder 案件 reconcile：派案 email 不完整建立的案件，從接案清冊修正 client/案由 + rename 資料夾
    reconcile_result = {}
    if not dry_run:
        try:
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

    # 3b. 上法扶網站驗證「待報結」與「可報結」案件的實際狀態
    #    目的：
    #    1. 避免 portal 其實已有暫存/待轉入/已轉入，卻還被列進 can_close
    #    2. 即使案件狀態區清單掃描偶發抓 0，也先用逐案 portal 驗證兜底
    portal_status = {}
    closing_verify_candidates = []
    seen_keys: set[str] = set()
    for case in [*(status.get("pending_close") or []), *(status.get("can_close") or [])]:
        key = _case_identity_key(case)
        if key and key not in seen_keys:
            seen_keys.add(key)
            closing_verify_candidates.append(case)

    if closing_verify_candidates and not dry_run:
        portal_status = verify_portal_closing_status(closing_verify_candidates, db=db)
        # 更新 status dict：依照 portal 結果分類
        status["portal_drafted"] = portal_status.get("drafted", [])              # 暫存
        status["portal_approved"] = portal_status.get("approved", [])            # 已轉入
        status["portal_pending_transfer"] = portal_status.get("pending_transfer", [])  # 待轉入
        status["portal_unreported"] = portal_status.get("unreported", [])        # 未報結
        status["can_close"] = _filter_can_close_by_portal_status(status["can_close"], portal_status)
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

    # 3e. 可開辦案件自動暫存（填寫表單+截圖，不送出）
    go_live_draft_result = {}
    if status["can_go_live"] and not dry_run:
        go_live_draft_result = _run_go_live_drafts(status["can_go_live"], max_cases=3, db=db)
        status["go_live_draft_result"] = go_live_draft_result
        logger.info("開辦自動暫存結果: processed=%d/%d",
                     go_live_draft_result.get("processed", 0),
                     go_live_draft_result.get("total", 0))

    # 3f. Portal 暫存/待處理全清單掃描（結案、二階段、開辦）
    portal_drafts = {}
    if not dry_run:
        portal_drafts = scan_portal_pending_drafts(db=db)
        status["portal_drafts"] = portal_drafts
        closing_drafts = status["portal_drafts"].get("closing_drafts") or []
        if not closing_drafts and status.get("portal_drafted"):
            fallback_closing = []
            for entry in status.get("portal_drafted", []):
                case = entry.get("case") or {}
                fallback_closing.append({
                    "applyno": _case_laf_number(case),
                    "reply_type": "結案回報",
                    "first_reply_date": "",
                    "latest_reply_date": "",
                    "status": entry.get("closing_status") or "暫存",
                    "row_text": entry.get("portal_info", ""),
                })
            if fallback_closing:
                status["portal_drafts"]["closing_drafts"] = fallback_closing
                logger.info("案件狀態區掃描為 0，已用逐案 portal 驗證補回 %d 件結案提醒", len(fallback_closing))
                closing_drafts = fallback_closing

        if closing_drafts and status.get("portal_drafted"):
            closing_keys = {str(it.get("applyno") or "").strip() for it in closing_drafts if str(it.get("applyno") or "").strip()}
            if closing_keys:
                status["portal_drafted"] = [
                    entry for entry in status["portal_drafted"]
                    if _case_identity_key(entry.get("case") or {}) not in closing_keys
                ]
    else:
        status["portal_drafts"] = {}

    # 4. 格式化報告
    report = format_audit_report(missing_laf, backfilled, status)

    # 5. 儲存報告
    save_report(report)

    # 6. 發送通知
    portal_still_missing = [f for f in portal_new_files if f.get("new_count", 0) > 0]
    _pd = status.get("portal_drafts") or {}
    _has_portal_pending = bool(
        _sanitize_portal_pending_items(_pd.get("closing_drafts", []), "摘要/結案")
        or _sanitize_portal_pending_items(_pd.get("case_status_drafts", []), "摘要/案件狀態區")
        or _sanitize_portal_pending_items(_pd.get("condition_pending", []), "摘要/二階段")
        or _sanitize_portal_pending_items(_pd.get("go_live_pending", []), "摘要/開辦")
    )
    has_issues = bool(
        missing_laf or status["not_started"] or status["can_go_live"]
        or status.get("portal_unreported") or status.get("portal_drafted")
        or status["can_close"] or portal_still_missing or _has_portal_pending
    )
    if notify and not dry_run:
        send_report(report, has_issues=has_issues)

    logger.info("🌙 法扶夜間巡檢完成")
    return {
        "ok": True,
        "missing_laf_count": len(missing_laf),
        "backfilled_count": len(backfilled),
        "not_started_count": len(status["not_started"]),
        "can_go_live_count": len(status["can_go_live"]),
        "pending_close_count": len(status["pending_close"]),
        "portal_drafted_count": len(status.get("portal_drafted", [])),
        "portal_approved_count": len(status.get("portal_approved", [])),
        "portal_unreported_count": len(status.get("portal_unreported", [])),
        "can_close_count": len(status["can_close"]),
        "closing_draft_processed": closing_draft_result.get("processed", 0),
        "portal_new_files_count": len(portal_new_files),
        "portal_auto_downloaded": sum(f.get("auto_downloaded", 0) for f in portal_new_files),
        "portal_still_missing_count": len(portal_still_missing),
        "portal_pending_closing_drafts": len(_sanitize_portal_pending_items(_pd.get("closing_drafts", []))),
        "portal_pending_case_status_drafts": len(_sanitize_portal_pending_items(_pd.get("case_status_drafts", []))),
        "portal_pending_condition": len(_sanitize_portal_pending_items(_pd.get("condition_pending", []))),
        "portal_pending_go_live": len(_sanitize_portal_pending_items(_pd.get("go_live_pending", []))),
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
    parser.add_argument("--mode", choices=["full", "backfill", "reconcile_placeholder"],
                        default="full",
                        help="full=完整巡檢, backfill=只補填案號, reconcile_placeholder=修補不完整 placeholder 案件")
    parser.add_argument("--laf-no", default="", help="只處理特定 LAF 案號（reconcile_placeholder 專用）")
    parser.add_argument("--force", action="store_true", help="跳過 1 小時節流（reconcile_placeholder 專用）")
    args = parser.parse_args()

    if args.mode == "backfill":
        result = run_backfill_only(notify=not args.no_notify)
    elif args.mode == "reconcile_placeholder":
        # 強制 load PROJECT_ROOT/osc.py（含 DatabaseManager），覆蓋
        # casper_ecosystem/law_firm_orchestrators/osc/ package（無 DatabaseManager）
        import sys as _sys
        try:
            import importlib.util as _ilu
            _osc_path = os.path.join(PROJECT_ROOT, "osc.py")
            if os.path.isfile(_osc_path):
                _spec = _ilu.spec_from_file_location("osc", _osc_path)
                _mod = _ilu.module_from_spec(_spec)
                _sys.modules["osc"] = _mod  # 先 register 才能讓 _spec.loader.exec_module 內部 self-import 成功
                _spec.loader.exec_module(_mod)
                logger.info("force-loaded osc.py from %s (has DatabaseManager=%s)",
                            _osc_path, hasattr(_mod, "DatabaseManager"))
        except Exception as _e:
            logger.warning("force load osc.py failed: %s", _e)
        db = _get_db()
        if not db:
            result = {"success": False, "error": "db init failed"}
        else:
            notifier = None
            if not args.no_notify:
                try:
                    from line_notifier import LAFNotifier  # type: ignore
                    notifier = LAFNotifier()
                except Exception:
                    notifier = None
            force = args.force or bool(args.laf_no)
            result = reconcile_placeholder_cases(
                db, force=force, only_laf_no=args.laf_no.strip(), notifier=notifier,
            )
            result["success"] = "error" not in result
    else:
        # Housekeeping: clean up old exports (>30 days)
        try:
            from api.server import cleanup_old_exports
            cleanup_old_exports(days=30)
        except Exception:
            pass
        result = run_audit(notify=not args.no_notify, dry_run=args.dry_run)

    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
