from __future__ import annotations

import json

from api.deep_task_control import enqueue_local_deep_admission
from api.deep_task_control import DeepTaskDeferred
from scripts.ops.local_deep_queue_worker import drain_once


class _Controller:
    def __init__(self, events): self.events = events
    def run(self, work):
        self.events.append("controlled_run")
        return work()


def _receipts(tmp_path):
    return [json.loads(line) for line in (tmp_path / "metrics" / "local_deep_queue_receipts.jsonl").read_text(encoding="utf-8").splitlines()]


def test_missing_secure_reference_is_deferred_not_completed(tmp_path, monkeypatch):
    monkeypatch.setenv("MAGI_RUNTIME_DIR", str(tmp_path))
    enqueue_local_deep_admission(task_type="legal_analysis", prompt="sensitive")
    assert drain_once(retrieve=lambda _: "sensitive", execute=lambda *_: "never") ["state"] == "deferred"
    assert _receipts(tmp_path)[0]["reason"] == "secure_task_ref_missing"


def test_worker_executes_one_secure_reference_through_controlled_controller(tmp_path, monkeypatch):
    monkeypatch.setenv("MAGI_RUNTIME_DIR", str(tmp_path))
    enqueue_local_deep_admission(task_type="legal_analysis", prompt="secret", secure_task_ref="vault:task-1")
    events = []
    result = drain_once(retrieve=lambda ref: "retrieved" if ref == "vault:task-1" else None, execute=lambda prompt, row: events.append((prompt, row["task_id"])), controller_factory=lambda: _Controller(events))
    assert result["state"] == "succeeded"
    assert events[0] == "controlled_run"
    assert events[1][0] == "retrieved"
    assert _receipts(tmp_path)[0]["state"] == "succeeded"


def test_runtime_gate_deferral_never_executes_task(tmp_path, monkeypatch):
    monkeypatch.setenv("MAGI_RUNTIME_DIR", str(tmp_path))
    enqueue_local_deep_admission(task_type="legal_analysis", prompt="secret", secure_task_ref="vault:task-1")
    class _Blocked:
        def run(self, work): raise DeepTaskDeferred("deep task deferred: resource gate rejected admission")
    result = drain_once(retrieve=lambda _: "retrieved", execute=lambda *_: (_ for _ in ()).throw(AssertionError("must not execute")), controller_factory=_Blocked)
    assert result["state"] == "deferred"
    assert "resource gate" in _receipts(tmp_path)[0]["reason"]


def test_deferred_task_retries_after_cooldown(tmp_path, monkeypatch):
    import scripts.ops.local_deep_queue_worker as worker

    monkeypatch.setenv("MAGI_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("MAGI_LOCAL_DEEP_RETRY_SEC", "30")
    enqueue_local_deep_admission(task_type="legal_analysis", prompt="secret", secure_task_ref="vault:task-1")
    class _Blocked:
        def run(self, work): raise DeepTaskDeferred("busy")
    assert drain_once(retrieve=lambda _: "retrieved", execute=lambda *_: "never", controller_factory=_Blocked)["state"] == "deferred"
    assert drain_once(retrieve=lambda _: "retrieved", execute=lambda *_: "never", controller_factory=lambda: _Controller([]))["state"] == "idle"
    first_at = _receipts(tmp_path)[0]["at_epoch"]
    monkeypatch.setattr(worker.time, "time", lambda: first_at + 31)
    assert drain_once(retrieve=lambda _: "retrieved", execute=lambda *_: "done", controller_factory=lambda: _Controller([]))["state"] == "succeeded"


def test_work_runtime_error_is_failed_not_retryable_deferred(tmp_path, monkeypatch):
    monkeypatch.setenv("MAGI_RUNTIME_DIR", str(tmp_path))
    enqueue_local_deep_admission(task_type="legal_analysis", prompt="secret", secure_task_ref="vault:task-1")
    result = drain_once(
        retrieve=lambda _: "retrieved",
        execute=lambda *_: (_ for _ in ()).throw(RuntimeError("model failed")),
        controller_factory=lambda: _Controller([]),
    )
    assert result["state"] == "failed"
    assert _receipts(tmp_path)[0]["state"] == "failed"
