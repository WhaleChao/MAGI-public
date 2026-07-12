from __future__ import annotations

import json


def test_saas_audit_event_redacts_sensitive_metadata(tmp_path, monkeypatch):
    from api import saas_audit

    audit_path = tmp_path / "audit.jsonl"
    monkeypatch.setattr(saas_audit, "AUDIT_PATH", audit_path)

    event = saas_audit.append_audit_event(
        "file.preview",
        resource_type="file",
        resource_id="abc",
        metadata={
            "file": saas_audit.file_ref("/Volumes/homes/client/secret.docx"),
            "api_key": "should-not-appear",
            "nested": {"password": "also-secret"},
        },
    )

    assert event["metadata"]["api_key"] == "[redacted]"
    saved = json.loads(audit_path.read_text(encoding="utf-8").strip())
    assert saved["action"] == "file.preview"
    assert saved["metadata"]["nested"]["password"] == "[redacted]"
    assert saved["metadata"]["file"]["name"] == "secret.docx"
    assert "Volumes/homes/client" not in audit_path.read_text(encoding="utf-8")

