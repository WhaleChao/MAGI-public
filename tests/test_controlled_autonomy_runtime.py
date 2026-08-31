from __future__ import annotations

from datetime import datetime, timedelta, timezone
import sqlite3
from types import SimpleNamespace

import pytest

from api.agentic.control import (
    ControlledAutonomyService,
    ControlledAutonomyStore,
    handoff_reply_succeeded,
    parse_controlled_command,
)
from api.agentic.contracts import PlanStatus, StepStatus
from api.pipelines.message_pipeline import _try_agentic_route
from api.pipelines import message_pipeline
from api.routing.intent_contract import KIND_AGENT_TASK


def _service(tmp_path, monkeypatch) -> ControlledAutonomyService:
    db = tmp_path / "agent" / "controlled-autonomy" / "plans.sqlite3"
    monkeypatch.setenv("MAGI_CONTROLLED_AUTONOMY_DB", str(db))
    return ControlledAutonomyService(ControlledAutonomyStore(db))


def test_mutable_plan_survives_new_service_and_requires_exact_bound_token(tmp_path, monkeypatch):
    service = _service(tmp_path, monkeypatch)
    proposal = service.propose(
        "請刪除 2026-0001 的舊檔案",
        user_id="u-1",
        platform="WEB",
    )
    assert proposal is not None
    assert proposal.plan.status is PlanStatus.AWAITING_CONFIRMATION
    assert proposal.plan.get_step("assess").status is StepStatus.SUCCEEDED
    assert proposal.plan.get_step("dispatch").confirmation.pending is True

    restarted = ControlledAutonomyService(ControlledAutonomyStore(service.store.path))
    stored = restarted.store.get(proposal.plan.plan_id, user_id="u-1", platform="WEB")
    assert stored["status"] == "awaiting_confirmation"
    assert proposal.confirmation_token not in service.store.path.read_bytes().decode("latin1", errors="ignore")

    with pytest.raises(LookupError):
        restarted.store.get(proposal.plan.plan_id, user_id="u-2", platform="WEB")
    with pytest.raises(LookupError):
        restarted.store.get(proposal.plan.plan_id, user_id="u-1", platform="TELEGRAM")
    with pytest.raises(PermissionError):
        restarted.store.begin_dispatch(
            proposal.plan.plan_id,
            "000000000000",
            user_id="u-1",
            platform="WEB",
        )

    lease = restarted.store.begin_dispatch(
        proposal.plan.plan_id,
        proposal.confirmation_token,
        user_id="u-1",
        platform="WEB",
    )
    assert lease.plan.status is PlanStatus.RUNNING
    assert lease.plan.get_step("dispatch").status is StepStatus.RUNNING
    with pytest.raises(RuntimeError):
        restarted.store.begin_dispatch(
            proposal.plan.plan_id,
            proposal.confirmation_token,
            user_id="u-1",
            platform="WEB",
        )


def test_repeated_identical_request_rotates_token_without_duplicate_plan(tmp_path, monkeypatch):
    service = _service(tmp_path, monkeypatch)
    first = service.propose(
        "請同步 2026-0009 的案件資料",
        user_id="u-1",
        platform="WEB",
    )
    second = service.propose(
        "請同步 2026-0009 的案件資料",
        user_id="u-1",
        platform="WEB",
    )
    assert second.plan.plan_id == first.plan.plan_id
    assert second.confirmation_token != first.confirmation_token
    assert len(service.store.list_for_owner(user_id="u-1", platform="WEB")) == 1
    with pytest.raises(PermissionError):
        service.store.begin_dispatch(
            first.plan.plan_id,
            first.confirmation_token,
            user_id="u-1",
            platform="WEB",
        )


def test_handoff_receipt_is_hash_only_and_does_not_claim_business_completion(tmp_path, monkeypatch):
    service = _service(tmp_path, monkeypatch)
    proposal = service.propose(
        "請同步 2026-0002 的案件資料",
        user_id="u-1",
        platform="LINE",
    )
    lease = service.store.begin_dispatch(
        proposal.plan.plan_id,
        proposal.confirmation_token,
        user_id="u-1",
        platform="LINE",
    )
    raw_reply = "已進入案件同步專用流程，尚待比對。"
    finished = service.store.finish_dispatch(lease, success=True, reply=raw_reply)
    assert finished.status is PlanStatus.SUCCEEDED
    record = service.store.get(proposal.plan.plan_id, user_id="u-1", platform="LINE")
    assert record["receipt"]["handoff_success"] is True
    assert record["receipt"]["business_completion_attested"] is False
    assert len(record["receipt"]["trace_id"]) == 32
    assert raw_reply not in service.store.path.read_bytes().decode("latin1", errors="ignore")


def test_expired_or_cancelled_plan_never_dispatches(tmp_path, monkeypatch):
    service = _service(tmp_path, monkeypatch)
    proposal = service.propose(
        "請上傳 2026-0003 的附件",
        user_id="u-1",
        platform="WEB",
    )
    with sqlite3.connect(service.store.path) as connection:
        connection.execute(
            "UPDATE controlled_plans SET expires_at=? WHERE plan_id=?",
            ((datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(), proposal.plan.plan_id),
        )
    with pytest.raises(TimeoutError):
        service.store.begin_dispatch(
            proposal.plan.plan_id,
            proposal.confirmation_token,
            user_id="u-1",
            platform="WEB",
        )

    second = service.propose(
        "請上傳 2026-0004 的附件",
        user_id="u-1",
        platform="WEB",
    )
    cancelled = service.store.cancel(second.plan.plan_id, user_id="u-1", platform="WEB")
    assert cancelled.status is PlanStatus.CANCELLED
    with pytest.raises(RuntimeError):
        service.store.begin_dispatch(
            second.plan.plan_id,
            second.confirmation_token,
            user_id="u-1",
            platform="WEB",
        )


def test_command_parser_is_exact_and_confirmation_returns_dispatch_lease(tmp_path, monkeypatch):
    service = _service(tmp_path, monkeypatch)
    proposal = service.propose(
        "請更新 2026-0005 的案件資料",
        user_id="u-1",
        platform="DISCORD",
    )
    command = parse_controlled_command(
        f"確認自主計畫 {proposal.plan.plan_id} {proposal.confirmation_token}"
    )
    assert command is not None and command.verb == "確認"
    assert parse_controlled_command(f"好 {proposal.plan.plan_id} {proposal.confirmation_token}") is None
    assert parse_controlled_command(f"自主計畫狀態 {proposal.plan.plan_id}").verb == "狀態"
    response, lease = service.command(
        f"確認自主計畫 {proposal.plan.plan_id} {proposal.confirmation_token}",
        user_id="u-1",
        platform="DISCORD",
    )
    assert response == ""
    assert lease is not None and lease.original_request.startswith("請更新")


def test_pipeline_confirmation_replays_only_registered_flow_and_persists_receipt(tmp_path, monkeypatch):
    service = _service(tmp_path, monkeypatch)
    proposal = service.propose(
        "請更新 2026-0007 的案件資料",
        user_id="u-1",
        platform="WEB",
    )
    seen = {}

    def _registered_flow(_orch, _user_id, request, **kwargs):
        seen["request"] = request
        seen["context"] = kwargs["channel_context"]
        return "已進入案件資料更新的專用流程。"

    monkeypatch.setattr(message_pipeline, "process_message_inner", _registered_flow)
    orch = SimpleNamespace(_append_route_trace=lambda *args, **kwargs: None)
    reply = message_pipeline._try_controlled_autonomy_command(
        orch,
        f"確認自主計畫 {proposal.plan.plan_id} {proposal.confirmation_token}",
        user_id="u-1",
        platform="WEB",
        role="user",
        attachment=None,
        correlation_id="cid",
        progress_callback=None,
        channel_context={"topic_key": "general"},
    )
    assert seen["request"] == "請更新 2026-0007 的案件資料"
    assert seen["context"]["_controlled_autonomy_plan_id"] == proposal.plan.plan_id
    assert "已完成安全交接" in reply
    record = service.store.get(proposal.plan.plan_id, user_id="u-1", platform="WEB")
    assert record["status"] == "succeeded"
    assert record["receipt"]["business_completion_attested"] is False


def test_interrupted_dispatch_is_reconciled_without_automatic_replay(tmp_path, monkeypatch):
    service = _service(tmp_path, monkeypatch)
    proposal = service.propose(
        "請更新 2026-0008 的案件資料",
        user_id="u-1",
        platform="WEB",
    )
    service.store.begin_dispatch(
        proposal.plan.plan_id,
        proposal.confirmation_token,
        user_id="u-1",
        platform="WEB",
    )
    with sqlite3.connect(service.store.path) as connection:
        connection.execute(
            "UPDATE controlled_plans SET updated_at=? WHERE plan_id=?",
            ((datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat(), proposal.plan.plan_id),
        )

    restarted = ControlledAutonomyStore(service.store.path)
    record = restarted.get(proposal.plan.plan_id, user_id="u-1", platform="WEB")
    assert record["status"] == "failed"
    assert record["replay_count"] == 1
    assert record["receipt"]["recovery_action"] == "verify_registered_workflow_state_before_retry"
    assert restarted.health_snapshot()["stale_running"] == 0


def test_ordinary_message_does_not_open_controlled_autonomy_database(monkeypatch):
    import api.agentic.control as control

    class _MustNotConstruct:
        def __init__(self):
            raise AssertionError("ordinary messages must not open the plan database")

    monkeypatch.setattr(control, "ControlledAutonomyService", _MustNotConstruct)
    reply = message_pipeline._try_controlled_autonomy_command(
        SimpleNamespace(),
        "今天過得怎麼樣？",
        user_id="u-1",
        platform="WEB",
        role="user",
        attachment=None,
        correlation_id="cid",
        progress_callback=None,
        channel_context=None,
    )
    assert reply is None


def test_broad_agent_route_plans_mutation_without_calling_model(tmp_path, monkeypatch):
    _service(tmp_path, monkeypatch)
    orch = SimpleNamespace(_append_route_trace=lambda *args, **kwargs: None)
    reply = _try_agentic_route(
        orch,
        "請刪除 2026-0006 的舊檔案",
        user_id="u-1",
        platform="WEB",
        decision=SimpleNamespace(kind=KIND_AGENT_TASK, confidence=0.99, reason="test"),
    )
    assert "已建立受控自主計畫" in reply
    assert "確認自主計畫 ca-" in reply

    blocked = _try_agentic_route(
        orch,
        "請刪除 2026-0006 的舊檔案",
        user_id="u-1",
        platform="WEB",
        decision=SimpleNamespace(kind=KIND_AGENT_TASK, confidence=0.99, reason="test"),
        controlled_replay=True,
    )
    assert "未找到可安全執行的專用流程" in blocked


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        ("已進入專用流程。", True),
        ("❌ 處理失敗。", False),
        ("❌ 未找到可安全執行的專用流程。", False),
        ("", False),
    ],
)
def test_handoff_reply_success_is_fail_closed(reply, expected):
    assert handoff_reply_succeeded(reply) is expected
