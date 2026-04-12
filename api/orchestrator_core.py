from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from api.coordinator import AgentCoordinator
from api.hooks import HookBus
from api.permissions import PermissionEnforcer
from api.session import SessionContextBuilder, SessionStore
from api.tasks import TaskRuntime
from api.tools import ToolRegistry


@dataclass()
class RuntimeFoundations:
    task_runtime: TaskRuntime
    session_store: SessionStore
    session_context_builder: SessionContextBuilder
    permission_enforcer: PermissionEnforcer
    hook_bus: HookBus
    tool_registry: Optional[ToolRegistry] = None
    agent_coordinator: Optional[AgentCoordinator] = None
