from __future__ import annotations

from scripts.ops import business_module_live_check as live_check


def test_business_live_check_redacts_sensitive_tails_and_samples():
    text = (
        "case_number=2025-0134 "
        "court_case_number=115年度消債更字第000071號 "
        "email=person@example.com phone=0912345678 "
        "path=/Users/example/private/case.pdf token='abc123'"
    )

    redacted = live_check._redact_text(text)

    assert "2025-0134" not in redacted
    assert "115年度消債更字第000071號" not in redacted
    assert "person@example.com" not in redacted
    assert "0912345678" not in redacted
    assert "/Users/example" not in redacted
    assert "abc123" not in redacted
    assert "<CASE_ID>" in redacted
    assert "<COURT_CASE_NO>" in redacted


def test_business_live_check_redacts_parsed_samples():
    payload = {
        "success": True,
        "eligible_cases": 2,
        "sample": [
            {
                "case_number": "2025-0134",
                "court_case_number": "115年度消債更字第000071號",
                "client_name": "測試姓名",
            }
        ],
    }

    redacted = live_check._redact_obj(payload)

    assert redacted["eligible_cases"] == 2
    assert redacted["sample"] == "<REDACTED:1 item(s)>"


def test_laf_portal_live_redacts_portal_errors(monkeypatch):
    class FakeAudit:
        @staticmethod
        def scan_portal_pending_drafts(db=None):
            return {
                "error": "case 2025-0134 for person@example.com at /Users/example/private",
                "closing_drafts": [],
                "case_status_drafts": [],
                "condition_pending": [],
                "go_live_pending": [],
                "progress_pending": [],
            }

    monkeypatch.setitem(__import__("sys").modules, "scripts.laf_nightly_audit", FakeAudit)

    result = live_check._laf_portal_live()

    assert result["ok"] is False
    assert "2025-0134" not in result["parsed"]["error"]
    assert "person@example.com" not in result["parsed"]["error"]
    assert "/Users/example" not in result["parsed"]["error"]
