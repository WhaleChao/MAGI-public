"""Side-effect-free contracts for agentic intent and workflow orchestration."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Iterable, Mapping


SCHEMA_VERSION = 1


class SideEffectLevel(str, Enum):
    """Highest external effect an intent or workflow step may produce."""

    NONE = "none"
    READ = "read"
    WRITE = "write"
    DESTRUCTIVE = "destructive"

    @property
    def rank(self) -> int:
        return tuple(SideEffectLevel).index(self)


class StepStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"

    @property
    def terminal(self) -> bool:
        return self in {
            StepStatus.SUCCEEDED,
            StepStatus.FAILED,
            StepStatus.SKIPPED,
            StepStatus.CANCELLED,
        }


class PlanStatus(str, Enum):
    DRAFT = "draft"
    AWAITING_INPUT = "awaiting_input"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    READY = "ready"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"

    @property
    def terminal(self) -> bool:
        return self in {PlanStatus.SUCCEEDED, PlanStatus.FAILED, PlanStatus.CANCELLED}


def _nonempty(value: Any, field_name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field_name} must not be empty")
    return text


def _confidence(value: Any, field_name: str = "confidence") -> float:
    if isinstance(value, bool):
        raise TypeError(f"{field_name} must be a number")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise TypeError(f"{field_name} must be a number") from exc
    if not math.isfinite(number) or not 0.0 <= number <= 1.0:
        raise ValueError(f"{field_name} must be between 0.0 and 1.0")
    return number


def _json_safe(value: Any, field_name: str) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{field_name} must not contain non-finite numbers")
        return value
    if isinstance(value, (list, tuple)):
        return [_json_safe(item, field_name) for item in value]
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(f"{field_name} keys must be strings")
            result[key] = _json_safe(item, field_name)
        return result
    raise TypeError(f"{field_name} must contain only JSON-compatible values")


def _mapping(value: Any, field_name: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise TypeError(f"{field_name} must be a mapping")
    return _json_safe(value, field_name)


def _enum(enum_type: type[Enum], value: Any, field_name: str) -> Any:
    try:
        return enum_type(value)
    except (TypeError, ValueError) as exc:
        allowed = ", ".join(str(item.value) for item in enum_type)
        raise ValueError(f"{field_name} must be one of: {allowed}") from exc


@dataclass(frozen=True, slots=True)
class Entity:
    name: str
    value: Any
    kind: str = "text"
    confidence: float = 1.0
    source: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _nonempty(self.name, "entity.name"))
        object.__setattr__(self, "kind", _nonempty(self.kind, "entity.kind"))
        object.__setattr__(self, "value", _json_safe(self.value, "entity.value"))
        object.__setattr__(self, "confidence", _confidence(self.confidence, "entity.confidence"))
        object.__setattr__(self, "metadata", _mapping(self.metadata, "entity.metadata"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "value": self.value,
            "kind": self.kind,
            "confidence": self.confidence,
            "source": self.source,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Entity:
        return cls(
            name=data.get("name", ""),
            value=data.get("value"),
            kind=data.get("kind", "text"),
            confidence=data.get("confidence", 1.0),
            source=str(data.get("source", "")),
            metadata=data.get("metadata", {}),
        )


@dataclass(frozen=True, slots=True)
class Constraint:
    name: str
    value: Any
    operator: str = "eq"
    required: bool = True
    description: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _nonempty(self.name, "constraint.name"))
        object.__setattr__(self, "operator", _nonempty(self.operator, "constraint.operator"))
        object.__setattr__(self, "value", _json_safe(self.value, "constraint.value"))
        object.__setattr__(self, "required", bool(self.required))
        object.__setattr__(self, "metadata", _mapping(self.metadata, "constraint.metadata"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "value": self.value,
            "operator": self.operator,
            "required": self.required,
            "description": self.description,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Constraint:
        return cls(
            name=data.get("name", ""),
            value=data.get("value"),
            operator=data.get("operator", "eq"),
            required=data.get("required", True),
            description=str(data.get("description", "")),
            metadata=data.get("metadata", {}),
        )


@dataclass(frozen=True, slots=True)
class MissingField:
    name: str
    reason: str = "required"
    prompt: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _nonempty(self.name, "missing_field.name"))
        object.__setattr__(self, "reason", _nonempty(self.reason, "missing_field.reason"))

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, "reason": self.reason, "prompt": self.prompt}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> MissingField:
        return cls(
            name=data.get("name", ""),
            reason=data.get("reason", "required"),
            prompt=str(data.get("prompt", "")),
        )


@dataclass(frozen=True, slots=True)
class ConfirmationRequirement:
    required: bool = False
    reason: str = ""
    prompt: str = ""
    confirmed: bool = False
    confirmation_id: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "required", bool(self.required))
        object.__setattr__(self, "confirmed", bool(self.confirmed))
        if self.confirmed and not self.required:
            raise ValueError("confirmation cannot be confirmed when it is not required")
        if self.required and not str(self.reason).strip():
            raise ValueError("confirmation.reason is required when confirmation is required")

    @property
    def pending(self) -> bool:
        return self.required and not self.confirmed

    def confirm(self, confirmation_id: str = "") -> ConfirmationRequirement:
        if not self.required:
            return self
        return replace(self, confirmed=True, confirmation_id=str(confirmation_id or self.confirmation_id))

    def to_dict(self) -> dict[str, Any]:
        return {
            "required": self.required,
            "reason": self.reason,
            "prompt": self.prompt,
            "confirmed": self.confirmed,
            "confirmation_id": self.confirmation_id,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> ConfirmationRequirement:
        data = data or {}
        return cls(
            required=data.get("required", False),
            reason=str(data.get("reason", "")),
            prompt=str(data.get("prompt", "")),
            confirmed=data.get("confirmed", False),
            confirmation_id=str(data.get("confirmation_id", "")),
        )


@dataclass(frozen=True, slots=True)
class IntentEnvelope:
    """Normalized intent plus enough routing data for gradual adoption."""

    intent: str
    utterance: str = ""
    entities: tuple[Entity, ...] = ()
    constraints: tuple[Constraint, ...] = ()
    missing_fields: tuple[MissingField, ...] = ()
    confidence: float = 0.0
    side_effect: SideEffectLevel = SideEffectLevel.NONE
    confirmation: ConfirmationRequirement = field(default_factory=ConfirmationRequirement)
    request_id: str = ""
    routing_context: dict[str, Any] = field(default_factory=dict)
    routing_decision: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError(f"unsupported intent schema version: {self.schema_version}")
        object.__setattr__(self, "intent", _nonempty(self.intent, "intent"))
        object.__setattr__(self, "entities", tuple(self.entities))
        object.__setattr__(self, "constraints", tuple(self.constraints))
        object.__setattr__(self, "missing_fields", tuple(self.missing_fields))
        if not all(isinstance(item, Entity) for item in self.entities):
            raise TypeError("entities must contain Entity instances")
        if not all(isinstance(item, Constraint) for item in self.constraints):
            raise TypeError("constraints must contain Constraint instances")
        if not all(isinstance(item, MissingField) for item in self.missing_fields):
            raise TypeError("missing_fields must contain MissingField instances")
        object.__setattr__(self, "confidence", _confidence(self.confidence))
        object.__setattr__(self, "side_effect", _enum(SideEffectLevel, self.side_effect, "side_effect"))
        if not isinstance(self.confirmation, ConfirmationRequirement):
            raise TypeError("confirmation must be a ConfirmationRequirement")
        object.__setattr__(self, "routing_context", _mapping(self.routing_context, "routing_context"))
        object.__setattr__(self, "routing_decision", _mapping(self.routing_decision, "routing_decision"))
        object.__setattr__(self, "metadata", _mapping(self.metadata, "metadata"))
        names = [item.name for item in self.missing_fields]
        if len(names) != len(set(names)):
            raise ValueError("missing_fields must have unique names")

    @property
    def complete(self) -> bool:
        return not self.missing_fields

    @property
    def requires_confirmation(self) -> bool:
        return self.confirmation.pending

    def entities_named(self, name: str) -> tuple[Entity, ...]:
        return tuple(entity for entity in self.entities if entity.name == name)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "intent": self.intent,
            "utterance": self.utterance,
            "entities": [item.to_dict() for item in self.entities],
            "constraints": [item.to_dict() for item in self.constraints],
            "missing_fields": [item.to_dict() for item in self.missing_fields],
            "confidence": self.confidence,
            "side_effect": self.side_effect.value,
            "confirmation": self.confirmation.to_dict(),
            "request_id": self.request_id,
            "routing_context": dict(self.routing_context),
            "routing_decision": dict(self.routing_decision),
            "metadata": dict(self.metadata),
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> IntentEnvelope:
        if not isinstance(data, Mapping):
            raise TypeError("intent envelope payload must be a mapping")
        return cls(
            schema_version=data.get("schema_version", SCHEMA_VERSION),
            intent=data.get("intent", ""),
            utterance=str(data.get("utterance", "")),
            entities=tuple(Entity.from_dict(item) for item in data.get("entities", ())),
            constraints=tuple(Constraint.from_dict(item) for item in data.get("constraints", ())),
            missing_fields=tuple(MissingField.from_dict(item) for item in data.get("missing_fields", ())),
            confidence=data.get("confidence", 0.0),
            side_effect=data.get("side_effect", SideEffectLevel.NONE.value),
            confirmation=ConfirmationRequirement.from_dict(data.get("confirmation")),
            request_id=str(data.get("request_id", "")),
            routing_context=data.get("routing_context", {}),
            routing_decision=data.get("routing_decision", {}),
            metadata=data.get("metadata", {}),
        )

    @classmethod
    def from_json(cls, payload: str) -> IntentEnvelope:
        data = json.loads(payload)
        return cls.from_dict(data)

    @classmethod
    def from_routing(
        cls,
        context: Any,
        decision: Any | None = None,
        **overrides: Any,
    ) -> IntentEnvelope:
        """Create an envelope from RoutingContext and an optional RoutingDecision."""

        context_data = context.as_dict() if hasattr(context, "as_dict") else _mapping(context, "context")
        if decision is None:
            decision_data: dict[str, Any] = {}
        elif hasattr(decision, "as_dict"):
            decision_data = decision.as_dict()
        elif hasattr(decision, "to_legacy_dict"):
            decision_data = decision.to_legacy_dict()
        else:
            decision_data = _mapping(decision, "decision")
        defaults = {
            "intent": decision_data.get("intent") or context_data.get("intent") or "unknown",
            "utterance": context_data.get("message", ""),
            "confidence": decision_data.get("confidence", context_data.get("confidence", 0.0)),
            "request_id": context_data.get("correlation_id", ""),
            "routing_context": context_data,
            "routing_decision": decision_data,
        }
        defaults.update(overrides)
        return cls(**defaults)

    def to_routing_context(self) -> Any:
        from api.routing.context import RoutingContext

        fields = RoutingContext.__dataclass_fields__
        payload = {key: value for key, value in self.routing_context.items() if key in fields}
        payload.update(
            message=self.utterance,
            intent=self.intent,
            confidence=self.confidence,
        )
        if self.request_id:
            payload["correlation_id"] = self.request_id
        return RoutingContext(**payload)

    def to_routing_decision(self, *, context: Any | None = None) -> Any:
        from api.routing.models import RoutingDecision

        data = self.routing_decision
        return RoutingDecision(
            action=str(data.get("action", "")),
            matched=str(data.get("matched", "")),
            handler=str(data.get("handler", "")),
            confidence=self.confidence,
            reason=str(data.get("reason", "")),
            intent=self.intent,
            candidates=tuple(data.get("candidates", ())),
            route_context=context or (self.to_routing_context() if self.routing_context else None),
            trace=tuple(data.get("trace", ())),
        )


@dataclass(frozen=True, slots=True)
class WorkflowStep:
    step_id: str
    action: str
    description: str = ""
    depends_on: tuple[str, ...] = ()
    status: StepStatus = StepStatus.PENDING
    side_effect: SideEffectLevel = SideEffectLevel.NONE
    confirmation: ConfirmationRequirement = field(default_factory=ConfirmationRequirement)
    inputs: dict[str, Any] = field(default_factory=dict)
    output: Any = None
    error: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "step_id", _nonempty(self.step_id, "step_id"))
        object.__setattr__(self, "action", _nonempty(self.action, "action"))
        dependencies = tuple(_nonempty(item, "depends_on item") for item in self.depends_on)
        if self.step_id in dependencies:
            raise ValueError(f"step {self.step_id!r} cannot depend on itself")
        if len(dependencies) != len(set(dependencies)):
            raise ValueError(f"step {self.step_id!r} has duplicate dependencies")
        object.__setattr__(self, "depends_on", dependencies)
        object.__setattr__(self, "status", _enum(StepStatus, self.status, "status"))
        object.__setattr__(self, "side_effect", _enum(SideEffectLevel, self.side_effect, "side_effect"))
        if not isinstance(self.confirmation, ConfirmationRequirement):
            raise TypeError("confirmation must be a ConfirmationRequirement")
        object.__setattr__(self, "inputs", _mapping(self.inputs, "inputs"))
        object.__setattr__(self, "output", _json_safe(self.output, "output"))
        object.__setattr__(self, "metadata", _mapping(self.metadata, "metadata"))
        if self.status is StepStatus.FAILED and not self.error.strip():
            raise ValueError("failed step must include an error")
        if self.status not in {StepStatus.FAILED, StepStatus.CANCELLED} and self.error.strip():
            raise ValueError("only failed or cancelled steps may include an error")

    @property
    def terminal(self) -> bool:
        return self.status.terminal

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "action": self.action,
            "description": self.description,
            "depends_on": list(self.depends_on),
            "status": self.status.value,
            "side_effect": self.side_effect.value,
            "confirmation": self.confirmation.to_dict(),
            "inputs": dict(self.inputs),
            "output": self.output,
            "error": self.error,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> WorkflowStep:
        return cls(
            step_id=data.get("step_id", ""),
            action=data.get("action", ""),
            description=str(data.get("description", "")),
            depends_on=tuple(data.get("depends_on", ())),
            status=data.get("status", StepStatus.PENDING.value),
            side_effect=data.get("side_effect", SideEffectLevel.NONE.value),
            confirmation=ConfirmationRequirement.from_dict(data.get("confirmation")),
            inputs=data.get("inputs", {}),
            output=data.get("output"),
            error=str(data.get("error", "")),
            metadata=data.get("metadata", {}),
        )

    def to_task_record(self, plan_id: str) -> Any:
        """Project this step into api.tasks without registering or executing it."""

        from api.tasks.models import TaskRecord, TaskStatus

        status_map = {
            StepStatus.PENDING: TaskStatus.PENDING,
            StepStatus.RUNNING: TaskStatus.RUNNING,
            StepStatus.SUCCEEDED: TaskStatus.COMPLETED,
            StepStatus.FAILED: TaskStatus.FAILED,
            StepStatus.SKIPPED: TaskStatus.COMPLETED,
            StepStatus.CANCELLED: TaskStatus.CANCELLED,
        }
        agentic = {
            "plan_id": plan_id,
            "step_id": self.step_id,
            "depends_on": list(self.depends_on),
            "side_effect": self.side_effect.value,
            "confirmation": self.confirmation.to_dict(),
            "inputs": dict(self.inputs),
            "step_metadata": dict(self.metadata),
            "skipped": self.status is StepStatus.SKIPPED,
        }
        return TaskRecord(
            task_id=f"{plan_id}/{self.step_id}",
            name=self.action,
            description=self.description,
            status=status_map[self.status],
            progress=1.0 if self.status in {StepStatus.SUCCEEDED, StepStatus.SKIPPED} else 0.0,
            result=self.output,
            error=self.error,
            metadata={"agentic": agentic},
        )

    @classmethod
    def from_task_record(cls, record: Any) -> WorkflowStep:
        from api.tasks.models import TaskStatus

        metadata = getattr(record, "metadata", {}) or {}
        agentic = metadata.get("agentic", {}) if isinstance(metadata, Mapping) else {}
        if not agentic:
            raise ValueError("task record does not contain agentic metadata")
        task_status = TaskStatus(getattr(record, "status"))
        if task_status is TaskStatus.COMPLETED and agentic.get("skipped"):
            status = StepStatus.SKIPPED
        else:
            status = {
                TaskStatus.PENDING: StepStatus.PENDING,
                TaskStatus.RUNNING: StepStatus.RUNNING,
                TaskStatus.COMPLETED: StepStatus.SUCCEEDED,
                TaskStatus.FAILED: StepStatus.FAILED,
                TaskStatus.CANCELLED: StepStatus.CANCELLED,
            }[task_status]
        return cls(
            step_id=agentic.get("step_id", ""),
            action=getattr(record, "name", ""),
            description=getattr(record, "description", ""),
            depends_on=tuple(agentic.get("depends_on", ())),
            status=status,
            side_effect=agentic.get("side_effect", SideEffectLevel.NONE.value),
            confirmation=ConfirmationRequirement.from_dict(agentic.get("confirmation")),
            inputs=agentic.get("inputs", {}),
            output=getattr(record, "result", None),
            error=str(getattr(record, "error", "")),
            metadata=agentic.get("step_metadata", {}),
        )


def _validate_dag(steps: Iterable[WorkflowStep]) -> None:
    step_tuple = tuple(steps)
    ids = [step.step_id for step in step_tuple]
    if len(ids) != len(set(ids)):
        raise ValueError("workflow step IDs must be unique")
    known = set(ids)
    for step in step_tuple:
        unknown = set(step.depends_on) - known
        if unknown:
            raise ValueError(f"step {step.step_id!r} has unknown dependencies: {sorted(unknown)!r}")

    visiting: set[str] = set()
    visited: set[str] = set()
    graph = {step.step_id: step.depends_on for step in step_tuple}

    def visit(step_id: str) -> None:
        if step_id in visiting:
            raise ValueError("workflow dependencies contain a cycle")
        if step_id in visited:
            return
        visiting.add(step_id)
        for dependency in graph[step_id]:
            visit(dependency)
        visiting.remove(step_id)
        visited.add(step_id)

    for step_id in ids:
        visit(step_id)


@dataclass(frozen=True, slots=True)
class WorkflowPlan:
    plan_id: str
    intent: IntentEnvelope
    steps: tuple[WorkflowStep, ...]
    status: PlanStatus = PlanStatus.DRAFT
    revision: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError(f"unsupported workflow schema version: {self.schema_version}")
        object.__setattr__(self, "plan_id", _nonempty(self.plan_id, "plan_id"))
        if not isinstance(self.intent, IntentEnvelope):
            raise TypeError("intent must be an IntentEnvelope")
        object.__setattr__(self, "steps", tuple(self.steps))
        if not all(isinstance(step, WorkflowStep) for step in self.steps):
            raise TypeError("steps must contain WorkflowStep instances")
        _validate_dag(self.steps)
        object.__setattr__(self, "status", _enum(PlanStatus, self.status, "status"))
        if isinstance(self.revision, bool) or not isinstance(self.revision, int) or self.revision < 0:
            raise ValueError("revision must be a non-negative integer")
        object.__setattr__(self, "metadata", _mapping(self.metadata, "metadata"))

    @property
    def step_map(self) -> dict[str, WorkflowStep]:
        return {step.step_id: step for step in self.steps}

    @property
    def max_side_effect(self) -> SideEffectLevel:
        levels = [self.intent.side_effect, *(step.side_effect for step in self.steps)]
        return max(levels, key=lambda item: item.rank)

    def get_step(self, step_id: str) -> WorkflowStep:
        try:
            return self.step_map[step_id]
        except KeyError as exc:
            raise KeyError(f"unknown workflow step: {step_id}") from exc

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "plan_id": self.plan_id,
            "intent": self.intent.to_dict(),
            "steps": [step.to_dict() for step in self.steps],
            "status": self.status.value,
            "revision": self.revision,
            "metadata": dict(self.metadata),
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def to_task_records(self) -> tuple[Any, ...]:
        return tuple(step.to_task_record(self.plan_id) for step in self.steps)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> WorkflowPlan:
        if not isinstance(data, Mapping):
            raise TypeError("workflow plan payload must be a mapping")
        return cls(
            schema_version=data.get("schema_version", SCHEMA_VERSION),
            plan_id=data.get("plan_id", ""),
            intent=IntentEnvelope.from_dict(data.get("intent", {})),
            steps=tuple(WorkflowStep.from_dict(item) for item in data.get("steps", ())),
            status=data.get("status", PlanStatus.DRAFT.value),
            revision=data.get("revision", 0),
            metadata=data.get("metadata", {}),
        )

    @classmethod
    def from_json(cls, payload: str) -> WorkflowPlan:
        return cls.from_dict(json.loads(payload))
