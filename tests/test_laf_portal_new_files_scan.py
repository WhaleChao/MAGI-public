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
