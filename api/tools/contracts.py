from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


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
