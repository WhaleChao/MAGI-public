from pathlib import Path

from casper_ecosystem.law_firm_orchestrators.laf_orchestrator import LAFOrchestrator


def test_oversize_pleading_pdf_falls_back_to_clean_word(tmp_path, monkeypatch):
    monkeypatch.setenv("MAGI_LAF_PORTAL_MAX_UPLOAD_MB", "1")

    case_dir = tmp_path / "2026-0001-測試-一審-損害賠償"
    plead_dir = case_dir / "04_我方歷次書狀" / "20260520 民事準備狀"
    plead_dir.mkdir(parents=True)
    oversize_pdf = plead_dir / "20260520 民事準備狀存底.pdf"
    clean_word = plead_dir / "20260520 民事準備狀清稿.docx"
    oversize_pdf.write_bytes(b"x" * (2 * 1024 * 1024))
    clean_word.write_bytes(b"placeholder")

    orch = object.__new__(LAFOrchestrator)
    result = orch._collect_progress_upload_pdfs(str(case_dir), laf_case_no="1150000-X-001", action="closing")

    assert str(clean_word) in result["pdf_files"]
    assert result["converted"][0]["fallback"] == "oversize_pdf_to_final_word"
