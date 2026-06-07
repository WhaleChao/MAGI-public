from __future__ import annotations

import sys
import types
from datetime import datetime, timedelta, timezone

from api.osc.drive_case_sync import (
    CaseFolder,
    CaseMeta,
    FileEntry,
    build_file_sync_plan,
    classify_drive_case_folder,
    classify_local_case_folder,
    compare_case_folders,
    create_missing_drive_case_folders,
    db_local_cases_for_numbers,
    drive_to_nas_download_skip_reason,
    drive_relative_path_for_local_case,
    ensure_drive_case_folder_for_local_case,
    ensure_drive_folder_path,
    execute_nas_to_drive_uploads,
    find_existing_drive_case_folder_for_local_case,
    find_drive_case_folder_by_broad_search,
    default_active_case_roots,
    drive_to_nas_relative_path,
    export_relative_path,
    extract_case_meta,
    infer_case_kind,
    is_decisive_context_term,
    match_keys,
    meaningful_terms,
    normalize_court_case_no,
    nas_filesystem_relative_path,
    resolve_ambiguous_cases_with_context,
    resolve_drive_only_cases_with_context,
    nas_to_drive_relative_path,
    safe_child_path,
    score_context_candidates,
    semantic_relative_path,
    suggest_canonical_path,
    sync_scope_exclusion_reason,
    load_case_aliases,
    load_case_exclusions,
    run_priority_case_sync,
    _download_drive_entry,
    _drive_list_children,
)


def test_extract_osc_case_folder_metadata():
    meta = extract_case_meta("2026-0001-測試甲-一審-勞工爭議")
    assert meta.case_number == "2026-0001"
    assert meta.client_hint == "測試甲"
    assert meta.reason_hint == "勞工爭議"


def test_extract_laf_drive_folder_metadata():
    meta = extract_case_meta("測試乙-1150101-A-001-刑事一審辯護-詐欺")
    assert meta.laf_case_no == "1150101-A-001"
    assert meta.client_hint == "測試乙"
    assert meta.reason_hint == "詐欺"


def test_extract_parenthesized_legacy_folder_metadata():
    meta = extract_case_meta("測試丙等案(債務人異議之訴)")
    assert meta.client_hint == "測試丙等案"
    assert meta.reason_hint == "債務人異議之訴"


def test_classify_drive_and_local_case_folders():
    assert classify_drive_case_folder("法扶案件/Lumi/測試乙-1150101-A-001-刑事一審辯護-詐欺") == {
        "category": "法扶案件",
        "status": "active",
        "owner_bucket": "Lumi",
        "case_kind": "",
    }
    assert classify_drive_case_folder("結案案件/法扶案件/Lumi/測試丁-1140101-T-001-消費者債務清理事件") == {
        "category": "法扶案件",
        "status": "closed",
        "owner_bucket": "Lumi",
        "case_kind": "",
    }
    assert classify_drive_case_folder("結案案件/法扶案件/Lumi") is None
    assert classify_drive_case_folder("法扶案件/Lumi/01.消債") is None
    assert classify_drive_case_folder("法扶案件/Lumi/01.消債/測試丁-1140101-T-001-更生") == {
        "category": "法扶案件",
        "status": "active",
        "owner_bucket": "Lumi",
        "case_kind": "消費者債務清理",
    }
    assert classify_local_case_folder("法扶案件/刑事/2026-0002-測試乙-一審-詐欺", status="active") == {
        "category": "法扶案件",
        "status": "active",
        "owner_bucket": "",
        "case_kind": "刑事",
    }


def test_infer_case_kind_and_suggest_path(monkeypatch):
    monkeypatch.setenv("MAGI_CANONICAL_ACTIVE_CASE_PREFIX", "Z:/lumi63181107/01_案件")
    case = CaseFolder(
        source="drive",
        path="法扶案件/Lumi/測試乙-1150101-A-001-刑事一審辯護-詐欺",
        relative_path="法扶案件/Lumi/測試乙-1150101-A-001-刑事一審辯護-詐欺",
        name="測試乙-1150101-A-001-刑事一審辯護-詐欺",
        category="法扶案件",
        status="active",
        owner_bucket="Lumi",
        meta=extract_case_meta("測試乙-1150101-A-001-刑事一審辯護-詐欺"),
    )
    assert infer_case_kind(case.category, case.name)[0] == "刑事"
    path, confidence, note = suggest_canonical_path(case)
    assert path == "Z:/lumi63181107/01_案件/法扶案件/刑事/測試乙-1150101-A-001-刑事一審辯護-詐欺"
    assert confidence == "medium"
    assert note == ""


def test_non_standard_drive_category_requires_review():
    case = CaseFolder(
        source="drive",
        path="諮詢案件/線上諮詢案件/中選會案件",
        relative_path="諮詢案件/線上諮詢案件/中選會案件",
        name="中選會案件",
        category="諮詢案件",
        status="active",
        case_kind="線上諮詢案件",
        meta=extract_case_meta("中選會案件"),
    )
    path, confidence, note = suggest_canonical_path(case)
    assert path == ""
    assert confidence == "needs_review"
    assert "非 OSC 標準案件根目錄" in note


def test_match_by_laf_case_number(monkeypatch):
    monkeypatch.setattr("api.osc.drive_case_sync.lookup_db_case_contexts", lambda nums: {})
    drive = CaseFolder(
        source="drive",
        path="法扶案件/Lumi/測試乙-1150101-A-001-刑事一審辯護-詐欺",
        relative_path="法扶案件/Lumi/測試乙-1150101-A-001-刑事一審辯護-詐欺",
        name="測試乙-1150101-A-001-刑事一審辯護-詐欺",
        category="法扶案件",
        status="active",
        meta=extract_case_meta("測試乙-1150101-A-001-刑事一審辯護-詐欺"),
    )
    local = CaseFolder(
        source="nas",
        path="/cases/法扶案件/刑事/2026-0002-測試乙-一審-詐欺",
        relative_path="法扶案件/刑事/2026-0002-測試乙-一審-詐欺",
        name="2026-0002-測試乙-一審-詐欺",
        category="法扶案件",
        status="active",
        meta=CaseMeta(case_number="2026-0002", laf_case_no="1150101-A-001", client_hint="測試乙", reason_hint="詐欺"),
    )
    result = compare_case_folders([drive], [local])
    assert len(result["matched"]) == 1
    assert not result["drive_only"]
    assert not result["local_only"]


def test_same_name_different_laf_numbers_do_not_match(monkeypatch):
    drive = CaseFolder(
        source="drive",
        path="法扶案件/Lumi/游秀鈴-1140715-A-024-刑事一審辯護-傷害致死等",
        relative_path="法扶案件/Lumi/游秀鈴-1140715-A-024-刑事一審辯護-傷害致死等",
        name="游秀鈴-1140715-A-024-刑事一審辯護-傷害致死等",
        category="法扶案件",
        status="active",
        owner_bucket="Lumi",
        meta=extract_case_meta("游秀鈴-1140715-A-024-刑事一審辯護-傷害致死等"),
    )
    different_laf_case = CaseFolder(
        source="nas",
        path="/cases/法扶案件/刑事/2026-0055-游秀鈴-二審-過失致死罪",
        relative_path="法扶案件/刑事/2026-0055-游秀鈴-二審-過失致死罪",
        name="2026-0055-游秀鈴-二審-過失致死罪",
        category="法扶案件",
        status="active",
        case_kind="刑事",
        meta=CaseMeta(case_number="2026-0055", laf_case_no="1150521-A-044", client_hint="游秀鈴", reason_hint="過失致死罪"),
    )
    monkeypatch.setattr("api.osc.drive_case_sync.lookup_db_case_contexts", lambda nums: {})
    result = compare_case_folders([drive], [different_laf_case])
    assert not result["matched"]
    assert not result["drive_only"]
    assert result["out_of_scope"][0]["drive"].relative_path == drive.relative_path
    assert "法扶案號不同" in result["out_of_scope"][0]["reason"]


def test_active_drive_folder_does_not_match_db_closed_active_shell(monkeypatch):
    drive = CaseFolder(
        source="drive",
        path="法扶案件/Lumi/游秀鈴-1140715-A-024-刑事一審辯護-傷害致死等",
        relative_path="法扶案件/Lumi/游秀鈴-1140715-A-024-刑事一審辯護-傷害致死等",
        name="游秀鈴-1140715-A-024-刑事一審辯護-傷害致死等",
        category="法扶案件",
        status="active",
        owner_bucket="Lumi",
        meta=extract_case_meta("游秀鈴-1140715-A-024-刑事一審辯護-傷害致死等"),
    )
    stale_active_shell = CaseFolder(
        source="nas",
        path="/cases/法扶案件/刑事/2025-0002-游秀鈴-一審-傷害致死",
        relative_path="法扶案件/刑事/2025-0002-游秀鈴-一審-傷害致死",
        name="2025-0002-游秀鈴-一審-傷害致死",
        category="法扶案件",
        status="active",
        case_kind="刑事",
        meta=CaseMeta(case_number="2025-0002", client_hint="游秀鈴", reason_hint="傷害致死"),
    )
    monkeypatch.setattr(
        "api.osc.drive_case_sync.lookup_db_case_contexts",
        lambda nums: {
            "2025-0002": {
                "status": "已結案",
                "legal_aid_status": "已結案，待送出",
                "manual_status_lock": 1,
                "folder_path": r"Y:\lumi\03_工作資料\10_結案\法扶案件\刑事\2025-0002-游秀鈴-一審-傷害致死",
                "opponents": [],
            }
        },
    )
    result = compare_case_folders([drive], [stale_active_shell])
    assert not result["matched"]
    assert not result["drive_only"]
    assert result["out_of_scope"][0]["drive"].relative_path == drive.relative_path
    assert "已結案" in result["out_of_scope"][0]["reason"]


def test_db_closed_active_shell_is_not_created_as_drive_missing(monkeypatch):
    stale_active_shell = CaseFolder(
        source="nas",
        path="/cases/法扶案件/刑事/2025-0002-游秀鈴-一審-傷害致死",
        relative_path="法扶案件/刑事/2025-0002-游秀鈴-一審-傷害致死",
        name="2025-0002-游秀鈴-一審-傷害致死",
        category="法扶案件",
        status="active",
        case_kind="刑事",
        meta=CaseMeta(case_number="2025-0002", client_hint="游秀鈴", reason_hint="傷害致死"),
    )
    monkeypatch.setattr(
        "api.osc.drive_case_sync.lookup_db_case_contexts",
        lambda nums: {
            "2025-0002": {
                "status": "已結案",
                "legal_aid_status": "已結案，待送出",
                "manual_status_lock": 1,
                "folder_path": r"Y:\lumi\03_工作資料\10_結案\法扶案件\刑事\2025-0002-游秀鈴-一審-傷害致死",
                "opponents": [],
            }
        },
    )
    result = compare_case_folders([], [stale_active_shell])
    assert not result["local_only"]
    assert result["out_of_scope"][0]["local"].relative_path == stale_active_shell.relative_path


def test_context_terms_trim_case_suffixes():
    terms = meaningful_terms(["測試乙行政訴訟案", "測試丙等人案", "115年度訴字第000001號"])
    assert "測試乙" in terms
    assert "測試丙" in terms
    assert "115年度訴字第1號" in terms


def test_document_type_terms_are_not_decisive():
    assert is_decisive_context_term("測試丁")
    assert not is_decisive_context_term("訴願駁回決定書")
    assert not is_decisive_context_term("已用印")


def test_context_scoring_does_not_force_mismatch():
    drive = CaseFolder(
        source="drive",
        path="一般案件/Lumi/測試基金會",
        relative_path="一般案件/Lumi/測試基金會",
        name="測試基金會",
        category="一般案件",
        status="active",
        owner_bucket="Lumi",
        drive_id="drive-id",
        meta=extract_case_meta("測試基金會"),
    )
    c1 = CaseFolder(
        source="nas",
        path="/cases/一般案件/行政/2026-0003-測試基金會-一審-行政爭議",
        local_path="/cases/一般案件/行政/2026-0003-測試基金會-一審-行政爭議",
        relative_path="一般案件/行政/2026-0003-測試基金會-一審-行政爭議",
        name="2026-0003-測試基金會-一審-行政爭議",
        category="一般案件",
        status="active",
        case_kind="行政",
        meta=CaseMeta(case_number="2026-0003", client_hint="測試基金會", reason_hint="行政爭議"),
    )
    c2 = CaseFolder(
        source="nas",
        path="/cases/一般案件/行政/2026-0004-測試基金會-一審-行政爭議",
        local_path="/cases/一般案件/行政/2026-0004-測試基金會-一審-行政爭議",
        relative_path="一般案件/行政/2026-0004-測試基金會-一審-行政爭議",
        name="2026-0004-測試基金會-一審-行政爭議",
        category="一般案件",
        status="active",
        case_kind="行政",
        meta=CaseMeta(case_number="2026-0004", client_hint="測試基金會", reason_hint="行政爭議"),
    )
    scores = score_context_candidates(
        drive,
        [c1, c2],
        drive_entries=[],
        db_context_by_case={
            "2026-0003": {"notes": "測試甲行政訴訟案", "opponents": []},
            "2026-0004": {"notes": "測試乙行政訴訟案", "opponents": []},
        },
    )
    assert [s.score for s in scores] == [0, 0]


def test_aaron_ambiguous_moves_out_of_scope():
    drive = CaseFolder(
        source="drive",
        path="一般案件/Aaron/測試甲(行政爭議)",
        relative_path="一般案件/Aaron/測試甲(行政爭議)",
        name="測試甲(行政爭議)",
        category="一般案件",
        status="active",
        owner_bucket="Aaron",
        meta=extract_case_meta("測試甲(行政爭議)"),
    )
    c1 = CaseFolder(
        source="nas",
        path="/cases/一般案件/行政/2026-0003-測試甲-一審-行政爭議",
        relative_path="一般案件/行政/2026-0003-測試甲-一審-行政爭議",
        name="2026-0003-測試甲-一審-行政爭議",
        category="一般案件",
        status="active",
        case_kind="行政",
        meta=CaseMeta(case_number="2026-0003", client_hint="測試甲", reason_hint="行政爭議"),
    )
    c2 = CaseFolder(
        source="nas",
        path="/cases/一般案件/行政/2026-0004-測試甲-一審-行政爭議",
        relative_path="一般案件/行政/2026-0004-測試甲-一審-行政爭議",
        name="2026-0004-測試甲-一審-行政爭議",
        category="一般案件",
        status="active",
        case_kind="行政",
        meta=CaseMeta(case_number="2026-0004", client_hint="測試甲", reason_hint="行政爭議"),
    )
    comparison = compare_case_folders([drive], [c1, c2])
    resolved = resolve_ambiguous_cases_with_context(comparison, drive_service=None)
    assert not resolved["ambiguous"]
    assert resolved["out_of_scope"][0]["drive"].relative_path == drive.relative_path


def test_court_case_number_normalization_and_keys():
    assert normalize_court_case_no("115年度訴字第000001號") == "115年度訴字第1號"
    meta = CaseMeta(court_case_no="115年度訴字第000001號", client_hint="測試甲")
    keys = match_keys(meta)
    assert "court:115年度訴字第1號" in keys
    assert "name:測試甲" in keys
    assert "name:測試乙" in match_keys(CaseMeta(client_hint="測試乙案"))


def test_default_active_root_uses_explicit_env(tmp_path, monkeypatch):
    root = tmp_path / "01_案件"
    root.mkdir()
    monkeypatch.setenv("MAGI_DRIVE_SYNC_ACTIVE_CASE_ROOT", str(root))
    assert default_active_case_roots() == [root]


def test_consultation_without_osc_number_is_out_of_scope():
    drive = CaseFolder(
        source="drive",
        path="諮詢案件/線上諮詢案件/測試諮詢",
        relative_path="諮詢案件/線上諮詢案件/測試諮詢",
        name="測試諮詢",
        category="諮詢案件",
        status="active",
        case_kind="線上諮詢案件",
        meta=extract_case_meta("測試諮詢"),
    )
    result = compare_case_folders([drive], [])
    assert not result["drive_only"]
    assert result["out_of_scope"][0]["drive"].relative_path == drive.relative_path
    assert "沒有 OSC 案號" in sync_scope_exclusion_reason(drive)


def test_county_mediation_with_osc_number_can_match():
    drive = CaseFolder(
        source="drive",
        path="縣府調解案件/花蓮縣政府/2026-0099-測試甲-調解-損害賠償",
        relative_path="縣府調解案件/花蓮縣政府/2026-0099-測試甲-調解-損害賠償",
        name="2026-0099-測試甲-調解-損害賠償",
        category="縣府調解案件",
        status="active",
        case_kind="花蓮縣政府",
        meta=extract_case_meta("2026-0099-測試甲-調解-損害賠償"),
    )
    local = CaseFolder(
        source="nas",
        path="/cases/一般案件/民事/2026-0099-測試甲-調解-損害賠償",
        relative_path="一般案件/民事/2026-0099-測試甲-調解-損害賠償",
        name="2026-0099-測試甲-調解-損害賠償",
        category="一般案件",
        status="active",
        case_kind="民事",
        meta=extract_case_meta("2026-0099-測試甲-調解-損害賠償"),
    )
    result = compare_case_folders([drive], [local])
    assert len(result["matched"]) == 1
    assert not result["out_of_scope"]


def test_export_relative_path_adds_google_doc_extension():
    entry = FileEntry(
        source="drive",
        path="文件",
        relative_path="書狀/文件",
        name="文件",
        is_folder=False,
        mime_type="application/vnd.google-apps.document",
    )
    assert export_relative_path(entry) == "書狀/文件.docx"


def test_drive_nas_relative_path_mapping_preserves_each_side_layout():
    assert drive_to_nas_relative_path("法院判決/a.pdf") == "10_判決書/a.pdf"
    assert drive_to_nas_relative_path("法院通知/a.pdf") == "09_法院通知或程序裁定/a.pdf"
    assert drive_to_nas_relative_path("結案酬金領款單/a.pdf") == "03_結案資料/a.pdf"
    assert drive_to_nas_relative_path("閱卷資料/筆錄/a.pdf") == "08_筆錄/a.pdf"
    assert nas_to_drive_relative_path("10_判決書/a.pdf") == "法院判決/a.pdf"
    assert nas_to_drive_relative_path("09_法院通知或程序裁定/a.pdf") == "法院通知/a.pdf"
    assert nas_to_drive_relative_path("08_筆錄/a.pdf") == "閱卷資料/筆錄/a.pdf"
    assert (
        nas_to_drive_relative_path("03_結案資料/結案酬金領款單_foo.pdf")
        == "結案酬金領款單/結案酬金領款單_foo.pdf"
    )
    assert semantic_relative_path("法院判決/a.pdf") == semantic_relative_path("10_判決書/a.pdf")
    assert (
        drive_to_nas_relative_path("游秀鈴-1140715-A-024-刑事一審辯護-傷害致死等/上訴理由一狀.pdf")
        == "04_我方歷次書狀/上訴理由一狀.pdf"
    )
    assert (
        drive_to_nas_relative_path("李明志-1131106-I-007-消費者債務清理事件/更生方案.pdf")
        == "04_我方歷次書狀/更生方案.pdf"
    )


def test_safe_child_path_rejects_parent_escape(tmp_path):
    assert safe_child_path(tmp_path, "安全/檔案.pdf").is_relative_to(tmp_path)
    try:
        safe_child_path(tmp_path, "../逃逸.pdf")
    except Exception as exc:
        assert "不安全" in str(exc)
    else:
        raise AssertionError("parent escape should be rejected")


def test_drive_only_can_resolve_by_db_notes(monkeypatch):
    drive = CaseFolder(
        source="drive",
        path="一般案件/Lumi/測試外號",
        relative_path="一般案件/Lumi/測試外號",
        name="測試外號",
        category="一般案件",
        status="active",
        meta=extract_case_meta("測試外號"),
    )
    local = CaseFolder(
        source="nas",
        path="/cases/一般案件/行政/2026-0111-測試法人-一審-行政爭議",
        relative_path="一般案件/行政/2026-0111-測試法人-一審-行政爭議",
        name="2026-0111-測試法人-一審-行政爭議",
        category="一般案件",
        status="active",
        case_kind="行政",
        meta=CaseMeta(case_number="2026-0111", client_hint="測試法人", reason_hint="行政爭議"),
    )
    monkeypatch.setattr(
        "api.osc.drive_case_sync.lookup_db_case_contexts",
        lambda nums: {"2026-0111": {"notes": "測試外號案", "opponents": []}},
    )
    comparison = {"matched": [], "drive_only": [drive], "local_only": [local], "ambiguous": [], "out_of_scope": []}
    resolved = resolve_drive_only_cases_with_context(comparison)
    assert len(resolved["matched"]) == 1
    assert resolved["matched"][0]["local"].meta.case_number == "2026-0111"
    assert not resolved["drive_only"]


def test_runtime_aliases_expand_drive_context(monkeypatch):
    monkeypatch.setenv("MAGI_DRIVE_SYNC_CASE_ALIASES_JSON", '{"測試代稱": ["測試本名"]}')
    load_case_aliases.cache_clear()
    drive = CaseFolder(
        source="drive",
        path="一般案件/Lumi/測試代稱",
        relative_path="一般案件/Lumi/測試代稱",
        name="測試代稱",
        category="一般案件",
        status="active",
        meta=extract_case_meta("測試代稱"),
    )
    local = CaseFolder(
        source="nas",
        path="/cases/一般案件/刑事/2026-0222-測試本名-偵查-傷害",
        relative_path="一般案件/刑事/2026-0222-測試本名-偵查-傷害",
        name="2026-0222-測試本名-偵查-傷害",
        category="一般案件",
        status="active",
        case_kind="刑事",
        meta=CaseMeta(case_number="2026-0222", client_hint="測試本名", reason_hint="傷害"),
    )
    comparison = {"matched": [], "drive_only": [drive], "local_only": [local], "ambiguous": [], "out_of_scope": []}
    resolved = resolve_drive_only_cases_with_context(comparison)
    assert len(resolved["matched"]) == 1
    load_case_aliases.cache_clear()


def test_runtime_exclusions_remove_drive_case_from_sync_scope(monkeypatch):
    monkeypatch.setenv(
        "MAGI_DRIVE_SYNC_CASE_EXCLUSIONS_JSON",
        '{"relative_paths": ["一般案件/Lumi/測試排除案"]}',
    )
    load_case_exclusions.cache_clear()
    drive = CaseFolder(
        source="drive",
        path="一般案件/Lumi/測試排除案",
        relative_path="一般案件/Lumi/測試排除案",
        name="測試排除案",
        category="一般案件",
        status="active",
        meta=extract_case_meta("測試排除案"),
    )
    result = compare_case_folders([drive], [])
    assert not result["drive_only"]
    assert result["out_of_scope"][0]["drive"].relative_path == drive.relative_path
    assert "不納入 Drive/NAS 案件同步" in sync_scope_exclusion_reason(drive)
    load_case_exclusions.cache_clear()


def test_file_sync_plan_reports_both_sides_missing_and_content_conflict(monkeypatch):
    monkeypatch.setenv("MAGI_DRIVE_SYNC_COMPARE_MD5", "1")
    drive = CaseFolder(
        source="drive",
        path="一般案件/Lumi/2026-0333-測試甲-一審-損害賠償",
        relative_path="一般案件/Lumi/2026-0333-測試甲-一審-損害賠償",
        name="2026-0333-測試甲-一審-損害賠償",
        category="一般案件",
        status="active",
        drive_id="drive-case",
        meta=extract_case_meta("2026-0333-測試甲-一審-損害賠償"),
    )
    local = CaseFolder(
        source="nas",
        path="/cases/一般案件/民事/2026-0333-測試甲-一審-損害賠償",
        local_path="/cases/一般案件/民事/2026-0333-測試甲-一審-損害賠償",
        relative_path="一般案件/民事/2026-0333-測試甲-一審-損害賠償",
        name="2026-0333-測試甲-一審-損害賠償",
        category="一般案件",
        status="active",
        case_kind="民事",
        meta=extract_case_meta("2026-0333-測試甲-一審-損害賠償"),
    )
    drive_entries = [
        FileEntry(
            source="drive",
            path="雲端缺NAS.pdf",
            relative_path="雲端缺NAS.pdf",
            name="雲端缺NAS.pdf",
            is_folder=False,
            size=10,
            drive_id="drive-missing",
        ),
        FileEntry(
            source="drive",
            path="同路徑不同.pdf",
            relative_path="同路徑不同.pdf",
            name="同路徑不同.pdf",
            is_folder=False,
            size=12,
            md5="drive-md5",
            drive_id="drive-conflict",
        ),
    ]
    local_entries = [
        FileEntry(
            source="nas",
            path="/cases/一般案件/民事/2026-0333-測試甲-一審-損害賠償/同路徑不同.pdf",
            relative_path="同路徑不同.pdf",
            name="同路徑不同.pdf",
            is_folder=False,
            size=12,
        ),
        FileEntry(
            source="nas",
            path="/cases/一般案件/民事/2026-0333-測試甲-一審-損害賠償/NAS缺雲端.pdf",
            relative_path="NAS缺雲端.pdf",
            name="NAS缺雲端.pdf",
            is_folder=False,
            size=13,
        ),
    ]
    monkeypatch.setattr(
        "api.osc.drive_case_sync.drive_descendant_context",
        lambda *args, **kwargs: drive_entries,
    )
    monkeypatch.setattr(
        "api.osc.drive_case_sync.local_descendant_context",
        lambda *args, **kwargs: local_entries,
    )
    monkeypatch.setattr("api.osc.drive_case_sync.local_file_md5", lambda path: "local-md5")
    plan = build_file_sync_plan({"matched": [{"drive": drive, "local": local}]}, drive_service=object())
    summary = plan["summary"]
    assert summary["drive_missing_in_nas_files"] == 1
    assert summary["nas_missing_in_drive_files"] == 1
    assert summary["conflict_files"] == 1
    assert summary["content_mismatch_files"] == 1
    case = plan["cases"][0]
    assert case["download_missing"][0]["target_relative_path"] == "雲端缺NAS.pdf"
    assert case["nas_only"][0]["relative_path"] == "NAS缺雲端.pdf"
    assert case["conflicts"][0]["reason"] == "same_relative_path_md5_differs"


def test_file_sync_plan_skips_drive_shortcuts(monkeypatch):
    drive = CaseFolder(
        source="drive",
        path="一般案件/Lumi/張國賢(確認決議無效)",
        relative_path="一般案件/Lumi/張國賢(確認決議無效)",
        name="張國賢(確認決議無效)",
        meta=CaseMeta(case_number="2025-0122"),
        drive_id="drive-case",
    )
    local = CaseFolder(
        source="nas",
        path="/cases/一般案件/民事/2025-0122-張國賢-一審-確認決議無效",
        local_path="/cases/一般案件/民事/2025-0122-張國賢-一審-確認決議無效",
        relative_path="一般案件/民事/2025-0122-張國賢-一審-確認決議無效",
        name="2025-0122-張國賢-一審-確認決議無效",
        meta=CaseMeta(case_number="2025-0122"),
    )
    monkeypatch.setattr(
        "api.osc.drive_case_sync.drive_descendant_context",
        lambda *_args, **_kwargs: [
            FileEntry(
                "drive",
                "張國賢案件",
                "張國賢案件",
                "張國賢案件",
                False,
                drive_id="shortcut",
                mime_type="application/vnd.google-apps.shortcut",
            )
        ],
    )
    monkeypatch.setattr("api.osc.drive_case_sync.local_descendant_context", lambda *_args, **_kwargs: [])

    plan = build_file_sync_plan({"matched": [{"drive": drive, "local": local}]}, drive_service=object())

    assert plan["summary"]["drive_missing_in_nas_files"] == 0
    assert plan["cases"][0]["download_missing"] == []


def test_build_file_sync_plan_compares_drive_and_nas_semantic_paths(monkeypatch):
    drive = CaseFolder(
        source="drive",
        path="一般案件/Lumi/2026-0333-測試甲-一審-損害賠償",
        relative_path="一般案件/Lumi/2026-0333-測試甲-一審-損害賠償",
        name="2026-0333-測試甲-一審-損害賠償",
        meta=CaseMeta(case_number="2026-0333"),
        drive_id="drive-case",
    )
    local = CaseFolder(
        source="nas",
        path="/cases/一般案件/民事/2026-0333-測試甲-一審-損害賠償",
        local_path="/cases/一般案件/民事/2026-0333-測試甲-一審-損害賠償",
        relative_path="一般案件/民事/2026-0333-測試甲-一審-損害賠償",
        name="2026-0333-測試甲-一審-損害賠償",
        meta=CaseMeta(case_number="2026-0333"),
    )
    drive_entries = [
        FileEntry(
            source="drive",
            path="法院判決/a.pdf",
            relative_path="法院判決/a.pdf",
            name="a.pdf",
            is_folder=False,
            size=5,
            md5="same-md5",
            drive_id="drive-a",
        ),
        FileEntry(
            source="drive",
            path="結案酬金領款單/結案酬金領款單_foo.pdf",
            relative_path="結案酬金領款單/結案酬金領款單_foo.pdf",
            name="結案酬金領款單_foo.pdf",
            is_folder=False,
            size=5,
            md5="same-md5",
            drive_id="drive-fee",
        ),
    ]
    local_entries = [
        FileEntry(
            source="nas",
            path="/cases/一般案件/民事/2026-0333-測試甲-一審-損害賠償/10_判決書/a.pdf",
            relative_path="10_判決書/a.pdf",
            name="a.pdf",
            is_folder=False,
            size=5,
        ),
        FileEntry(
            source="nas",
            path="/cases/一般案件/民事/2026-0333-測試甲-一審-損害賠償/03_結案資料/結案酬金領款單_foo.pdf",
            relative_path="03_結案資料/結案酬金領款單_foo.pdf",
            name="結案酬金領款單_foo.pdf",
            is_folder=False,
            size=5,
        ),
    ]
    monkeypatch.setattr(
        "api.osc.drive_case_sync.drive_descendant_context",
        lambda *args, **kwargs: drive_entries,
    )
    monkeypatch.setattr(
        "api.osc.drive_case_sync.local_descendant_context",
        lambda *args, **kwargs: local_entries,
    )
    monkeypatch.setattr("api.osc.drive_case_sync.local_file_md5", lambda path: "same-md5")
    plan = build_file_sync_plan({"matched": [{"drive": drive, "local": local}]}, drive_service=object())
    case = plan["cases"][0]
    assert case["download_missing"] == []
    assert case["nas_only"] == []
    assert case["conflicts"] == []
    assert case["skipped_existing"] == 2


def test_build_file_sync_plan_prioritizes_upcoming_todo_cases(monkeypatch):
    def make_pair(case_number: str):
        drive = CaseFolder(
            source="drive",
            path=f"一般案件/Lumi/{case_number}-測試-一審-損害賠償",
            relative_path=f"一般案件/Lumi/{case_number}-測試-一審-損害賠償",
            name=f"{case_number}-測試-一審-損害賠償",
            meta=CaseMeta(case_number=case_number),
            drive_id=f"drive-{case_number}",
        )
        local = CaseFolder(
            source="nas",
            path=f"/cases/一般案件/民事/{case_number}-測試-一審-損害賠償",
            local_path=f"/cases/一般案件/民事/{case_number}-測試-一審-損害賠償",
            relative_path=f"一般案件/民事/{case_number}-測試-一審-損害賠償",
            name=f"{case_number}-測試-一審-損害賠償",
            meta=CaseMeta(case_number=case_number),
        )
        return {"drive": drive, "local": local}

    monkeypatch.setattr("api.osc.drive_case_sync.drive_descendant_context", lambda *args, **kwargs: [])
    monkeypatch.setattr("api.osc.drive_case_sync.local_descendant_context", lambda *args, **kwargs: [])

    plan = build_file_sync_plan(
        {
            "matched": [
                make_pair("2026-0001"),
                make_pair("2026-0002"),
                make_pair("2026-0003"),
            ]
        },
        drive_service=object(),
        matched_case_limit=2,
        matched_case_offset=1,
        priority_case_numbers={"2026-0003"},
    )

    assert [case["case_number"] for case in plan["cases"]] == ["2026-0003", "2026-0002"]


def test_drive_import_aliases_map_to_nas_canonical_folders():
    assert drive_to_nas_relative_path("法院裁判/a.pdf") == "09_法院通知或程序裁定/a.pdf"
    assert drive_to_nas_relative_path("法院裁判/20260101 裁定.pdf") == "09_法院通知或程序裁定/20260101 裁定.pdf"
    assert drive_to_nas_relative_path("法院裁判/20260101 復權裁定.pdf") == "10_判決書/20260101 復權裁定.pdf"
    assert drive_to_nas_relative_path("法院裁判/偵查案件起訴書.pdf") == "10_判決書/偵查案件起訴書.pdf"
    assert drive_to_nas_relative_path("法院裁定/20260101 普通裁定.pdf") == "09_法院通知或程序裁定/20260101 普通裁定.pdf"
    assert drive_to_nas_relative_path("法院裁定/20260101 復權裁定.pdf") == "10_判決書/20260101 復權裁定.pdf"
    assert drive_to_nas_relative_path("法院資料/法院裁判/a.pdf") == "09_法院通知或程序裁定/a.pdf"
    assert drive_to_nas_relative_path("法院資料/法院裁判/20260101 開庭通知.pdf") == "09_法院通知或程序裁定/20260101 開庭通知.pdf"
    assert drive_to_nas_relative_path("起訴書/20250306_聲請接續羈押理由書.pdf") == "09_法院通知或程序裁定/20250306_聲請接續羈押理由書.pdf"
    assert drive_to_nas_relative_path("起訴書/20250306_起訴書.pdf") == "10_判決書/20250306_起訴書.pdf"
    assert drive_to_nas_relative_path("法院資料/起訴書/a.pdf") == "10_判決書/a.pdf"
    assert drive_to_nas_relative_path("開庭通知/a.pdf") == "09_法院通知或程序裁定/a.pdf"
    assert drive_to_nas_relative_path("法庭通知/a.pdf") == "09_法院通知或程序裁定/a.pdf"
    assert drive_to_nas_relative_path("傳票/a.pdf") == "09_法院通知或程序裁定/a.pdf"
    assert drive_to_nas_relative_path("地檢署通知/a.pdf") == "09_法院通知或程序裁定/a.pdf"
    assert drive_to_nas_relative_path("地檢署起訴書/a.pdf") == "10_判決書/a.pdf"
    assert drive_to_nas_relative_path("電子筆錄/b.pdf") == "08_筆錄/b.pdf"
    assert drive_to_nas_relative_path("調解筆錄/b.pdf") == "08_筆錄/b.pdf"
    assert drive_to_nas_relative_path("書狀資料/c.pdf") == "04_我方歷次書狀/c.pdf"
    assert drive_to_nas_relative_path("訊問筆錄/b.pdf") == "08_筆錄/b.pdf"
    assert drive_to_nas_relative_path("信件/c.pdf") == "12_信件往返/c.pdf"
    assert drive_to_nas_relative_path("自行收納款項收據/d.pdf") == "11_回執/d.pdf"


def test_semantic_paths_treat_legacy_drive_folders_as_same_category():
    assert semantic_relative_path("法院裁判/a.pdf") == "法院通知/a.pdf"
    assert semantic_relative_path("法院裁判/20260101 開庭通知.pdf") == "法院通知/20260101 開庭通知.pdf"
    assert semantic_relative_path("開庭通知/a.pdf") == "法院通知/a.pdf"
    assert semantic_relative_path("法院裁定/20260101 復權裁定.pdf") == "法院判決/20260101 復權裁定.pdf"
    assert semantic_relative_path("10_判決書/a.pdf") == "法院判決/a.pdf"
    assert semantic_relative_path("起訴書/a.pdf") == "法院判決/a.pdf"
    assert semantic_relative_path("起訴書/20250306_聲請接續羈押理由書.pdf") == "法院通知/20250306_聲請接續羈押理由書.pdf"
    assert semantic_relative_path("電子筆錄/b.pdf") == "筆錄/b.pdf"
    assert semantic_relative_path("訊問筆錄/b.pdf") == "筆錄/b.pdf"
    assert semantic_relative_path("08_筆錄/b.pdf") == "筆錄/b.pdf"
    assert semantic_relative_path("信件/c.pdf") == "信件往返/c.pdf"
    assert semantic_relative_path("12_信件往返/c.pdf") == "信件往返/c.pdf"


def test_file_sync_plan_does_not_hash_existing_nas_files_by_default(monkeypatch):
    drive = CaseFolder(
        source="drive",
        path="法扶案件/Lumi/測試",
        relative_path="法扶案件/Lumi/測試",
        name="測試",
        meta=CaseMeta(case_number="2026-0001"),
        drive_id="drive-case",
    )
    local = CaseFolder(
        source="nas",
        path="/cases/法扶案件/刑事/2026-0001-測試-一審-詐欺",
        local_path="/cases/法扶案件/刑事/2026-0001-測試-一審-詐欺",
        relative_path="法扶案件/刑事/2026-0001-測試-一審-詐欺",
        name="2026-0001-測試-一審-詐欺",
        meta=CaseMeta(case_number="2026-0001"),
    )
    monkeypatch.delenv("MAGI_DRIVE_SYNC_COMPARE_MD5", raising=False)
    monkeypatch.setattr(
        "api.osc.drive_case_sync.drive_descendant_context",
        lambda *_args, **_kwargs: [
            FileEntry("drive", "法院通知/a.pdf", "法院通知/a.pdf", "a.pdf", False, size=12, md5="drive-md5")
        ],
    )
    monkeypatch.setattr(
        "api.osc.drive_case_sync.local_descendant_context",
        lambda *_args, **_kwargs: [
            FileEntry("nas", "/cases/09_法院通知或程序裁定/a.pdf", "09_法院通知或程序裁定/a.pdf", "a.pdf", False, size=12)
        ],
    )
    monkeypatch.setattr("api.osc.drive_case_sync.local_file_md5", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not hash NAS files")))

    plan = build_file_sync_plan(
        {"matched": [{"drive": drive, "local": local}]},
        drive_service=object(),
        matched_case_limit=1,
    )

    assert plan["summary"]["skipped_existing_files"] == 1
    assert plan["summary"]["conflict_files"] == 0


def test_drive_to_nas_long_filename_is_shortened_and_not_reuploaded(monkeypatch):
    drive = CaseFolder(
        source="drive",
        path="法扶案件/Lumi/測試",
        relative_path="法扶案件/Lumi/測試",
        name="測試",
        meta=CaseMeta(case_number="2026-0001"),
        drive_id="drive-case",
    )
    local = CaseFolder(
        source="nas",
        path="/cases/法扶案件/民事/2026-0001-測試-一審-清算",
        local_path="/cases/法扶案件/民事/2026-0001-測試-一審-清算",
        relative_path="法扶案件/民事/2026-0001-測試-一審-清算",
        name="2026-0001-測試-一審-清算",
        meta=CaseMeta(case_number="2026-0001"),
    )
    long_name = "20251015 新北地方法院民事執行處函（測試；" + "請於文到七日內提出資料" * 18 + "）.pdf"
    raw_target = f"09_法院通知或程序裁定/{long_name}"
    safe_target = nas_filesystem_relative_path(raw_target)
    assert safe_target != raw_target
    assert len(safe_target.rsplit("/", 1)[-1].encode("utf-8")) <= 220

    drive_entry = FileEntry("drive", f"法院通知/{long_name}", f"法院通知/{long_name}", long_name, False, size=12, drive_id="drive-file")
    monkeypatch.setattr("api.osc.drive_case_sync.drive_descendant_context", lambda *_args, **_kwargs: [drive_entry])
    monkeypatch.setattr("api.osc.drive_case_sync.local_descendant_context", lambda *_args, **_kwargs: [])

    plan = build_file_sync_plan({"matched": [{"drive": drive, "local": local}]}, drive_service=object(), matched_case_limit=1)
    action = plan["cases"][0]["download_missing"][0]
    assert action["target_relative_path"] == safe_target
    assert action["filename_shortened_for_nas"] is True

    safe_name = safe_target.rsplit("/", 1)[-1]
    monkeypatch.setattr(
        "api.osc.drive_case_sync.local_descendant_context",
        lambda *_args, **_kwargs: [FileEntry("nas", f"/cases/{safe_target}", safe_target, safe_name, False, size=12)],
    )
    plan2 = build_file_sync_plan({"matched": [{"drive": drive, "local": local}]}, drive_service=object(), matched_case_limit=1)
    assert plan2["cases"][0]["download_missing"] == []
    assert plan2["cases"][0]["nas_only"] == []


def test_drive_download_plan_skips_unmapped_drive_folder_instead_of_copying_raw_folder(monkeypatch):
    drive = CaseFolder(
        source="drive",
        path="法扶案件/Lumi/測試",
        relative_path="法扶案件/Lumi/測試",
        name="測試",
        meta=CaseMeta(case_number="2026-0001"),
        drive_id="drive-case",
    )
    local = CaseFolder(
        source="nas",
        path="/cases/法扶案件/刑事/2026-0001-測試-一審-詐欺",
        local_path="/cases/法扶案件/刑事/2026-0001-測試-一審-詐欺",
        relative_path="法扶案件/刑事/2026-0001-測試-一審-詐欺",
        name="2026-0001-測試-一審-詐欺",
        meta=CaseMeta(case_number="2026-0001"),
    )
    monkeypatch.setattr(
        "api.osc.drive_case_sync.drive_descendant_context",
        lambda *_args, **_kwargs: [
            FileEntry("drive", "未知雲端資料夾/a.pdf", "未知雲端資料夾/a.pdf", "a.pdf", False, size=12)
        ],
    )
    monkeypatch.setattr("api.osc.drive_case_sync.local_descendant_context", lambda *_args, **_kwargs: [])

    assert drive_to_nas_download_skip_reason("未知雲端資料夾/a.pdf", "未知雲端資料夾/a.pdf") == "unmapped_drive_folder:未知雲端資料夾"
    plan = build_file_sync_plan({"matched": [{"drive": drive, "local": local}]}, drive_service=object(), matched_case_limit=1)
    assert plan["cases"][0]["download_missing"] == []
    assert plan["cases"][0]["download_skipped"][0]["reason"] == "unmapped_drive_folder:未知雲端資料夾"
    assert plan["summary"]["skipped_unmapped_drive_downloads"] == 1
    assert plan["summary"]["drive_missing_in_nas_files"] == 0


def test_drive_download_plan_skips_same_content_existing_in_different_nas_folder(monkeypatch, tmp_path):
    drive = CaseFolder(
        source="drive",
        path="法扶案件/Lumi/李明志-1131106-I-007-消費者債務清理事件",
        relative_path="法扶案件/Lumi/李明志-1131106-I-007-消費者債務清理事件",
        name="李明志-1131106-I-007-消費者債務清理事件",
        meta=CaseMeta(case_number="2025-0058"),
        drive_id="drive-case",
    )
    case_dir = tmp_path / "法扶案件" / "消費者債務清理" / "2025-0058-李明志-消費者債務清理-更生"
    local_file = case_dir / "04_我方歷次書狀" / "更生方案.pdf"
    local_file.parent.mkdir(parents=True)
    local_file.write_bytes(b"same-plan")
    digest = __import__("hashlib").md5(b"same-plan").hexdigest()
    local = CaseFolder(
        source="nas",
        path=str(case_dir),
        local_path=str(case_dir),
        relative_path="法扶案件/消費者債務清理/2025-0058-李明志-消費者債務清理-更生",
        name="2025-0058-李明志-消費者債務清理-更生",
        meta=CaseMeta(case_number="2025-0058"),
    )
    monkeypatch.setattr(
        "api.osc.drive_case_sync.drive_descendant_context",
        lambda *_args, **_kwargs: [
            FileEntry(
                "drive",
                "提供資料/更生方案.pdf",
                "提供資料/更生方案.pdf",
                "更生方案.pdf",
                False,
                size=len(b"same-plan"),
                md5=digest,
                drive_id="drive-file",
            )
        ],
    )
    monkeypatch.setattr(
        "api.osc.drive_case_sync.local_descendant_context",
        lambda *_args, **_kwargs: [
            FileEntry(
                "nas",
                str(local_file),
                "04_我方歷次書狀/更生方案.pdf",
                "更生方案.pdf",
                False,
                size=len(b"same-plan"),
            )
        ],
    )

    plan = build_file_sync_plan({"matched": [{"drive": drive, "local": local}]}, drive_service=object(), matched_case_limit=1)
    case = plan["cases"][0]
    assert case["download_missing"] == []
    assert case["download_skipped"][0]["reason"] == "same_content_elsewhere"
    assert plan["summary"]["skipped_duplicate_content_downloads"] == 1
    assert plan["summary"]["drive_missing_in_nas_files"] == 0


def test_drive_download_plan_skips_large_same_name_size_without_hashing(monkeypatch):
    drive = CaseFolder(
        source="drive",
        path="法扶案件/Lumi/李明志-1131106-I-007-消費者債務清理事件",
        relative_path="法扶案件/Lumi/李明志-1131106-I-007-消費者債務清理事件",
        name="李明志-1131106-I-007-消費者債務清理事件",
        meta=CaseMeta(case_number="2025-0058"),
        drive_id="drive-case",
    )
    local = CaseFolder(
        source="nas",
        path="/cases/法扶案件/消費者債務清理/2025-0058-李明志-消費者債務清理-更生",
        local_path="/cases/法扶案件/消費者債務清理/2025-0058-李明志-消費者債務清理-更生",
        relative_path="法扶案件/消費者債務清理/2025-0058-李明志-消費者債務清理-更生",
        name="2025-0058-李明志-消費者債務清理-更生",
        meta=CaseMeta(case_number="2025-0058"),
    )
    monkeypatch.setenv("MAGI_DRIVE_SYNC_LOCAL_HASH_MAX_BYTES", "10")
    monkeypatch.setattr(
        "api.osc.drive_case_sync.drive_descendant_context",
        lambda *_args, **_kwargs: [
            FileEntry(
                "drive",
                "提供資料/更生方案.pdf",
                "提供資料/更生方案.pdf",
                "更生方案.pdf",
                False,
                size=50,
                md5="drive-md5",
                drive_id="drive-file",
            )
        ],
    )
    monkeypatch.setattr(
        "api.osc.drive_case_sync.local_descendant_context",
        lambda *_args, **_kwargs: [
            FileEntry(
                "nas",
                "/cases/04_我方歷次書狀/更生方案.pdf",
                "04_我方歷次書狀/更生方案.pdf",
                "更生方案.pdf",
                False,
                size=50,
            )
        ],
    )
    monkeypatch.setattr(
        "api.osc.drive_case_sync.local_file_md5",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("large NAS file must not be hashed")),
    )

    plan = build_file_sync_plan({"matched": [{"drive": drive, "local": local}]}, drive_service=object(), matched_case_limit=1)

    case = plan["cases"][0]
    assert case["download_missing"] == []
    assert case["download_skipped"][0]["reason"] == "same_content_elsewhere"
    assert case["download_skipped"][0]["hash_verification"] == "skipped_large_same_name_size"
    assert plan["summary"]["skipped_duplicate_content_downloads"] == 1
    assert plan["summary"]["drive_missing_in_nas_files"] == 0


def test_drive_download_uses_short_temp_name_for_long_court_filenames(monkeypatch, tmp_path):
    """Long court filenames must not make the temporary download filename exceed OS limits."""

    class FakeDownloader:
        def __init__(self, fh, _request, chunksize=0):
            self.fh = fh
            self.done = False

        def next_chunk(self):
            if not self.done:
                self.fh.write(b"%PDF-1.4\n")
                self.done = True
            return None, True

    class FakeFiles:
        def get_media(self, **_kwargs):
            return object()

    class FakeService:
        def files(self):
            return FakeFiles()

    monkeypatch.setitem(
        sys.modules,
        "googleapiclient.http",
        types.SimpleNamespace(MediaIoBaseDownload=FakeDownloader),
    )
    long_name = "20251015_notice_" + "A" * 210 + ".pdf"
    target = tmp_path / long_name
    result = _download_drive_entry(
        FakeService(),
        FileEntry(
            source="drive",
            path=long_name,
            relative_path=long_name,
            name=long_name,
            is_folder=False,
            drive_id="drive-file",
            mime_type="application/pdf",
        ),
        target,
    )

    assert result["status"] == "downloaded"
    assert target.exists()
    assert not list(tmp_path.glob(".magi-drive-sync-*"))


def test_nas_upload_does_not_put_procedural_docs_into_indictment_folder():
    assert (
        nas_to_drive_relative_path(
            "09_法院通知或程序裁定/20260101 開庭通知.pdf",
            drive_existing_first_segments={"起訴書"},
        )
        == "法院通知/20260101 開庭通知.pdf"
    )


def test_build_file_sync_plan_uses_drive_aliases_instead_of_creating_parallel_folders(monkeypatch):
    drive = CaseFolder(
        source="drive",
        path="法扶案件/Lumi/2025-0002-游秀鈴-一審-傷害致死",
        relative_path="法扶案件/Lumi/2025-0002-游秀鈴-一審-傷害致死",
        name="2025-0002-游秀鈴-一審-傷害致死",
        meta=CaseMeta(case_number="2025-0002"),
        drive_id="drive-case",
    )
    local = CaseFolder(
        source="nas",
        path="/cases/法扶案件/刑事/2025-0002-游秀鈴-一審-傷害致死",
        local_path="/cases/法扶案件/刑事/2025-0002-游秀鈴-一審-傷害致死",
        relative_path="法扶案件/刑事/2025-0002-游秀鈴-一審-傷害致死",
        name="2025-0002-游秀鈴-一審-傷害致死",
        meta=CaseMeta(case_number="2025-0002"),
    )
    drive_entries = [
        FileEntry("drive", "法院裁判", "法院裁判", "法院裁判", True),
        FileEntry("drive", "訊問筆錄", "訊問筆錄", "訊問筆錄", True),
        FileEntry("drive", "信件", "信件", "信件", True),
    ]
    local_entries = [
        FileEntry(
            source="nas",
            path="/cases/法扶案件/刑事/2025-0002-游秀鈴-一審-傷害致死/10_判決書/new.pdf",
            relative_path="10_判決書/new.pdf",
            name="new.pdf",
            is_folder=False,
            size=5,
        ),
        FileEntry(
            source="nas",
            path="/cases/法扶案件/刑事/2025-0002-游秀鈴-一審-傷害致死/08_筆錄/t.pdf",
            relative_path="08_筆錄/t.pdf",
            name="t.pdf",
            is_folder=False,
            size=5,
        ),
        FileEntry(
            source="nas",
            path="/cases/法扶案件/刑事/2025-0002-游秀鈴-一審-傷害致死/12_信件往返/mail.pdf",
            relative_path="12_信件往返/mail.pdf",
            name="mail.pdf",
            is_folder=False,
            size=5,
        ),
    ]
    monkeypatch.setattr("api.osc.drive_case_sync.drive_descendant_context", lambda *args, **kwargs: drive_entries)
    monkeypatch.setattr("api.osc.drive_case_sync.local_descendant_context", lambda *args, **kwargs: local_entries)

    plan = build_file_sync_plan({"matched": [{"drive": drive, "local": local}]}, drive_service=object())
    targets = {item["target_relative_path"] for item in plan["cases"][0]["nas_only"]}
    assert "法院裁判/new.pdf" in targets
    assert "訊問筆錄/t.pdf" in targets
    assert "信件/mail.pdf" in targets
    assert "法院判決/new.pdf" not in targets
    assert "閱卷資料/筆錄/t.pdf" not in targets


def test_execute_uploads_uses_nas_only_files_without_overwrite(monkeypatch, tmp_path):
    src = tmp_path / "NAS缺雲端.pdf"
    src.write_bytes(b"hello")
    plan = {
        "cases": [
            {
                "case_number": "2026-0333",
                "drive_path": "一般案件/Lumi/2026-0333-測試甲-一審-損害賠償",
                "drive_id": "drive-case",
                "nas_only": [
                    {
                        "path": str(src),
                        "relative_path": "01_書狀/NAS缺雲端.pdf",
                        "size": src.stat().st_size,
                    }
                ],
            }
        ]
    }
    calls = []

    def fake_upload(service, *, local_path, drive_case_folder_id, relative_path):
        calls.append((local_path, drive_case_folder_id, relative_path))
        return {
            "status": "uploaded",
            "drive_id": "new-file",
            "web_url": "https://drive.example/file",
            "bytes": local_path.stat().st_size,
            "created_folders": ["01_書狀"],
        }

    monkeypatch.setattr("api.osc.drive_case_sync.upload_local_file_to_drive", fake_upload)
    result = execute_nas_to_drive_uploads(object(), plan, upload_limit=10, max_upload_bytes=1000)
    assert result["summary"]["attempted"] == 1
    assert result["summary"]["uploaded"] == 1
    assert result["summary"]["bytes"] == 5
    assert result["summary"]["folders_created"] == 1
    assert calls[0][1] == "drive-case"
    assert calls[0][2] == "01_書狀/NAS缺雲端.pdf"


def test_execute_uploads_uses_drive_target_relative_path(monkeypatch, tmp_path):
    src = tmp_path / "a.pdf"
    src.write_bytes(b"hello")
    plan = {
        "cases": [
            {
                "case_number": "2026-0333",
                "drive_path": "一般案件/Lumi/2026-0333-測試甲-一審-損害賠償",
                "drive_id": "drive-case",
                "nas_only": [
                    {
                        "path": str(src),
                        "relative_path": "10_判決書/a.pdf",
                        "target_relative_path": "法院判決/a.pdf",
                        "size": src.stat().st_size,
                    }
                ],
            }
        ]
    }
    calls = []

    def fake_upload(service, *, local_path, drive_case_folder_id, relative_path):
        calls.append(relative_path)
        return {"status": "uploaded", "bytes": local_path.stat().st_size, "created_folders": []}

    monkeypatch.setattr("api.osc.drive_case_sync.upload_local_file_to_drive", fake_upload)
    result = execute_nas_to_drive_uploads(object(), plan, upload_limit=10, max_upload_bytes=1000)
    assert result["summary"]["uploaded"] == 1
    assert calls == ["法院判決/a.pdf"]


def test_execute_uploads_respects_byte_limit(tmp_path):
    src = tmp_path / "big.pdf"
    src.write_bytes(b"12345")
    plan = {
        "cases": [
            {
                "case_number": "2026-0333",
                "drive_path": "一般案件/Lumi/2026-0333-測試甲-一審-損害賠償",
                "drive_id": "drive-case",
                "nas_only": [
                    {"path": str(src), "relative_path": "big.pdf", "size": src.stat().st_size}
                ],
            }
        ]
    }
    result = execute_nas_to_drive_uploads(object(), plan, max_upload_bytes=4)
    assert result["summary"]["attempted"] == 0
    assert result["summary"]["uploaded"] == 0
    assert result["summary"]["stopped_by_bytes"] is True


def test_drive_relative_path_for_local_case_preserves_drive_layout(monkeypatch):
    monkeypatch.setenv("MAGI_DRIVE_SYNC_OWNER_BUCKET", "Lumi")
    normal = CaseFolder(
        source="nas",
        path="/cases/一般案件/行政/2026-0001-測試甲-一審-訴願",
        relative_path="一般案件/行政/2026-0001-測試甲-一審-訴願",
        name="2026-0001-測試甲-一審-訴願",
        category="一般案件",
        status="active",
        case_kind="行政",
        meta=CaseMeta(case_number="2026-0001"),
    )
    assert drive_relative_path_for_local_case(normal) == "一般案件/Lumi/測試甲-一審-訴願"

    debt = CaseFolder(
        source="nas",
        path="/cases/法扶案件/消費者債務清理/2026-0002-測試乙-更生-清算",
        relative_path="法扶案件/消費者債務清理/2026-0002-測試乙-更生-清算",
        name="2026-0002-測試乙-更生-清算",
        category="法扶案件",
        status="active",
        case_kind="消費者債務清理",
        meta=CaseMeta(case_number="2026-0002"),
    )
    assert drive_relative_path_for_local_case(debt) == "法扶案件/Lumi/01.消債/測試乙-更生-清算"

    closed = CaseFolder(
        source="nas",
        path="/closed/法扶案件/刑事/2026-0003-測試丙-一審-詐欺",
        relative_path="法扶案件/刑事/2026-0003-測試丙-一審-詐欺",
        name="2026-0003-測試丙-一審-詐欺",
        category="法扶案件",
        status="closed",
        case_kind="刑事",
        meta=CaseMeta(case_number="2026-0003"),
    )
    assert drive_relative_path_for_local_case(closed) == "結案案件/法扶案件/Lumi/測試丙-一審-詐欺"

    appointed = CaseFolder(
        source="nas",
        path="/cases/指定辯護案件/刑事/2026-0004-測試丁-一審-殺人",
        relative_path="指定辯護案件/刑事/2026-0004-測試丁-一審-殺人",
        name="2026-0004-測試丁-一審-殺人",
        category="指定辯護案件",
        status="active",
        case_kind="刑事",
        meta=CaseMeta(case_number="2026-0004"),
    )
    assert drive_relative_path_for_local_case(appointed) == "指定辯護案件/測試丁-一審-殺人"


def test_drive_relative_path_for_laf_keeps_laf_number_without_osc_number(monkeypatch):
    monkeypatch.setenv("MAGI_DRIVE_SYNC_OWNER_BUCKET", "Lumi")
    criminal = CaseFolder(
        source="nas",
        path="/cases/法扶案件/刑事/2026-0052-胡裕生-偵查-竊盜",
        relative_path="法扶案件/刑事/2026-0052-胡裕生-偵查-竊盜",
        name="2026-0052-胡裕生-偵查-竊盜",
        category="法律扶助案件",
        status="active",
        case_kind="刑事",
        meta=CaseMeta(case_number="2026-0052", laf_case_no="1150521-E-011", client_hint="胡裕生"),
    )
    assert drive_relative_path_for_local_case(criminal) == "法扶案件/Lumi/胡裕生-1150521-E-011-刑事偵查中辯護-竊盜"

    debt = CaseFolder(
        source="nas",
        path="/cases/法扶案件/消費者債務清理/2026-0051-金李連芯-消費者債務清理-更生",
        relative_path="法扶案件/消費者債務清理/2026-0051-金李連芯-消費者債務清理-更生",
        name="2026-0051-金李連芯-消費者債務清理-更生",
        category="法律扶助案件",
        status="active",
        case_kind="消費者債務清理",
        meta=CaseMeta(case_number="2026-0051", laf_case_no="1150519-E-014", client_hint="金李連芯"),
    )
    assert drive_relative_path_for_local_case(debt) == "法扶案件/Lumi/01.消債/金李連芯-1150519-E-014-消費者債務清理事件-消費者債務清理事件"


def test_ensure_drive_folder_path_creates_only_missing_segments(monkeypatch):
    existing = {("root", "一般案件"): "folder-general"}
    created = []

    def fake_find(_service, parent_id, name):
        return existing.get((parent_id, name), "")

    def fake_create(_service, parent_id, name):
        new_id = f"{parent_id}/{name}"
        existing[(parent_id, name)] = new_id
        created.append((parent_id, name))
        return new_id

    monkeypatch.setattr("api.osc.drive_case_sync.find_drive_child_folder", fake_find)
    monkeypatch.setattr("api.osc.drive_case_sync.create_drive_folder", fake_create)
    result = ensure_drive_folder_path(object(), "root", "一般案件/Lumi/2026-0001-測試甲-一審-訴願")
    assert result["drive_id"] == "folder-general/Lumi/2026-0001-測試甲-一審-訴願"
    assert result["created_folders"] == [
        "一般案件/Lumi",
        "一般案件/Lumi/2026-0001-測試甲-一審-訴願",
    ]


def test_ensure_drive_case_folder_renames_legacy_osc_number_folder(monkeypatch):
    existing = {
        ("root", "法扶案件"): "laf",
        ("laf", "Lumi"): "lumi",
        ("lumi", "2026-0052-胡裕生-偵查-竊盜"): "legacy",
    }
    updates = []

    def fake_find(_service, parent_id, name):
        return existing.get((parent_id, name), "")

    def fake_find_by_case(_service, parent_id, case_number):
        return ""

    def fake_update(_service, folder_id, *, name="", app_properties=None):
        updates.append((folder_id, name, app_properties or {}))

    monkeypatch.setattr("api.osc.drive_case_sync.find_drive_child_folder", fake_find)
    monkeypatch.setattr("api.osc.drive_case_sync.find_drive_child_folder_by_osc_case_number", fake_find_by_case)
    monkeypatch.setattr("api.osc.drive_case_sync.update_drive_folder_metadata", fake_update)
    case = CaseFolder(
        source="nas",
        path="/cases/法扶案件/刑事/2026-0052-胡裕生-偵查-竊盜",
        relative_path="法扶案件/刑事/2026-0052-胡裕生-偵查-竊盜",
        name="2026-0052-胡裕生-偵查-竊盜",
        category="法律扶助案件",
        status="active",
        case_kind="刑事",
        meta=CaseMeta(case_number="2026-0052", laf_case_no="1150521-E-011", client_hint="胡裕生"),
    )
    result = ensure_drive_case_folder_for_local_case(object(), "root", case, owner_bucket="Lumi")
    assert result["status"] == "renamed_legacy_osc_number_folder"
    assert result["relative_path"] == "法扶案件/Lumi/胡裕生-1150521-E-011-刑事偵查中辯護-竊盜"
    assert updates == [
        (
            "legacy",
            "胡裕生-1150521-E-011-刑事偵查中辯護-竊盜",
            {"magi_osc_case_number": "2026-0052", "magi_source": "osc", "magi_laf_case_no": "1150521-E-011"},
        )
    ]


def test_find_existing_drive_case_folder_does_not_create(monkeypatch):
    existing = {
        ("root", "法扶案件"): "laf",
        ("laf", "Lumi"): "lumi",
        ("lumi", "01.消債"): "debt",
        ("debt", "金李連芯-1150519-E-014-消費者債務清理事件-消費者債務清理事件"): "drive-case",
    }
    created = []

    def fake_find(_service, parent_id, name):
        return existing.get((parent_id, name), "")

    def fake_create(_service, parent_id, name):
        created.append((parent_id, name))
        return "unexpected"

    monkeypatch.setattr("api.osc.drive_case_sync.find_drive_child_folder", fake_find)
    monkeypatch.setattr("api.osc.drive_case_sync.find_drive_child_folder_by_osc_case_number", lambda *_args: "")
    monkeypatch.setattr("api.osc.drive_case_sync.create_drive_folder", fake_create)
    case = CaseFolder(
        source="nas",
        path="/cases/法扶案件/消費者債務清理/2026-0051-金李連芯-消費者債務清理-更生",
        relative_path="法扶案件/消費者債務清理/2026-0051-金李連芯-消費者債務清理-更生",
        name="2026-0051-金李連芯-消費者債務清理-更生",
        category="法律扶助案件",
        status="active",
        case_kind="消費者債務清理",
        meta=CaseMeta(case_number="2026-0051", laf_case_no="1150519-E-014", client_hint="金李連芯"),
    )
    result = find_existing_drive_case_folder_for_local_case(object(), "root", case, owner_bucket="Lumi")
    assert result["ok"] is True
    assert result["drive_id"] == "drive-case"
    assert created == []


def test_find_existing_drive_case_folder_uses_broad_search_when_expected_name_moved(monkeypatch):
    existing = {
        ("root", "一般案件"): "general",
        ("general", "Lumi"): "lumi",
    }

    def fake_find(_service, parent_id, name):
        return existing.get((parent_id, name), "")

    candidates = [
        {"id": "consult", "name": "張國賢案件", "mimeType": "application/vnd.google-apps.folder"},
        {"id": "case", "name": "張國賢(確認決議無效)", "mimeType": "application/vnd.google-apps.folder"},
    ]
    rels = {
        "consult": "諮詢案件/實體諮詢案件/張國賢案件",
        "case": "一般案件/Lumi/張國賢(確認決議無效)",
    }

    monkeypatch.setattr("api.osc.drive_case_sync.find_drive_child_folder", fake_find)
    monkeypatch.setattr("api.osc.drive_case_sync.find_drive_child_folder_by_osc_case_number", lambda *_args: "")
    monkeypatch.setattr("api.osc.drive_case_sync._search_drive_folders_by_name_tokens", lambda *_args, **_kwargs: candidates)
    monkeypatch.setattr("api.osc.drive_case_sync._drive_folder_relative_path_to_root", lambda _service, folder_id, _root_id: rels[folder_id])

    case = CaseFolder(
        source="nas",
        path="/cases/一般案件/民事/2025-0122-張國賢-一審-確認決議無效",
        relative_path="一般案件/民事/2025-0122-張國賢-一審-確認決議無效",
        name="2025-0122-張國賢-一審-確認決議無效",
        category="一般案件",
        status="active",
        case_kind="民事",
        meta=CaseMeta(case_number="2025-0122", client_hint="張國賢", reason_hint="確認決議無效"),
    )

    result = find_existing_drive_case_folder_for_local_case(object(), "root", case, owner_bucket="Lumi")

    assert result["ok"] is True
    assert result["status"] == "existing_by_broad_search"
    assert result["drive_id"] == "case"
    assert result["relative_path"] == "一般案件/Lumi/張國賢(確認決議無效)"
    assert result["matched_terms"] == ["確認決議無效", "張國賢"]


def test_broad_search_prefers_laf_number_over_same_name_closed_case(monkeypatch):
    candidates = [
        {"id": "active", "name": "張偉銘-1140306-W-001-刑事一審辯護-妨害秩序等", "mimeType": "application/vnd.google-apps.folder"},
        {"id": "closed", "name": "張偉銘-1131018-W-001-刑事偵查中辯護-殺人未遂等", "mimeType": "application/vnd.google-apps.folder"},
    ]
    rels = {
        "active": "法扶案件/Lumi/張偉銘-1140306-W-001-刑事一審辯護-妨害秩序等",
        "closed": "結案案件/法扶案件/Lumi-2/張偉銘-1131018-W-001-刑事偵查中辯護-殺人未遂等",
    }
    monkeypatch.setattr("api.osc.drive_case_sync._search_drive_folders_by_name_tokens", lambda *_args, **_kwargs: candidates)
    monkeypatch.setattr("api.osc.drive_case_sync._drive_folder_relative_path_to_root", lambda _service, folder_id, _root_id: rels[folder_id])

    case = CaseFolder(
        source="nas",
        path="/cases/法扶案件/刑事/2025-0007-張偉銘-一審-傷害致死",
        relative_path="法扶案件/刑事/2025-0007-張偉銘-一審-傷害致死",
        name="2025-0007-張偉銘-一審-傷害致死",
        category="法扶案件",
        status="active",
        case_kind="刑事",
        meta=CaseMeta(case_number="2025-0007", laf_case_no="1140306-W-001", client_hint="張偉銘", reason_hint="傷害致死"),
    )

    result = find_drive_case_folder_by_broad_search(object(), "root", case)

    assert result["ok"] is True
    assert result["drive_id"] == "active"
    assert result["relative_path"] == "法扶案件/Lumi/張偉銘-1140306-W-001-刑事一審辯護-妨害秩序等"
    assert "1140306-W-001" in result["matched_terms"]


def test_db_local_cases_for_numbers_uses_db_canonical_path(monkeypatch, tmp_path):
    case_dir = tmp_path / "01_案件" / "法扶案件" / "消費者債務清理" / "2026-0051-金李連芯-消費者債務清理-更生"
    case_dir.mkdir(parents=True)
    monkeypatch.setattr(
        "api.osc.drive_case_sync.lookup_db_case_contexts",
        lambda nums: {
            "2026-0051": {
                "case_number": "2026-0051",
                "client_name": "金李連芯",
                "case_category": "法律扶助案件",
                "case_type": "消費者債務清理",
                "case_stage": "更生",
                "case_reason": "消費者債務清理",
                "folder_path": r"Z:\lumi63181107\01_案件\法扶案件\消費者債務清理\2026-0051-金李連芯-消費者債務清理-更生",
                "laf_case_no": "1150519-E-014",
                "status": "進行中",
            }
        },
    )
    monkeypatch.setattr("api.case_path_mapper.local_case_path_candidates", lambda _path: [str(case_dir)])
    cases, skipped = db_local_cases_for_numbers(["2026-0051"])
    assert skipped == []
    assert len(cases) == 1
    assert cases[0].local_path == str(case_dir)
    assert cases[0].relative_path == "法扶案件/消費者債務清理/2026-0051-金李連芯-消費者債務清理-更生"
    assert cases[0].category == "法扶案件"
    assert cases[0].case_kind == "消費者債務清理"
    assert cases[0].meta.laf_case_no == "1150519-E-014"


def test_run_priority_case_sync_uses_direct_db_mapping(monkeypatch, tmp_path):
    local_case = CaseFolder(
        source="nas",
        path="/cases/一般案件/行政/2026-0099-測試甲-一審-訴願",
        local_path="/cases/一般案件/行政/2026-0099-測試甲-一審-訴願",
        relative_path="一般案件/行政/2026-0099-測試甲-一審-訴願",
        name="2026-0099-測試甲-一審-訴願",
        category="一般案件",
        status="active",
        case_kind="行政",
        meta=CaseMeta(case_number="2026-0099", client_hint="測試甲", reason_hint="訴願"),
    )
    monkeypatch.setattr("api.osc.drive_case_sync.load_local_env", lambda: None)
    monkeypatch.setattr("api.osc.drive_case_sync.build_drive_service", lambda **_kwargs: object())
    monkeypatch.setattr(
        "api.osc.drive_case_sync.find_drive_root",
        lambda *_args, **_kwargs: {"id": "root", "name": "案件辦理", "webViewLink": "https://drive.example/root"},
    )
    monkeypatch.setattr("api.osc.drive_case_sync.db_local_cases_for_numbers", lambda nums: ([local_case], []))
    monkeypatch.setattr(
        "api.osc.drive_case_sync.ensure_drive_case_folder_for_local_case",
        lambda *_args, **_kwargs: {
            "ok": True,
            "drive_id": "drive-case",
            "relative_path": "一般案件/Lumi/測試甲-一審-訴願",
            "created_count": 0,
            "created_folders": [],
            "status": "existing_by_name",
        },
    )
    monkeypatch.setattr("api.osc.drive_case_sync.drive_descendant_context", lambda *_args, **_kwargs: [])
    monkeypatch.setattr("api.osc.drive_case_sync.local_descendant_context", lambda *_args, **_kwargs: [])
    report = run_priority_case_sync(
        case_numbers=["2026-0099"],
        root_name="案件辦理",
        output_dir=tmp_path,
        file_diff=True,
        execute_downloads=False,
        execute_uploads=False,
        ensure_drive_case_folders=True,
        drive_owner_bucket_name="Lumi",
    )
    assert report["mode"] == "direct_db_case_sync"
    assert report["summary"]["matched_case_folders"] == 1
    assert report["matched"][0]["drive"]["relative_path"] == "一般案件/Lumi/測試甲-一審-訴願"
    assert "2026-0099-" not in report["matched"][0]["drive"]["relative_path"]


def test_run_priority_case_sync_no_create_does_not_ensure_folder(monkeypatch, tmp_path):
    local_case = CaseFolder(
        source="nas",
        path="/cases/一般案件/行政/2026-0099-測試甲-一審-訴願",
        local_path="/cases/一般案件/行政/2026-0099-測試甲-一審-訴願",
        relative_path="一般案件/行政/2026-0099-測試甲-一審-訴願",
        name="2026-0099-測試甲-一審-訴願",
        category="一般案件",
        status="active",
        case_kind="行政",
        meta=CaseMeta(case_number="2026-0099", client_hint="測試甲", reason_hint="訴願"),
    )
    monkeypatch.setattr("api.osc.drive_case_sync.load_local_env", lambda: None)
    monkeypatch.setattr("api.osc.drive_case_sync.build_drive_service", lambda **_kwargs: object())
    monkeypatch.setattr(
        "api.osc.drive_case_sync.find_drive_root",
        lambda *_args, **_kwargs: {"id": "root", "name": "案件辦理", "webViewLink": "https://drive.example/root"},
    )
    monkeypatch.setattr("api.osc.drive_case_sync.db_local_cases_for_numbers", lambda nums: ([local_case], []))

    def fail_ensure(*_args, **_kwargs):
        raise AssertionError("ensure_drive_case_folder_for_local_case must not run when creation is disabled")

    monkeypatch.setattr("api.osc.drive_case_sync.ensure_drive_case_folder_for_local_case", fail_ensure)
    monkeypatch.setattr(
        "api.osc.drive_case_sync.find_existing_drive_case_folder_for_local_case",
        lambda *_args, **_kwargs: {
            "ok": True,
            "drive_id": "drive-case",
            "relative_path": "一般案件/Lumi/測試甲-一審-訴願",
            "created_count": 0,
            "created_folders": [],
            "status": "existing_by_name",
        },
    )
    monkeypatch.setattr("api.osc.drive_case_sync.drive_descendant_context", lambda *_args, **_kwargs: [])
    monkeypatch.setattr("api.osc.drive_case_sync.local_descendant_context", lambda *_args, **_kwargs: [])
    report = run_priority_case_sync(
        case_numbers=["2026-0099"],
        root_name="案件辦理",
        output_dir=tmp_path,
        file_diff=True,
        execute_downloads=False,
        execute_uploads=True,
        upload_limit=1,
        ensure_drive_case_folders=False,
        drive_owner_bucket_name="Lumi",
    )
    assert report["drive_folder_result"]["summary"]["created_folders"] == 0


def test_create_missing_drive_case_folders_only_for_recent_nas_cases(monkeypatch):
    recent = datetime.now(timezone.utc).isoformat()
    old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    local_recent = CaseFolder(
        source="nas",
        path="/cases/一般案件/行政/2026-0101-測試甲-一審-訴願",
        relative_path="一般案件/行政/2026-0101-測試甲-一審-訴願",
        name="2026-0101-測試甲-一審-訴願",
        category="一般案件",
        status="active",
        case_kind="行政",
        modified_time=recent,
        meta=CaseMeta(case_number="2026-0101"),
    )
    local_old = CaseFolder(
        source="nas",
        path="/cases/一般案件/行政/2025-0001-舊案-一審-訴願",
        relative_path="一般案件/行政/2025-0001-舊案-一審-訴願",
        name="2025-0001-舊案-一審-訴願",
        category="一般案件",
        status="active",
        case_kind="行政",
        modified_time=old,
        meta=CaseMeta(case_number="2025-0001"),
    )
    created = []

    def fake_ensure(_service, _root, relative_path):
        created.append(relative_path)
        return {"drive_id": "new", "created_folders": [relative_path], "created_count": 1}

    monkeypatch.setattr("api.osc.drive_case_sync.ensure_drive_folder_path", fake_ensure)
    result = create_missing_drive_case_folders(
        object(),
        "root",
        {"local_only": [local_recent, local_old]},
        create_limit=10,
        max_age_hours=24,
        owner_bucket="Lumi",
    )
    assert result["summary"]["attempted"] == 1
    assert result["summary"]["created_or_existing"] == 1
    assert result["summary"]["skipped"] == 1
    assert created == ["一般案件/Lumi/測試甲-一審-訴願"]


def test_build_file_sync_plan_supports_matched_case_offset(monkeypatch):
    cases = []
    for idx in range(3):
        cases.append({
            "drive": CaseFolder(
                source="drive",
                path=f"一般案件/Lumi/2026-020{idx}-測試-一審-事件",
                relative_path=f"一般案件/Lumi/2026-020{idx}-測試-一審-事件",
                name=f"2026-020{idx}-測試-一審-事件",
                meta=CaseMeta(case_number=f"2026-020{idx}"),
                drive_id=f"drive-{idx}",
            ),
            "local": CaseFolder(
                source="nas",
                path=f"/cases/一般案件/民事/2026-020{idx}-測試-一審-事件",
                local_path=f"/cases/一般案件/民事/2026-020{idx}-測試-一審-事件",
                relative_path=f"一般案件/民事/2026-020{idx}-測試-一審-事件",
                name=f"2026-020{idx}-測試-一審-事件",
                meta=CaseMeta(case_number=f"2026-020{idx}"),
            ),
        })
    monkeypatch.setattr("api.osc.drive_case_sync.drive_descendant_context", lambda *args, **kwargs: [])
    monkeypatch.setattr("api.osc.drive_case_sync.local_descendant_context", lambda *args, **kwargs: [])
    plan = build_file_sync_plan({"matched": cases}, object(), matched_case_limit=1, matched_case_offset=1)
    assert plan["summary"]["matched_cases_scanned"] == 1
    assert plan["summary"]["matched_case_offset"] == 1
    assert plan["cases"][0]["case_number"] == "2026-0201"


def test_build_file_sync_plan_marks_local_scan_timeout_without_download_actions(monkeypatch):
    drive = CaseFolder(
        source="drive",
        path="一般案件/Lumi/測試甲-一審-訴願",
        relative_path="一般案件/Lumi/測試甲-一審-訴願",
        name="測試甲-一審-訴願",
        meta=CaseMeta(case_number="2026-0099"),
        drive_id="drive-case",
    )
    local = CaseFolder(
        source="nas",
        path="/cases/一般案件/行政/2026-0099-測試甲-一審-訴願",
        local_path="/cases/一般案件/行政/2026-0099-測試甲-一審-訴願",
        relative_path="一般案件/行政/2026-0099-測試甲-一審-訴願",
        name="2026-0099-測試甲-一審-訴願",
        meta=CaseMeta(case_number="2026-0099"),
    )
    monkeypatch.setattr(
        "api.osc.drive_case_sync.drive_descendant_context",
        lambda *_args, **_kwargs: [
            FileEntry(
                source="drive",
                path="法院裁判/a.pdf",
                relative_path="法院裁判/a.pdf",
                name="a.pdf",
                is_folder=False,
                drive_id="file",
            )
        ],
    )
    monkeypatch.setattr(
        "api.osc.drive_case_sync.local_descendant_context",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("local_scandir_timeout:/cases")),
    )
    plan = build_file_sync_plan({"matched": [{"drive": drive, "local": local}]}, object())
    assert plan["summary"]["case_errors"] == 1
    assert plan["summary"]["drive_missing_in_nas_files"] == 0
    assert plan["cases"][0]["download_missing"] == []
    assert "local_scandir_timeout" in plan["cases"][0]["error"]


def test_drive_list_children_timeout_is_not_treated_as_empty(monkeypatch):
    class SlowRequest:
        def execute(self):
            __import__("time").sleep(0.7)
            return {"files": []}

    class Files:
        def list(self, **_kwargs):
            return SlowRequest()

    class Service:
        def files(self):
            return Files()

    monkeypatch.setenv("MAGI_DRIVE_SYNC_DRIVE_LIST_TIMEOUT_SEC", "0.01")
    try:
        _drive_list_children(Service(), "folder")
    except Exception as exc:
        assert "drive_api_timeout:list_children:folder" in str(exc)
    else:
        raise AssertionError("Drive API timeout should raise instead of returning an empty listing")
