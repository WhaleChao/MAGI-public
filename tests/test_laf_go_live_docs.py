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


def test_laf_norm_token_unifies_common_name_variants():
    assert LAFOrchestrator._norm_token("遊秀鈴") == LAFOrchestrator._norm_token("游秀鈴")
    assert LAFOrchestrator._norm_token("王臺銘") == LAFOrchestrator._norm_token("王台銘")
