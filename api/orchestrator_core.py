from __future__ import annotations

from dataclasses import dataclass

from api.coordinator import AgentCoordinator
from api.hooks import HookBus
from api.permissions import PermissionEnforcer
from api.session import SessionContextBuilder, SessionStore
from api.tasks import TaskRuntime
from api.tools import ToolRegistry


@dataclass(slots=True)
class RuntimeFoundations:
    task_runtime: TaskRuntime
    session_store: SessionStore
    session_context_builder: SessionContextBuilder
    permission_enforcer: PermissionEnforcer
    hook_bus: HookBus
    tool_registry: ToolRegistry | None = None
    agent_coordinator: AgentCoordinator | None = None
