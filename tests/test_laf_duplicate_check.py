"""T1 tests: _check_duplicate root fix ([當事人S]/[當事人B] scenario).

Tests the fixed logic by embedding it in a minimal stub class.
"""
import logging
import sys, os
from unittest.mock import MagicMock
import pytest

logger = logging.getLogger("test_dup")


# ── Minimal stub replicating the fixed _check_duplicate logic ──
class _OrchestratorStub:
    def __init__(self, db):
        self.db = db
        self.dry_run = False

    def _norm_token(self, name):
        return (name or "").strip()

    def _check_duplicate(self, laf_number, client_name, case_type, case_reason):
        """Fixed version: Strategy 1 validates client_name; Strategy 2 requires laf_no match when reason is empty."""
        if not self.db:
            return None
        try:
            laf_number = str(laf_number or "").strip()
            client_key = self._norm_token(client_name)

            # Strategy 1: LAF number exact match
            if laf_number:
                result = self.db.fetch_one(
                    "SELECT * FROM `cases` WHERE `legal_aid_number` = %s LIMIT 1",
                    (laf_number,), as_dict=True
                )
                if result:
                    return result

                # Strategy 1b: notes LIKE — must also validate client_name
                rows_by_notes = self.db.fetch_all(
                    "SELECT * FROM `cases` WHERE `notes` LIKE %s LIMIT 20",
                    (f"%{laf_number}%",), as_dict=True
                )
                for result in (rows_by_notes or []):
                    if not isinstance(result, dict):
                        continue
                    db_client_key = self._norm_token(result.get("client_name"))
                    if client_key and db_client_key and db_client_key != client_key:
                        continue  # Different client — skip (馬/黃 fix)
                    return result

            # Strategy 2: Name + type + category
            if client_key and case_type:
                rows = self.db.fetch_all(
                    """SELECT * FROM `cases`
                       WHERE `case_type` = %s
                       AND `case_category` = '法律扶助案件'
                       ORDER BY `created_date` DESC LIMIT 50""",
                    (case_type,), as_dict=True
                )
                for result in (rows or []):
                    if not isinstance(result, dict):
                        continue
                    db_client_key = self._norm_token(result.get("client_name"))
                    if db_client_key != client_key:
                        continue

                    existing_laf = str(result.get("legal_aid_number") or "").strip()
                    db_reason = str(result.get("case_reason") or "").strip()
                    _reason_empty = not case_reason or case_reason in ("待確認", "")

                    if laf_number:
                        notes = str(result.get("notes") or "")
                        if existing_laf and existing_laf != laf_number and laf_number not in notes:
                            continue
                        # When reason is empty, require laf_no to match exactly
                        if _reason_empty and existing_laf and existing_laf != laf_number:
                            continue

                    if case_reason and not _reason_empty:
                        if db_reason and (case_reason in db_reason or db_reason in case_reason):
                            return result
                        continue

                    return result

        except Exception as e:
            logger.error("Duplicate check error: %s", e)

        return None


def _make_orch(db_rows_by_laf=None, db_rows_by_notes=None, db_rows_by_type=None):
    db = MagicMock()

    def _fetch_one(sql, params=None, as_dict=False):
        if db_rows_by_laf is not None and "legal_aid_number" in sql:
            return (db_rows_by_laf or {}).get(params[0] if params else "")
        return None

    def _fetch_all(sql, params=None, as_dict=False):
        # SQL uses backtick: `notes` LIKE — match on 'LIKE' presence when no 'case_type'
        if ("notes" in sql and "LIKE" in sql) and db_rows_by_notes is not None:
            return list(db_rows_by_notes)
        if "case_type" in sql and db_rows_by_type is not None:
            return list(db_rows_by_type)
        return []

    db.fetch_one = _fetch_one
    db.fetch_all = _fetch_all
    return _OrchestratorStub(db)


# ── Test A: 全新案號 + 全新姓名 → 無重複 ──
def test_A_new_laf_new_client_no_dup():
    orch = _make_orch(db_rows_by_laf={}, db_rows_by_notes=[], db_rows_by_type=[])
    assert orch._check_duplicate("1160101-A-001", "全新當事人", "民事", "損害賠償") is None


# ── Test B: 新案號，但舊案 notes 含案號 substring，client_name 不同 → 無重複（馬/黃情境）──
def test_B_notes_substring_different_client_no_dup():
    old_huang = {
        "id": 100, "legal_aid_number": "1140601-B-001",
        "client_name": "[當事人B]", "case_type": "刑事",
        "case_reason": "竊盜", "notes": "相關案號：1140910-E-010"
    }
    orch = _make_orch(db_rows_by_laf={}, db_rows_by_notes=[old_huang], db_rows_by_type=[])
    result = orch._check_duplicate("1140910-E-010", "[當事人S]", "民事", "損害賠償")
    assert result is None, "不同 client_name 不應被誤判重複"


# ── Test C: 同 client+type，case_reason 為空，不同 laf_no → 無重複 ──
def test_C_same_client_empty_reason_different_laf_no_dup():
    existing = {
        "id": 200, "legal_aid_number": "1140601-A-001",
        "client_name": "測試人", "case_type": "民事",
        "case_reason": "", "notes": ""
    }
    orch = _make_orch(db_rows_by_laf={}, db_rows_by_notes=[], db_rows_by_type=[existing])
    result = orch._check_duplicate("1140910-A-099", "測試人", "民事", "")
    assert result is None, "case_reason 為空時，不同 laf_no 不應被合併"


# ── Test D: 完全相同 laf_case_no → 重複 ──
def test_D_same_laf_no_is_dup():
    existing = {
        "id": 300, "legal_aid_number": "1140601-A-001",
        "client_name": "測試人", "case_type": "民事",
        "case_reason": "損害賠償", "notes": ""
    }
    orch = _make_orch(db_rows_by_laf={"1140601-A-001": existing}, db_rows_by_notes=[], db_rows_by_type=[])
    result = orch._check_duplicate("1140601-A-001", "測試人", "民事", "損害賠償")
    assert result is not None
    assert result["id"] == 300


# ── Test E: notes LIKE 命中 + client_name 相同 → 重複 ──
def test_E_notes_same_client_is_dup():
    existing = {
        "id": 400, "legal_aid_number": "1140601-C-001",
        "client_name": "王大明", "case_type": "民事",
        "case_reason": "借貸", "notes": "1140601-C-001 舊派案"
    }
    orch = _make_orch(db_rows_by_laf={}, db_rows_by_notes=[existing], db_rows_by_type=[])
    result = orch._check_duplicate("1140601-C-001", "王大明", "民事", "借貸")
    assert result is not None
    assert result["id"] == 400
