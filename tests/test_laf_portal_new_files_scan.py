from casper_ecosystem.law_firm_orchestrators import laf_nightly_audit as audit


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
                    "folder_path": "Z:\\lumi63181107\\01_案件\\法扶案件\\消費者債務清理\\2026-0060-林文俊-消費者債務清理-更生",
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
    assert result[0]["new_count"] == 1


def test_to_mac_path_falls_back_to_backup_closed_case_root(monkeypatch, tmp_path):
    backup_root = tmp_path / "backup" / "01_案件"
    case_dir = backup_root / "法扶案件" / "刑事" / "2025-0010-劉信義-一審-殺人"
    case_dir.mkdir(parents=True)

    monkeypatch.setattr(audit, "NAS_CASE_ROOT", str(tmp_path / "active_missing"))
    monkeypatch.setattr(audit, "Y_DRIVE_ROOT", str(tmp_path / "closed_missing"))
    monkeypatch.setattr(audit, "translate_case_path_to_local", lambda *_args, **_kwargs: "")
    monkeypatch.setenv("MAGI_LAF_CLOSED_CASE_FALLBACK_ROOTS", str(backup_root))

    result = audit._to_mac_path(
        r"Y:\lumi\03_工作資料\10_結案\法扶案件\刑事\2025-0010-劉信義-一審-殺人"
    )

    assert result == str(case_dir)


def test_to_mac_path_uses_canonical_mapper_for_mounted_closed_share(monkeypatch, tmp_path):
    case_dir = tmp_path / "lumi" / "03_工作資料" / "10_結案" / "法扶案件" / "刑事" / "2026-0050-測試"
    case_dir.mkdir(parents=True)
    monkeypatch.setattr(audit, "translate_case_path_to_local", lambda *_args, **_kwargs: str(case_dir))

    result = audit._to_mac_path(r"Y:\lumi\03_工作資料\10_結案\法扶案件\刑事\2026-0050-測試")

    assert result == str(case_dir)


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
    assert __import__("os").environ["MAGI_SKIP_IMPORT_PROBES"] == "1"


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
