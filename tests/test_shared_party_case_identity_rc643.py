from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

from api.osc import drive_case_sync


ROOT = Path(__file__).resolve().parents[1]


def _row(case_number: str, court_no: str, subject: str) -> dict:
    return {
        "case_number": case_number,
        "client_name": "共同權利主體",
        "case_reason": "權利回復條例",
        "court_case_no": court_no,
        "case_category": "一般案件",
        "case_type": "行政",
        "case_stage": "一審",
        "notes": subject,
        "folder_path": f"Z:/01_案件/一般案件/行政/{case_number}-共同權利主體-一審-權利回復條例",
        "status": "進行中",
        "legal_aid_status": "",
        "manual_status_lock": 0,
        "legal_aid_number": "",
        "laf_case_no": "",
        "application_no": "",
    }


def _case(case_number: str, *, source: str, drive_id: str = "") -> drive_case_sync.CaseFolder:
    path = f"一般案件/行政/{case_number}-共同權利主體-一審-權利回復條例"
    return drive_case_sync.CaseFolder(
        source=source,
        path=path,
        relative_path=path,
        name=path.rsplit("/", 1)[-1],
        category="一般案件",
        status="active",
        case_kind="行政",
        meta=drive_case_sync.CaseMeta(
            case_number=case_number,
            client_hint="共同權利主體",
            reason_hint="權利回復條例",
        ),
        drive_id=drive_id,
        local_path=path if source == "nas" else "",
    )


def _load_smart_filer(monkeypatch):
    rag_module = types.ModuleType("rag_feedback")
    rag_module.rag_engine = types.SimpleNamespace(query=lambda *_args, **_kwargs: [])
    monkeypatch.setitem(sys.modules, "rag_feedback", rag_module)
    module_path = ROOT / "skills" / "pdf-namer" / "smart_filer.py"
    spec = importlib.util.spec_from_file_location("smart_filer_rc643_identity_test", module_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _index_entry(case_id: str, court_no: str, counterparty: str) -> dict:
    return {
        "case_type": "一般案件",
        "domain": "行政",
        "folder_name": f"{case_id}-共同權利主體-一審-權利回復條例",
        "path": f"/cases/{case_id}",
        "parties": ["共同權利主體"],
        "counterparties": [counterparty] if counterparty else [],
        "court_case_numbers": [court_no] if court_no else [],
        "case_id": case_id,
        "year": case_id[:4],
        "seq": case_id[-4:],
        "stage": "一審",
        "reason": "權利回復條例",
        "subfolders": ["01_我方歷次書狀"],
    }


def test_db_peer_probe_detects_same_visible_drive_namespace() -> None:
    rows = [
        _row("2026-0048", "115年度訴字第551號", "甲案"),
        _row("2026-0049", "115年度訴字第530號", "乙案"),
        _row("2026-0080", "", "丙案"),
    ]

    conflicts = drive_case_sync.detect_db_drive_namespace_conflicts(
        rows,
        ["2026-0049"],
        owner_bucket="Lumi",
    )

    assert len(conflicts) == 1
    assert conflicts[0]["selected_case_numbers"] == ["2026-0049"]
    assert conflicts[0]["conflicting_case_numbers"] == ["2026-0048", "2026-0049", "2026-0080"]
    assert len(conflicts[0]["expected_path_sha256"]) == 64
    assert "共同權利主體" not in str(conflicts[0])


def test_same_drive_folder_cannot_match_multiple_nas_cases() -> None:
    drive = _case("2026-0048", source="drive", drive_id="shared-drive-folder")
    first = _case("2026-0048", source="nas")
    second = _case("2026-0049", source="nas")
    comparison = {
        "matched": [
            {"drive": drive, "local": first, "match_keys": []},
            {"drive": drive, "local": second, "match_keys": []},
        ],
        "drive_only": [],
        "local_only": [],
        "ambiguous": [],
        "out_of_scope": [],
        "drive_many_to_one": [],
    }

    result = drive_case_sync.enforce_bijective_matches(comparison)

    assert result["matched"] == []
    assert len(result["drive_one_to_many"]) == 1
    assert {case.meta.case_number for case in result["drive_one_to_many"][0]["locals"]} == {
        "2026-0048",
        "2026-0049",
    }


def test_single_case_direct_worker_blocks_when_db_peer_shares_namespace(monkeypatch, tmp_path) -> None:
    local = _case("2026-0049", source="nas")
    monkeypatch.setattr(drive_case_sync, "load_local_env", lambda: None)
    monkeypatch.setattr(drive_case_sync, "build_drive_service", lambda **_kwargs: object())
    monkeypatch.setattr(
        drive_case_sync,
        "find_drive_root",
        lambda *_args, **_kwargs: {"id": "root", "name": "案件辦理"},
    )
    monkeypatch.setattr(
        drive_case_sync,
        "db_local_cases_for_numbers",
        lambda *_args, **_kwargs: ([local], []),
    )
    monkeypatch.setattr(
        drive_case_sync,
        "lookup_db_drive_namespace_peer_rows",
        lambda *_args, **_kwargs: [
            _row("2026-0048", "115年度訴字第551號", "甲案"),
            _row("2026-0049", "115年度訴字第530號", "乙案"),
        ],
    )
    monkeypatch.setattr(
        drive_case_sync,
        "find_existing_drive_case_folder_for_local_case",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("folder lookup must not run")),
    )
    monkeypatch.setattr(
        drive_case_sync,
        "build_file_sync_plan",
        lambda *_args, **_kwargs: {"summary": {}, "cases": []},
    )
    monkeypatch.setattr(drive_case_sync, "write_report_files", lambda *_args, **_kwargs: {})

    report = drive_case_sync.run_priority_case_sync(
        case_numbers=["2026-0049"],
        output_dir=tmp_path,
    )

    assert report["matched"] == []
    skipped = report["drive_folder_result"]["db_skipped_cases"]
    assert skipped[0]["reason"] == "shared_drive_namespace_requires_case_disambiguation"
    assert skipped[0]["conflicting_case_numbers"] == ["2026-0048", "2026-0049"]


def test_existing_drive_app_property_cannot_be_stolen(monkeypatch) -> None:
    monkeypatch.setattr(
        drive_case_sync,
        "_drive_item_metadata",
        lambda *_args, **_kwargs: {
            "id": "shared-drive-folder",
            "appProperties": {"magi_osc_case_number": "2026-0048"},
        },
    )

    result = drive_case_sync.drive_folder_case_binding_conflict(
        object(),
        "shared-drive-folder",
        "2026-0049",
    )

    assert result is not None
    assert result["reason"] == "drive_folder_bound_to_different_osc_case"
    assert result["bound_case_number"] == "2026-0048"


def test_pdf_filer_uses_actual_registered_court_number(monkeypatch) -> None:
    smart_filer = _load_smart_filer(monkeypatch)
    index = [
        _index_entry("2026-0048", "115年度訴字第551號", "甲方"),
        _index_entry("2026-0049", "115年度訴字第530號", "乙方"),
    ]

    result = smart_filer.match_to_case(
        "共同權利主體 乙方 115年度訴字第530號",
        "法院通知.pdf",
        case_index=index,
    )

    assert result["matched"] is True
    assert result["case_info"]["folder_name"].startswith("2026-0049-")
    assert result["match_method"] == "法院案號+案件身分精確匹配"


def test_pdf_filer_rejects_shared_party_without_verified_identity(monkeypatch) -> None:
    smart_filer = _load_smart_filer(monkeypatch)
    index = [
        _index_entry("2026-0048", "", ""),
        _index_entry("2026-0049", "", ""),
    ]

    result = smart_filer.match_to_case(
        "共同權利主體",
        "共同權利主體書狀.pdf",
        case_index=index,
    )

    assert result["matched"] is False
    assert "案件識別歧義" in result["reason"]
    assert result["candidate_case_ids"] == ["2026-0048", "2026-0049"]


def test_pdf_filer_rejects_duplicate_registered_court_number(monkeypatch) -> None:
    smart_filer = _load_smart_filer(monkeypatch)
    index = [
        _index_entry("2026-0048", "115年度訴字第551號", ""),
        _index_entry("2026-0049", "115年度訴字第551號", ""),
    ]

    result = smart_filer.match_to_case(
        "共同權利主體 115年度訴字第551號",
        "通知.pdf",
        case_index=index,
    )

    assert result["matched"] is False
    assert "案件識別歧義" in result["reason"]
