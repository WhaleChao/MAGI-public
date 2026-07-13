from __future__ import annotations

from api.agentic import (
    ConfirmationRequirement,
    Entity,
    IntentEnvelope,
    PlanStatus,
    SideEffectLevel,
    StepStatus,
    WorkflowStep,
    build_plan,
    reconcile_task_record,
)
from api.routing import RoutingContext, RoutingDecision
from api.tasks import TaskRuntime, TaskStatus


def test_intent_envelope_round_trips_routing_context_and_decision():
    context = RoutingContext(
        user_id="u-1",
        platform="line",
        role="admin",
        message="幫我查案件",
        intent="QUERY",
        correlation_id="corr-1",
        confidence=0.7,
        matched_skill="case_query",
        method="semantic",
        channel_context={"group_id": "g-1"},
        attachment_type="pdf",
        extra={"tenant": "t-1"},
    )
    decision = RoutingDecision(
        action="dispatch",
        matched="case_query",
        handler="case_handler",
        confidence=0.91,
        reason="semantic match",
        intent="QUERY",
        candidates=({"name": "case_query", "score": 0.91},),
        route_context=context,
        trace=({"stage": "semantic"},),
    )

    envelope = IntentEnvelope.from_routing(
        context,
        decision,
        entities=(Entity("case_id", "2026-123"),),
        side_effect=SideEffectLevel.READ,
    )

    assert envelope.intent == "QUERY"
    assert envelope.confidence == 0.91
    assert envelope.request_id == "corr-1"
    assert envelope.routing_decision["matched"] == "case_query"

    restored_context = envelope.to_routing_context()
    restored_decision = envelope.to_routing_decision(context=restored_context)
    assert restored_context.as_dict() == context.as_dict() | {"confidence": 0.91}
    assert restored_decision.matched == "case_query"
    assert restored_decision.handler == "case_handler"
    assert restored_decision.route_context == restored_context
    assert restored_decision.trace == ({"stage": "semantic"},)


def test_routing_dicts_are_supported_for_incremental_legacy_adoption():
    envelope = IntentEnvelope.from_routing(
        {
            "message": "legacy request",
            "intent": "CMD",
            "confidence": 0.6,
            "correlation_id": "legacy-1",
        },
        {
            "action": "dispatch",
            "matched": "legacy_skill",
            "confidence": 0.8,
            "intent": "CMD",
        },
    )

    assert envelope.utterance == "legacy request"
    assert envelope.confidence == 0.8
    assert envelope.to_routing_decision().matched == "legacy_skill"


def test_workflow_projects_to_task_records_without_runtime_registration():
    plan = build_plan(
        IntentEnvelope(intent="report.create", confidence=0.9),
        (
            WorkflowStep("collect", "report.collect", inputs={"case_id": "C-1"}),
            WorkflowStep(
                "publish",
                "report.publish",
                depends_on=("collect",),
                side_effect=SideEffectLevel.WRITE,
                confirmation=ConfirmationRequirement(required=True, reason="publishes report"),
                metadata={"owner": "documents"},
            ),
        ),
        plan_id="plan-tasks",
    )

    records = plan.to_task_records()

    assert [record.task_id for record in records] == ["plan-tasks/collect", "plan-tasks/publish"]
    assert all(record.status is TaskStatus.PENDING for record in records)
    assert records[1].metadata["agentic"]["depends_on"] == ["collect"]
    assert records[1].metadata["agentic"]["side_effect"] == "write"
    assert records[1].metadata["agentic"]["step_metadata"] == {"owner": "documents"}


def test_task_record_round_trip_preserves_step_contract_and_status():
    original = WorkflowStep(
        "write",
        "document.write",
        description="Write generated document",
        depends_on=("render",),
        status=StepStatus.SUCCEEDED,
        side_effect=SideEffectLevel.WRITE,
        inputs={"path": "out.docx"},
        output={"bytes": 120},
        metadata={"agent": "writer"},
    )

    record = original.to_task_record("plan-8")
    restored = WorkflowStep.from_task_record(record)

    assert record.status is TaskStatus.COMPLETED
    assert record.progress == 1.0
    assert restored == original


def test_skipped_step_survives_task_projection():
    step = WorkflowStep("optional", "optional.lookup", status=StepStatus.SKIPPED)
    restored = WorkflowStep.from_task_record(step.to_task_record("p"))
    assert restored.status is StepStatus.SKIPPED


def test_reconcile_task_record_updates_plan_snapshot_purely():
    plan = build_plan(
        IntentEnvelope(intent="lookup", confidence=1.0),
        (WorkflowStep("lookup", "case.lookup"),),
        plan_id="plan-reconcile",
    )
    record = plan.to_task_records()[0]
    record.status = TaskStatus.COMPLETED
    record.progress = 1.0
    record.result = {"count": 3}

    updated = reconcile_task_record(plan, record)

    assert plan.status is PlanStatus.READY
    assert plan.get_step("lookup").status is StepStatus.PENDING
    assert updated.status is PlanStatus.SUCCEEDED
    assert updated.get_step("lookup").output == {"count": 3}
    assert updated.revision == 1


def test_task_record_without_agentic_metadata_is_rejected():
    from api.tasks import TaskRecord

    record = TaskRecord(task_id="plain", name="plain")
    try:
        WorkflowStep.from_task_record(record)
    except ValueError as exc:
        assert "agentic metadata" in str(exc)
    else:
        raise AssertionError("expected a bridge validation error")


def test_cancelled_task_runtime_record_preserves_reason():
    step = WorkflowStep("lookup", "case.lookup")
    projected = step.to_task_record("plan-cancel")
    runtime = TaskRuntime()
    runtime.register(
        projected.task_id,
        projected.name,
        description=projected.description,
        metadata=projected.metadata,
    )

    cancelled = runtime.cancel(projected.task_id, reason="user cancelled")
    restored = WorkflowStep.from_task_record(cancelled)

    assert restored.status is StepStatus.CANCELLED
    assert restored.error == "user cancelled"
