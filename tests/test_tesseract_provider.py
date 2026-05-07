# -*- coding: utf-8 -*-
"""
Tests for skills.engine.ocr.tesseract_provider.

所有測試均 mock SafeProcess.run，不真跑 tesseract binary。
禁止在 module level import api.server / api.tools_api / daemon（SIGCHLD 守則）。
"""

from __future__ import annotations

import os
import tempfile
import threading
from unittest.mock import MagicMock, patch

import pytest

# --- 不在 module level 做 heavy import，延後到 fixture/test ---

@pytest.fixture(autouse=True)
def _reset_probe_cache():
    """每次測試前後清除 session-cached probe 結果。"""
    from skills.engine.ocr import tesseract_provider as tp
    tp.reset_probe_cache()
    yield
    tp.reset_probe_cache()


def _make_safe_run_result(returncode=0, stdout="", stderr="", timed_out=False):
    """建立 SafeRunResult mock。"""
    r = MagicMock()
    r.returncode = returncode
    r.stdout = stdout
    r.stderr = stderr
    r.timed_out = timed_out
    r.killed = False
    r.duration_sec = 0.1
    return r


# --- check_available() 測試 -------------------------------------------------

class TestCheckAvailable:
    def test_feature_flag_disabled(self, monkeypatch):
        monkeypatch.setenv("MAGI_TESSERACT_ENABLE", "0")
        from skills.engine.ocr import tesseract_provider as tp
        tp.reset_probe_cache()
        ok, reason = tp.check_available()
        assert ok is False
        assert "MAGI_TESSERACT_ENABLE=0" in reason

    @patch("skills.engine.ocr.tesseract_provider._safe_run")
    def test_binary_not_found_returns_false(self, mock_run, monkeypatch):
        monkeypatch.setenv("MAGI_TESSERACT_ENABLE", "1")
        mock_run.side_effect = Exception("No such file")
        from skills.engine.ocr import tesseract_provider as tp
        tp.reset_probe_cache()
        ok, reason = tp.check_available()
        assert ok is False
        assert "error" in reason.lower()

    @patch("skills.engine.ocr.tesseract_provider._safe_run")
    def test_chi_tra_not_in_langs_returns_false(self, mock_run, monkeypatch):
        monkeypatch.setenv("MAGI_TESSERACT_ENABLE", "1")
        # version probe OK, langs probe missing chi_tra
        mock_run.side_effect = [
            _make_safe_run_result(returncode=0, stderr="tesseract 5.5.2"),
            _make_safe_run_result(returncode=0, stdout="eng\nfra\n", stderr=""),
        ]
        from skills.engine.ocr import tesseract_provider as tp
        tp.reset_probe_cache()
        ok, reason = tp.check_available()
        assert ok is False
        assert "chi_tra" in reason

    @patch("skills.engine.ocr.tesseract_provider._safe_run")
    def test_all_probes_pass_returns_true(self, mock_run, monkeypatch):
        monkeypatch.setenv("MAGI_TESSERACT_ENABLE", "1")
        mock_run.side_effect = [
            _make_safe_run_result(returncode=0, stderr="tesseract 5.5.2"),   # version
            _make_safe_run_result(returncode=0, stdout="chi_tra\neng\n"),     # langs
            _make_safe_run_result(returncode=0, stdout=""),                   # functional
        ]
        from skills.engine.ocr import tesseract_provider as tp
        tp.reset_probe_cache()
        ok, reason = tp.check_available()
        assert ok is True
        assert reason == ""

    @patch("skills.engine.ocr.tesseract_provider._safe_run")
    def test_probe_cached_after_first_call(self, mock_run, monkeypatch):
        monkeypatch.setenv("MAGI_TESSERACT_ENABLE", "1")
        mock_run.side_effect = [
            _make_safe_run_result(returncode=0, stderr="tesseract 5.5.2"),
            _make_safe_run_result(returncode=0, stdout="chi_tra\neng\n"),
            _make_safe_run_result(returncode=0, stdout=""),
        ]
        from skills.engine.ocr import tesseract_provider as tp
        tp.reset_probe_cache()
        ok1, _ = tp.check_available()
        ok2, _ = tp.check_available()   # should use cache, not call _safe_run again
        assert ok1 == ok2
        # mock_run called only 3 times total (for first call), not 6
        assert mock_run.call_count == 3


# --- run() 測試 -------------------------------------------------------------

class TestRun:
    @patch("skills.engine.ocr.tesseract_provider.check_available")
    def test_provider_not_available_returns_failure(self, mock_check):
        mock_check.return_value = (False, "test: not available")
        from skills.engine.ocr import tesseract_provider as tp
        result = tp.run("/tmp/test.png")
        assert result.success is False
        assert "not available" in (result.error or "")

    @patch("skills.engine.ocr.tesseract_provider.check_available")
    def test_missing_image_file_returns_failure(self, mock_check):
        mock_check.return_value = (True, "")
        from skills.engine.ocr import tesseract_provider as tp
        result = tp.run("/tmp/nonexistent_ocr_test_12345.png")
        assert result.success is False
        assert "not found" in (result.error or "").lower()

    @patch("skills.engine.ocr.tesseract_provider._safe_run")
    @patch("skills.engine.ocr.tesseract_provider.check_available")
    def test_successful_ocr_returns_result(self, mock_check, mock_run):
        mock_check.return_value = (True, "")
        mock_run.return_value = _make_safe_run_result(
            returncode=0,
            stdout="臺灣花蓮地方法院\n114年度訴字第123號\n判決書",
        )
        # create temp file
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            f.write(b"\x89PNG\r\n")  # minimal PNG header bytes
            tmp_path = f.name
        try:
            from skills.engine.ocr import tesseract_provider as tp
            result = tp.run(tmp_path)
            assert result.success is True
            assert result.provider == "tesseract"
            assert "臺灣花蓮地方法院" in result.raw_text
            assert result.quality_score >= 0.0
            assert result.entities is not None
        finally:
            os.unlink(tmp_path)

    @patch("skills.engine.ocr.tesseract_provider._safe_run")
    @patch("skills.engine.ocr.tesseract_provider.check_available")
    def test_timed_out_returns_failure(self, mock_check, mock_run):
        mock_check.return_value = (True, "")
        mock_run.return_value = _make_safe_run_result(timed_out=True, returncode=-1)
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            f.write(b"\x89PNG")
            tmp_path = f.name
        try:
            from skills.engine.ocr import tesseract_provider as tp
            result = tp.run(tmp_path, timeout_sec=1.0)
            assert result.success is False
            assert result.timed_out is True
        finally:
            os.unlink(tmp_path)

    @patch("skills.engine.ocr.tesseract_provider._safe_run")
    @patch("skills.engine.ocr.tesseract_provider.check_available")
    def test_captcha_task_type_skips_entities(self, mock_check, mock_run):
        mock_check.return_value = (True, "")
        mock_run.return_value = _make_safe_run_result(returncode=0, stdout="4Xk7mP")
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            f.write(b"\x89PNG")
            tmp_path = f.name
        try:
            from skills.engine.ocr import tesseract_provider as tp
            result = tp.run(tmp_path, task_type="captcha")
            assert result.entities is None
            # raw text unchanged (no correction for captcha)
            assert result.raw_text == "4Xk7mP"
            # corrected_text also unchanged for captcha
            assert result.corrected_text == "4Xk7mP"
        finally:
            os.unlink(tmp_path)

    @patch("skills.engine.ocr.tesseract_provider._safe_run")
    @patch("skills.engine.ocr.tesseract_provider.check_available")
    def test_psm_default_is_3(self, mock_check, mock_run):
        mock_check.return_value = (True, "")
        mock_run.return_value = _make_safe_run_result(returncode=0, stdout="法院")
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            f.write(b"\x89PNG")
            tmp_path = f.name
        try:
            from skills.engine.ocr import tesseract_provider as tp
            result = tp.run(tmp_path)
            assert result.psm == 3  # PSM_AUTO default
        finally:
            os.unlink(tmp_path)

    @patch("skills.engine.ocr.tesseract_provider._safe_run")
    @patch("skills.engine.ocr.tesseract_provider.check_available")
    def test_custom_psm_used(self, mock_check, mock_run):
        mock_check.return_value = (True, "")
        mock_run.return_value = _make_safe_run_result(returncode=0, stdout="法院")
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            f.write(b"\x89PNG")
            tmp_path = f.name
        try:
            from skills.engine.ocr import tesseract_provider as tp
            result = tp.run(tmp_path, psm=6)
            assert result.psm == 6
            # Verify --psm 6 was passed to _safe_run
            call_args = mock_run.call_args[0][0]
            assert "--psm" in call_args
            assert "6" in call_args
        finally:
            os.unlink(tmp_path)

    @patch("skills.engine.ocr.tesseract_provider.check_available")
    def test_feature_flag_disabled_returns_failure(self, mock_check, monkeypatch):
        monkeypatch.setenv("MAGI_TESSERACT_ENABLE", "0")
        mock_check.return_value = (True, "")  # won't be reached
        from skills.engine.ocr import tesseract_provider as tp
        result = tp.run("/tmp/test.png")
        assert result.success is False

    @patch("skills.engine.ocr.tesseract_provider._safe_run")
    @patch("skills.engine.ocr.tesseract_provider.check_available")
    def test_safe_process_blocked_returns_failure(self, mock_check, mock_run):
        mock_check.return_value = (True, "")
        mock_run.side_effect = PermissionError("argv[0] not whitelisted")
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            f.write(b"\x89PNG")
            tmp_path = f.name
        try:
            from skills.engine.ocr import tesseract_provider as tp
            result = tp.run(tmp_path)
            assert result.success is False
            assert "SafeProcess blocked" in (result.error or "")
        finally:
            os.unlink(tmp_path)


# --- OCRProviderResult structure tests --------------------------------------

class TestOCRProviderResultStructure:
    def test_failure_factory(self):
        from skills.engine.ocr.ocr_schema import OCRProviderResult
        r = OCRProviderResult.failure("tesseract", "test error")
        assert r.success is False
        assert r.provider == "tesseract"
        assert r.error == "test error"
        assert r.raw_text == ""
        assert r.corrected_text == ""

    def test_to_dict_no_entities_string(self):
        from skills.engine.ocr.ocr_schema import OCRProviderResult
        r = OCRProviderResult(success=True, provider="tesseract")
        d = r.to_dict()
        assert "success" in d
        assert "provider" in d
        assert "quality_score" in d
        # Must NOT contain entities content strings
        assert "entities" not in d or isinstance(d.get("entities"), (type(None), int))
        # entities_counts should be there
        assert "entities_counts" in d
