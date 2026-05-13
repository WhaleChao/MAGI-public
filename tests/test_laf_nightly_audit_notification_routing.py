from __future__ import annotations

from pathlib import Path
import sys
from types import SimpleNamespace


def test_laf_nightly_audit_send_report_uses_laf_general(monkeypatch):
    import scripts.laf_nightly_audit as audit

    calls = []

    def fake_alert_admin(**kwargs):
        calls.append(kwargs)
        return {"telegram": True}

    monkeypatch.setitem(sys.modules, "red_phone", SimpleNamespace(alert_admin=fake_alert_admin))

    audit.send_report(
        "📋 法扶夜間巡檢報告\n⚠️ 進行中逾 18 個月，需確認進度回報：13 件",
        has_issues=True,
    )

    assert calls
    assert calls[0]["source"] == "laf_nightly_audit"
    assert calls[0]["severity"] == "warning"
    assert calls[0]["topic_key"] == "laf_general"


def test_orchestrator_laf_nightly_audit_send_report_uses_laf_general(monkeypatch):
    import casper_ecosystem.law_firm_orchestrators.laf_nightly_audit as audit

    calls = []

    def fake_alert_admin(**kwargs):
        calls.append(kwargs)
        return {"telegram": True}

    monkeypatch.setitem(sys.modules, "red_phone", SimpleNamespace(alert_admin=fake_alert_admin))

    audit.send_report(
        "📋 法扶夜間巡檢報告\n⚠️ 進行中逾 18 個月，需確認進度回報：13 件",
        has_issues=True,
    )

    assert calls
    assert calls[0]["source"] == "laf_nightly_audit"
    assert calls[0]["severity"] == "warning"
    assert calls[0]["topic_key"] == "laf_general"


def test_laf_nightly_reconcile_notifications_do_not_use_generic_laf_topic():
    src = Path("casper_ecosystem/law_firm_orchestrators/laf_nightly_audit.py").read_text(encoding="utf-8")

    assert 'topic_key="laf_general"' in src
    assert 'topic_key="laf")' not in src
