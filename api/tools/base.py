from __future__ import annotations

from typing import Any, Mapping, Protocol, runtime_checkable

from api.tools.contracts import ToolContext


@runtime_checkable
class ToolExecutor(Protocol):
    def execute(self, arguments: Mapping[str, Any], context: ToolContext | None = None) -> Any:
        ...
