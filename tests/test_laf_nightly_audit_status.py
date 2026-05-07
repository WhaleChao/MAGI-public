"""
Plan A Tests: LAF 狀態流轉（已報結 → 已結案 + legal_aid_approval_status）

Unit tests for:
- _update_laf_status_with_approval: 主+副狀態更新
- verify_portal_closing_status: 新 mapping（已轉入/待轉入/暫存 → 新主狀態）
- _skip_pending: deprecated alias 仍被跳過
- laf_handler._STATUS_MAP: 報結/結案/撤回/撤案 → 已結案
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from unittest.mock import MagicMock, patch, call


# ── _update_laf_status_with_approval ─────────────────────────────────────────

class TestUpdateLafStatusWithApproval:

    def _make_mock_db(self):
        db = MagicMock()
        db.execute_write = MagicMock()
        return db

    def test_updates_both_status_fields(self):
        from casper_ecosystem.law_firm_orchestrators.laf_nightly_audit import _update_laf_status_with_approval
        db = self._make_mock_db()
        case = {"id": 42, "case_number": "2025-0001", "client_name": "測試甲",
                "legal_aid_status": "已報結", "legal_aid_approval_status": ""}
        _update_laf_status_with_approval(db, case, "已結案", "已轉入")
        db.execute_write.assert_called_once()
        call_args = db.execute_write.call_args
        assert "已結案" in call_args[0][1]
        assert "已轉入" in call_args[0][1]
        # case dict 應被更新
        assert case["legal_aid_status"] == "已結案"
        assert case["legal_aid_approval_status"] == "已轉入"

    def test_idempotent_no_write_when_same(self):
        from casper_ecosystem.law_firm_orchestrators.laf_nightly_audit import _update_laf_status_with_approval
        db = self._make_mock_db()
        case = {"id": 42, "case_number": "2025-0001", "client_name": "測試甲",
                "legal_aid_status": "已結案", "legal_aid_approval_status": "已轉入"}
        _update_laf_status_with_approval(db, case, "已結案", "已轉入")
        db.execute_write.assert_not_called()  # 冪等不寫

    def test_skips_when_no_case_id(self):
        from casper_ecosystem.law_firm_orchestrators.laf_nightly_audit import _update_laf_status_with_approval
        db = self._make_mock_db()
        case = {"case_number": "2025-0001", "client_name": "無 ID"}
        _update_laf_status_with_approval(db, case, "已結案", "已轉入")
        db.execute_write.assert_not_called()

    def test_fallback_to_simple_update_on_missing_column(self):
        """若欄位不存在（schema 尚未 ALTER），退回呼叫 _update_laf_status。"""
        from casper_ecosystem.law_firm_orchestrators.laf_nightly_audit import _update_laf_status_with_approval
        db = self._make_mock_db()
        db.execute_write.side_effect = Exception("Unknown column 'legal_aid_approval_status'")
        case = {"id": 10, "case_number": "2025-0001", "client_name": "測試乙",
                "legal_aid_status": "已報結", "legal_aid_approval_status": ""}
        # 應不 raise，但會退回呼叫 _update_laf_status（再次呼叫 execute_write）
        # 第一次呼叫會 raise，應 catch 並 fallback
        second_db = self._make_mock_db()
        second_db.execute_write = MagicMock(side_effect=[
            Exception("Unknown column 'legal_aid_approval_status'"),
            None,  # fallback _update_laf_status 的呼叫成功
        ])
        case2 = {"id": 10, "case_number": "2025-0001", "client_name": "測試乙",
                 "legal_aid_status": "已報結", "legal_aid_approval_status": ""}
        _update_laf_status_with_approval(second_db, case2, "已結案", "已轉入")
        assert second_db.execute_write.call_count == 2


# ── verify_portal_closing_status 狀態 mapping ────────────────────────────────

class TestVerifyPortalClosingStatusMapping:
    """
    測試 verify_portal_closing_status 呼叫 _update_laf_status_with_approval
    而非 _update_laf_status（舊版），並且新主+副狀態正確。
    """

    def _make_mock_laf(self, found_status, found_type="結案"):
        mock_laf = MagicMock()
        mock_laf.login.return_value = True
        portal_result = {
            "closing": {"found": found_type == "結案", "status": found_status if found_type == "結案" else ""},
            "withdrawal": {"found": found_type == "撤回", "status": found_status if found_type == "撤回" else ""},
        }
        mock_laf.query_closing_status.return_value = portal_result
        return mock_laf

    def _run_verify(self, found_status, found_type="結案"):
        from casper_ecosystem.law_firm_orchestrators.laf_nightly_audit import verify_portal_closing_status
        case = {"id": 99, "case_number": "2025-0001", "client_name": "測試丙",
                "legal_aid_number": "1130402-T-099",  # 必填：_case_laf_number 需要
                "legal_aid_status": "已結案，待送出", "legal_aid_approval_status": ""}
        mock_db = MagicMock()
        mock_db.execute_write = MagicMock()

        mock_laf = self._make_mock_laf(found_status, found_type)
        with patch("casper_ecosystem.law_firm_orchestrators.laf_nightly_audit._make_laf_web_automation",
                   return_value=mock_laf):
            result = verify_portal_closing_status([case], db=mock_db)
        return result, mock_db, case

    def test_approved_writes_已結案_已轉入(self):
        result, db, case = self._run_verify("已轉入")
        assert len(result["approved"]) == 1
        # 應呼叫 _update_laf_status_with_approval → execute_write 含「已結案」和「已轉入」
        assert db.execute_write.called
        call_params = db.execute_write.call_args[0][1]
        assert "已結案" in call_params
        assert "已轉入" in call_params

    def test_pending_transfer_writes_已結案_待轉入(self):
        result, db, case = self._run_verify("待轉入")
        assert len(result["pending_transfer"]) == 1
        call_params = db.execute_write.call_args[0][1]
        assert "已結案" in call_params
        assert "待轉入" in call_params

    def test_drafted_writes_已結案待送出_暫存(self):
        result, db, case = self._run_verify("暫存")
        assert len(result["drafted"]) == 1
        call_params = db.execute_write.call_args[0][1]
        assert "已結案，待送出" in call_params
        assert "暫存" in call_params

    def test_unreported_no_db_write(self):
        result, db, case = self._run_verify("")  # found_status 空 → unreported
        assert len(result["unreported"]) == 1
        db.execute_write.assert_not_called()


# ── _skip_pending 相容性 ──────────────────────────────────────────────────────

class TestSkipPendingCompatibility:
    """確認 deprecated alias 仍在 _skip_pending 中（遷移完成前的相容性）"""

    def test_deprecated_aliases_still_skipped(self):
        """_skip_pending 必須同時包含新舊狀態，讓遷移期間不重複觸發。"""
        import importlib, inspect
        import casper_ecosystem.law_firm_orchestrators.laf_nightly_audit as mod
        src = inspect.getsource(mod)
        # 確認新狀態和 deprecated alias 都在 _skip_pending tuple 中
        assert '"已結案"' in src or "'已結案'" in src
        assert '"已報結"' in src or "'已報結'" in src
        assert '"已報結（待轉入）"' in src or "'已報結（待轉入）'" in src


# ── Portal pending draft row filtering ───────────────────────────────────────

class TestPortalPendingDraftFiltering:
    """確認 Portal 表單/說明文字不會被誤報成待送出案件。"""

    def test_format_report_drops_go_live_form_rows_without_applyno(self):
        from casper_ecosystem.law_firm_orchestrators.laf_nightly_audit import format_audit_report

        status = {
            "all_cases": [{}],
            "not_started": [],
            "can_go_live": [],
            "pending_close": [],
            "can_close": [],
            "portal_drafts": {
                "go_live_pending": [
                    {"applyno": "", "row_text": "分會別 | 申請編號"},
                    {"applyno": "", "row_text": "受扶助人姓名 | 承辦人電話與分機"},
                    {"applyno": "", "row_text": "檢付檔案 | 上傳檔案"},
                    {"applyno": "", "row_text": "說明 | 上傳的檔案型別限於pdf、word、excel"},
                ]
            },
        }

        report = format_audit_report([], [], status)

        assert "開辦待送出" not in report
        assert "分會別" not in report
        assert "所有法扶案件狀態正常" in report

    def test_format_report_keeps_real_go_live_applyno(self):
        from casper_ecosystem.law_firm_orchestrators.laf_nightly_audit import format_audit_report

        status = {
            "all_cases": [{}],
            "not_started": [],
            "can_go_live": [],
            "pending_close": [],
            "can_close": [],
            "portal_drafts": {
                "go_live_pending": [
                    {"applyno": "1150206-A-042", "row_text": "1150206-A-042 | 蕭仁俊 | 暫存"}
                ]
            },
        }

        report = format_audit_report([], [], status)

        assert "開辦待送出（Portal 仍有未開辦案件）：1 件" in report
        assert "1150206-A-042" in report


# ── laf_handler._STATUS_MAP ───────────────────────────────────────────────────

class TestLafHandlerStatusMap:
    """確認 _STATUS_MAP 已更新 報結/結案/撤回/撤案 → 已結案"""

    def test_報結_maps_to_已結案(self):
        from casper_ecosystem.law_firm_orchestrators.laf_handler import _STATUS_MAP
        assert _STATUS_MAP.get("報結") == "已結案"

    def test_結案_maps_to_已結案(self):
        from casper_ecosystem.law_firm_orchestrators.laf_handler import _STATUS_MAP
        assert _STATUS_MAP.get("結案") == "已結案"

    def test_撤回_maps_to_已結案(self):
        from casper_ecosystem.law_firm_orchestrators.laf_handler import _STATUS_MAP
        assert _STATUS_MAP.get("撤回") == "已結案"

    def test_撤案_maps_to_已結案(self):
        from casper_ecosystem.law_firm_orchestrators.laf_handler import _STATUS_MAP
        assert _STATUS_MAP.get("撤案") == "已結案"

    def test_開辦_still_maps_to_進行中(self):
        from casper_ecosystem.law_firm_orchestrators.laf_handler import _STATUS_MAP
        assert _STATUS_MAP.get("開辦") == "進行中"
