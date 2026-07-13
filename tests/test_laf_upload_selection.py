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


def test_closing_upload_includes_mediation_transcript_from_transcript_folder(tmp_path):
    case_dir = tmp_path / "2026-0002-測試-調解-損害賠償"
    transcript_dir = case_dir / "08_筆錄"
    transcript_dir.mkdir(parents=True)
    mediation = transcript_dir / "20260601 花蓮地方法院調解筆錄.pdf"
    mediation.write_bytes(b"pdf")

    orch = object.__new__(LAFOrchestrator)
    result = orch._collect_progress_upload_pdfs(str(case_dir), laf_case_no="1150000-X-002", action="closing")

    assert str(mediation) in result["mediation_success_pdf_files"]
    assert str(mediation) in result["pdf_files"]


def test_closing_basis_only_mode_allows_mediation_transcript(tmp_path, monkeypatch):
    monkeypatch.setenv("MAGI_LAF_CLOSING_INCLUDE_PLEADINGS", "0")
    case_dir = tmp_path / "2026-0003-測試-調解-損害賠償"
    transcript_dir = case_dir / "08_筆錄"
    transcript_dir.mkdir(parents=True)
    mediation = transcript_dir / "20260601 花蓮地方法院調解筆錄.pdf"
    mediation.write_bytes(b"pdf")

    orch = object.__new__(LAFOrchestrator)
    docs = orch._scan_case_folder_docs(str(case_dir), action="closing")
    result = orch._collect_progress_upload_pdfs(str(case_dir), laf_case_no="1150000-X-003", action="closing")

    assert str(mediation) in docs["mediation_success_files"]
    assert result["ok"] is True
    assert str(mediation) in result["pdf_files"]
