# -*- coding: utf-8 -*-
"""
Tests for skills.engine.ocr.consensus.

所有測試均 mock tesseract_provider.run 與 apple_vision_provider.run，
不真跑任何 binary 或 Vision framework。

禁止在 module level import api.server / api.tools_api / daemon（SIGCHLD 守則）。
"""

from __future__ import annotations

import os
import tempfile
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _make_provider_result(
    success=True,
    provider="tesseract",
    raw_text="",
    corrected_text="",
    quality_score=0.5,
    case_numbers=None,
    roc_dates=None,
    courts=None,
    parties=None,
    laf_case_numbers=None,
    error=None,
    timed_out=False,
):
    """建立 OCRProviderResult mock（使用真實 dataclass）。"""
    from skills.engine.ocr.ocr_schema import OCRProviderResult, OCREntities
    ents = None
    if success:
        ents = OCREntities(
            case_numbers=case_numbers or [],
            roc_dates=roc_dates or [],
            courts=courts or [],
            parties=parties or [],
            laf_case_numbers=laf_case_numbers or [],
        )
    return OCRProviderResult(
        success=success,
        provider=provider,
        raw_text=raw_text,
        corrected_text=corrected_text,
        quality_score=quality_score,
        entities=ents,
        error=error,
        timed_out=timed_out,
    )


def _tmp_image():
    """建立暫存 PNG 檔（內容不重要，只需存在）。"""
    f = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    f.write(b"\x89PNG\r\n\x1a\n" + b"\x00" * 8)
    f.close()
    return f.name


# ---------------------------------------------------------------------------
# 基本成功路徑
# ---------------------------------------------------------------------------

class TestConsensusBasicSuccess:
    @patch("skills.engine.ocr.consensus.apple_vision_provider.run")
    @patch("skills.engine.ocr.consensus.tesseract_provider.run")
    def test_both_success_returns_success(self, mock_tess, mock_vision):
        mock_tess.return_value = _make_provider_result(
            provider="tesseract",
            raw_text="臺灣花蓮地方法院",
            corrected_text="臺灣花蓮地方法院",
            quality_score=0.7,
            case_numbers=["114年度訴字第123號"],
            courts=["臺灣花蓮地方法院"],
        )
        mock_vision.return_value = _make_provider_result(
            provider="apple_vision",
            raw_text="臺灣花蓮地方法院",
            corrected_text="臺灣花蓮地方法院",
            quality_score=0.72,
            case_numbers=["114年度訴字第123號"],
            courts=["臺灣花蓮地方法院"],
        )

        tmp = _tmp_image()
        try:
            from skills.engine.ocr.consensus import run_consensus
            result = run_consensus(tmp)
            assert result.success is True
            assert result.confidence > 0.0
            assert result.provider_results["tesseract"].success is True
            assert result.provider_results["apple_vision"].success is True
        finally:
            os.unlink(tmp)

    @patch("skills.engine.ocr.consensus.apple_vision_provider.run")
    @patch("skills.engine.ocr.consensus.tesseract_provider.run")
    def test_returns_consensus_result_type(self, mock_tess, mock_vision):
        from skills.engine.ocr.ocr_schema import OCRConsensusResult
        mock_tess.return_value = _make_provider_result(
            provider="tesseract", corrected_text="法院", quality_score=0.5
        )
        mock_vision.return_value = _make_provider_result(
            provider="apple_vision", corrected_text="法院", quality_score=0.5
        )
        tmp = _tmp_image()
        try:
            from skills.engine.ocr.consensus import run_consensus
            result = run_consensus(tmp)
            assert isinstance(result, OCRConsensusResult)
        finally:
            os.unlink(tmp)

    @patch("skills.engine.ocr.consensus.apple_vision_provider.run")
    @patch("skills.engine.ocr.consensus.tesseract_provider.run")
    def test_high_agreement_high_confidence(self, mock_tess, mock_vision):
        """兩邊案號一致、法院一致、日期一致 → confidence 應 >= 0.75。"""
        mock_tess.return_value = _make_provider_result(
            provider="tesseract",
            corrected_text="臺灣花蓮地方法院\n114年度訴字第123號\n114年3月15日",
            quality_score=0.8,
            case_numbers=["114年度訴字第123號"],
            roc_dates=["114年3月15日"],
            courts=["臺灣花蓮地方法院"],
        )
        mock_vision.return_value = _make_provider_result(
            provider="apple_vision",
            corrected_text="臺灣花蓮地方法院\n114年度訴字第123號\n114年3月15日",
            quality_score=0.82,
            case_numbers=["114年度訴字第123號"],
            roc_dates=["114年3月15日"],
            courts=["臺灣花蓮地方法院"],
        )
        tmp = _tmp_image()
        try:
            from skills.engine.ocr.consensus import run_consensus
            result = run_consensus(tmp)
            assert result.confidence >= 0.75
            assert result.writable is True
            assert result.critical_conflict is False
        finally:
            os.unlink(tmp)

    @patch("skills.engine.ocr.consensus.apple_vision_provider.run")
    @patch("skills.engine.ocr.consensus.tesseract_provider.run")
    def test_selected_text_nonempty(self, mock_tess, mock_vision):
        mock_tess.return_value = _make_provider_result(
            provider="tesseract",
            corrected_text="臺灣法院判決書",
            quality_score=0.6,
        )
        mock_vision.return_value = _make_provider_result(
            provider="apple_vision",
            corrected_text="臺灣法院判決書",
            quality_score=0.65,
        )
        tmp = _tmp_image()
        try:
            from skills.engine.ocr.consensus import run_consensus
            result = run_consensus(tmp)
            assert result.success is True
            assert len(result.selected_text) > 0
        finally:
            os.unlink(tmp)

    @patch("skills.engine.ocr.consensus.apple_vision_provider.run")
    @patch("skills.engine.ocr.consensus.tesseract_provider.run")
    def test_corrected_text_populated(self, mock_tess, mock_vision):
        mock_tess.return_value = _make_provider_result(
            provider="tesseract",
            raw_text="法院１１４年度訴字第１２３號",
            corrected_text="法院114年度訴字第123號",
            quality_score=0.5,
        )
        mock_vision.return_value = _make_provider_result(
            provider="apple_vision",
            raw_text="法院114年度訴字第123號",
            corrected_text="法院114年度訴字第123號",
            quality_score=0.55,
        )
        tmp = _tmp_image()
        try:
            from skills.engine.ocr.consensus import run_consensus
            result = run_consensus(tmp)
            assert "114" in result.corrected_text
        finally:
            os.unlink(tmp)


# ---------------------------------------------------------------------------
# 單邊失敗 / 不可用
# ---------------------------------------------------------------------------

class TestConsensusSingleProvider:
    @patch("skills.engine.ocr.consensus.apple_vision_provider.run")
    @patch("skills.engine.ocr.consensus.tesseract_provider.run")
    def test_tesseract_only_still_succeeds(self, mock_tess, mock_vision):
        mock_tess.return_value = _make_provider_result(
            provider="tesseract",
            corrected_text="法院判決",
            quality_score=0.6,
        )
        mock_vision.return_value = _make_provider_result(
            success=False,
            provider="apple_vision",
            error="not available",
        )
        tmp = _tmp_image()
        try:
            from skills.engine.ocr.consensus import run_consensus
            result = run_consensus(tmp)
            assert result.success is True
            assert "法院判決" in result.selected_text
        finally:
            os.unlink(tmp)

    @patch("skills.engine.ocr.consensus.apple_vision_provider.run")
    @patch("skills.engine.ocr.consensus.tesseract_provider.run")
    def test_vision_only_still_succeeds(self, mock_tess, mock_vision):
        mock_tess.return_value = _make_provider_result(
            success=False,
            provider="tesseract",
            error="not available",
        )
        mock_vision.return_value = _make_provider_result(
            provider="apple_vision",
            corrected_text="法院判決",
            quality_score=0.65,
        )
        tmp = _tmp_image()
        try:
            from skills.engine.ocr.consensus import run_consensus
            result = run_consensus(tmp)
            assert result.success is True
            assert "法院判決" in result.selected_text
        finally:
            os.unlink(tmp)

    @patch("skills.engine.ocr.consensus.apple_vision_provider.run")
    @patch("skills.engine.ocr.consensus.tesseract_provider.run")
    def test_single_provider_has_warning(self, mock_tess, mock_vision):
        mock_tess.return_value = _make_provider_result(
            success=False,
            provider="tesseract",
            error="binary not found",
        )
        mock_vision.return_value = _make_provider_result(
            provider="apple_vision",
            corrected_text="法院",
            quality_score=0.5,
        )
        tmp = _tmp_image()
        try:
            from skills.engine.ocr.consensus import run_consensus
            result = run_consensus(tmp)
            assert result.success is True
            assert any("tesseract" in w for w in result.warnings)
        finally:
            os.unlink(tmp)

    @patch("skills.engine.ocr.consensus.apple_vision_provider.run")
    @patch("skills.engine.ocr.consensus.tesseract_provider.run")
    def test_single_provider_low_confidence(self, mock_tess, mock_vision):
        """單邊成功 → confidence 受限於 quality_score * 0.4，通常 < 0.75。"""
        mock_tess.return_value = _make_provider_result(
            success=False,
            provider="tesseract",
            error="not available",
        )
        mock_vision.return_value = _make_provider_result(
            provider="apple_vision",
            corrected_text="法院",
            quality_score=0.8,  # max base = 0.8 * 0.4 = 0.32
        )
        tmp = _tmp_image()
        try:
            from skills.engine.ocr.consensus import run_consensus
            result = run_consensus(tmp)
            # 單邊最大 = 0.32，一定 < 0.75，所以 writable=False
            assert result.writable is False
        finally:
            os.unlink(tmp)


# ---------------------------------------------------------------------------
# 兩邊都失敗
# ---------------------------------------------------------------------------

class TestConsensusBothFailed:
    @patch("skills.engine.ocr.consensus.apple_vision_provider.run")
    @patch("skills.engine.ocr.consensus.tesseract_provider.run")
    def test_both_fail_returns_failure(self, mock_tess, mock_vision):
        mock_tess.return_value = _make_provider_result(
            success=False, provider="tesseract", error="tess error"
        )
        mock_vision.return_value = _make_provider_result(
            success=False, provider="apple_vision", error="vision error"
        )
        tmp = _tmp_image()
        try:
            from skills.engine.ocr.consensus import run_consensus
            result = run_consensus(tmp)
            assert result.success is False
            assert result.error is not None
        finally:
            os.unlink(tmp)

    @patch("skills.engine.ocr.consensus.apple_vision_provider.run")
    @patch("skills.engine.ocr.consensus.tesseract_provider.run")
    def test_both_timeout_error_message(self, mock_tess, mock_vision):
        mock_tess.return_value = _make_provider_result(
            success=False, provider="tesseract",
            error="timeout", timed_out=True,
        )
        mock_vision.return_value = _make_provider_result(
            success=False, provider="apple_vision",
            error="timeout", timed_out=True,
        )
        tmp = _tmp_image()
        try:
            from skills.engine.ocr.consensus import run_consensus
            result = run_consensus(tmp)
            assert result.success is False
            assert "timeout" in (result.error or "").lower()
        finally:
            os.unlink(tmp)


# ---------------------------------------------------------------------------
# Critical conflict 偵測
# ---------------------------------------------------------------------------

class TestCriticalConflict:
    @patch("skills.engine.ocr.consensus.apple_vision_provider.run")
    @patch("skills.engine.ocr.consensus.tesseract_provider.run")
    def test_case_number_mismatch_is_critical(self, mock_tess, mock_vision):
        mock_tess.return_value = _make_provider_result(
            provider="tesseract",
            corrected_text="114年度訴字第123號",
            quality_score=0.8,
            case_numbers=["114年度訴字第123號"],
        )
        mock_vision.return_value = _make_provider_result(
            provider="apple_vision",
            corrected_text="114年度訴字第456號",
            quality_score=0.8,
            case_numbers=["114年度訴字第456號"],
        )
        tmp = _tmp_image()
        try:
            from skills.engine.ocr.consensus import run_consensus
            result = run_consensus(tmp)
            assert result.critical_conflict is True
            assert result.writable is False
        finally:
            os.unlink(tmp)

    @patch("skills.engine.ocr.consensus.apple_vision_provider.run")
    @patch("skills.engine.ocr.consensus.tesseract_provider.run")
    def test_date_diff_over_30_days_is_critical(self, mock_tess, mock_vision):
        mock_tess.return_value = _make_provider_result(
            provider="tesseract",
            corrected_text="114年1月1日",
            quality_score=0.7,
            roc_dates=["114年1月1日"],
        )
        mock_vision.return_value = _make_provider_result(
            provider="apple_vision",
            corrected_text="114年6月1日",
            quality_score=0.7,
            roc_dates=["114年6月1日"],  # 差 150 天
        )
        tmp = _tmp_image()
        try:
            from skills.engine.ocr.consensus import run_consensus
            result = run_consensus(tmp)
            assert result.critical_conflict is True
            assert result.writable is False
        finally:
            os.unlink(tmp)

    @patch("skills.engine.ocr.consensus.apple_vision_provider.run")
    @patch("skills.engine.ocr.consensus.tesseract_provider.run")
    def test_date_diff_within_30_days_not_critical(self, mock_tess, mock_vision):
        mock_tess.return_value = _make_provider_result(
            provider="tesseract",
            corrected_text="114年3月1日",
            quality_score=0.7,
            roc_dates=["114年3月1日"],
        )
        mock_vision.return_value = _make_provider_result(
            provider="apple_vision",
            corrected_text="114年3月15日",
            quality_score=0.7,
            roc_dates=["114年3月15日"],  # 差 14 天
        )
        tmp = _tmp_image()
        try:
            from skills.engine.ocr.consensus import run_consensus
            result = run_consensus(tmp)
            assert result.critical_conflict is False
        finally:
            os.unlink(tmp)

    @patch("skills.engine.ocr.consensus.apple_vision_provider.run")
    @patch("skills.engine.ocr.consensus.tesseract_provider.run")
    def test_critical_conflict_warning_included(self, mock_tess, mock_vision):
        mock_tess.return_value = _make_provider_result(
            provider="tesseract",
            quality_score=0.8,
            case_numbers=["114年度訴字第100號"],
        )
        mock_vision.return_value = _make_provider_result(
            provider="apple_vision",
            quality_score=0.8,
            case_numbers=["114年度刑字第200號"],
        )
        tmp = _tmp_image()
        try:
            from skills.engine.ocr.consensus import run_consensus
            result = run_consensus(tmp)
            assert result.critical_conflict is True
            assert any("critical" in w for w in result.warnings)
        finally:
            os.unlink(tmp)


# ---------------------------------------------------------------------------
# Confidence 計算
# ---------------------------------------------------------------------------

class TestConfidenceCalculation:
    @patch("skills.engine.ocr.consensus.apple_vision_provider.run")
    @patch("skills.engine.ocr.consensus.tesseract_provider.run")
    def test_confidence_bounded_0_to_1(self, mock_tess, mock_vision):
        mock_tess.return_value = _make_provider_result(
            provider="tesseract", quality_score=1.0,
            case_numbers=["114年度訴字第1號"],
            roc_dates=["114年3月1日"],
            courts=["臺灣花蓮地方法院"],
        )
        mock_vision.return_value = _make_provider_result(
            provider="apple_vision", quality_score=1.0,
            case_numbers=["114年度訴字第1號"],
            roc_dates=["114年3月1日"],
            courts=["臺灣花蓮地方法院"],
        )
        tmp = _tmp_image()
        try:
            from skills.engine.ocr.consensus import run_consensus
            result = run_consensus(tmp)
            assert 0.0 <= result.confidence <= 1.0
        finally:
            os.unlink(tmp)

    @patch("skills.engine.ocr.consensus.apple_vision_provider.run")
    @patch("skills.engine.ocr.consensus.tesseract_provider.run")
    def test_no_entity_agreement_lower_confidence(self, mock_tess, mock_vision):
        """無實體資訊（空列表）時，confidence 只有 quality_score * 0.4。"""
        mock_tess.return_value = _make_provider_result(
            provider="tesseract", quality_score=0.5,
        )
        mock_vision.return_value = _make_provider_result(
            provider="apple_vision", quality_score=0.5,
        )
        tmp = _tmp_image()
        try:
            from skills.engine.ocr.consensus import run_consensus
            result = run_consensus(tmp)
            # base = (0.5 + 0.5) / 2 * 0.4 = 0.2；無 entity agreement
            assert abs(result.confidence - 0.2) < 0.05
        finally:
            os.unlink(tmp)

    @patch("skills.engine.ocr.consensus.apple_vision_provider.run")
    @patch("skills.engine.ocr.consensus.tesseract_provider.run")
    def test_writable_threshold_exactly_0_75(self, mock_tess, mock_vision):
        """剛好 0.75 → writable=True。"""
        # base = (1.0 + 1.0) / 2 * 0.4 = 0.4 + agree 0.3 + 0.0 + 0.0 = 0.7（不夠）
        # 加 courts: 0.4 + 0.3 + 0.0 + 0.1 = 0.8 → writable
        mock_tess.return_value = _make_provider_result(
            provider="tesseract", quality_score=1.0,
            case_numbers=["114年度訴字第1號"],
            courts=["臺灣花蓮地方法院"],
        )
        mock_vision.return_value = _make_provider_result(
            provider="apple_vision", quality_score=1.0,
            case_numbers=["114年度訴字第1號"],
            courts=["臺灣花蓮地方法院"],
        )
        tmp = _tmp_image()
        try:
            from skills.engine.ocr.consensus import run_consensus
            result = run_consensus(tmp)
            assert result.confidence >= 0.75
            assert result.writable is True
        finally:
            os.unlink(tmp)


# ---------------------------------------------------------------------------
# Captcha bypass
# ---------------------------------------------------------------------------

class TestCaptchaTaskType:
    @patch("skills.engine.ocr.consensus.apple_vision_provider.run")
    @patch("skills.engine.ocr.consensus.tesseract_provider.run")
    def test_captcha_entities_is_none(self, mock_tess, mock_vision):
        mock_tess.return_value = _make_provider_result(
            provider="tesseract",
            corrected_text="4Xk7mP",
            quality_score=0.3,
        )
        mock_vision.return_value = _make_provider_result(
            provider="apple_vision",
            corrected_text="4Xk7mP",
            quality_score=0.3,
        )
        tmp = _tmp_image()
        try:
            from skills.engine.ocr.consensus import run_consensus
            result = run_consensus(tmp, task_type="captcha")
            assert result.entities is None
        finally:
            os.unlink(tmp)

    @patch("skills.engine.ocr.consensus.apple_vision_provider.run")
    @patch("skills.engine.ocr.consensus.tesseract_provider.run")
    def test_captcha_corrected_text_unchanged(self, mock_tess, mock_vision):
        """captcha task_type: corrected_text 等於 selected_text（不做法律修正）。"""
        mock_tess.return_value = _make_provider_result(
            provider="tesseract",
            corrected_text="l1O0aB",  # 不應被修正
            quality_score=0.4,
        )
        mock_vision.return_value = _make_provider_result(
            provider="apple_vision",
            corrected_text="l1O0aB",
            quality_score=0.4,
        )
        tmp = _tmp_image()
        try:
            from skills.engine.ocr.consensus import run_consensus
            result = run_consensus(tmp, task_type="captcha")
            # selected_text 應保持原樣（包含 l / O / 0）
            assert result.corrected_text == result.selected_text
        finally:
            os.unlink(tmp)


# ---------------------------------------------------------------------------
# 錯誤路徑
# ---------------------------------------------------------------------------

class TestConsensusErrorPaths:
    def test_missing_image_returns_failure(self):
        from skills.engine.ocr.consensus import run_consensus
        result = run_consensus("/tmp/definitely_not_exist_ocr_12345.png")
        assert result.success is False
        assert result.error is not None

    def test_empty_path_returns_failure(self):
        from skills.engine.ocr.consensus import run_consensus
        result = run_consensus("")
        assert result.success is False

    @patch("skills.engine.ocr.consensus.apple_vision_provider.run")
    @patch("skills.engine.ocr.consensus.tesseract_provider.run")
    def test_result_has_provider_results_dict(self, mock_tess, mock_vision):
        mock_tess.return_value = _make_provider_result(
            provider="tesseract", corrected_text="法院", quality_score=0.5
        )
        mock_vision.return_value = _make_provider_result(
            success=False, provider="apple_vision", error="not available"
        )
        tmp = _tmp_image()
        try:
            from skills.engine.ocr.consensus import run_consensus
            result = run_consensus(tmp)
            assert "tesseract" in result.provider_results
            assert "apple_vision" in result.provider_results
        finally:
            os.unlink(tmp)


# ---------------------------------------------------------------------------
# to_dict 結構
# ---------------------------------------------------------------------------

class TestConsensusResultToDict:
    @patch("skills.engine.ocr.consensus.apple_vision_provider.run")
    @patch("skills.engine.ocr.consensus.tesseract_provider.run")
    def test_to_dict_has_required_keys(self, mock_tess, mock_vision):
        mock_tess.return_value = _make_provider_result(
            provider="tesseract", corrected_text="法院", quality_score=0.5
        )
        mock_vision.return_value = _make_provider_result(
            provider="apple_vision", corrected_text="法院", quality_score=0.5
        )
        tmp = _tmp_image()
        try:
            from skills.engine.ocr.consensus import run_consensus
            result = run_consensus(tmp)
            d = result.to_dict()
            for key in ("success", "confidence", "writable", "critical_conflict",
                        "warnings", "providers", "entities_counts", "duration_sec"):
                assert key in d, f"missing key: {key}"
        finally:
            os.unlink(tmp)

    def test_failure_to_dict(self):
        from skills.engine.ocr.ocr_schema import OCRConsensusResult
        r = OCRConsensusResult.failure("test error")
        d = r.to_dict()
        assert d["success"] is False
        assert d["error"] == "test error"

    @patch("skills.engine.ocr.consensus.apple_vision_provider.run")
    @patch("skills.engine.ocr.consensus.tesseract_provider.run")
    def test_duration_sec_positive(self, mock_tess, mock_vision):
        mock_tess.return_value = _make_provider_result(
            provider="tesseract", corrected_text="法院", quality_score=0.5
        )
        mock_vision.return_value = _make_provider_result(
            provider="apple_vision", corrected_text="法院", quality_score=0.5
        )
        tmp = _tmp_image()
        try:
            from skills.engine.ocr.consensus import run_consensus
            result = run_consensus(tmp)
            assert result.duration_sec >= 0.0
        finally:
            os.unlink(tmp)


# ---------------------------------------------------------------------------
# _compute_confidence 內部函式直接測試
# ---------------------------------------------------------------------------

class TestComputeConfidenceInternal:
    def _make_tess(self, **kwargs):
        return _make_provider_result(provider="tesseract", **kwargs)

    def _make_vision(self, **kwargs):
        return _make_provider_result(provider="apple_vision", **kwargs)

    def test_both_failed_zero_confidence(self):
        from skills.engine.ocr.consensus import _compute_confidence
        t = _make_provider_result(success=False, provider="tesseract", error="e")
        v = _make_provider_result(success=False, provider="apple_vision", error="e")
        conf, crit, warns = _compute_confidence(t, v)
        assert conf == 0.0
        assert crit is False

    def test_case_number_agree_adds_0_3(self):
        from skills.engine.ocr.consensus import _compute_confidence
        t = self._make_tess(quality_score=0.5, case_numbers=["114年度訴字第1號"])
        v = self._make_vision(quality_score=0.5, case_numbers=["114年度訴字第1號"])
        conf, crit, warns = _compute_confidence(t, v)
        # base = 0.5 * 0.4 = 0.2；agree = +0.3 → 0.5
        assert abs(conf - 0.5) < 0.01
        assert crit is False

    def test_roc_date_intersect_adds_0_2(self):
        from skills.engine.ocr.consensus import _compute_confidence
        t = self._make_tess(quality_score=0.5, roc_dates=["114年3月15日"])
        v = self._make_vision(quality_score=0.5, roc_dates=["114年3月15日"])
        conf, crit, warns = _compute_confidence(t, v)
        # base 0.2 + dates 0.2 = 0.4
        assert abs(conf - 0.4) < 0.01
        assert crit is False

    def test_courts_intersect_adds_0_1(self):
        from skills.engine.ocr.consensus import _compute_confidence
        t = self._make_tess(quality_score=0.5, courts=["臺灣花蓮地方法院"])
        v = self._make_vision(quality_score=0.5, courts=["臺灣花蓮地方法院"])
        conf, crit, warns = _compute_confidence(t, v)
        # base 0.2 + courts 0.1 = 0.3
        assert abs(conf - 0.3) < 0.01
        assert crit is False

    def test_disjoint_case_numbers_critical(self):
        from skills.engine.ocr.consensus import _compute_confidence
        t = self._make_tess(quality_score=0.8, case_numbers=["114年度訴字第100號"])
        v = self._make_vision(quality_score=0.8, case_numbers=["114年度刑字第200號"])
        conf, crit, warns = _compute_confidence(t, v)
        assert crit is True
        assert any("critical" in w for w in warns)


# ---------------------------------------------------------------------------
# _select_text 內部函式直接測試
# ---------------------------------------------------------------------------

class TestSelectTextInternal:
    def test_higher_quality_wins(self):
        from skills.engine.ocr.consensus import _select_text
        tess = _make_provider_result(
            provider="tesseract", corrected_text="Tesseract 文字", quality_score=0.4
        )
        vision = _make_provider_result(
            provider="apple_vision", corrected_text="Vision 文字長度更長更多資訊", quality_score=0.7
        )
        selected = _select_text(tess, vision)
        assert selected == "Vision 文字長度更長更多資訊"

    def test_tess_only_when_vision_failed(self):
        from skills.engine.ocr.consensus import _select_text
        tess = _make_provider_result(
            provider="tesseract", corrected_text="Tesseract 文字", quality_score=0.5
        )
        vision = _make_provider_result(
            success=False, provider="apple_vision", error="not available"
        )
        selected = _select_text(tess, vision)
        assert "Tesseract" in selected

    def test_similar_quality_longer_text_wins(self):
        from skills.engine.ocr.consensus import _select_text
        tess = _make_provider_result(
            provider="tesseract", corrected_text="短文字", quality_score=0.501
        )
        vision = _make_provider_result(
            provider="apple_vision",
            corrected_text="比較長的文字內容，包含更多詳細資訊",
            quality_score=0.5,
        )
        selected = _select_text(tess, vision)
        # 品質差 < 0.05 → 用字元較長的
        assert "比較長的文字內容" in selected
