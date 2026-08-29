from casper_ecosystem.law_firm_orchestrators import laf_nightly_audit as audit
import json


def _synthetic_windows_path(drive, *parts):
    return f"{drive}:\\" + "\\".join(parts)


def _synthetic_posix_path(*parts):
    return "/" + "/".join(parts)


def test_run_portal_new_files_scan_uses_portal_case_fetch(monkeypatch):
    class FakeDb:
        def fetch_all(self, query, params=(), as_dict=False):
            assert "legal_aid_number" in query
            assert as_dict is True
            return [
                {
                    "case_number": "2026-0060",
                    "client_name": "林文俊",
                    "legal_aid_number": "1150529-W-002",
                    "folder_path": "synthetic_cases/法扶案件/2026-0060-林文俊-消費者債務清理-更生",
                }
            ]

    captured = {}

    def fake_scan(cases, *, only_laf_no="", auto_download=True):
        captured["cases"] = cases
        captured["only_laf_no"] = only_laf_no
        captured["auto_download"] = auto_download
        return [{"laf_no": only_laf_no, "auto_downloaded": 2, "new_count": 0}]

    monkeypatch.setattr(audit, "_get_db", lambda: FakeDb())
    monkeypatch.setattr(audit, "scan_portal_new_files", fake_scan)

    result = audit.run_portal_new_files_scan(
        only_laf_no="1150529-W-002",
        auto_download=False,
    )

    assert result["ok"] is True
    assert result["scanned_cases"] == 1
    assert result["portal_auto_downloaded"] == 2
    assert result["portal_still_missing"] == 0
    assert captured["only_laf_no"] == "1150529-W-002"
    assert captured["auto_download"] is False


def test_run_portal_new_files_scan_marks_remaining_missing_as_action_required(monkeypatch):
    monkeypatch.setattr(audit, "_get_db", lambda: object())
    monkeypatch.setattr(
        audit,
        "fetch_laf_cases_for_portal_scan",
        lambda _db: [{"case_number": "2026-0060"}],
    )
    monkeypatch.setattr(
        audit,
        "scan_portal_new_files",
        lambda cases, *, only_laf_no="", auto_download=True: [
            {
                "laf_no": "1150529-W-002",
                "auto_downloaded": 0,
                "new_count": 2,
                "missing_files": ["結案審查通知書.pdf", "結案酬金領款單.pdf"],
            }
        ],
    )

    result = audit.run_portal_new_files_scan(auto_download=True)

    assert result["ok"] is True
    assert result["status"] == "action_required"
    assert result["action_required"] is True
    assert result["severity"] == "warning"
    assert result["portal_still_missing"] == 2
    assert "仍有 2 份附件未歸檔" in result["message"]


def test_run_portal_new_files_scan_keeps_unverified_mapping_out_of_missing_count(monkeypatch):
    monkeypatch.setattr(audit, "_get_db", lambda: object())
    monkeypatch.setattr(
        audit,
        "fetch_laf_cases_for_portal_scan",
        lambda _db: [{"case_number": "2026-0060"}],
    )
    monkeypatch.setattr(
        audit,
        "scan_portal_new_files",
        lambda cases, *, only_laf_no="", auto_download=True: [
            {
                "laf_no": "1150529-W-002",
                "auto_downloaded": 0,
                "new_count": 0,
                "file_count": 2,
                "mapping_unverified_count": 2,
                "mapping_unverified_files": ["接案通知書.pdf", "委任狀.pdf"],
                "reason_code": "nas_mapping_unverified",
            }
        ],
    )

    result = audit.run_portal_new_files_scan(auto_download=True)

    assert result["ok"] is True
    assert result["status"] == "mapping_unverified"
    assert result["action_required"] is False
    assert result["portal_still_missing"] == 0
    assert result["portal_mapping_unverified_cases"] == 1
    assert result["portal_mapping_unverified_files"] == 2
    assert "未判定為欠檔" in result["message"]


def test_run_portal_new_files_scan_fails_closed_when_listing_unavailable(monkeypatch):
    monkeypatch.setattr(audit, "_get_db", lambda: object())
    monkeypatch.setattr(
        audit,
        "fetch_laf_cases_for_portal_scan",
        lambda _db: [{"case_number": "2026-0042"}],
    )
    unavailable = audit.PortalNewFilesScanResult()
    unavailable.scan_error = "download_table_timeout"
    monkeypatch.setattr(
        audit,
        "scan_portal_new_files",
        lambda cases, *, only_laf_no="", auto_download=True: unavailable,
    )

    result = audit.run_portal_new_files_scan(auto_download=True)

    assert result["ok"] is False
    assert result["status"] == "portal_scan_failed"
    assert result["action_required"] is True
    assert result["error"] == "download_table_timeout"
    assert result["portal_auto_downloaded"] == 0


def test_run_portal_new_files_scan_defers_when_case_inventory_empty(monkeypatch):
    monkeypatch.setattr(audit, "_get_db", lambda: object())
    monkeypatch.setattr(audit, "fetch_laf_cases_for_portal_scan", lambda _db: [])

    result = audit.run_portal_new_files_scan(auto_download=False)

    assert result["ok"] is False
    assert result["status"] == "deferred_case_inventory_unavailable"
    assert result["deferred"] is True
    assert result["retryable"] is True
    assert result["action_required"] is False
    assert result["portal_still_missing"] == 0
    assert result["portal_new_files"] == []


def test_scan_portal_new_files_dry_run_does_not_download(monkeypatch, tmp_path):
    class FakeLaf:
        def __init__(self):
            self.download_count = 0

        def login(self):
            return True

        def get_downloadable_cases(self):
            return [
                {
                    "case_number": "1150529-W-002",
                    "client_name": "林文俊",
                    "file_list": ["扶助律師接案通知書_1150529-W-002_1150601.pdf"],
                    "row_element": object(),
                },
                {
                    "case_number": "1150529-W-999",
                    "client_name": "跳過",
                    "file_list": ["其他.pdf"],
                    "row_element": object(),
                },
            ]

        def download_case_files(self, **_kwargs):
            self.download_count += 1
            return []

        def close(self):
            pass

    fake_laf = FakeLaf()
    monkeypatch.setattr(audit, "_make_laf_web_automation", lambda log_prefix="": fake_laf)
    monkeypatch.setattr(audit, "_local_portal_case_matches", lambda all_cases, dc: all_cases)
    monkeypatch.setattr(audit, "_collect_existing_portal_files", lambda _cases: set())
    monkeypatch.setattr(
        audit,
        "_find_missing_portal_files",
        lambda file_list, _existing: list(file_list),
    )

    result = audit.scan_portal_new_files(
        [{"folder_path": str(tmp_path)}],
        only_laf_no="1150529-W-002",
        auto_download=False,
    )

    assert fake_laf.download_count == 0
    assert len(result) == 1
    assert result[0]["laf_no"] == "1150529-W-002"
    assert result[0]["new_count"] == 0
    assert result[0]["mapping_unverified_count"] == 1
    assert result[0]["reason_code"] == "nas_mapping_unverified"


def test_scan_portal_new_files_ignores_retained_row_for_final_closed_case(
    monkeypatch, tmp_path
):
    class FakeLaf:
        def __init__(self):
            self.download_count = 0

        def login(self):
            return True

        def get_downloadable_cases(self):
            return [
                {
                    "case_number": "1150507-E-023",
                    "client_name": "隱私當事人",
                    "file_list": ["歷史保留附件.pdf"],
                    "row_element": object(),
                }
            ]

        def download_case_files(self, **_kwargs):
            self.download_count += 1
            return []

        def close(self):
            pass

    fake_laf = FakeLaf()
    monkeypatch.setattr(audit, "_make_laf_web_automation", lambda log_prefix="": fake_laf)
    monkeypatch.setattr(audit, "_collect_existing_portal_files", lambda _cases: set())
    monkeypatch.setattr(
        audit,
        "_find_missing_portal_files",
        lambda file_list, _existing: list(file_list),
    )

    result = audit.scan_portal_new_files(
        [
            {
                "case_number": "2026-0042",
                "client_name": "隱私當事人",
                "case_type": "刑事",
                "case_reason": "測試",
                "status": "已結案",
                "legal_aid_status": "已結案",
                "legal_aid_number": "1150507-E-023",
                "folder_path": str(tmp_path / "archive-currently-unavailable"),
            }
        ],
        only_laf_no="1150507-E-023",
        auto_download=True,
    )

    assert list(result) == []
    assert fake_laf.download_count == 0


def test_scan_portal_new_files_keeps_pending_report_case_actionable(
    monkeypatch, tmp_path
):
    fallback_case = tmp_path / "CloudStorage" / "pending-report-case"
    fallback_case.mkdir(parents=True)

    class FakeLaf:
        def login(self):
            return True

        def get_downloadable_cases(self):
            return [
                {
                    "case_number": "1150507-E-024",
                    "client_name": "隱私當事人",
                    "file_list": ["待補附件.pdf"],
                    "row_element": object(),
                }
            ]

        def download_case_files(self, **_kwargs):
            return []

        def close(self):
            pass

    monkeypatch.setattr(audit, "_make_laf_web_automation", lambda log_prefix="": FakeLaf())
    monkeypatch.setattr(audit, "is_authoritative_case_storage_path", lambda _path: False)
    monkeypatch.setattr(audit, "_collect_existing_portal_files", lambda _cases: set())
    monkeypatch.setattr(
        audit,
        "_find_missing_portal_files",
        lambda file_list, _existing: list(file_list),
    )

    result = audit.scan_portal_new_files(
        [
            {
                "case_number": "2026-0043",
                "client_name": "隱私當事人",
                "case_type": "刑事",
                "case_reason": "測試",
                "status": "結案中",
                "legal_aid_status": "已結案，待報結",
                "legal_aid_number": "1150507-E-024",
                "folder_path": str(fallback_case),
            }
        ],
        only_laf_no="1150507-E-024",
        auto_download=False,
    )

    assert len(result) == 1
    # A non-authoritative temporary path must not turn an unverified mapping
    # into a false missing-attachment alarm.  The current contract keeps the
    # row visible for reconciliation while leaving the missing count at zero.
    assert result[0]["new_count"] == 0
    assert result[0]["mapping_unverified_count"] == 1
    assert result[0]["reason_code"] == "nas_mapping_unverified"


def test_schedule_fixture_provider_runs_real_download_and_archive_flow(
    monkeypatch, tmp_path
):
    case_root = tmp_path / "cases" / "2026-0060-隔離當事人-法扶"
    case_root.mkdir(parents=True)
    fixture = tmp_path / "portal-provider.json"
    fixture.write_text(
        json.dumps(
            {
                "portal_cases": [
                    {
                        "case_number": "1150529-W-002",
                        "client_name": "隔離當事人",
                        "case_type": "民事",
                        "case_reason": "消費者債務清理",
                        "file_list": [
                            "接案通知書_1150529-W-002_1150601.pdf"
                        ],
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("MAGI_V3_REALISM_SANDBOX", "1")
    monkeypatch.setenv("MAGI_V3_SCHEDULE_FIXTURE_ROOT", str(tmp_path))
    monkeypatch.setenv("MAGI_LAF_PORTAL_PROVIDER_FIXTURE", str(fixture))
    monkeypatch.setattr(
        audit,
        "is_authoritative_case_storage_path",
        lambda path: str(path) == str(case_root),
    )

    result = audit.scan_portal_new_files(
        [
            {
                "case_number": "2026-0060",
                "client_name": "隔離當事人",
                "case_type": "民事",
                "case_reason": "消費者債務清理",
                "legal_aid_number": "1150529-W-002",
                "folder_path": str(case_root),
            }
        ],
        only_laf_no="1150529-W-002",
        auto_download=True,
    )

    assert len(result) == 1
    assert result[0]["auto_downloaded"] == 1
    assert result[0]["new_count"] == 0
    archived = list((case_root / "01_法扶資料").glob("*.pdf"))
    assert len(archived) == 1
    transcript = json.loads(
        (tmp_path / "portal_provider_transcript.json").read_text(encoding="utf-8")
    )
    assert [row["action"] for row in transcript] == [
        "login",
        "get_downloadable_cases",
        "download_case_files",
        "close",
    ]


def test_to_mac_path_falls_back_to_backup_closed_case_root(monkeypatch, tmp_path):
    backup_root = tmp_path / "backup" / "01_案件"
    case_dir = backup_root / "法扶案件" / "刑事" / "2099-0010-測試當事人-一審-測試案由"
    case_dir.mkdir(parents=True)

    monkeypatch.setattr(audit, "NAS_CASE_ROOT", str(tmp_path / "active_missing"))
    monkeypatch.setattr(audit, "Y_DRIVE_ROOT", str(tmp_path / "closed_missing"))
    monkeypatch.setattr(audit, "translate_case_path_to_local", lambda *_args, **_kwargs: "")
    monkeypatch.setenv("MAGI_LAF_CLOSED_CASE_FALLBACK_ROOTS", str(backup_root))

    result = audit._to_mac_path(
        _synthetic_windows_path(
            "Y",
            "lumi",
            "03_工作資料",
            "10_結案",
            "法扶案件",
            "刑事",
            "2099-0010-測試當事人-一審-測試案由",
        )
    )

    assert result == str(case_dir)


def test_to_mac_path_uses_canonical_mapper_for_mounted_closed_share(monkeypatch, tmp_path):
    case_dir = tmp_path / "shared" / "closed" / "法扶案件" / "刑事" / "2099-0050-測試"
    case_dir.mkdir(parents=True)
    monkeypatch.setattr(audit, "translate_case_path_to_local", lambda *_args, **_kwargs: str(case_dir))

    result = audit._to_mac_path(
        _synthetic_windows_path(
            "Y", "shared", "closed", "法扶案件", "刑事", "2099-0050-測試"
        )
    )

    assert result == str(case_dir)


def test_resolve_existing_case_folder_finds_unique_moved_case(monkeypatch, tmp_path):
    active_root = tmp_path / "active" / "01_案件"
    moved = active_root / "法扶案件" / "民事" / "2026-0060-測試當事人-更生"
    moved.mkdir(parents=True)

    monkeypatch.setattr(audit, "_CASE_ROOTS", [str(active_root)])
    monkeypatch.setattr(audit, "_FALLBACK_CASE_ROOTS", [])
    monkeypatch.setattr(audit, "NAS_CASE_ROOT", str(active_root))
    monkeypatch.setattr(audit, "Y_DRIVE_ROOT", "")
    monkeypatch.setattr(audit, "translate_case_path_to_local", lambda *_args, **_kwargs: "")
    monkeypatch.setattr(
        audit,
        "is_authoritative_case_storage_path",
        lambda path: str(path).startswith(str(active_root)),
    )

    result = audit._resolve_existing_case_folder(
        {
            "case_number": "2026-0060",
            "folder_path": _synthetic_windows_path(
                "Z", "old", "cases", "法扶案件", "民事", "2026-0060-舊名稱"
            ),
        }
    )

    assert result == str(moved)


def test_resolve_existing_case_folder_fails_closed_when_case_number_is_ambiguous(
    monkeypatch, tmp_path
):
    active_root = tmp_path / "active" / "01_案件"
    closed_root = tmp_path / "closed" / "03_工作資料" / "10_結案"
    (active_root / "法扶案件" / "民事" / "2026-0060-測試當事人-一審").mkdir(parents=True)
    (closed_root / "法扶案件" / "民事" / "2026-0060-測試當事人-結案").mkdir(parents=True)

    monkeypatch.setattr(audit, "_CASE_ROOTS", [str(active_root), str(closed_root)])
    monkeypatch.setattr(audit, "NAS_CASE_ROOT", str(active_root))
    monkeypatch.setattr(audit, "Y_DRIVE_ROOT", str(closed_root))
    monkeypatch.setattr(audit, "translate_case_path_to_local", lambda *_args, **_kwargs: "")

    result = audit._resolve_existing_case_folder(
        {
            "case_number": "2026-0060",
            "folder_path": _synthetic_windows_path(
                "Z", "old", "cases", "法扶案件", "民事", "2026-0060-不存在"
            ),
        }
    )

    assert result == ""


def test_resolve_existing_case_folder_uses_unique_client_when_ids_differ(
    monkeypatch, tmp_path
):
    active_root = tmp_path / "active" / "01_案件"
    moved = (
        active_root
        / "法扶案件"
        / "刑事"
        / "2026-0084-王小明-一審-竊盜"
    )
    moved.mkdir(parents=True)

    monkeypatch.setattr(audit, "_CASE_ROOTS", [str(active_root)])
    monkeypatch.setattr(audit, "_FALLBACK_CASE_ROOTS", [])
    monkeypatch.setattr(audit, "NAS_CASE_ROOT", str(active_root))
    monkeypatch.setattr(audit, "Y_DRIVE_ROOT", "")
    monkeypatch.setattr(audit, "translate_case_path_to_local", lambda *_args, **_kwargs: "")
    monkeypatch.setattr(
        audit,
        "is_authoritative_case_storage_path",
        lambda path: str(path).startswith(str(active_root)),
    )

    result = audit._resolve_existing_case_folder(
        {
            "case_number": "1150812-J-004",
            "client_name": "王小明",
            "folder_path": _synthetic_windows_path(
                "Z", "old", "法扶案件", "刑事", "不存在"
            ),
        },
        authoritative_only=True,
    )

    assert result == str(moved)


def test_resolve_existing_case_folder_rejects_ambiguous_client_name(
    monkeypatch, tmp_path
):
    active_root = tmp_path / "active" / "01_案件"
    for number in ("2026-0084", "2026-0099"):
        (
            active_root
            / "法扶案件"
            / "刑事"
            / f"{number}-王小明-一審-測試"
        ).mkdir(parents=True)

    monkeypatch.setattr(audit, "_CASE_ROOTS", [str(active_root)])
    monkeypatch.setattr(audit, "_FALLBACK_CASE_ROOTS", [])
    monkeypatch.setattr(audit, "NAS_CASE_ROOT", str(active_root))
    monkeypatch.setattr(audit, "Y_DRIVE_ROOT", "")
    monkeypatch.setattr(audit, "translate_case_path_to_local", lambda *_args, **_kwargs: "")
    monkeypatch.setattr(
        audit,
        "is_authoritative_case_storage_path",
        lambda path: str(path).startswith(str(active_root)),
    )

    result = audit._resolve_existing_case_folder(
        {
            "case_number": "1150812-J-004",
            "client_name": "王小明",
            "folder_path": _synthetic_windows_path(
                "Z", "old", "法扶案件", "刑事", "不存在"
            ),
        },
        authoritative_only=True,
    )

    assert result == ""


def test_resolve_existing_case_folder_does_not_treat_file_provider_as_nas(
    monkeypatch, tmp_path
):
    cloud_root = tmp_path / "Library" / "CloudStorage" / "SynologyDrive-homes" / "01_案件"
    case_dir = cloud_root / "法扶案件" / "民事" / "2099-0061-隔離當事人-民事"
    case_dir.mkdir(parents=True)
    monkeypatch.setattr(audit, "_CASE_ROOTS", [str(cloud_root)])
    monkeypatch.setattr(audit, "_FALLBACK_CASE_ROOTS", [str(cloud_root)])
    monkeypatch.setattr(audit, "NAS_CASE_ROOT", str(cloud_root))
    monkeypatch.setattr(audit, "Y_DRIVE_ROOT", "")
    monkeypatch.setattr(
        audit,
        "translate_case_path_to_local",
        lambda *_args, **_kwargs: str(case_dir),
    )
    monkeypatch.setattr(audit, "is_authoritative_case_storage_path", lambda _path: False)
    case = {
        "case_number": "2099-0061",
        "folder_path": _synthetic_windows_path(
            "Z", "office", "01_案件", "法扶案件", "民事", case_dir.name
        ),
    }

    assert audit._resolve_existing_case_folder(case) == str(case_dir)
    assert audit._resolve_existing_case_folder(case, authoritative_only=True) == ""
    assert audit._portal_case_storage_state([case]) == {
        "status": "local_fallback_only",
        "authoritative_count": 0,
        "fallback_only_count": 1,
        "missing_count": 0,
    }


def test_collect_existing_portal_files_ignores_local_fallback(monkeypatch, tmp_path):
    case_dir = tmp_path / "CloudStorage" / "2099-0062-隔離當事人"
    target = case_dir / "01_法扶資料"
    target.mkdir(parents=True)
    (target / "測試接案通知書.pdf").write_bytes(b"fixture")

    def _resolve(_case, *, authoritative_only=False):
        return "" if authoritative_only else str(case_dir)

    monkeypatch.setattr(audit, "_resolve_existing_case_folder", _resolve)

    assert audit._collect_existing_portal_files([{"case_number": "2099-0062"}]) == []


def test_portal_scan_refuses_to_archive_into_file_provider_only_case(
    monkeypatch, tmp_path
):
    class FakeLaf:
        last_downloadable_cases_scan = {"ok": True}

        def __init__(self):
            self.download_count = 0

        def login(self):
            return True

        def get_downloadable_cases(self):
            return [{
                "case_number": "1190101-W-001",
                "client_name": "隔離當事人",
                "file_list": ["接案通知書.pdf"],
                "row_element": object(),
            }]

        def download_case_files(self, **_kwargs):
            self.download_count += 1
            return []

        def close(self):
            pass

    fake = FakeLaf()
    local_case = tmp_path / "CloudStorage" / "case"
    local_case.mkdir(parents=True)
    monkeypatch.setattr(audit, "_make_laf_web_automation", lambda **_kwargs: fake)
    monkeypatch.setattr(audit, "_local_portal_case_matches", lambda _all, _dc: _all)
    monkeypatch.setattr(audit, "_collect_existing_portal_files", lambda _cases: [])

    def _resolve(_case, *, authoritative_only=False):
        return "" if authoritative_only else str(local_case)

    monkeypatch.setattr(audit, "_resolve_existing_case_folder", _resolve)

    result = audit.scan_portal_new_files(
        [{
            "case_number": "2099-0063",
            "client_name": "隔離當事人",
            "legal_aid_number": "1190101-W-001",
            "folder_path": _synthetic_windows_path("Z", "office", "01_案件", "case"),
        }],
        only_laf_no="1190101-W-001",
        auto_download=True,
    )

    assert fake.download_count == 0
    assert len(result) == 1
    assert result[0]["storage_status"] == "local_fallback_only"
    assert result[0]["storage_authoritative_count"] == 0
    assert result[0]["storage_fallback_only_count"] == 1
    assert result[0]["storage_missing_count"] == 0
    assert result[0]["reason_code"] == "nas_mapping_unverified"


def test_portal_storage_state_rejects_mixed_authority(monkeypatch):
    def _resolve(case, *, authoritative_only=False):
        if case["kind"] == "nas":
            return _synthetic_posix_path(
                "Volumes", "homes", "office", "01_案件", "case"
            )
        return "" if authoritative_only else _synthetic_posix_path(
            "Users", "test", "Library", "CloudStorage", "case"
        )

    monkeypatch.setattr(audit, "_resolve_existing_case_folder", _resolve)

    assert audit._portal_case_storage_state([{"kind": "nas"}, {"kind": "cloud"}]) == {
        "status": "mixed_authority",
        "authoritative_count": 1,
        "fallback_only_count": 1,
        "missing_count": 0,
    }


def test_laf_portal_scan_script_writes_json(monkeypatch, tmp_path):
    from scripts.ops import laf_portal_new_files_scan as script

    out = tmp_path / "latest.json"
    monkeypatch.setattr(
        audit,
        "run_portal_new_files_scan",
        lambda only_laf_no="", auto_download=True: {
            "ok": True,
            "only_laf_no": only_laf_no,
            "auto_download": auto_download,
            "portal_auto_downloaded": 0,
            "portal_still_missing": 0,
        },
    )

    code = script.main([
        "--only-laf-no",
        "1150529-W-002",
        "--dry-run",
        "--json-out",
        str(out),
    ])

    assert code == 0
    text = out.read_text(encoding="utf-8")
    assert "1150529-W-002" in text
    assert '"auto_download": false' in text
    assert '"dry_run": true' in text


def test_laf_portal_scan_script_preserves_known_missing_on_portal_failure(
    monkeypatch, tmp_path
):
    from scripts.ops import laf_portal_new_files_scan as script

    out = tmp_path / "latest.json"
    out.write_text(
        json.dumps(
            {
                "ok": True,
                "checked_at": "2026-07-23T12:16:49",
                "scanned_cases": 1,
                "portal_still_missing": 1,
                "matched_or_missing_cases": 1,
                "portal_new_files": [{"laf_no": "1150507-E-023", "new_count": 1}],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        audit,
        "run_portal_new_files_scan",
        lambda only_laf_no="", auto_download=True: {
            "ok": False,
            "status": "portal_scan_failed",
            "error": "download_table_timeout",
            "portal_still_missing": 0,
            "portal_new_files": [],
        },
    )

    code = script.main(["--apply", "--json-out", str(out)])

    payload = json.loads(out.read_text(encoding="utf-8"))
    assert code == 1
    assert payload["portal_still_missing"] == 1
    assert payload["portal_new_files"][0]["laf_no"] == "1150507-E-023"
    assert payload["stale_last_success"] is True
    assert payload["last_successful_checked_at"] == "2026-07-23T12:16:49"


def test_laf_portal_scan_script_does_not_preserve_empty_inventory_synthetic_success(
    monkeypatch, tmp_path
):
    from scripts.ops import laf_portal_new_files_scan as script

    out = tmp_path / "latest.json"
    out.write_text(
        json.dumps(
            {
                "ok": True,
                "status": "action_required",
                "scanned_cases": 0,
                "matched_or_missing_cases": 10,
                "portal_still_missing": 49,
                "portal_new_files": [{"reason_code": "portal_files_missing"}],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        audit,
        "run_portal_new_files_scan",
        lambda only_laf_no="", auto_download=True: {
            "ok": False,
            "status": "deferred_case_inventory_unavailable",
            "deferred": True,
            "retryable": True,
            "action_required": False,
            "reason": "case_inventory_unavailable",
            "portal_still_missing": 0,
            "portal_new_files": [],
        },
    )

    code = script.main(["--json-out", str(out)])

    payload = json.loads(out.read_text(encoding="utf-8"))
    assert code == 1
    assert payload["status"] == "deferred_case_inventory_unavailable"
    assert payload["portal_still_missing"] == 0
    assert payload["portal_new_files"] == []
    assert "stale_last_success" not in payload


def test_laf_portal_scan_script_defaults_to_dry_run(monkeypatch, tmp_path):
    from scripts.ops import laf_portal_new_files_scan as script

    out = tmp_path / "latest.json"
    seen = {}

    def fake_run(only_laf_no="", auto_download=True):
        seen["only_laf_no"] = only_laf_no
        seen["auto_download"] = auto_download
        return {"ok": True, "portal_auto_downloaded": 0, "portal_still_missing": 0}

    monkeypatch.delenv("MAGI_LAF_PORTAL_APPLY", raising=False)
    monkeypatch.delenv("MAGI_SKIP_IMPORT_PROBES", raising=False)
    monkeypatch.setattr(audit, "run_portal_new_files_scan", fake_run)

    code = script.main(["--json-out", str(out)])

    assert code == 0
    assert seen["auto_download"] is False
    data = out.read_text(encoding="utf-8")
    assert '"dry_run": true' in data
    assert '"apply": false' in data
    assert "MAGI_SKIP_IMPORT_PROBES" not in __import__("os").environ


def test_laf_portal_scan_script_restores_explicit_import_probe_policy(
    monkeypatch, tmp_path
):
    from scripts.ops import laf_portal_new_files_scan as script

    out = tmp_path / "latest.json"
    seen = {}

    def fake_run(only_laf_no="", auto_download=True):
        seen["policy"] = __import__("os").environ.get("MAGI_SKIP_IMPORT_PROBES")
        return {"ok": True, "portal_auto_downloaded": 0, "portal_still_missing": 0}

    monkeypatch.setenv("MAGI_SKIP_IMPORT_PROBES", "0")
    monkeypatch.setattr(audit, "run_portal_new_files_scan", fake_run)

    assert script.main(["--json-out", str(out)]) == 0
    assert seen["policy"] == "0"
    assert __import__("os").environ["MAGI_SKIP_IMPORT_PROBES"] == "0"


def test_laf_portal_scan_script_apply_enables_download(monkeypatch, tmp_path):
    from scripts.ops import laf_portal_new_files_scan as script

    out = tmp_path / "latest.json"
    seen = {}
    def fake_run(only_laf_no="", auto_download=True):
        seen["auto_download"] = auto_download
        return {
            "ok": True,
            "portal_auto_downloaded": 0,
            "portal_still_missing": 0,
        }

    monkeypatch.setattr(audit, "run_portal_new_files_scan", fake_run)

    code = script.main(["--apply", "--json-out", str(out)])

    assert code == 0
    assert seen["auto_download"] is True
