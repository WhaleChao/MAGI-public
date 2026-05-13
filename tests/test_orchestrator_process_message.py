# -*- coding: utf-8 -*-
from __future__ import annotations


def test_process_message_logs_unhandled_exception(monkeypatch):
    from api.orchestrator import Orchestrator
    import skills.management.issue_tracker as issue_tracker

    calls = []

    def fake_log_issue(**kwargs):
        calls.append(kwargs)
        return True

    def fail_inner(self, *args, **kwargs):
        raise ValueError("broken path")

    traces = []
    orch = Orchestrator.__new__(Orchestrator)
    monkeypatch.setattr(issue_tracker, "log_issue", fake_log_issue)
    monkeypatch.setattr(orch, "_process_message_inner", fail_inner.__get__(orch, Orchestrator))
    monkeypatch.setattr(orch, "_append_route_trace", lambda *args, **kwargs: traces.append((args, kwargs)))

    response = orch.process_message("u1", "hello", platform="LINE")

    assert "系統暫時忙碌" in response
    assert calls == [
        {
            "command": "hello",
            "error_msg": "ValueError: broken path",
            "context": "user_id=u1 platform=LINE",
            "severity": "High",
            "source": "orchestrator.process_message",
        }
    ]
    assert traces
