from __future__ import annotations

from dataclasses import replace
from collections.abc import Mapping
import ast
from pathlib import Path
import threading
from types import MethodType

import pytest

from api.agentic.runtime_control import (
    AgentControlError,
    AgentRunEvent,
    AgentRunStatus,
    RootAgentControl,
    derive_root_id,
    new_run_id,
    reduce_agent_status,
)


def _token(control: RootAgentControl, *, user: str = "u1", category: str = "general"):
    root = derive_root_id(platform="TEST", user_id=user)
    token = control.reserve(
        root_id=root,
        run_id=new_run_id(root_id=root, correlation_id="corr"),
        intent_category=category,
        tool_category="search" if category == "research" else "none",
        side_effect="read_only",
    )
    return root, token


def test_root_ids_are_stable_opaque_and_platform_bound() -> None:
    first = derive_root_id(platform="line", user_id="private-user@example.test")
    same = derive_root_id(platform="LINE", user_id="private-user@example.test")
    other = derive_root_id(platform="discord", user_id="private-user@example.test")

    assert first == same
    assert first != other
    assert first.startswith("root-")
    assert "private" not in first
    assert "example" not in first
    with pytest.raises(AgentControlError, match="bounded"):
        derive_root_id(platform="LINE", user_id="u" * 4097)


def test_event_reducer_is_deterministic_and_terminal_outcomes_are_closed() -> None:
    status = reduce_agent_status(None, AgentRunEvent.RESERVED)
    status = reduce_agent_status(status, AgentRunEvent.STARTED)
    status = reduce_agent_status(status, AgentRunEvent.INTERRUPTED)
    status = reduce_agent_status(status, AgentRunEvent.RESUMED)
    status = reduce_agent_status(status, AgentRunEvent.COMPLETED)

    assert status is AgentRunStatus.COMPLETED
    with pytest.raises(AgentControlError, match="cannot apply"):
        reduce_agent_status(status, AgentRunEvent.STARTED)


def test_exact_owner_token_controls_once_only_terminal_transition() -> None:
    control = RootAgentControl()
    _root, token = _token(control)

    assert control.start(token).status is AgentRunStatus.RUNNING
    completed = control.complete(token)
    assert completed.status is AgentRunStatus.COMPLETED
    assert control.complete(token) == completed

    forged = replace(token, owner_token="0" * 64)
    with pytest.raises(AgentControlError, match="ownership"):
        control.snapshot(forged)
    with pytest.raises(AgentControlError, match="terminal"):
        control.error(token)
    assert control.request_cancel(token) == completed
    with pytest.raises(AgentControlError, match="cannot apply"):
        control.interrupt(token)
    with pytest.raises(AgentControlError, match="cannot apply"):
        control.acknowledge_cancel(token)
    assert control.snapshot(token) == completed


def test_interrupt_resume_and_cooperative_cancel_are_root_owned() -> None:
    control = RootAgentControl()
    _root, token = _token(control)
    control.start(token)

    interrupted = control.interrupt(token)
    assert interrupted.status is AgentRunStatus.INTERRUPTED
    assert interrupted.cancel_requested is True
    assert control.cancellation_requested(token) is True
    resumed = control.resume(token)
    assert resumed.status is AgentRunStatus.RUNNING
    assert resumed.cancel_requested is False
    assert control.request_cancel(token).status is AgentRunStatus.INTERRUPTED
    assert len(control.active()) == 1
    assert control.acknowledge_cancel(token).status is AgentRunStatus.CANCELLED
    assert control.active() == ()


def test_finishing_one_concurrent_root_run_does_not_hide_the_other() -> None:
    control = RootAgentControl()
    _root_a, token_a = _token(control, user="u1", category="legal")
    _root_b, token_b = _token(control, user="u2", category="research")
    control.start(token_a)
    control.start(token_b)

    before = control.public_snapshot()
    assert before["active_count"] == 2
    assert before["status"] == "running"
    assert before["intent_category"] == "research"
    assert before["tool_category"] == "search"
    assert before["side_effect"] == "read_only"
    assert isinstance(before["sequence"], int)

    control.complete(token_a)
    remaining = control.public_snapshot()
    assert remaining["active_count"] == 1
    assert remaining["status"] == "running"
    assert remaining["intent_category"] == "research"

    control.complete(token_b)
    assert control.public_snapshot()["active_count"] == 0
    assert control.public_snapshot()["status"] == "completed"


def test_capacity_is_bounded_and_only_terminal_history_is_pruned() -> None:
    control = RootAgentControl(max_active=1, max_history=2)
    root, first = _token(control, user="u1")
    control.start(first)
    with pytest.raises(AgentControlError, match="capacity"):
        control.reserve(root_id=root, run_id=new_run_id(root_id=root))

    control.complete(first)
    second = control.reserve(root_id=root, run_id=new_run_id(root_id=root))
    control.start(second)
    control.complete(second)
    third = control.reserve(root_id=root, run_id=new_run_id(root_id=root))
    assert control.start(third).status is AgentRunStatus.RUNNING


def test_shadow_finish_keeps_public_status_running_when_another_run_is_active(monkeypatch) -> None:
    from api.agentic import shadow

    published = []
    monkeypatch.setattr(shadow, "_last_control_sequence", -1)
    monkeypatch.setattr(shadow, "_last_control_payload", {})
    monkeypatch.setattr(
        shadow,
        "write_public_agent_status",
        lambda snapshot: published.append(dict(snapshot)) or dict(snapshot),
    )

    result = shadow.observe_finish(
        "第一個工作完成",
        "完成",
        control_snapshot={
            "active_count": 1,
            "sequence": 1,
            "status": "running",
            "intent_category": "research",
            "tool_category": "search",
            "side_effect": "read_only",
        },
    )

    assert result["status"] == "running"
    assert result["plan_status"] == "running"
    assert result["step_counts"] == {"total": 1, "running": 1}
    assert published == [result]


def test_shadow_publication_fences_a_stale_concurrent_finish(monkeypatch) -> None:
    from api.agentic import shadow

    writes = []
    monkeypatch.setattr(shadow, "_last_control_sequence", -1)
    monkeypatch.setattr(shadow, "_last_control_payload", {})
    monkeypatch.setattr(
        shadow,
        "write_public_agent_status",
        lambda snapshot: writes.append(dict(snapshot)) or dict(snapshot),
    )
    active = {
        "active_count": 1,
        "sequence": 10,
        "status": "running",
        "intent_category": "research",
        "tool_category": "search",
        "side_effect": "read_only",
    }
    stale_terminal = {
        "active_count": 0,
        "sequence": 9,
        "status": "completed",
        "intent_category": "legal",
        "tool_category": "search",
        "side_effect": "read_only",
    }

    current = shadow.observe_start("研究", control_snapshot=active)
    stale = shadow.observe_finish("法律", "完成", control_snapshot=stale_terminal)

    assert current["status"] == "running"
    assert stale == current
    assert len(writes) == 1


def test_shadow_control_mapping_is_read_once_and_invalid_control_does_not_publish(monkeypatch) -> None:
    from api.agentic import shadow

    writes = []
    monkeypatch.setattr(shadow, "_last_control_sequence", -1)
    monkeypatch.setattr(shadow, "_last_control_payload", {})
    monkeypatch.setattr(
        shadow,
        "write_public_agent_status",
        lambda snapshot: writes.append(dict(snapshot)) or dict(snapshot),
    )

    class ChangingMapping(Mapping):
        def __init__(self):
            self.calls = 0

        def __iter__(self):
            return iter(())

        def __len__(self):
            return 0

        def __getitem__(self, _key):
            raise KeyError

        def items(self):
            self.calls += 1
            if self.calls == 1:
                return {
                    "active_count": 1,
                    "sequence": 20,
                    "status": "running",
                    "intent_category": "research",
                    "tool_category": "search",
                    "side_effect": "read_only",
                }.items()
            return {
                "active_count": 0,
                "sequence": 21,
                "status": "completed",
            }.items()

    changing = ChangingMapping()
    observed = shadow.observe_finish("done", "done", control_snapshot=changing)
    assert changing.calls == 1
    assert observed["status"] == "running"
    assert len(writes) == 1

    class BrokenMapping(ChangingMapping):
        def items(self):
            raise RuntimeError("broken")

    inert = shadow.observe_finish("done", "done", control_snapshot=BrokenMapping())
    assert inert == observed
    assert len(writes) == 1


def test_runtime_control_module_has_no_io_network_process_or_model_imports() -> None:
    source_path = Path(__file__).resolve().parents[1] / "api" / "agentic" / "runtime_control.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
    imported_roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_roots.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_roots.add(node.module.split(".", 1)[0])
    assert imported_roots.isdisjoint(
        {"os", "pathlib", "socket", "subprocess", "requests", "urllib", "sqlite3", "magi_v3", "skills"}
    )


def _bare_orchestrator(inner):
    from api.orchestrator import Orchestrator

    orchestrator = Orchestrator.__new__(Orchestrator)
    orchestrator._agent_control = RootAgentControl()
    orchestrator._progress_callback = None
    orchestrator._ensure_runtime_foundations = MethodType(lambda _self: None, orchestrator)
    orchestrator._append_route_trace = MethodType(lambda _self, *_args, **_kwargs: None, orchestrator)
    orchestrator._process_message_inner = MethodType(inner, orchestrator)
    return orchestrator


def test_orchestrator_scopes_token_and_restores_thread_context(monkeypatch) -> None:
    from api import orchestrator as orchestrator_module

    monkeypatch.setattr(
        "api.agentic.shadow.should_publish_public_agent_status",
        lambda _platform: False,
    )
    seen = []

    def inner(self, *_args, **_kwargs):
        token = self._current_agent_run_token()
        seen.append(token)
        assert token is not None
        assert self._current_correlation_id() == "corr-1"
        return "ok"

    orchestrator = _bare_orchestrator(inner)
    orchestrator_module._orchestrator_tls.correlation_id = "outer-correlation"
    orchestrator_module._orchestrator_tls.agent_run_token = "outer-token"

    assert orchestrator.process_message("u1", "研究案件", platform="TEST", correlation_id="corr-1") == "ok"
    assert seen and orchestrator._agent_control.snapshot(seen[0]).status is AgentRunStatus.COMPLETED
    assert orchestrator_module._orchestrator_tls.correlation_id == "outer-correlation"
    assert orchestrator_module._orchestrator_tls.agent_run_token == "outer-token"


def test_orchestrator_baseexception_closes_run_and_propagates(monkeypatch) -> None:
    monkeypatch.setattr(
        "api.agentic.shadow.should_publish_public_agent_status",
        lambda _platform: False,
    )

    class StopNow(BaseException):
        pass

    def inner(_self, *_args, **_kwargs):
        raise StopNow("stop")

    orchestrator = _bare_orchestrator(inner)
    with pytest.raises(StopNow):
        orchestrator.process_message("u1", "停止", platform="TEST", correlation_id="corr-stop")

    assert orchestrator._agent_control.active() == ()
    assert orchestrator._agent_control.public_snapshot()["status"] == "shutdown"


def test_orchestrator_start_exception_releases_reserved_capacity(monkeypatch) -> None:
    monkeypatch.setattr(
        "api.agentic.shadow.should_publish_public_agent_status",
        lambda _platform: False,
    )

    orchestrator = _bare_orchestrator(lambda *_args, **_kwargs: "must-not-run")
    control = orchestrator._agent_control
    monkeypatch.setattr(
        control,
        "start",
        lambda _token: (_ for _ in ()).throw(RuntimeError("start failed")),
    )

    assert orchestrator.process_message("u1", "start", platform="TEST") == "❌ 系統暫時忙碌，請稍後再試。"
    assert control.active() == ()
    assert control.public_snapshot()["status"] == "shutdown"


def test_orchestrator_post_start_baseexception_releases_running_capacity(monkeypatch) -> None:
    monkeypatch.setattr(
        "api.agentic.shadow.should_publish_public_agent_status",
        lambda _platform: False,
    )

    class StartCancelled(BaseException):
        pass

    orchestrator = _bare_orchestrator(lambda *_args, **_kwargs: "must-not-run")
    control = orchestrator._agent_control
    original_start = control.start

    def mutate_then_cancel(token):
        original_start(token)
        raise StartCancelled("cancelled after reducer commit")

    monkeypatch.setattr(control, "start", mutate_then_cancel)

    with pytest.raises(StartCancelled):
        orchestrator.process_message("u1", "start", platform="TEST")
    assert control.active() == ()
    assert control.public_snapshot()["status"] == "shutdown"


def test_orchestrator_acknowledges_cooperative_cancel_before_return(monkeypatch) -> None:
    monkeypatch.setattr(
        "api.agentic.shadow.should_publish_public_agent_status",
        lambda _platform: False,
    )
    seen = []

    def inner(self, *_args, **_kwargs):
        token = self._current_agent_run_token()
        self._agent_control.request_cancel(token)
        assert self._agent_cancel_requested() is True
        seen.append(token)
        return "bounded-result"

    orchestrator = _bare_orchestrator(inner)
    assert orchestrator.process_message("u1", "cancel", platform="TEST") == "bounded-result"
    assert orchestrator._agent_control.snapshot(seen[0]).status is AgentRunStatus.CANCELLED
    assert orchestrator._agent_control.active() == ()


def test_orchestrator_fails_closed_when_root_reservation_is_unavailable(monkeypatch) -> None:
    monkeypatch.setattr(
        "api.agentic.shadow.should_publish_public_agent_status",
        lambda _platform: False,
    )
    called = []

    def inner(_self, *_args, **_kwargs):
        called.append(True)
        return "must-not-run"

    orchestrator = _bare_orchestrator(inner)
    orchestrator._agent_control = RootAgentControl(max_active=1, max_history=1)
    root, token = _token(orchestrator._agent_control, user="occupied")
    assert root
    orchestrator._agent_control.start(token)

    result = orchestrator.process_message(
        "new-user",
        "new request",
        platform="TEST",
        correlation_id="new-correlation",
    )

    assert result == "❌ 系統暫時忙碌，請稍後再試。"
    assert called == []


def test_real_orchestrator_concurrency_keeps_remaining_run_owned(monkeypatch) -> None:
    monkeypatch.setattr(
        "api.agentic.shadow.should_publish_public_agent_status",
        lambda _platform: False,
    )
    entered = {"a": threading.Event(), "b": threading.Event()}
    release = {"a": threading.Event(), "b": threading.Event()}

    def inner(_self, _user_id, message, *_args, **_kwargs):
        entered[message].set()
        assert release[message].wait(2)
        return message

    orchestrator = _bare_orchestrator(inner)
    results = {}

    def run(name: str) -> None:
        results[name] = orchestrator.process_message(
            "same-user",
            name,
            platform="TEST",
            correlation_id=f"corr-{name}",
        )

    first = threading.Thread(target=run, args=("a",))
    second = threading.Thread(target=run, args=("b",))
    first.start()
    second.start()
    assert entered["a"].wait(2) and entered["b"].wait(2)
    assert len(orchestrator._agent_control.active()) == 2

    release["a"].set()
    first.join(2)
    assert not first.is_alive()
    assert orchestrator._agent_control.public_snapshot()["active_count"] == 1
    assert orchestrator._agent_control.public_snapshot()["status"] == "running"

    release["b"].set()
    second.join(2)
    assert not second.is_alive()
    assert results == {"a": "a", "b": "b"}
    assert orchestrator._agent_control.public_snapshot()["active_count"] == 0
