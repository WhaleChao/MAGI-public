from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from api.tools.base import ToolExecutor
from api.tools.contracts import ToolContext, ToolResult, ToolSpec
from api.tools.executors import CallableToolExecutor


@dataclass()
class RegisteredTool:
    spec: ToolSpec
    executor: ToolExecutor
    aliases: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, Any]:
        payload = self.spec.as_dict()
        if self.aliases:
            payload["aliases"] = list(self.aliases)
        return payload


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, RegisteredTool] = {}

    def register(
        self,
        spec: ToolSpec,
        executor: ToolExecutor,
        *,
        aliases: tuple[str, ...] | list[str] = (),
    ) -> RegisteredTool:
        entry = RegisteredTool(spec=spec, executor=executor, aliases=tuple(aliases))
        self._tools[spec.name] = entry
        for alias in entry.aliases:
            self._tools[alias] = entry
        return entry

    def register_callable(
        self,
        name: str,
        fn,
        *,
        description: str = "",
        permission_tag: str = "",
        timeout_sec: int = 60,
        input_schema: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        aliases: tuple[str, ...] | list[str] = (),
    ) -> RegisteredTool:
        spec = ToolSpec(
            name=name,
            description=description,
            permission_tag=permission_tag,
            timeout_sec=timeout_sec,
            input_schema=dict(input_schema or {}),
            metadata=dict(metadata or {}),
        )
        return self.register(spec, CallableToolExecutor(fn), aliases=aliases)

    def get(self, name: str) -> Optional[RegisteredTool]:
        return self._tools.get(name)

    def list_tools(self) -> list[dict[str, Any]]:
        seen: set[str] = set()
        items: list[dict[str, Any]] = []
        for key, entry in sorted(self._tools.items(), key=lambda item: item[0]):
            if entry.spec.name in seen:
                continue
            seen.add(entry.spec.name)
            items.append(entry.as_dict())
        return items

    def execute(
        self,
        name: str,
        arguments: Mapping[str, Any] | None = None,
        context: Optional[ToolContext] = None,
    ) -> ToolResult:
        entry = self.get(name)
        if entry is None:
            return ToolResult(tool_name=name, success=False, error=f"Unknown tool: {name}")
        try:
            output = entry.executor.execute(dict(arguments or {}), context=context)
            if isinstance(output, ToolResult):
                return output
            return ToolResult(
                tool_name=entry.spec.name,
                success=True,
                output=output,
                metadata=dict(entry.spec.metadata),
            )
        except Exception as exc:
            return ToolResult(
                tool_name=entry.spec.name,
                success=False,
                error=str(exc),
                metadata=dict(entry.spec.metadata),
            )


GLOBAL_TOOL_REGISTRY = ToolRegistry()


def get_global_tool_registry() -> ToolRegistry:
    return GLOBAL_TOOL_REGISTRY
