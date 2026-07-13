import json

from skills.magi import council_approval, night_talk
from api.pipelines import message_pipeline


def test_queue_deduplicates_changing_backlog_counts(monkeypatch, tmp_path):
    pending = tmp_path / "pending.json"
    monkeypatch.setattr(council_approval, "PENDING_FILE", str(pending))

    first = council_approval.queue_core_change_for_approval(
        "Issue: 判決摘要缺失 91,233 筆 | Action: 批次補全",
        "分批處理 court_judgments，含驗證與回滾。",
        {"casper": "Yes"},
        "degraded draft",
    )
    second = council_approval.queue_core_change_for_approval(
        "Issue: 判決摘要缺失 91,820 筆 | Action: 批次補全",
        "分批處理 court_judgments，含驗證與回滾。",
        {"casper": "Yes"},
        "degraded draft",
    )

    assert first["created"] is True
    assert first["item"]["review_mode"] == "degraded_draft"
    assert second["created"] is False
    assert second["item"]["id"] == first["item"]["id"]
    assert second["item"]["repeat_count"] == 2
    assert council_approval.list_pending_core_changes()["count"] == 1


def test_legacy_fallback_archive_preserves_audit_record(monkeypatch, tmp_path):
    pending = tmp_path / "pending.json"
    pending.write_text(
        json.dumps(
            {
                "version": 1,
                "items": [
                    {
                        "id": "ccr-20260712232356",
                        "status": "pending",
                        "created_at": "2026-07-12T23:23:56",
                        "updated_at": "2026-07-12T23:23:56",
                        "quorum_rule": "2/2 fallback",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(council_approval, "PENDING_FILE", str(pending))

    result = council_approval.archive_legacy_fallback_pending()
    stored = json.loads(pending.read_text(encoding="utf-8"))["items"][0]

    assert result["archived"] == 1
    assert stored["status"] == "expired"
    assert stored["approved_by"] == "policy_migration"
    assert "不構成完整表決" in stored["decision_note"]
    detail = council_approval.format_core_change_detail("ccr-20260712232356")
    assert "降級草案，未經完整三方審查" in detail


def test_fallback_notification_is_compact_and_never_says_passed(monkeypatch):
    calls = []

    def fake_alert(message, **kwargs):
        calls.append((message, kwargs))
        return {"telegram": True, "outbox_queued": False}

    monkeypatch.setattr("skills.ops.red_phone.alert_admin", fake_alert)
    item = {
        "id": "ccr-20260712232356",
        "issue": "判決摘要缺失" + "很長" * 500,
        "proposal": "批次產生摘要" + "內容" * 1000,
        "review_mode": "degraded_draft",
        "risk_reasons": ["涉及大量資料", "涉及法律或案件資料"],
    }

    result = night_talk._notify_pending_core_change(item, "degraded draft")
    message, kwargs = calls[0]

    assert result["telegram"] is True
    assert len(message) <= night_talk.MAX_COUNCIL_ALERT_CHARS
    assert "尚未通過" in message
    assert "夜議通過" not in message
    assert "查看提案 ccr-20260712232356" in message
    assert kwargs["source"] == "nightly_council"


def test_managed_backlogs_do_not_become_new_bulk_proposals():
    jobs = {"job_legacy_judgment_resummary_quality", "job_obsidian_repair_notes"}

    judgment = night_talk._managed_backlog_note(
        "court_judgments 判決摘要缺失 91,820 筆，建議批次處理", jobs
    )
    obsidian = night_talk._managed_backlog_note(
        "Obsidian bad_notes 39%，包含 weak_extraction", jobs
    )

    assert "小批次" in judgment
    assert "不啟動全量重寫" in judgment
    assert "小批次" in obsidian
    assert "不搬移、不刪除" in obsidian


def test_council_commands_run_before_chat_and_require_admin(monkeypatch, tmp_path):
    pending = tmp_path / "pending.json"
    monkeypatch.setattr(council_approval, "PENDING_FILE", str(pending))
    queued = council_approval.queue_core_change_for_approval(
        "判決摘要缺失 10000 筆",
        "分批處理並驗證。",
        {"casper": "Yes"},
        "degraded draft",
    )
    approval_id = queued["item"]["id"]

    denied = message_pipeline._handle_council_command(f"查看提案 {approval_id}", "user", "user")
    listed = message_pipeline._handle_council_command("核心變更待審", "admin", "admin")
    viewed = message_pipeline._handle_council_command(f"查看提案 {approval_id}", "admin", "admin")

    assert "只限已驗證的管理者" in denied
    assert approval_id in listed
    assert "降級草案" in listed
    assert f"提案 {approval_id}" in viewed
