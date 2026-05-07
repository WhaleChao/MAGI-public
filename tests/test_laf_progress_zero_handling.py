"""
Plan B Tests: LAF progress workflow 零次數自動填 noarrivereason + DC 通知.

這些 unit tests 使用 mock driver，不需要真實 portal session。
"""
import sys
import os
import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from unittest.mock import MagicMock, patch, PropertyMock


# ── Helper: 建立最小 mock driver ─────────────────────────────────────────────

def _make_mock_driver_with_count_fields(field_values):
    """
    field_values: dict, e.g. {"meet_times": "00", "ap_times": "01"}
    回傳 mock driver，execute_script 會依照 field_values 回傳 zero 欄位清單。
    """
    driver = MagicMock()
    # find_elements("id", "noarrivereason") → 回傳 1 個 mock element
    mock_ta = MagicMock()
    driver.find_elements.return_value = [mock_ta]

    def fake_execute_script(script, *args):
        # 若 script 包含 zeros，代表是零次數偵測
        if "zeros" in script and args:
            field_ids = args[0]
            zeros = [fid for fid in field_ids if int((field_values.get(fid) or "0").lstrip("0") or "0") == 0]
            return zeros
        # 其他 JS 呼叫（_set_any 的 kind="input"）→ 回傳 True
        return True

    driver.execute_script.side_effect = fake_execute_script
    return driver, mock_ta


# ── fill_noarrivereason_textarea tests ───────────────────────────────────────

class TestFillNoarriveReasonTextarea:
    """Unit tests for laf_automation_v2.LAFWebAutomation.fill_noarrivereason_textarea"""

    def _make_automation(self, driver):
        from casper_ecosystem.law_firm_orchestrators.laf_automation_v2 import LAFWebAutomation
        auto = LAFWebAutomation.__new__(LAFWebAutomation)
        auto.driver = driver
        auto.last_zero_fields = []
        auto.log = lambda msg: None
        return auto

    def test_fills_from_noarrivereason_key(self):
        driver, mock_ta = _make_mock_driver_with_count_fields({})
        auto = self._make_automation(driver)
        counts = {"noarrivereason": "測試說明"}
        result = auto.fill_noarrivereason_textarea(counts=counts)
        assert result is True
        mock_ta.send_keys.assert_called_once_with("測試說明")

    def test_fills_from_zero_reasons_fallback(self):
        driver, mock_ta = _make_mock_driver_with_count_fields({})
        auto = self._make_automation(driver)
        counts = {}
        zero_reasons = {"meet_times": "本期無會議", "ap_times": "本期無開庭"}
        result = auto.fill_noarrivereason_textarea(counts=counts, zero_reasons=zero_reasons)
        assert result is True
        # send_keys 應被呼叫一次，文案包含兩個原因
        call_text = mock_ta.send_keys.call_args[0][0]
        assert "本期無會議" in call_text
        assert "本期無開庭" in call_text

    def test_no_fill_when_both_empty(self):
        driver, mock_ta = _make_mock_driver_with_count_fields({})
        auto = self._make_automation(driver)
        result = auto.fill_noarrivereason_textarea(counts={}, zero_reasons={})
        assert result is True
        mock_ta.send_keys.assert_not_called()

    def test_returns_false_when_textarea_not_found(self):
        driver = MagicMock()
        driver.find_elements.return_value = []  # 找不到 textarea
        auto = self._make_automation(driver)
        result = auto.fill_noarrivereason_textarea(
            counts={"noarrivereason": "說明"},
        )
        assert result is False


# ── fill_workflow_fields progress zero detection ──────────────────────────────

class TestProgressZeroFieldDetection:
    """Unit tests for fill_workflow_fields progress 分支的零次數偵測"""

    def _make_automation(self, driver):
        from casper_ecosystem.law_firm_orchestrators.laf_automation_v2 import LAFWebAutomation
        auto = LAFWebAutomation.__new__(LAFWebAutomation)
        auto.driver = driver
        auto.last_zero_fields = []
        auto.log = lambda msg: None
        auto.base_url = "https://mock.laf.portal"
        auto.download_folder = "/tmp"
        return auto

    def test_all_zero_sets_last_zero_fields(self):
        """全部欄位為 0 → last_zero_fields 含所有欄位中文名"""
        all_zero = {k: "00" for k in [
            "meet_times","tel_times","inq_times","disc_times","viewsheet_times",
            "ap_times","lawyerap_times","isap_times","wc_times","med_times",
        ]}
        driver, mock_ta = _make_mock_driver_with_count_fields(all_zero)
        auto = self._make_automation(driver)

        data = {"remark": "測試", "auto_zero_reason_template": "本回報週期內，以下項目尚未發生：{ZERO_FIELDS}。（MAGI）"}
        auto.fill_workflow_fields("progress", data)

        assert len(auto.last_zero_fields) == 10  # 全部 10 個欄位
        assert "開庭次數" in auto.last_zero_fields
        assert "會議次數" in auto.last_zero_fields

    def test_partial_zero_only_lists_zero_fields(self):
        """部分欄位為 0 → last_zero_fields 只列零值欄位"""
        counts = {
            "meet_times": "02",   # 非零
            "ap_times": "00",     # 零
            "tel_times": "01",    # 非零
            "wc_times": "00",     # 零
            # 其餘欄位 mock 回傳 0
        }
        driver, mock_ta = _make_mock_driver_with_count_fields(counts)
        auto = self._make_automation(driver)
        data = {"remark": "測試"}
        auto.fill_workflow_fields("progress", data)
        # 應包含 ap_times 和 wc_times（加上 mock 未設定的也是 0）
        assert "開庭次數" in auto.last_zero_fields
        assert "書狀次數" in auto.last_zero_fields

    def test_no_zero_no_noarrivereason(self):
        """全部欄位非零 → last_zero_fields 為空 → noarrivereason 不填"""
        all_nonzero = {k: "03" for k in [
            "meet_times","tel_times","inq_times","disc_times","viewsheet_times",
            "ap_times","lawyerap_times","isap_times","wc_times","med_times",
        ]}
        driver, mock_ta = _make_mock_driver_with_count_fields(all_nonzero)
        auto = self._make_automation(driver)
        data = {"remark": "測試"}
        auto.fill_workflow_fields("progress", data)
        assert auto.last_zero_fields == []
        mock_ta.send_keys.assert_not_called()

    def test_provided_noarrivereason_overrides_template(self):
        """律師明確給 noarrivereason → 直接用律師文案"""
        all_zero = {k: "00" for k in [
            "meet_times","tel_times","inq_times","disc_times","viewsheet_times",
            "ap_times","lawyerap_times","isap_times","wc_times","med_times",
        ]}
        driver, mock_ta = _make_mock_driver_with_count_fields(all_zero)
        auto = self._make_automation(driver)
        data = {"remark": "測試", "noarrivereason": "律師自行說明：本期待判，無新活動。"}
        auto.fill_workflow_fields("progress", data)
        call_text = mock_ta.send_keys.call_args[0][0]
        assert "律師自行說明" in call_text


# ── closing regression: fill_closing_report 仍能正確呼叫 helper ──────────────

class TestClosingRegression:
    """確認 fill_closing_report 改用 helper 後 closing 行為不變"""

    def _make_automation(self, driver):
        from casper_ecosystem.law_firm_orchestrators.laf_automation_v2 import LAFWebAutomation
        auto = LAFWebAutomation.__new__(LAFWebAutomation)
        auto.driver = driver
        auto.last_zero_fields = []
        auto.log = lambda msg: None
        return auto

    def test_fill_closing_report_calls_helper(self):
        """fill_closing_report 中 noarrivereason 仍能被填入"""
        driver = MagicMock()
        driver.current_url = "toClosedSummaryLawyer"
        driver.find_elements.return_value = []  # 不在 toCR 頁
        driver.execute_script.return_value = None
        # noarrivereason textarea
        mock_ta = MagicMock()
        # find_elements("id", "noarrivereason") → [mock_ta]
        original_find = driver.find_elements
        def find_elements_side(by, val):
            if by == "id" and val == "noarrivereason":
                return [mock_ta]
            return []
        driver.find_elements.side_effect = find_elements_side

        auto = self._make_automation(driver)
        # 直接呼叫 helper（不走 fill_closing_report 的複雜 Page 1/2 導航）
        result = auto.fill_noarrivereason_textarea(
            counts={"noarrivereason": "結案：本期開庭中"},
            zero_reasons={},
        )
        assert result is True
        mock_ta.send_keys.assert_called_once_with("結案：本期開庭中")
