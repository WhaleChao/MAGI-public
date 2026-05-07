"""Tests for safe DB court_case_number backfill during file-review archiving."""

from casper_ecosystem.law_firm_orchestrators.file_review_automation import FileReviewManager


class _DummyDB:
    def __init__(self, rows):
        self.rows = rows
        self.updates = []

    def execute(self, query, params=None, fetch=None):
        if query.startswith("SELECT `id`, `case_number`, `client_name`, `court_case_number`, `folder_path`"):
            return list(self.rows)
        if query.startswith("UPDATE `cases` SET `court_case_number` = %s"):
            self.updates.append(params)
            return 1
        raise AssertionError(query)

    def translate_path_to_local(self, path):
        return path


def _make_mgr(rows):
    mgr = object.__new__(FileReviewManager)
    mgr.db = _DummyDB(rows)
    mgr._to_local_case_path = lambda p: p
    mgr._parse_court_case_no = lambda text: FileReviewManager._parse_court_case_no(text)
    mgr._norm = lambda text: FileReviewManager._norm(text)
    mgr._manual_key_norm = lambda text: FileReviewManager._manual_key_norm(text)
    mgr._looks_like_human_party_name = lambda text: FileReviewManager._looks_like_human_party_name(text)
    mgr._court_case_numbers_match = FileReviewManager._court_case_numbers_match.__get__(mgr, FileReviewManager)
    mgr.log = lambda msg: None
    return mgr


def test_sync_court_case_number_fills_blank_value():
    mgr = _make_mgr(
        [
            {
                "id": "1",
                "case_number": "2025-1000",
                "client_name": "測試甲",
                "court_case_number": "",
                "folder_path": "/tmp/case-a",
            }
        ]
    )

    updated = mgr._sync_court_case_number_to_db_if_safe(
        "/tmp/case-a",
        "114年度原上訴字第000123號",
        "測試甲",
    )

    assert updated == 1
    assert mgr.db.updates == [("114年度原上訴字第000123號", "1")]


def test_sync_court_case_number_skips_conflicting_existing_value():
    mgr = _make_mgr(
        [
            {
                "id": "2",
                "case_number": "2025-1001",
                "client_name": "測試乙",
                "court_case_number": "114年度原訴字第000015號",
                "folder_path": "/tmp/case-b",
            }
        ]
    )

    updated = mgr._sync_court_case_number_to_db_if_safe(
        "/tmp/case-b",
        "114年度原上訴字第000160號",
        "測試乙",
    )

    assert updated == 0
    assert mgr.db.updates == []
