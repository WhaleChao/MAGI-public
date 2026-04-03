from __future__ import annotations

import sys
import types
from pathlib import Path

from api.hooks import HookBus
from api.permissions import PermissionEnforcer, PermissionMode, PermissionPolicy, deny_command, deny_path
from skills.plugin import SkillPlugin, SkillRegistry


class _FakeOrchestrator:
    def __init__(self, enforcer: PermissionEnforcer):
        self._permission_enforcer = enforcer
        self._hook_bus = HookBus(source="test")

    def _ensure_runtime_foundations(self) -> None:
        return None

    def _current_correlation_id(self) -> str:
        return "cid-test"


class _BlockedPlugin(SkillPlugin):
    name = "blocked"
    description = "blocked plugin"

    def __init__(self) -> None:
        self.calls = 0

    def execute(self, message: str, **ctx):
        self.calls += 1
        return f"handled:{message}"


def test_plugin_dispatch_respects_permission_policy_and_emits_hooks():
    plugin = _BlockedPlugin()
    registry = SkillRegistry()
    registry.register_plugin(plugin)
    orchestrator = _FakeOrchestrator(
        PermissionEnforcer(
            policy=PermissionPolicy.from_rules(
                [
                    deny_command(
                        name="deny-blocked",
                        commands=("skill:blocked",),
                        reason="blocked by policy",
                        priority=1,
                    )
                ],
                mode=PermissionMode.PERMISSIVE,
            )
        )
    )
    seen: list[str] = []
    orchestrator._hook_bus.subscribe("*", lambda event: seen.append(event.event_type))

    handled, reply = registry.dispatch(
        "blocked",
        "hello",
        user_id="u1",
        platform="LINE",
        orchestrator=orchestrator,
    )

    assert handled is True
    assert "權限策略已阻擋技能執行" in reply
    assert plugin.calls == 0
    assert seen == ["hook.tool.pre", "hook.tool.post"]


def test_subprocess_dispatch_respects_path_permission_and_skips_runner(monkeypatch, tmp_path):
    skill_dir = tmp_path / "skills"
    action_dir = skill_dir / "blocked-sub"
    action_dir.mkdir(parents=True)
    (action_dir / "action.py").write_text("print('should not run')\n", encoding="utf-8")

    fake_module = types.SimpleNamespace()
    called = {"count": 0}

    def _fake_run_skill_action(*args, **kwargs):
        called["count"] += 1
        return {"success": True, "output": "ok"}

    fake_module.run_skill_action = _fake_run_skill_action
    monkeypatch.setitem(sys.modules, "skills.evolution.skill_genesis", fake_module)

    registry = SkillRegistry(skills_dirs=[str(skill_dir)])
    orchestrator = _FakeOrchestrator(
        PermissionEnforcer(
            policy=PermissionPolicy.from_rules(
                [
                    deny_path(
                        name="deny-test-skill-path",
                        paths=(str(action_dir),),
                        reason="test skill path denied",
                        priority=1,
                    )
                ],
                mode=PermissionMode.PERMISSIVE,
            )
        )
    )
    seen: list[str] = []
    orchestrator._hook_bus.subscribe("*", lambda event: seen.append(event.event_type))

    handled, reply = registry.dispatch(
        "blocked-sub",
        "hello",
        user_id="u1",
        platform="LINE",
        orchestrator=orchestrator,
    )

    assert handled is True
    assert "權限策略已阻擋技能執行" in reply
    assert called["count"] == 0
    assert seen == ["hook.tool.pre", "hook.tool.post"]


def test_discover_ignores_generated_code_skills(tmp_path: Path):
    skill_dir = tmp_path / "skills"
    normal = skill_dir / "normal-skill"
    generated = skill_dir / "code-generated-skill"
    hidden = skill_dir / ".versions"

    for entry in (normal, generated, hidden):
        entry.mkdir(parents=True)
        (entry / "SKILL.md").write_text("---\nname: demo\n---\n", encoding="utf-8")
        (entry / "action.py").write_text("print('ok')\n", encoding="utf-8")

    registry = SkillRegistry(skills_dirs=[str(skill_dir)])
    count = registry.discover()

    assert count == 1
    assert "demo" in registry._skill_meta
    assert registry._skill_meta["demo"].folder == "normal-skill"
