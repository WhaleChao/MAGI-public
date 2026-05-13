from __future__ import annotations

from pathlib import Path
import sys
from types import SimpleNamespace


def test_laf_nightly_audit_send_report_splits_progress_for_dc(monkeypatch):
    import scripts.laf_nightly_audit as audit

    calls = []

    def fake_alert_admin(**kwargs):
        calls.append(kwargs)
        return {"telegram": True}

    monkeypatch.setitem(sys.modules, "red_phone", SimpleNamespace(alert_admin=fake_alert_admin))

    audit.send_report(
        "\n".join(
            [
                "📋 法扶夜間巡檢報告",
                "日期：2026-05-14",
                "",
                "📥 法扶官網文件：已自動下載 5 份",
                "",
                "⚠️ 進行中逾 18 個月，需確認進度回報：13 件",
                "  • 1121228-U-017 羅伊辰 — 派案/建案 2023-12-28，已 868 天",
                "  👉 若已回報，可回覆「<案號/姓名> 已回報」；MAGI 會冷卻 60 天後再提醒，並登上行事曆。",
            ]
        ),
        has_issues=True,
    )

    assert len(calls) == 2
    assert calls[0]["source"] == "laf_nightly_audit"
    assert calls[0]["severity"] == "warning"
    assert calls[0]["topic_key"] == "laf_general"
    assert calls[1]["source"] == "laf_progress_reminder"
    assert calls[1]["severity"] == "warning"
    assert calls[1]["topic_key"] == "laf_progress"
    assert "法扶官網文件" not in calls[1]["message"]
    assert "羅伊辰" in calls[1]["message"]


def test_orchestrator_laf_nightly_audit_send_report_splits_progress_for_dc(monkeypatch):
    import casper_ecosystem.law_firm_orchestrators.laf_nightly_audit as audit

    calls = []

    def fake_alert_admin(**kwargs):
        calls.append(kwargs)
        return {"telegram": True}

    monkeypatch.setitem(sys.modules, "red_phone", SimpleNamespace(alert_admin=fake_alert_admin))

    audit.send_report(
        "\n".join(
            [
                "📋 法扶夜間巡檢報告",
                "日期：2026-05-14",
                "",
                "⚠️ 法扶官網仍缺文件：1 件",
                "",
                "🚨 法扶官網要求進度回報：1 件",
                "  • 1121228-U-017 — 需回報",
            ]
        ),
        has_issues=True,
    )

    assert len(calls) == 2
    assert calls[0]["source"] == "laf_nightly_audit"
    assert calls[0]["severity"] == "warning"
    assert calls[0]["topic_key"] == "laf_general"
    assert calls[1]["source"] == "laf_progress_reminder"
    assert calls[1]["topic_key"] == "laf_progress"
    assert "仍缺文件" not in calls[1]["message"]
    assert "法扶官網要求進度回報" in calls[1]["message"]


def test_laf_nightly_reconcile_notifications_do_not_use_generic_laf_topic():
    src = Path("casper_ecosystem/law_firm_orchestrators/laf_nightly_audit.py").read_text(encoding="utf-8")

    assert 'topic_key="laf_general"' in src
    assert 'topic_key="laf_progress"' in src
    assert 'topic_key="laf")' not in src
