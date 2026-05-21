from __future__ import annotations

from api.osc.drive_case_sync import (
    CaseFolder,
    CaseMeta,
    classify_drive_case_folder,
    classify_local_case_folder,
    compare_case_folders,
    default_active_case_roots,
    extract_case_meta,
    infer_case_kind,
    match_keys,
    normalize_court_case_no,
    suggest_canonical_path,
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


def test_match_by_laf_case_number():
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


def test_court_case_number_normalization_and_keys():
    assert normalize_court_case_no("115年度訴字第000001號") == "115年度訴字第1號"
    meta = CaseMeta(court_case_no="115年度訴字第000001號", client_hint="測試甲")
    keys = match_keys(meta)
    assert "court:115年度訴字第1號" in keys
    assert "name:測試甲" in keys


def test_default_active_root_uses_explicit_env(tmp_path, monkeypatch):
    root = tmp_path / "01_案件"
    root.mkdir()
    monkeypatch.setenv("MAGI_DRIVE_SYNC_ACTIVE_CASE_ROOT", str(root))
    assert default_active_case_roots() == [root]
