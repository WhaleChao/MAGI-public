from __future__ import annotations


def test_read_only_portal_scan_does_not_consume_nightly_baseline(monkeypatch):
    from casper_ecosystem.law_firm_orchestrators import laf_nightly_audit as audit

    calls = []

    class FakePortal:
        def login(self):
            return True

        def query_pending_drafts_all(self):
            return {
                "case_status": [],
                "closing": [],
                "condition": [],
                "go_live": [],
                "progress": [],
            }

        def close(self):
            calls.append("close")

    monkeypatch.setattr(audit, "_load_draft_state", lambda: calls.append("load") or {})
    monkeypatch.setattr(audit, "_save_draft_state", lambda _state: calls.append("save"))
    monkeypatch.setattr(audit, "_make_laf_web_automation", lambda **_kwargs: FakePortal())

    result = audit.scan_portal_pending_drafts(db=None, read_only=True)

    assert result["error"] is None
    assert result["read_only"] is True
    assert result["auto_resolved"] == []
    assert calls == ["close"]
