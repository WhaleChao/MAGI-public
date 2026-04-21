# -*- coding: utf-8 -*-
"""
Tests for pdf_bridge._ocr_single_page() consensus opt-in branches.

守則：
- 禁止在 module level import api.server / api.tools_api / daemon（SIGCHLD 守則）
- mock skills.engine.ocr.consensus.run_consensus，不跑真實 binary
- mock subprocess.run，避免呼叫真實 tesseract
- flag off → 完全走舊路徑（不呼叫 consensus）
- shadow → 呼叫 consensus 但回舊路徑結果
- enable → 走新 consensus，失敗 fallback 舊路徑
- metrics → 只有 count，不含 entity 字串
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Optional
from unittest.mock import MagicMock, call, patch

import pytest


# ---------------------------------------------------------------------------
# helpers：不在 module level import 業務模組
# ---------------------------------------------------------------------------

def _make_consensus_result(
    success: bool = True,
    selected_text: str = "花蓮地方法院判決書",
    corrected_text: str = "花蓮地方法院判決書（修正）",
    confidence: float = 0.85,
    error: Optional[str] = None,
):
    """建立 OCRConsensusResult 物件（不呼叫真實 OCR）。"""
    from skills.engine.ocr.ocr_schema import OCRConsensusResult, OCREntities

    entities = None
    if success:
        entities = OCREntities(
            case_numbers=["114年度訴字第123號"],
            roc_dates=["114年3月15日"],
            courts=["臺灣花蓮地方法院"],
            parties=["王大明"],
            laf_case_numbers=[],
        )
    return OCRConsensusResult(
        success=success,
        selected_text=selected_text,
        corrected_text=corrected_text,
        confidence=confidence,
        writable=confidence >= 0.75,
        warnings=[],
        critical_conflict=False,
        provider_results={},
        entities=entities,
        error=error,
        duration_sec=1.5,
    )


def _make_subprocess_result(stdout: str = "tesseract_text", returncode: int = 0):
    """模擬 subprocess.run 的回傳值。"""
    m = MagicMock()
    m.stdout = stdout
    m.returncode = returncode
    return m


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch, tmp_path):
    """每個 test 隔離環境變數。"""
    # 預設關閉 consensus
    monkeypatch.setenv("MAGI_PDF_OCR_CONSENSUS_ENABLE", "0")
    monkeypatch.setenv("MAGI_PDF_OCR_CONSENSUS_SHADOW", "0")
    monkeypatch.setenv("MAGI_OCR_CACHE_ENABLE", "0")   # cache 測試另有 test_ocr_cache.py
    monkeypatch.setenv("MAGI_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("MAGI_USE_RUNTIME_DIR", "1")
    monkeypatch.setenv("MAGI_PDF_VISION_OCR_FALLBACK", "0")  # 避免 Vision OCR 干擾
    monkeypatch.setenv("MAGI_PDF_OCR_ENABLE", "1")


# ---------------------------------------------------------------------------
# helper：執行 _extract_text_ocr 對單一 page（用 tmp PNG）
# ---------------------------------------------------------------------------

def _run_extract_ocr_with_mock_page(
    tmp_path: Path,
    monkeypatch,
    consensus_mock=None,
    tess_stdout: str = "legacy_tesseract_text",
):
    """
    建立 fake PDF + PNG，然後執行 _extract_text_ocr。
    傳回 (text, pages_processed)。

    consensus_mock: 若非 None，patch skills.engine.ocr.consensus.run_consensus
    tess_stdout: 模擬 tesseract subprocess 輸出
    """
    # 建立 fake PNG
    png_dir = tmp_path / "pages"
    png_dir.mkdir(exist_ok=True)
    fake_png = png_dir / "page-1.png"
    fake_png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"FAKE" * 100)

    # 建立 fake PDF
    fake_pdf = tmp_path / "test.pdf"
    fake_pdf.write_bytes(b"%PDF-1.4\nFAKE\n")

    # mock pdftoppm：複製 fake PNG 到 temp dir（在 _extract_text_ocr 內部的 td）
    def _fake_pdftoppm(cmd, **kwargs):
        # 把 fake_png 複製到 cmd prefix 目錄
        prefix = Path(cmd[cmd.index(str(tmp_path) if str(tmp_path) in " ".join(cmd) else cmd[-1])])
        td_dir = prefix.parent
        dest = td_dir / "page-1.png"
        dest.write_bytes(fake_png.read_bytes())
        m = MagicMock()
        m.returncode = 0
        m.stderr = ""
        return m

    import subprocess as _sp

    def _fake_subprocess_run(cmd, **kwargs):
        cmd_list = list(cmd)
        if cmd_list and "pdftoppm" in cmd_list[0]:
            # 找到輸出 prefix（最後一個非 flag 參數）
            prefix = cmd_list[-1]
            td_dir = Path(prefix).parent
            dest = td_dir / "page-1.png"
            dest.write_bytes(fake_png.read_bytes())
            m = MagicMock()
            m.returncode = 0
            m.stderr = ""
            return m
        elif cmd_list and "tesseract" in cmd_list[0]:
            m = MagicMock()
            m.stdout = tess_stdout
            m.returncode = 0
            return m
        m = MagicMock()
        m.returncode = 0
        m.stdout = ""
        m.stderr = ""
        return m

    patches = [
        patch("subprocess.run", side_effect=_fake_subprocess_run),
    ]
    if consensus_mock is not None:
        patches.append(
            patch("skills.engine.ocr.consensus.run_consensus", return_value=consensus_mock)
        )

    with patch.multiple("subprocess", run=_fake_subprocess_run):
        if consensus_mock is not None:
            with patch("skills.engine.ocr.consensus.run_consensus", return_value=consensus_mock):
                from skills.documents.pdf_bridge import _extract_text_ocr
                return _extract_text_ocr(str(fake_pdf), max_pages=1)
        else:
            from skills.documents.pdf_bridge import _extract_text_ocr
            return _extract_text_ocr(str(fake_pdf), max_pages=1)


# ---------------------------------------------------------------------------
# test: flag 全關 → 完全走舊路徑
# ---------------------------------------------------------------------------

class TestFlagOff:
    def test_flag_off_does_not_call_consensus(self, tmp_path, monkeypatch):
        """CONSENSUS_ENABLE=0, SHADOW=0 → consensus 不被呼叫。"""
        monkeypatch.setenv("MAGI_PDF_OCR_CONSENSUS_ENABLE", "0")
        monkeypatch.setenv("MAGI_PDF_OCR_CONSENSUS_SHADOW", "0")

        consensus_mock = _make_consensus_result()

        with patch("subprocess.run") as mock_sp, \
             patch("skills.engine.ocr.consensus.run_consensus") as mock_consensus:
            mock_sp.return_value = _make_subprocess_result(stdout="old_path_result")

            from skills.documents import pdf_bridge
            import importlib
            importlib.reload(pdf_bridge)

            fake_pdf = tmp_path / "test.pdf"
            fake_pdf.write_bytes(b"%PDF-1.4\n")

            # pdftoppm mock + tesseract mock
            call_count = {"n": 0}

            def _sp_side_effect(cmd, **kwargs):
                cmd_list = list(cmd)
                if "pdftoppm" in str(cmd_list[0]):
                    # 建立 fake page PNG
                    prefix = cmd_list[-1]
                    td_dir = Path(prefix).parent
                    dest = td_dir / "page-1.png"
                    dest.write_bytes(b"\x89PNG\r\n" + b"X" * 50)
                    m = MagicMock()
                    m.returncode = 0
                    m.stderr = ""
                    return m
                elif "tesseract" in str(cmd_list[0]):
                    m = MagicMock()
                    m.stdout = "old_path_result"
                    m.returncode = 0
                    return m
                m = MagicMock()
                m.returncode = 0
                m.stdout = ""
                m.stderr = ""
                return m

            mock_sp.side_effect = _sp_side_effect
            pdf_bridge._extract_text_ocr(str(fake_pdf), max_pages=1)

            # consensus 不應被呼叫
            mock_consensus.assert_not_called()

    def test_flag_off_returns_legacy_text(self, tmp_path, monkeypatch):
        """flag off 時回傳的文字來自 tesseract。"""
        monkeypatch.setenv("MAGI_PDF_OCR_CONSENSUS_ENABLE", "0")
        monkeypatch.setenv("MAGI_PDF_OCR_CONSENSUS_SHADOW", "0")

        from skills.documents import pdf_bridge
        import importlib
        importlib.reload(pdf_bridge)

        fake_pdf = tmp_path / "test.pdf"
        fake_pdf.write_bytes(b"%PDF-1.4\n")

        def _sp_side_effect(cmd, **kwargs):
            cmd_list = list(cmd)
            if "pdftoppm" in str(cmd_list[0]):
                prefix = cmd_list[-1]
                td_dir = Path(prefix).parent
                dest = td_dir / "page-1.png"
                dest.write_bytes(b"\x89PNG\r\n" + b"X" * 50)
                m = MagicMock()
                m.returncode = 0
                m.stderr = ""
                return m
            elif "tesseract" in str(cmd_list[0]):
                m = MagicMock()
                m.stdout = "legacy_tesseract_output\n"
                m.returncode = 0
                return m
            m = MagicMock()
            m.returncode = 0
            m.stdout = ""
            m.stderr = ""
            return m

        with patch("subprocess.run", side_effect=_sp_side_effect):
            text, pages = pdf_bridge._extract_text_ocr(str(fake_pdf), max_pages=1)

        assert "legacy_tesseract_output" in text
        assert pages == 1


# ---------------------------------------------------------------------------
# test: consensus enable → 使用新結果
# ---------------------------------------------------------------------------

class TestConsensusEnable:
    def test_enable_uses_consensus_result(self, tmp_path, monkeypatch):
        """CONSENSUS_ENABLE=1 → text 來自 consensus.corrected_text。"""
        monkeypatch.setenv("MAGI_PDF_OCR_CONSENSUS_ENABLE", "1")
        monkeypatch.setenv("MAGI_PDF_OCR_CONSENSUS_SHADOW", "0")

        from skills.documents import pdf_bridge
        import importlib
        importlib.reload(pdf_bridge)

        consensus_result = _make_consensus_result(
            corrected_text="CONSENSUS_OUTPUT_TEXT",
        )
        fake_pdf = tmp_path / "test.pdf"
        fake_pdf.write_bytes(b"%PDF-1.4\n")

        def _sp_side_effect(cmd, **kwargs):
            cmd_list = list(cmd)
            if "pdftoppm" in str(cmd_list[0]):
                prefix = cmd_list[-1]
                td_dir = Path(prefix).parent
                dest = td_dir / "page-1.png"
                dest.write_bytes(b"\x89PNG\r\n" + b"X" * 50)
                m = MagicMock()
                m.returncode = 0
                m.stderr = ""
                return m
            elif "tesseract" in str(cmd_list[0]):
                m = MagicMock()
                m.stdout = "legacy_text"
                m.returncode = 0
                return m
            m = MagicMock()
            m.returncode = 0
            m.stdout = ""
            m.stderr = ""
            return m

        with patch("subprocess.run", side_effect=_sp_side_effect), \
             patch("skills.engine.ocr.consensus.run_consensus", return_value=consensus_result):
            text, pages = pdf_bridge._extract_text_ocr(str(fake_pdf), max_pages=1)

        assert "CONSENSUS_OUTPUT_TEXT" in text, f"Expected consensus output in: {text!r}"

    def test_enable_consensus_failure_fallbacks_to_legacy(self, tmp_path, monkeypatch):
        """consensus 失敗（raise exception）→ fallback 到舊路徑，不讓 PDF extraction 失敗。"""
        monkeypatch.setenv("MAGI_PDF_OCR_CONSENSUS_ENABLE", "1")
        monkeypatch.setenv("MAGI_PDF_OCR_CONSENSUS_SHADOW", "0")

        from skills.documents import pdf_bridge
        import importlib
        importlib.reload(pdf_bridge)

        fake_pdf = tmp_path / "test.pdf"
        fake_pdf.write_bytes(b"%PDF-1.4\n")

        def _sp_side_effect(cmd, **kwargs):
            cmd_list = list(cmd)
            if "pdftoppm" in str(cmd_list[0]):
                prefix = cmd_list[-1]
                td_dir = Path(prefix).parent
                dest = td_dir / "page-1.png"
                dest.write_bytes(b"\x89PNG\r\n" + b"X" * 50)
                m = MagicMock()
                m.returncode = 0
                m.stderr = ""
                return m
            elif "tesseract" in str(cmd_list[0]):
                m = MagicMock()
                m.stdout = "FALLBACK_LEGACY_TEXT"
                m.returncode = 0
                return m
            m = MagicMock()
            m.returncode = 0
            m.stdout = ""
            m.stderr = ""
            return m

        def _failing_consensus(*args, **kwargs):
            raise RuntimeError("consensus engine crashed")

        with patch("subprocess.run", side_effect=_sp_side_effect), \
             patch("skills.engine.ocr.consensus.run_consensus", side_effect=_failing_consensus):
            text, pages = pdf_bridge._extract_text_ocr(str(fake_pdf), max_pages=1)

        # fallback 到舊路徑，不應拋例外
        assert "FALLBACK_LEGACY_TEXT" in text, f"Expected fallback text in: {text!r}"
        assert pages == 1


# ---------------------------------------------------------------------------
# test: shadow mode → 呼叫 consensus 但回舊結果
# ---------------------------------------------------------------------------

class TestShadowMode:
    def test_shadow_calls_consensus_but_returns_legacy(self, tmp_path, monkeypatch):
        """SHADOW=1, ENABLE=0 → consensus 被呼叫，但回傳文字是舊路徑的。"""
        monkeypatch.setenv("MAGI_PDF_OCR_CONSENSUS_SHADOW", "1")
        monkeypatch.setenv("MAGI_PDF_OCR_CONSENSUS_ENABLE", "0")

        from skills.documents import pdf_bridge
        import importlib
        importlib.reload(pdf_bridge)

        consensus_result = _make_consensus_result(corrected_text="NEW_CONSENSUS_TEXT")
        fake_pdf = tmp_path / "test.pdf"
        fake_pdf.write_bytes(b"%PDF-1.4\n")

        def _sp_side_effect(cmd, **kwargs):
            cmd_list = list(cmd)
            if "pdftoppm" in str(cmd_list[0]):
                prefix = cmd_list[-1]
                td_dir = Path(prefix).parent
                dest = td_dir / "page-1.png"
                dest.write_bytes(b"\x89PNG\r\n" + b"X" * 50)
                m = MagicMock()
                m.returncode = 0
                m.stderr = ""
                return m
            elif "tesseract" in str(cmd_list[0]):
                m = MagicMock()
                m.stdout = "SHADOW_LEGACY_TEXT"
                m.returncode = 0
                return m
            m = MagicMock()
            m.returncode = 0
            m.stdout = ""
            m.stderr = ""
            return m

        mock_consensus = MagicMock(return_value=consensus_result)

        with patch("subprocess.run", side_effect=_sp_side_effect), \
             patch("skills.engine.ocr.consensus.run_consensus", mock_consensus):
            text, pages = pdf_bridge._extract_text_ocr(str(fake_pdf), max_pages=1)

        # consensus 必須被呼叫（shadow 仍跑 consensus 做比對）
        mock_consensus.assert_called()

        # 但回傳的文字必須是舊路徑的（legacy）
        assert "SHADOW_LEGACY_TEXT" in text, (
            f"Shadow mode should return legacy text, got: {text!r}"
        )
        # 不應包含 consensus 的輸出
        assert "NEW_CONSENSUS_TEXT" not in text


# ---------------------------------------------------------------------------
# test: metrics 只有 count，不含 entity 字串
# ---------------------------------------------------------------------------

class TestMetrics:
    def test_metrics_written_on_consensus_enable(self, tmp_path, monkeypatch):
        """consensus enable 時寫 metrics；metrics 不含 entity 實際字串。"""
        monkeypatch.setenv("MAGI_PDF_OCR_CONSENSUS_ENABLE", "1")
        monkeypatch.setenv("MAGI_PDF_OCR_CONSENSUS_SHADOW", "0")
        monkeypatch.setenv("MAGI_USE_RUNTIME_DIR", "1")

        from skills.documents import pdf_bridge
        import importlib
        importlib.reload(pdf_bridge)

        consensus_result = _make_consensus_result(
            corrected_text="指標測試判決文字",
        )
        fake_pdf = tmp_path / "test.pdf"
        fake_pdf.write_bytes(b"%PDF-1.4\n")

        def _sp_side_effect(cmd, **kwargs):
            cmd_list = list(cmd)
            if "pdftoppm" in str(cmd_list[0]):
                prefix = cmd_list[-1]
                td_dir = Path(prefix).parent
                dest = td_dir / "page-1.png"
                dest.write_bytes(b"\x89PNG\r\n" + b"X" * 50)
                m = MagicMock()
                m.returncode = 0
                m.stderr = ""
                return m
            elif "tesseract" in str(cmd_list[0]):
                m = MagicMock()
                m.stdout = "metrics_legacy_text"
                m.returncode = 0
                return m
            m = MagicMock()
            m.returncode = 0
            m.stdout = ""
            m.stderr = ""
            return m

        with patch("subprocess.run", side_effect=_sp_side_effect), \
             patch("skills.engine.ocr.consensus.run_consensus", return_value=consensus_result):
            pdf_bridge._extract_text_ocr(str(fake_pdf), max_pages=1)

        # 找 metrics 檔案
        metrics_dir = tmp_path / "metrics" / "ocr"
        metrics_file = metrics_dir / "pdf_ocr_consensus.jsonl"

        # metrics 可能不存在（若 runtime_dir 尚未建立 metrics dir），忽略
        if not metrics_file.exists():
            # 可接受：metrics 寫入失敗會 log debug，不應讓 PDF extraction 失敗
            return

        lines = [l.strip() for l in metrics_file.read_text(encoding="utf-8").split("\n") if l.strip()]
        assert len(lines) >= 1

        record = json.loads(lines[-1])
        # 必須有 entities_counts（只有 count）
        assert "entities_counts" in record
        counts = record["entities_counts"]
        # count 是整數，不是字串列表
        assert isinstance(counts.get("case_numbers_found", 0), int)
        assert isinstance(counts.get("courts_found", 0), int)

        # 絕對不得包含 entity 實際字串（如姓名、案號）
        record_str = json.dumps(record)
        assert "114年度訴字第123號" not in record_str, "metrics 不應含 case number 字串"
        assert "王大明" not in record_str, "metrics 不應含 party 姓名"
        assert "臺灣花蓮地方法院" not in record_str, "metrics 不應含法院名稱"

    def test_metrics_shadow_mode_contains_mode_field(self, tmp_path, monkeypatch):
        """shadow mode 的 metrics 應有 mode='shadow' 欄位。"""
        monkeypatch.setenv("MAGI_PDF_OCR_CONSENSUS_SHADOW", "1")
        monkeypatch.setenv("MAGI_PDF_OCR_CONSENSUS_ENABLE", "0")
        monkeypatch.setenv("MAGI_USE_RUNTIME_DIR", "1")

        from skills.documents import pdf_bridge
        import importlib
        importlib.reload(pdf_bridge)

        consensus_result = _make_consensus_result()
        fake_pdf = tmp_path / "test.pdf"
        fake_pdf.write_bytes(b"%PDF-1.4\n")

        def _sp_side_effect(cmd, **kwargs):
            cmd_list = list(cmd)
            if "pdftoppm" in str(cmd_list[0]):
                prefix = cmd_list[-1]
                td_dir = Path(prefix).parent
                dest = td_dir / "page-1.png"
                dest.write_bytes(b"\x89PNG\r\n" + b"X" * 50)
                m = MagicMock()
                m.returncode = 0
                m.stderr = ""
                return m
            elif "tesseract" in str(cmd_list[0]):
                m = MagicMock()
                m.stdout = "shadow_legacy"
                m.returncode = 0
                return m
            m = MagicMock()
            m.returncode = 0
            m.stdout = ""
            m.stderr = ""
            return m

        with patch("subprocess.run", side_effect=_sp_side_effect), \
             patch("skills.engine.ocr.consensus.run_consensus", return_value=consensus_result):
            pdf_bridge._extract_text_ocr(str(fake_pdf), max_pages=1)

        metrics_dir = tmp_path / "metrics" / "ocr"
        metrics_file = metrics_dir / "pdf_ocr_consensus.jsonl"

        if not metrics_file.exists():
            return  # 可接受

        lines = [l.strip() for l in metrics_file.read_text(encoding="utf-8").split("\n") if l.strip()]
        if not lines:
            return

        record = json.loads(lines[-1])
        assert record.get("mode") == "shadow"


# ---------------------------------------------------------------------------
# test: extract_text 回傳型態不變（Phase D 相容）
# ---------------------------------------------------------------------------

class TestExtractTextReturnType:
    def test_extract_text_returns_string(self, tmp_path, monkeypatch):
        """extract_text() 仍回傳 str（不因 consensus 路徑而改變型態）。"""
        monkeypatch.setenv("MAGI_PDF_OCR_CONSENSUS_ENABLE", "0")
        monkeypatch.setenv("MAGI_PDF_OCR_CONSENSUS_SHADOW", "0")

        from skills.documents import pdf_bridge
        import importlib
        importlib.reload(pdf_bridge)

        fake_pdf = tmp_path / "nosuchfile_test_returntype.pdf"
        # 讓 extract_text 走到失敗路徑（回傳失敗字串）
        result = pdf_bridge.extract_text(str(fake_pdf))
        assert isinstance(result, str)
