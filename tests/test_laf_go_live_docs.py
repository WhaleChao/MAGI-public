from casper_ecosystem.law_firm_orchestrators.laf_orchestrator import LAFOrchestrator


def test_go_live_docs_scan_ignores_laf_download_folder(tmp_path):
    case_dir = tmp_path / "2026-0055-游秀鈴-二審-過失致死罪"
    laf_dir = case_dir / "01_法扶資料"
    laf_dir.mkdir(parents=True)
    notice = laf_dir / "扶助律師接案通知書_1150521-A-044_1150525.pdf"
    poa = laf_dir / "委任狀_1150521-A-044_1150525.pdf"
    notice.write_bytes(b"%PDF-1.4\n")
    poa.write_bytes(b"%PDF-1.4\n")

    orch = LAFOrchestrator(dry_run=True)
    docs, scan_scope = orch._scan_go_live_docs(str(case_dir))

    assert str(notice) not in docs["opening_notice_files"]
    assert str(poa) not in docs["poa_files"]
    assert "02_開辦資料" in scan_scope


def test_laf_portal_attachment_summary_counts_existing_blank_forms(tmp_path):
    case_dir = tmp_path / "2026-0059-測試當事人-一審-測試案由"
    laf_dir = case_dir / "01_法扶資料"
    staff_dir = laf_dir / "專員來信"
    staff_dir.mkdir(parents=True)
    official = [
        laf_dir / "扶助律師接案通知書_1150527-E-024_1150601.pdf",
        laf_dir / "委任狀_1150527-E-024_1150601.pdf",
        laf_dir / "法律扶助申請書_1150527-E-024_1150601.pdf",
        laf_dir / "案件概述單_1150527-E-024_1150601.pdf",
    ]
    for path in official:
        path.write_bytes(b"%PDF-1.4\n")
    (laf_dir / "1150601_1150527-E-024.zip").write_bytes(b"zip")
    (laf_dir / ".gitkeep").write_text("", encoding="utf-8")
    (staff_dir / "1150527-E-024 測試當事人 2A.pdf").write_bytes(b"%PDF-1.4\n")

    orch = LAFOrchestrator(dry_run=True)

    assert {p.split("/")[-1] for p in orch._existing_laf_portal_attachment_files(str(case_dir))} == {
        path.name for path in official
    }
    docs, _scan_scope = orch._scan_go_live_docs(str(case_dir))
    assert docs["opening_notice_files"] == []
    assert docs["poa_files"] == []


def test_go_live_docs_scan_uses_prepared_opening_folder(tmp_path):
    case_dir = tmp_path / "2026-0055-游秀鈴-二審-過失致死罪"
    go_live_dir = case_dir / "02_開辦資料"
    go_live_dir.mkdir(parents=True)
    notice = go_live_dir / "開辦通知書_1150521-A-044_已簽.pdf"
    poa = go_live_dir / "委任狀_1150521-A-044_已簽.pdf"
    notice.write_bytes(b"%PDF-1.4\n")
    poa.write_bytes(b"%PDF-1.4\n")

    orch = LAFOrchestrator(dry_run=True)
    docs, scan_scope = orch._scan_go_live_docs(str(case_dir))

    assert str(notice) in docs["opening_notice_files"]
    assert str(poa) in docs["poa_files"]
    assert "02_開辦資料" in scan_scope


def test_go_live_docs_scan_accepts_opening_report_form(tmp_path):
    case_dir = tmp_path / "2026-0058-王惠薰-消費者債務清理-更生"
    go_live_dir = case_dir / "02_開辦資料"
    go_live_dir.mkdir(parents=True)
    report = go_live_dir / "1150529-E-005 王惠薰 開辦回報單.pdf"
    report.write_bytes(b"%PDF-1.4\n")

    orch = LAFOrchestrator(dry_run=True)
    docs, scan_scope = orch._scan_go_live_docs(str(case_dir))

    assert str(report) in docs["opening_notice_files"]
    assert "02_開辦資料" in scan_scope


def test_go_live_docs_scan_accepts_stored_pleading_as_general_case_proof(tmp_path):
    case_dir = tmp_path / "2026-0056-張家茂-一審-監獄行刑法"
    go_live_dir = case_dir / "02_開辦資料"
    pleading_dir = case_dir / "04_我方歷次書狀" / "20260608 行政訴訟起訴狀"
    go_live_dir.mkdir(parents=True)
    pleading_dir.mkdir(parents=True)
    report = go_live_dir / "1150525-J-015 張家茂 開辦回報單.pdf"
    stored = pleading_dir / "20260608 行政訴訟起訴狀存底.pdf"
    report.write_bytes(b"%PDF-1.4\n")
    stored.write_bytes(b"%PDF-1.4\n")

    orch = LAFOrchestrator(dry_run=True)
    docs, scan_scope = orch._scan_go_live_docs(str(case_dir))
    upload_files = orch._find_go_live_upload_files(str(case_dir), is_consumer_debt=False)

    assert str(report) in docs["opening_notice_files"]
    assert str(stored) in docs["stored_pleading_files"]
    assert str(stored) in docs["opening_proof_files"]
    assert "04_我方歷次書狀" in scan_scope
    assert str(stored) in upload_files
    assert str(report) in upload_files


def test_laf_norm_token_unifies_common_name_variants():
    assert LAFOrchestrator._norm_token("遊秀鈴") == LAFOrchestrator._norm_token("游秀鈴")
    assert LAFOrchestrator._norm_token("王臺銘") == LAFOrchestrator._norm_token("王台銘")
