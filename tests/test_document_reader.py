"""
test_document_reader.py
========================
Phase 1 tests for skills/engine/document_reader.py (MarkItDown adapter).

Covers:
- Quality gate scoring
- MarkItDown routing for digital files (DOCX, CSV, XLSX)
- PDF quality gate → OCR fallback trigger
- Graceful degradation when markitdown is unavailable
- Plain text extraction from markdown
"""
import os
import pytest
import tempfile
from unittest.mock import patch, MagicMock

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from skills.engine.document_reader import (
    DocumentResult,
    text_quality_score,
    read_document,
    _is_meaningful,
    _markdown_to_plain,
)


# ── Quality Gate Tests ────────────────────────────────────────────────────────

class TestTextQualityScore:
    """Quality gate should reliably distinguish real content from garbled text."""

    def test_legal_chinese_text_scores_high(self):
        text = (
            "原告主張被告侵權行為損害賠償，依民法第184條規定，法院判決被告應賠償。"
            "裁定主文：被告應給付原告新臺幣伍拾萬元。理由如下：原告起訴主張被告於民國"
            "114年3月15日，因過失致原告受傷，爰依侵權行為法律關係，請求損害賠償。"
        )
        score = text_quality_score(text)
        assert score >= 0.2, f"Legal Chinese text scored too low: {score}"

    def test_legal_english_text_scores_high(self):
        text = (
            "The court finds that the application for preliminary objections "
            "is without merit. The judgment of the tribunal is affirmed. "
            "Article 36 of the statute applies to this case."
        )
        score = text_quality_score(text)
        assert score >= 0.3, f"Legal English text scored too low: {score}"

    def test_empty_text_scores_zero(self):
        assert text_quality_score("") == 0.0
        assert text_quality_score("   ") == 0.0

    def test_garbled_text_scores_low(self):
        text = "x7@#$% \\x00 ||| <<< >>> ~~~ {{{ }}}"
        score = text_quality_score(text)
        assert score < 0.3, f"Garbled text scored too high: {score}"

    def test_short_meaningful_text(self):
        text = "法院裁定"
        score = text_quality_score(text)
        # Short but meaningful — should still be non-zero
        assert score > 0.0


class TestIsMeaningful:

    def test_meaningful(self):
        assert _is_meaningful("Hello World 測試中文文字內容") is True

    def test_not_meaningful_short(self):
        assert _is_meaningful("hi") is False

    def test_not_meaningful_whitespace(self):
        assert _is_meaningful("   \n\t   ") is False

    def test_empty(self):
        assert _is_meaningful("") is False


# ── Markdown → Plain Text ─────────────────────────────────────────────────────

class TestMarkdownToPlain:

    def test_strips_headings(self):
        assert _markdown_to_plain("# Title\n## Subtitle\nBody") == "Title\nSubtitle\nBody"

    def test_strips_bold_italic(self):
        assert _markdown_to_plain("**bold** and *italic*") == "bold and italic"

    def test_strips_links(self):
        assert _markdown_to_plain("[click](http://example.com)") == "click"

    def test_preserves_plain_text(self):
        text = "法院判決書\n被告王大明"
        assert _markdown_to_plain(text) == text


# ── read_document with Real MarkItDown ────────────────────────────────────────

class TestReadDocumentReal:
    """Tests that use real MarkItDown (if installed) with temp files."""

    def test_read_txt_file(self):
        with tempfile.NamedTemporaryFile(suffix=".txt", mode="w", delete=False, encoding="utf-8") as f:
            f.write("法院判決書：原告勝訴。被告應賠償損害。")
            path = f.name
        try:
            r = read_document(path)
            assert r.success is True
            assert "法院判決書" in r.text
            assert r.method == "markitdown"
        finally:
            os.unlink(path)

    def test_read_csv_file(self):
        with tempfile.NamedTemporaryFile(suffix=".csv", mode="w", delete=False, encoding="utf-8") as f:
            f.write("案號,當事人,案由\n114原訴24,王大明,侵權行為\n114原訴25,張三,債務不履行\n")
            path = f.name
        try:
            r = read_document(path)
            assert r.success is True
            assert "王大明" in r.text
            assert r.method == "markitdown"
        finally:
            os.unlink(path)

    def test_read_nonexistent_file(self):
        r = read_document("/tmp/does_not_exist_12345.pdf")
        assert r.success is False
        assert "file_not_found" in r.error

    def test_legacy_mode_skips_markitdown(self):
        """mode='legacy' should NOT call MarkItDown at all."""
        with tempfile.NamedTemporaryFile(suffix=".txt", mode="w", delete=False, encoding="utf-8") as f:
            f.write("test content")
            path = f.name
        try:
            with patch("skills.engine.document_reader._get_markitdown") as mock_md:
                r = read_document(path, mode="legacy")
                mock_md.assert_not_called()
        finally:
            os.unlink(path)


# ── PDF Quality Gate Routing ──────────────────────────────────────────────────

class TestPdfQualityGate:
    """PDF quality gate should trigger OCR fallback for low-quality extraction."""

    def test_good_quality_pdf_stays_markitdown(self):
        """When MarkItDown produces good text, no fallback needed."""
        mock_md = MagicMock()
        mock_result = MagicMock()
        mock_result.text_content = (
            "法院判決書 原告主張被告侵權行為損害賠償 "
            "依民法第184條規定 法院判決被告應賠償 裁定主文理由 " * 10
        )
        mock_md.convert.return_value = mock_result

        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            f.write(b"%PDF-1.4 fake")
            path = f.name

        try:
            with patch("skills.engine.document_reader._get_markitdown", return_value=mock_md), \
                 patch("skills.engine.document_reader._pdf_ocr_fallback") as mock_fb:
                # quality_threshold=0.15 ensures the rich legal text passes
                r = read_document(path, quality_threshold=0.15)
                assert r.method == "markitdown", f"got {r.method}, score={r.quality_score}"
                assert r.success is True
                mock_fb.assert_not_called()
        finally:
            os.unlink(path)

    def test_low_quality_pdf_triggers_fallback(self):
        """When MarkItDown produces garbled text, should fall back to OCR."""
        mock_md = MagicMock()
        mock_result = MagicMock()
        mock_result.text_content = "x7@#$% garbled \\x00 nonsense"
        mock_md.convert.return_value = mock_result

        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            f.write(b"%PDF-1.4 fake")
            path = f.name

        try:
            with patch("skills.engine.document_reader._get_markitdown", return_value=mock_md), \
                 patch("skills.engine.document_reader._pdf_ocr_fallback") as mock_fallback:
                mock_fallback.return_value = DocumentResult(
                    success=True, text="OCR result", method="legacy_ocr", quality_score=0.5
                )
                r = read_document(path)
                assert r.method == "legacy_ocr"
                mock_fallback.assert_called_once()
        finally:
            os.unlink(path)

    def test_markitdown_mode_no_fallback(self):
        """mode='markitdown' should not trigger OCR fallback."""
        mock_md = MagicMock()
        mock_result = MagicMock()
        mock_result.text_content = ""
        mock_md.convert.return_value = mock_result

        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            f.write(b"%PDF-1.4 fake")
            path = f.name

        try:
            with patch("skills.engine.document_reader._get_markitdown", return_value=mock_md):
                r = read_document(path, mode="markitdown")
                assert r.success is False
                assert r.error == "empty_output"
        finally:
            os.unlink(path)


# ── Graceful Degradation ─────────────────────────────────────────────────────

class TestGracefulDegradation:
    """When markitdown is unavailable, should silently fall back."""

    def test_unavailable_markitdown_falls_back(self):
        with tempfile.NamedTemporaryFile(suffix=".docx", delete=False) as f:
            f.write(b"fake docx content")
            path = f.name

        try:
            with patch("skills.engine.document_reader._get_markitdown", return_value=None), \
                 patch("skills.engine.document_reader._file_bridge_fallback") as mock_fb:
                mock_fb.return_value = DocumentResult(
                    success=True, text="fallback text", method="fallback"
                )
                r = read_document(path)
                assert r.method == "fallback"
                mock_fb.assert_called_once()
        finally:
            os.unlink(path)

    def test_markitdown_exception_falls_back(self):
        mock_md = MagicMock()
        mock_md.convert.side_effect = RuntimeError("converter crash")

        with tempfile.NamedTemporaryFile(suffix=".docx", delete=False) as f:
            f.write(b"fake docx content")
            path = f.name

        try:
            with patch("skills.engine.document_reader._get_markitdown", return_value=mock_md), \
                 patch("skills.engine.document_reader._file_bridge_fallback") as mock_fb:
                mock_fb.return_value = DocumentResult(
                    success=True, text="fallback text", method="fallback"
                )
                r = read_document(path)
                assert r.method == "fallback"
        finally:
            os.unlink(path)


# ── XLSX Metadata ─────────────────────────────────────────────────────────────

class TestXlsxMetadata:

    def test_xlsx_has_cell_access_false(self):
        """XLSX via MarkItDown should signal no cell-level access."""
        mock_md = MagicMock()
        mock_result = MagicMock()
        mock_result.text_content = "| 案號 | 金額 |\n|------|------|\n| 114 | 5000 |"
        mock_md.convert.return_value = mock_result

        with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as f:
            f.write(b"fake xlsx")
            path = f.name

        try:
            with patch("skills.engine.document_reader._get_markitdown", return_value=mock_md):
                r = read_document(path)
                assert r.success is True
                assert r.metadata.get("has_cell_access") is False
        finally:
            os.unlink(path)


# ── Elapsed Time Tracking ─────────────────────────────────────────────────────

class TestElapsedTime:

    def test_elapsed_ms_is_populated(self):
        with tempfile.NamedTemporaryFile(suffix=".txt", mode="w", delete=False, encoding="utf-8") as f:
            f.write("test content")
            path = f.name
        try:
            r = read_document(path)
            assert r.elapsed_ms >= 0.0
        finally:
            os.unlink(path)
