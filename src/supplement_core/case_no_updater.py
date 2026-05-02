# -*- coding: utf-8 -*-
"""
case_no_updater.py — M10 → M12 重構：通用化案號同步移到 api.osc.case_no_sync。
本檔保留以維持既有 import 不破裂（向後相容）。

設計原則：module 頂層不 import api.* 以保持 standalone 可測試性。
所有委派呼叫於函式執行時才 lazy import api.osc.case_no_sync。
"""
from __future__ import annotations

import re
import unicodedata
from typing import Optional


_GENERAL_CACHE: dict = {}


def _get_general():
    """Lazy import api.osc.case_no_sync。

    用 importlib.util.spec_from_file_location 直接載入檔案，
    **繞過** api/osc/__init__.py（避免測試環境因 flask_login 缺失而炸）。
    生產環境照樣走正常 import；快取在 _GENERAL_CACHE 避免重複載入。
    """
    if _GENERAL_CACHE:
        m = _GENERAL_CACHE["mod"]
        return m.extract_court_case_no, m.verify_filename_for_case, m.sync_case_no_from_notices

    import importlib.util
    import os
    import sys

    here = os.path.dirname(os.path.abspath(__file__))
    sync_path = os.path.normpath(os.path.join(here, "..", "..", "api", "osc", "case_no_sync.py"))

    if not os.path.isfile(sync_path):
        # fallback: 標準 import（生產環境）
        from api.osc.case_no_sync import (
            extract_court_case_no, verify_filename_for_case, sync_case_no_from_notices,
        )
        class _M: pass
        m = _M()
        m.extract_court_case_no = extract_court_case_no
        m.verify_filename_for_case = verify_filename_for_case
        m.sync_case_no_from_notices = sync_case_no_from_notices
        _GENERAL_CACHE["mod"] = m
        return extract_court_case_no, verify_filename_for_case, sync_case_no_from_notices

    spec = importlib.util.spec_from_file_location("api_osc_case_no_sync_isolated", sync_path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    _GENERAL_CACHE["mod"] = m
    return m.extract_court_case_no, m.verify_filename_for_case, m.sync_case_no_from_notices


# ── 維持 M10 原有內部 API（測試直接 import 這些函式）────────────────────────────

def _extract_case_no_from_filename(filename: str) -> tuple[str, Optional[str]]:
    """M10 內部介面 → 委派至 M12 通用抽取（lazy）。"""
    filename = unicodedata.normalize("NFC", filename)
    _extract_general, _, _ = _get_general()
    result = _extract_general(filename)
    return (result["case_no"], result["institution"])


def _verify(filename: str, case_meta: dict, court_extracted: Optional[str]) -> dict:
    """M10 內部驗證介面 → 委派至 M12 通用驗證（lazy）。

    保留相同回傳格式（含 court_match key）以相容 test_m10。
    """
    filename = unicodedata.normalize("NFC", filename)
    _, _verify_general, _ = _get_general()

    parties = case_meta.get("parties", [])
    party_name = parties[0] if parties else ""

    # M10 是消債專用，type_match 一律以消費者債務清理關鍵字判斷（不 skip）
    effective_case_type = case_meta.get("case_type") or "消費者債務清理"
    result = _verify_general(
        filename,
        party_name=party_name,
        case_type=effective_case_type,
        institution_hint=case_meta.get("court_name"),
    )
    # 將 institution_match 映射回 M10 的 court_match key
    return {
        "name_match": result["name_match"],
        "type_match": result["type_match"],
        "court_match": result["institution_match"],
        "score": result["score"],
    }


def _update_db_case_no(case_dir: str, new_case_no: str, new_court: Optional[str]) -> bool:
    """M10 DB 更新介面 → 直接用 case_dir 反查 id 然後更新。"""
    try:
        from api.osc.drafts import _osc_exec
    except ImportError:
        try:
            import api.server as _srv
            _osc_exec = _srv._osc_exec
        except (ImportError, AttributeError):
            return False

    sql_select = "SELECT id, court_case_number FROM cases WHERE folder_path=%s LIMIT 1"
    try:
        row = _osc_exec(sql_select, (case_dir,), fetch="one")
    except Exception:
        return False
    if not row:
        return False

    sql_update = (
        "UPDATE cases "
        "SET court_case_number=%s, court_case_no=%s, "
        "court_name=COALESCE(NULLIF(%s,''), court_name) "
        "WHERE id=%s"
    )
    try:
        _osc_exec(sql_update, (new_case_no, new_case_no, new_court or "", row["id"]))
        return True
    except Exception:
        return False


def update_case_no_from_notices(
    case_meta: dict,
    notices: list[dict],
    *,
    dry_run: bool = False,
) -> dict:
    """M10 公開介面 → M12 通用模組（lazy import）。

    維持 M10 原有回傳 key（current_case_no / new_court）以向後相容。
    """
    _, _, _sync_general = _get_general()

    parties = case_meta.get("parties", [])
    case_record = {
        "id": case_meta.get("case_id"),
        "case_dir": case_meta.get("case_dir", ""),
        "client_name": parties[0] if parties else "",
        "case_type": case_meta.get("case_type", "消費者債務清理"),
        "current_court_case_no": case_meta.get("court_case_number", "") or "",
        "current_institution": case_meta.get("court_name", "") or "",
    }

    result = _sync_general(case_record, notices, dry_run=dry_run)

    # 將 M12 格式的回傳鍵映射回 M10 格式（向後相容）
    return {
        "current_case_no": result.get("current_case_no", ""),
        "new_case_no": result.get("new_case_no"),
        "new_court": result.get("new_institution"),
        "source_pdf": result.get("source_pdf"),
        "verification": result.get("verification", {}),
        "updated": result.get("updated", False),
        "errors": result.get("errors", []),
    }
