# -*- coding: utf-8 -*-
"""
tests/test_obsidian_ocr_consensus.py

Unit tests for Phase E: Obsidian extractors consensus opt-in.

Verified behaviours:
  1. flag off  → consensus NOT called; legacy tesseract path used
  2. flag on + consensus success  → consensus text returned
  3. flag on + consensus failure  → fallback to legacy text
  4. metrics written (count field present) when flag on + consensus called

禁止 module-level import:  api.server / api.tools_api / daemon
Python 3.9 + 3.14 相容 (no walrus, no |union syntax, no slots/kw_only).
"""

from __future__ import annotations

import os
import sys
import types
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Ensure repo root is on path
# ---------------------------------------------------------------------------
_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))


# ---------------------------------------------------------------------------
# Helpers to fake OCRConsensusResult
# ---------------------------------------------------------------------------

def _make_consensus_result(success, corrected_text="", confidence=0.85):
    """Return a lightweight mock that quacks like OCRConsensusResult."""
    r = MagicMock()
    r.success = success
    r.corrected_text = corrected_text
    r.selected_text = corrected_text
    r.confidence = confidence
    return r


# ---------------------------------------------------------------------------
# Fixture: patch away heavy dependencies so tests run without GPU / tesseract
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _no_fitz_subprocess(monkeypatch):
    """
    Mock fitz.open so we control pages / PNG rendering.
    Legacy OCR is patched through the shared tesseract_provider.
    The consensus module is patched individually per test.
    """
    # fake fitz pixmap
    fake_pix = MagicMock()
    fake_pix.tobytes.return_value = b"\x89PNG_fake_bytes"

    # fake fitz page
    fake_page = MagicMock()
    fake_page.get_pixmap.return_value = fake_pix

    # fake fitz doc with 1 page
    fake_doc = MagicMock()
    fake_doc.__len__ = MagicMock(return_value=1)
    fake_doc.__getitem__ = MagicMock(return_value=fake_page)
    fake_doc.close = MagicMock()

    fake_fitz_module = types.ModuleType("fitz")
    fake_fitz_module.open = MagicMock(return_value=fake_doc)
    monkeypatch.setitem(sys.modules, "fitz", fake_fitz_module)
    return fake_doc


# ---------------------------------------------------------------------------
# Test 1: flag off → consensus NOT called
# ---------------------------------------------------------------------------

class TestFlagOff:
    def test_no_consensus_call_when_flag_off(self, monkeypatch, tmp_path):
        """When MAGI_OBSIDIAN_OCR_CONSENSUS_ENABLE=0, run_consensus must not be invoked."""
        monkeypatch.setenv("MAGI_OBSIDIAN_OCR_CONSENSUS_ENABLE", "0")

        consensus_called = []

        # Patch consensus module so we detect if it's imported
        fake_consensus_mod = MagicMock()
        fake_consensus_mod.run_consensus.side_effect = lambda *a, **k: consensus_called.append(1)
        monkeypatch.setitem(sys.modules, "skills.engine.ocr.consensus", fake_consensus_mod)

        from skills.engine.ocr.ocr_schema import OCRProviderResult
        monkeypatch.setattr(
            "skills.engine.ocr.tesseract_provider.run",
            lambda *a, **k: OCRProviderResult(
                success=True,
                provider="tesseract",
                raw_text="legacy ocr text",
                corrected_text="legacy ocr text",
            ),
        )

        from skills.obsidian.extractors import _extract_pdf_ocr
        import importlib
        import skills.obsidian.extractors as ext_mod
        importlib.reload(ext_mod)

        dummy_pdf = tmp_path / "dummy.pdf"
        dummy_pdf.write_bytes(b"%PDF-1.4 fake")

        result = ext_mod._extract_pdf_ocr(dummy_pdf)

        assert result is not None
        assert result["success"] is True
        assert "legacy" in result["text"] or result["text"]
        assert len(consensus_called) == 0, "run_consensus should not be called when flag is off"

    def test_method_string_contains_tesseract_when_flag_off(self, monkeypatch, tmp_path):
        """When flag off, method field should mention tesseract (not consensus)."""
        monkeypatch.setenv("MAGI_OBSIDIAN_OCR_CONSENSUS_ENABLE", "0")

        from skills.engine.ocr.ocr_schema import OCRProviderResult
        monkeypatch.setattr(
            "skills.engine.ocr.tesseract_provider.run",
            lambda *a, **k: OCRProviderResult(
                success=True,
                provider="tesseract",
                raw_text="some OCR text here",
                corrected_text="some OCR text here",
            ),
        )

        import importlib
        import skills.obsidian.extractors as ext_mod
        importlib.reload(ext_mod)

        dummy_pdf = tmp_path / "dummy.pdf"
        dummy_pdf.write_bytes(b"%PDF-1.4 fake")

        result = ext_mod._extract_pdf_ocr(dummy_pdf)
        assert result is not None
        assert "tesseract" in result["method"]
        assert "consensus" not in result["method"]


# ---------------------------------------------------------------------------
# Test 2: flag on + consensus success → consensus text adopted
# ---------------------------------------------------------------------------

class TestFlagOnConsensusSuccess:
    def test_consensus_text_returned_when_flag_on(self, monkeypatch, tmp_path):
        """When flag on and consensus succeeds, the consensus text is returned."""
        monkeypatch.setenv("MAGI_OBSIDIAN_OCR_CONSENSUS_ENABLE", "1")
        from skills.engine.ocr.ocr_schema import OCRProviderResult
        monkeypatch.setattr(
            "skills.engine.ocr.tesseract_provider.run",
            lambda *a, **k: OCRProviderResult(
                success=True,
                provider="tesseract",
                raw_text="legacy ocr text",
                corrected_text="legacy ocr text",
            ),
        )

        consensus_text = "高品質共識文字 114年度某字第123號"
        fake_result = _make_consensus_result(success=True, corrected_text=consensus_text)

        fake_consensus_mod = MagicMock()
        fake_consensus_mod.run_consensus.return_value = fake_result

        # Patch the nested import path used inside _run_consensus_page
        monkeypatch.setitem(sys.modules, "skills.engine.ocr.consensus", fake_consensus_mod)

        # Also mock runtime_dir to avoid FS side effects
        fake_rt = MagicMock()
        fake_rt.metrics.return_value = tmp_path
        fake_rt.atomic_append_jsonl = MagicMock()
        monkeypatch.setitem(sys.modules, "api.platforms.runtime_dir", fake_rt)
        monkeypatch.setitem(sys.modules, "api.platforms", types.ModuleType("api.platforms"))

        import importlib
        import skills.obsidian.extractors as ext_mod
        importlib.reload(ext_mod)

        dummy_pdf = tmp_path / "dummy.pdf"
        dummy_pdf.write_bytes(b"%PDF-1.4 fake")

        result = ext_mod._extract_pdf_ocr(dummy_pdf)

        assert result is not None
        assert result["success"] is True
        assert consensus_text in result["text"]
        assert "consensus" in result["method"]

    def test_legacy_text_not_in_result_when_consensus_succeeds(self, monkeypatch, tmp_path):
        """If consensus provides text, legacy text must not appear in output."""
        monkeypatch.setenv("MAGI_OBSIDIAN_OCR_CONSENSUS_ENABLE", "1")
        from skills.engine.ocr.ocr_schema import OCRProviderResult
        monkeypatch.setattr(
            "skills.engine.ocr.tesseract_provider.run",
            lambda *a, **k: OCRProviderResult(
                success=True,
                provider="tesseract",
                raw_text="LEGACY_MARKER_TEXT",
                corrected_text="LEGACY_MARKER_TEXT",
            ),
        )

        fake_result = _make_consensus_result(success=True, corrected_text="共識文字勝出")
        fake_consensus_mod = MagicMock()
        fake_consensus_mod.run_consensus.return_value = fake_result
        monkeypatch.setitem(sys.modules, "skills.engine.ocr.consensus", fake_consensus_mod)

        fake_rt = MagicMock()
        fake_rt.metrics.return_value = tmp_path
        monkeypatch.setitem(sys.modules, "api.platforms.runtime_dir", fake_rt)
        monkeypatch.setitem(sys.modules, "api.platforms", types.ModuleType("api.platforms"))

        import importlib
        import skills.obsidian.extractors as ext_mod
        importlib.reload(ext_mod)

        dummy_pdf = tmp_path / "dummy.pdf"
        dummy_pdf.write_bytes(b"%PDF-1.4 fake")

        result = ext_mod._extract_pdf_ocr(dummy_pdf)
        assert result is not None
        assert "LEGACY_MARKER_TEXT" not in result["text"]


# ---------------------------------------------------------------------------
# Test 3: flag on + consensus failure → fallback to legacy
# ---------------------------------------------------------------------------

class TestFlagOnConsensusFail:
    def test_fallback_to_legacy_when_consensus_fails(self, monkeypatch, tmp_path):
        """When consensus raises exception, legacy tesseract result is returned."""
        monkeypatch.setenv("MAGI_OBSIDIAN_OCR_CONSENSUS_ENABLE", "1")
        from skills.engine.ocr.ocr_schema import OCRProviderResult
        monkeypatch.setattr(
            "skills.engine.ocr.tesseract_provider.run",
            lambda *a, **k: OCRProviderResult(
                success=True,
                provider="tesseract",
                raw_text="FALLBACK_LEGACY_TEXT",
                corrected_text="FALLBACK_LEGACY_TEXT",
            ),
        )

        # consensus module raises
        fake_consensus_mod = MagicMock()
        fake_consensus_mod.run_consensus.side_effect = RuntimeError("OCR engine not available")
        monkeypatch.setitem(sys.modules, "skills.engine.ocr.consensus", fake_consensus_mod)

        fake_rt = MagicMock()
        fake_rt.metrics.return_value = tmp_path
        monkeypatch.setitem(sys.modules, "api.platforms.runtime_dir", fake_rt)
        monkeypatch.setitem(sys.modules, "api.platforms", types.ModuleType("api.platforms"))

        import importlib
        import skills.obsidian.extractors as ext_mod
        importlib.reload(ext_mod)

        dummy_pdf = tmp_path / "dummy.pdf"
        dummy_pdf.write_bytes(b"%PDF-1.4 fake")

        result = ext_mod._extract_pdf_ocr(dummy_pdf)

        assert result is not None
        assert result["success"] is True
        assert "FALLBACK_LEGACY_TEXT" in result["text"]

    def test_fallback_when_consensus_returns_none(self, monkeypatch, tmp_path):
        """When consensus returns None (success=False), legacy text is used."""
        monkeypatch.setenv("MAGI_OBSIDIAN_OCR_CONSENSUS_ENABLE", "1")
        from skills.engine.ocr.ocr_schema import OCRProviderResult
        monkeypatch.setattr(
            "skills.engine.ocr.tesseract_provider.run",
            lambda *a, **k: OCRProviderResult(
                success=True,
                provider="tesseract",
                raw_text="LEGACY_FALLBACK_RESULT",
                corrected_text="LEGACY_FALLBACK_RESULT",
            ),
        )

        failure_result = _make_consensus_result(success=False)
        fake_consensus_mod = MagicMock()
        fake_consensus_mod.run_consensus.return_value = failure_result
        monkeypatch.setitem(sys.modules, "skills.engine.ocr.consensus", fake_consensus_mod)

        fake_rt = MagicMock()
        fake_rt.metrics.return_value = tmp_path
        monkeypatch.setitem(sys.modules, "api.platforms.runtime_dir", fake_rt)
        monkeypatch.setitem(sys.modules, "api.platforms", types.ModuleType("api.platforms"))

        import importlib
        import skills.obsidian.extractors as ext_mod
        importlib.reload(ext_mod)

        dummy_pdf = tmp_path / "dummy.pdf"
        dummy_pdf.write_bytes(b"%PDF-1.4 fake")

        result = ext_mod._extract_pdf_ocr(dummy_pdf)
        assert result is not None
        assert "LEGACY_FALLBACK_RESULT" in result["text"]


# ---------------------------------------------------------------------------
# Test 4: metrics written when flag on
# ---------------------------------------------------------------------------

class TestMetricsWrite:
    def test_metrics_written_when_consensus_enabled(self, monkeypatch, tmp_path):
        """When flag on, atomic_append_jsonl should be called at least once per page."""
        monkeypatch.setenv("MAGI_OBSIDIAN_OCR_CONSENSUS_ENABLE", "1")
        from skills.engine.ocr.ocr_schema import OCRProviderResult
        monkeypatch.setattr(
            "skills.engine.ocr.tesseract_provider.run",
            lambda *a, **k: OCRProviderResult(
                success=True,
                provider="tesseract",
                raw_text="some text",
                corrected_text="some text",
            ),
        )

        fake_result = _make_consensus_result(success=True, corrected_text="文字")
        fake_consensus_mod = MagicMock()
        fake_consensus_mod.run_consensus.return_value = fake_result
        monkeypatch.setitem(sys.modules, "skills.engine.ocr.consensus", fake_consensus_mod)

        append_calls = []
        metrics_dir = tmp_path / "metrics" / "ocr"
        metrics_dir.mkdir(parents=True)

        fake_rt = MagicMock()
        fake_rt.metrics.return_value = metrics_dir
        fake_rt.atomic_append_jsonl.side_effect = lambda *a, **k: append_calls.append(a)
        monkeypatch.setitem(sys.modules, "api.platforms.runtime_dir", fake_rt)
        monkeypatch.setitem(sys.modules, "api.platforms", types.ModuleType("api.platforms"))

        import importlib
        import skills.obsidian.extractors as ext_mod
        importlib.reload(ext_mod)

        dummy_pdf = tmp_path / "dummy.pdf"
        dummy_pdf.write_bytes(b"%PDF-1.4 fake")

        ext_mod._extract_pdf_ocr(dummy_pdf)

        assert len(append_calls) >= 1, "atomic_append_jsonl should be called at least once per page"

    def test_metrics_record_has_no_raw_text(self, monkeypatch, tmp_path):
        """Metrics record must only contain counts/hashes, not raw OCR text."""
        monkeypatch.setenv("MAGI_OBSIDIAN_OCR_CONSENSUS_ENABLE", "1")
        from skills.engine.ocr.ocr_schema import OCRProviderResult
        monkeypatch.setattr(
            "skills.engine.ocr.tesseract_provider.run",
            lambda *a, **k: OCRProviderResult(
                success=True,
                provider="tesseract",
                raw_text="SECRET_TEXT_SHOULD_NOT_BE_IN_METRICS",
                corrected_text="SECRET_TEXT_SHOULD_NOT_BE_IN_METRICS",
            ),
        )

        fake_result = _make_consensus_result(success=True, corrected_text="共識文字")
        fake_consensus_mod = MagicMock()
        fake_consensus_mod.run_consensus.return_value = fake_result
        monkeypatch.setitem(sys.modules, "skills.engine.ocr.consensus", fake_consensus_mod)

        captured_records = []
        fake_rt = MagicMock()
        fake_rt.metrics.return_value = tmp_path
        fake_rt.atomic_append_jsonl.side_effect = lambda path, record, **k: captured_records.append(record)
        monkeypatch.setitem(sys.modules, "api.platforms.runtime_dir", fake_rt)
        monkeypatch.setitem(sys.modules, "api.platforms", types.ModuleType("api.platforms"))

        import importlib
        import skills.obsidian.extractors as ext_mod
        importlib.reload(ext_mod)

        dummy_pdf = tmp_path / "dummy.pdf"
        dummy_pdf.write_bytes(b"%PDF-1.4 fake")

        ext_mod._extract_pdf_ocr(dummy_pdf)

        assert len(captured_records) >= 1
        record = captured_records[0]
        # Record must have count/hash fields
        assert "img_hash" in record
        assert "consensus_len" in record
        assert "legacy_len" in record
        # Must NOT contain raw text strings
        record_str = str(record)
        assert "SECRET_TEXT_SHOULD_NOT_BE_IN_METRICS" not in record_str
        assert "共識文字" not in record_str
