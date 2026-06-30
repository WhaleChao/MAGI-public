from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


GENERAL_ERROR_CATEGORIES = (
    "auth_required",
    "login_failed",
    "path_missing",
    "external_service",
    "validation_failed",
    "unknown",
)


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


@dataclass()
class ToolSpec:
    name: str
    description: str = ""
    permission_tag: str = ""
    timeout_sec: int = 60
    input_schema: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "permission_tag": self.permission_tag,
            "timeout_sec": self.timeout_sec,
            "input_schema": dict(self.input_schema),
            "metadata": dict(self.metadata),
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
