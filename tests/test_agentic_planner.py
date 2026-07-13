from __future__ import annotations

from dataclasses import replace

import pytest

from api.agentic import (
    ConfirmationRequirement,
    IntentEnvelope,
    InvalidTransition,
    MissingField,
    PlanStatus,
    SideEffectLevel,
    StepStatus,
    WorkflowPlan,
    WorkflowStep,
    build_plan,
    cancel_plan,
    confirm_intent,
    confirm_step,
    pending_confirmations,
    ready_steps,
    restore_plan,
    topological_order,
    transition_step,
)


def _intent(**changes):
    values = {"intent": "case.report", "utterance": "整理案件報告", "confidence": 0.9}
    values.update(changes)
    return IntentEnvelope(**values)


def _dag_steps():
    return (
        WorkflowStep("fetch", "case.fetch", side_effect=SideEffectLevel.READ),
        WorkflowStep("analyze", "case.analyze", depends_on=("fetch",)),
        WorkflowStep("render", "report.render", depends_on=("analyze",)),
        WorkflowStep(
            "send",
            "report.send",
            depends_on=("render",),
            side_effect=SideEffectLevel.WRITE,
            confirmation=ConfirmationRequirement(required=True, reason="sends an external message"),
        ),
    )


def test_build_plan_validates_and_stably_sorts_a_dag():
    steps = _dag_steps()
    plan = build_plan(_intent(), (steps[2], steps[0], steps[3], steps[1]), plan_id="plan-1")

    assert plan.status is PlanStatus.READY
    assert [step.step_id for step in topological_order(plan)] == ["fetch", "analyze", "render", "send"]
    assert [step.step_id for step in ready_steps(plan)] == ["fetch"]
    assert plan.max_side_effect is SideEffectLevel.WRITE


def test_plan_rejects_duplicate_unknown_self_and_cyclic_dependencies():
    intent = _intent()
    with pytest.raises(ValueError, match="unique"):
        build_plan(intent, (WorkflowStep("x", "one"), WorkflowStep("x", "two")))
    with pytest.raises(ValueError, match="unknown dependencies"):
        build_plan(intent, (WorkflowStep("x", "one", depends_on=("missing",)),))
    with pytest.raises(ValueError, match="depend on itself"):
        WorkflowStep("x", "one", depends_on=("x",))
    with pytest.raises(ValueError, match="cycle"):
        build_plan(
            intent,
            (
                WorkflowStep("a", "one", depends_on=("b",)),
                WorkflowStep("b", "two", depends_on=("a",)),
            ),
        )


def test_missing_fields_block_all_steps_until_new_intent_is_planned():
    plan = build_plan(
        _intent(missing_fields=(MissingField("case_id", prompt="哪一個案件？"),)),
        _dag_steps(),
        plan_id="plan-input",
    )

    assert plan.status is PlanStatus.AWAITING_INPUT
    assert ready_steps(plan) == ()
    with pytest.raises(InvalidTransition, match="not ready"):
        transition_step(plan, "fetch", StepStatus.RUNNING)


def test_intent_confirmation_gates_whole_plan():
    plan = build_plan(
        _intent(confirmation=ConfirmationRequirement(required=True, reason="user approval")),
        _dag_steps(),
        plan_id="plan-confirm",
    )

    assert plan.status is PlanStatus.AWAITING_CONFIRMATION
    assert pending_confirmations(plan) == ("intent",)

    confirmed = confirm_intent(plan, "intent-ok")
    assert confirmed.status is PlanStatus.READY
    assert confirmed.intent.confirmation.confirmation_id == "intent-ok"
    assert confirmed.revision == 1


def test_step_lifecycle_unlocks_dependencies_and_local_confirmation():
    plan = build_plan(_intent(), _dag_steps(), plan_id="plan-life")

    plan = transition_step(plan, "fetch", StepStatus.RUNNING)
    assert plan.status is PlanStatus.RUNNING
    plan = transition_step(plan, "fetch", StepStatus.SUCCEEDED, output={"case": 42})
    assert [step.step_id for step in ready_steps(plan)] == ["analyze"]

    plan = transition_step(plan, "analyze", StepStatus.RUNNING)
    plan = transition_step(plan, "analyze", StepStatus.SUCCEEDED, output={"risk": "low"})
    plan = transition_step(plan, "render", StepStatus.RUNNING)
    plan = transition_step(plan, "render", StepStatus.SUCCEEDED, output="report.docx")

    assert plan.status is PlanStatus.AWAITING_CONFIRMATION
    assert pending_confirmations(plan) == ("send",)
    assert ready_steps(plan) == ()

    plan = confirm_step(plan, "send", "send-ok")
    assert plan.status is PlanStatus.READY
    plan = transition_step(plan, "send", StepStatus.RUNNING)
    plan = transition_step(plan, "send", StepStatus.SUCCEEDED, output={"message_id": "m-1"})

    assert plan.status is PlanStatus.SUCCEEDED
    assert plan.revision == 9
    assert plan.get_step("fetch").output == {"case": 42}


def test_invalid_transitions_and_payloads_are_rejected():
    plan = build_plan(_intent(), _dag_steps(), plan_id="plan-invalid")

    with pytest.raises(InvalidTransition, match="not ready"):
        transition_step(plan, "analyze", StepStatus.RUNNING)
    with pytest.raises(InvalidTransition, match="cannot transition"):
        transition_step(plan, "fetch", StepStatus.SUCCEEDED)
    with pytest.raises(InvalidTransition, match="only valid for succeeded"):
        transition_step(plan, "fetch", StepStatus.RUNNING, output="early")

    running = transition_step(plan, "fetch", StepStatus.RUNNING)
    with pytest.raises(InvalidTransition, match="requires an error"):
        transition_step(running, "fetch", StepStatus.FAILED)
    with pytest.raises(InvalidTransition, match="only valid for failed or cancelled"):
        transition_step(running, "fetch", StepStatus.SUCCEEDED, error="wrong")


def test_failed_step_fails_plan_and_retains_error():
    plan = build_plan(_intent(), _dag_steps(), plan_id="plan-fail")
    plan = transition_step(plan, "fetch", StepStatus.RUNNING)
    plan = transition_step(plan, "fetch", StepStatus.FAILED, error="case service unavailable")

    assert plan.status is PlanStatus.FAILED
    assert plan.get_step("fetch").error == "case service unavailable"
    assert ready_steps(plan) == ()


def test_skip_unlocks_dependency_and_cancel_is_terminal():
    intent = _intent()
    plan = build_plan(
        intent,
        (
            WorkflowStep("optional", "lookup.optional"),
            WorkflowStep("final", "answer.compose", depends_on=("optional",)),
        ),
        plan_id="plan-skip",
    )
    plan = transition_step(plan, "optional", StepStatus.SKIPPED)
    assert [step.step_id for step in ready_steps(plan)] == ["final"]

    cancelled = cancel_plan(plan)
    assert cancelled.status is PlanStatus.CANCELLED
    assert all(step.status.terminal for step in cancelled.steps)
    assert cancel_plan(cancelled) is cancelled


def test_completed_plan_cannot_be_cancelled():
    plan = build_plan(_intent(), (), plan_id="empty")
    assert plan.status is PlanStatus.SUCCEEDED
    with pytest.raises(InvalidTransition, match="cannot cancel"):
        cancel_plan(plan)


def test_plan_json_round_trip_and_strict_restore():
    plan = build_plan(_intent(), _dag_steps(), plan_id="plan-json", metadata={"owner": "route"})
    plan = transition_step(plan, "fetch", StepStatus.RUNNING)
    payload = plan.to_json()

    assert WorkflowPlan.from_json(payload) == plan
    assert restore_plan(payload) == plan

    inconsistent = replace(plan, status=PlanStatus.READY)
    with pytest.raises(ValueError, match="inconsistent"):
        restore_plan(inconsistent.to_dict())


def test_failed_step_contract_requires_error():
    with pytest.raises(ValueError, match="must include an error"):
        WorkflowStep("x", "action", status=StepStatus.FAILED)
