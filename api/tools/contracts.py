from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping


GENERAL_ERROR_CATEGORIES = (
    "auth_required",
    "login_failed",
    "path_missing",
    "external_service",
    "validation_failed",
    "unknown",
)


TOOL_SIDE_EFFECTS = (
    "read_only",
    "local_draft",
    "reversible_write",
    "external_commit",
    "destructive",
)


class ToolSideEffect:
    READ_ONLY = "read_only"
    LOCAL_DRAFT = "local_draft"
    REVERSIBLE_WRITE = "reversible_write"
    EXTERNAL_COMMIT = "external_commit"
    DESTRUCTIVE = "destructive"


def normalize_tool_side_effect(value: Any) -> str:
    """Return the safest side-effect classification for invalid legacy input."""
    normalized = str(value or ToolSideEffect.READ_ONLY).strip().lower()
    if normalized not in TOOL_SIDE_EFFECTS:
        return ToolSideEffect.DESTRUCTIVE
    return normalized


class GeneralErrorCategory:
    AUTH_REQUIRED = "auth_required"
    LOGIN_FAILED = "login_failed"
    PATH_MISSING = "path_missing"
    EXTERNAL_SERVICE = "external_service"
    VALIDATION_FAILED = "validation_failed"
    UNKNOWN = "unknown"


@dataclass()
class GeneralError:
    category: str = GeneralErrorCategory.UNKNOWN
    message: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        category = self.category if self.category in GENERAL_ERROR_CATEGORIES else GeneralErrorCategory.UNKNOWN
        return {
            "category": category,
            "message": self.message,
            "details": dict(self.details),
        }


@dataclass()
class ToolContext:
    user_id: str = ""
    platform: str = ""
    correlation_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    permissions: set[str] | tuple[str, ...] | list[str] | None = None
    confirmation_token: str = ""
    confirmation_tokens: set[str] | tuple[str, ...] | list[str] = field(default_factory=set)


@dataclass()
class ToolSpec:
    name: str
    description: str = ""
    permission_tag: str = ""
    timeout_sec: int = 60
    input_schema: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    side_effect: str = ToolSideEffect.READ_ONLY
    requires_confirmation: bool = False
    idempotent: bool = True
    verification: Callable[..., Any] | Mapping[str, Any] | bool | None = None
    rollback: Callable[..., Any] | Mapping[str, Any] | bool | None = None
    retry: dict[str, Any] = field(default_factory=dict)
    health: dict[str, Any] = field(default_factory=dict)
    degraded: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        normalized = str(self.side_effect or ToolSideEffect.READ_ONLY).strip().lower()
        if normalized not in TOOL_SIDE_EFFECTS:
            raise ValueError(f"unsupported tool side effect: {self.side_effect}")
        self.side_effect = normalized
        if not isinstance(self.input_schema, dict):
            raise TypeError("input_schema must be a mapping")
        if normalized in {ToolSideEffect.EXTERNAL_COMMIT, ToolSideEffect.DESTRUCTIVE}:
            self.requires_confirmation = True
            if self.verification in (None, False):
                raise ValueError("external commit and destructive tools require verification")

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "permission_tag": self.permission_tag,
            "timeout_sec": self.timeout_sec,
            "input_schema": dict(self.input_schema),
            "metadata": dict(self.metadata),
            "side_effect": normalize_tool_side_effect(self.side_effect),
            "requires_confirmation": self.requires_confirmation,
            "idempotent": self.idempotent,
            "verification": _contract_descriptor(self.verification),
            "rollback": _contract_descriptor(self.rollback),
            "retry": dict(self.retry),
            "health": dict(self.health),
            "degraded": dict(self.degraded),
        }


@dataclass()
class ToolResult:
    tool_name: str
    success: bool
    output: Any = None
    error: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "tool_name": self.tool_name,
            "success": self.success,
            "output": self.output,
            "error": self.error,
            "metadata": dict(self.metadata),
        }


def _contract_descriptor(value: Callable[..., Any] | Mapping[str, Any] | bool | None) -> dict[str, Any]:
    """Expose contract capability without leaking callable implementation details."""
    if callable(value):
        return {"enabled": True, "handler": getattr(value, "__name__", value.__class__.__name__)}
    if isinstance(value, Mapping):
        descriptor = dict(value)
        handler = descriptor.get("handler")
        if callable(handler):
            descriptor["handler"] = getattr(handler, "__name__", handler.__class__.__name__)
        descriptor.setdefault("enabled", bool(handler) or bool(descriptor))
        return descriptor
    return {"enabled": bool(value)}
