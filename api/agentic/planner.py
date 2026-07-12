"""Deterministic DAG planning and immutable workflow state transitions."""

from __future__ import annotations

import uuid
from dataclasses import replace
from typing import Any, Iterable, Mapping

from api.agentic.contracts import (
    IntentEnvelope,
    PlanStatus,
    StepStatus,
    WorkflowPlan,
    WorkflowStep,
)


class InvalidTransition(ValueError):
    """Raised when a requested workflow transition violates the state machine."""


_ALLOWED_TRANSITIONS = {
    StepStatus.PENDING: {StepStatus.RUNNING, StepStatus.SKIPPED, StepStatus.CANCELLED},
    StepStatus.RUNNING: {StepStatus.SUCCEEDED, StepStatus.FAILED, StepStatus.CANCELLED},
    StepStatus.SUCCEEDED: set(),
    StepStatus.FAILED: set(),
    StepStatus.SKIPPED: set(),
    StepStatus.CANCELLED: set(),
}
_UNSET = object()


def topological_order(steps_or_plan: Iterable[WorkflowStep] | WorkflowPlan) -> tuple[WorkflowStep, ...]:
    """Return stable topological order, preserving declaration order for ties."""

    steps = steps_or_plan.steps if isinstance(steps_or_plan, WorkflowPlan) else tuple(steps_or_plan)
    by_id = {step.step_id: step for step in steps}
    if len(by_id) != len(steps):
        raise ValueError("workflow step IDs must be unique")
    unknown = {
        dependency
        for step in steps
        for dependency in step.depends_on
        if dependency not in by_id
    }
    if unknown:
        raise ValueError(f"workflow has unknown dependencies: {sorted(unknown)!r}")
    remaining = {step.step_id: set(step.depends_on) for step in steps}
    ordered: list[WorkflowStep] = []
    while remaining:
        ready_ids = [step.step_id for step in steps if step.step_id in remaining and not remaining[step.step_id]]
        if not ready_ids:
            raise ValueError("workflow dependencies contain a cycle")
        for step_id in ready_ids:
            ordered.append(by_id[step_id])
            remaining.pop(step_id)
        for dependencies in remaining.values():
            dependencies.difference_update(ready_ids)
    return tuple(ordered)


def ready_steps(plan: WorkflowPlan) -> tuple[WorkflowStep, ...]:
    if plan.status.terminal or not plan.intent.complete or plan.intent.confirmation.pending:
        return ()
    by_id = plan.step_map
    ready: list[WorkflowStep] = []
    for step in topological_order(plan):
        if step.status is not StepStatus.PENDING or step.confirmation.pending:
            continue
        if all(by_id[dependency].status in {StepStatus.SUCCEEDED, StepStatus.SKIPPED} for dependency in step.depends_on):
            ready.append(step)
    return tuple(ready)


def pending_confirmations(plan: WorkflowPlan) -> tuple[str, ...]:
    """Return confirmation IDs currently blocking execution."""

    pending: list[str] = []
    if plan.intent.confirmation.pending:
        pending.append("intent")
        return tuple(pending)
    by_id = plan.step_map
    for step in plan.steps:
        dependencies_done = all(
            by_id[dependency].status in {StepStatus.SUCCEEDED, StepStatus.SKIPPED}
            for dependency in step.depends_on
        )
        if step.status is StepStatus.PENDING and dependencies_done and step.confirmation.pending:
            pending.append(step.step_id)
    return tuple(pending)


def derive_plan_status(plan: WorkflowPlan) -> PlanStatus:
    if plan.status is PlanStatus.CANCELLED:
        return PlanStatus.CANCELLED
    if not plan.intent.complete:
        return PlanStatus.AWAITING_INPUT
    if plan.intent.confirmation.pending:
        return PlanStatus.AWAITING_CONFIRMATION
    if any(step.status is StepStatus.FAILED for step in plan.steps):
        return PlanStatus.FAILED
    if not plan.steps:
        return PlanStatus.SUCCEEDED
    if all(step.status in {StepStatus.SUCCEEDED, StepStatus.SKIPPED} for step in plan.steps):
        return PlanStatus.SUCCEEDED
    if all(step.status.terminal for step in plan.steps):
        return PlanStatus.CANCELLED
    if any(step.status is StepStatus.RUNNING for step in plan.steps):
        return PlanStatus.RUNNING
    if pending_confirmations(plan):
        return PlanStatus.AWAITING_CONFIRMATION
    if ready_steps(replace(plan, status=PlanStatus.DRAFT)):
        return PlanStatus.READY
    return PlanStatus.BLOCKED


def build_plan(
    intent: IntentEnvelope,
    steps: Iterable[WorkflowStep],
    *,
    plan_id: str = "",
    metadata: Mapping[str, Any] | None = None,
) -> WorkflowPlan:
    resolved_id = str(plan_id or (f"workflow-{intent.request_id}" if intent.request_id else uuid.uuid4().hex))
    draft = WorkflowPlan(
        plan_id=resolved_id,
        intent=intent,
        steps=tuple(steps),
        metadata=dict(metadata or {}),
    )
    topological_order(draft)
    return replace(draft, status=derive_plan_status(draft))


def confirm_intent(plan: WorkflowPlan, confirmation_id: str = "") -> WorkflowPlan:
    confirmation = plan.intent.confirmation.confirm(confirmation_id)
    if confirmation is plan.intent.confirmation:
        return plan
    updated = replace(
        plan,
        intent=replace(plan.intent, confirmation=confirmation),
        revision=plan.revision + 1,
        status=PlanStatus.DRAFT,
    )
    return replace(updated, status=derive_plan_status(updated))


def confirm_step(plan: WorkflowPlan, step_id: str, confirmation_id: str = "") -> WorkflowPlan:
    step = plan.get_step(step_id)
    confirmation = step.confirmation.confirm(confirmation_id)
    if confirmation is step.confirmation:
        return plan
    updated_step = replace(step, confirmation=confirmation)
    updated = _replace_step(plan, updated_step, status=PlanStatus.DRAFT)
    return replace(updated, status=derive_plan_status(updated))


def transition_step(
    plan: WorkflowPlan,
    step_id: str,
    status: StepStatus | str,
    *,
    output: Any = _UNSET,
    error: str = "",
) -> WorkflowPlan:
    target = StepStatus(status)
    step = plan.get_step(step_id)
    if target is step.status:
        return plan
    if target not in _ALLOWED_TRANSITIONS[step.status]:
        raise InvalidTransition(f"cannot transition step {step_id!r} from {step.status.value} to {target.value}")
    if target is StepStatus.RUNNING:
        ready_ids = {item.step_id for item in ready_steps(plan)}
        if step_id not in ready_ids:
            raise InvalidTransition(f"step {step_id!r} is not ready")
    if target is StepStatus.FAILED and not str(error).strip():
        raise InvalidTransition("failed transition requires an error")
    if target not in {StepStatus.FAILED, StepStatus.CANCELLED} and str(error).strip():
        raise InvalidTransition("error is only valid for failed or cancelled transitions")
    if output is not _UNSET and target is not StepStatus.SUCCEEDED:
        raise InvalidTransition("output is only valid for succeeded transitions")

    changes: dict[str, Any] = {"status": target, "error": str(error)}
    if output is not _UNSET:
        changes["output"] = output
    updated = _replace_step(plan, replace(step, **changes), status=PlanStatus.DRAFT)
    return replace(updated, status=derive_plan_status(updated))


def cancel_plan(plan: WorkflowPlan) -> WorkflowPlan:
    if plan.status.terminal:
        if plan.status is PlanStatus.CANCELLED:
            return plan
        raise InvalidTransition(f"cannot cancel a {plan.status.value} plan")
    steps = tuple(
        replace(step, status=StepStatus.CANCELLED)
        if step.status in {StepStatus.PENDING, StepStatus.RUNNING}
        else step
        for step in plan.steps
    )
    return replace(plan, steps=steps, status=PlanStatus.CANCELLED, revision=plan.revision + 1)


def reconcile_task_record(plan: WorkflowPlan, record: Any) -> WorkflowPlan:
    """Merge an api.tasks TaskRecord snapshot back into its workflow step."""

    projected = WorkflowStep.from_task_record(record)
    current = plan.get_step(projected.step_id)
    if projected.action != current.action or projected.depends_on != current.depends_on:
        raise ValueError("task record does not match the workflow step contract")
    merged = replace(
        current,
        status=projected.status,
        output=projected.output,
        error=projected.error,
    )
    updated = _replace_step(plan, merged, status=PlanStatus.DRAFT)
    return replace(updated, status=derive_plan_status(updated))


def restore_plan(payload: str | Mapping[str, Any]) -> WorkflowPlan:
    plan = WorkflowPlan.from_json(payload) if isinstance(payload, str) else WorkflowPlan.from_dict(payload)
    derived = derive_plan_status(replace(plan, status=PlanStatus.DRAFT))
    if plan.status is PlanStatus.CANCELLED:
        derived = PlanStatus.CANCELLED
    if plan.status is not derived:
        raise ValueError(
            f"stored plan status {plan.status.value!r} is inconsistent with derived status {derived.value!r}"
        )
    return plan


def _replace_step(plan: WorkflowPlan, replacement: WorkflowStep, *, status: PlanStatus) -> WorkflowPlan:
    steps = tuple(replacement if step.step_id == replacement.step_id else step for step in plan.steps)
    return replace(plan, steps=steps, status=status, revision=plan.revision + 1)


class WorkflowPlanner:
    """Small facade suitable for injection into existing routes."""

    build = staticmethod(build_plan)
    ready_steps = staticmethod(ready_steps)
    topological_order = staticmethod(topological_order)
    pending_confirmations = staticmethod(pending_confirmations)
    confirm_intent = staticmethod(confirm_intent)
    confirm_step = staticmethod(confirm_step)
    transition_step = staticmethod(transition_step)
    cancel = staticmethod(cancel_plan)
    reconcile_task_record = staticmethod(reconcile_task_record)
    restore = staticmethod(restore_plan)
