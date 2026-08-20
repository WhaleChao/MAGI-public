from __future__ import annotations

import pytest
import threading
import time
import json

from api.deep_task_control import DeepTaskController, assess_deep_admission
from api.deep_task_control import runtime_owner_worker_transaction_free
from api.deep_task_control import enqueue_local_deep_admission, enqueue_local_deep_job
from api.deep_task_control import infer_local_deep_task_type


def test_admission_is_local_and_escalates_disagreement():
    admission = assess_deep_admission(task_type="general", disagreement=True)
    assert admission.required is True
    assert "local" in admission.reason


def test_controller_switches_one_task_and_restores_day():
    calls: list[str] = []
    controller = DeepTaskController(switch=calls.append, health=lambda profile: True, resource_ok=lambda: True, transaction_free=lambda: True)
    assert controller.run(lambda: "done") == "done"
    assert calls == ["night", "day"]


def test_controller_defers_while_transaction_is_active_without_switching():
    calls: list[str] = []
    controller = DeepTaskController(switch=calls.append, health=lambda profile: True, resource_ok=lambda: True, transaction_free=lambda: False)
    with pytest.raises(RuntimeError, match="transaction lock"):
        controller.run(lambda: "must not run")
    assert calls == []


def test_controller_rolls_back_after_work_failure_without_losing_cleanup():
    calls: list[str] = []
    controller = DeepTaskController(switch=calls.append, health=lambda profile: True, resource_ok=lambda: True, transaction_free=lambda: True)
    with pytest.raises(ValueError, match="work failed"):
        controller.run(lambda: (_ for _ in ()).throw(ValueError("work failed")))
    assert calls == ["night", "day"]


def test_controller_refuses_unhealthy_night_and_restores_day():
    calls: list[str] = []
    controller = DeepTaskController(switch=calls.append, health=lambda profile: profile == "day", resource_ok=lambda: True, transaction_free=lambda: True)
    with pytest.raises(RuntimeError, match="night topology"):
        controller.run(lambda: "must not run")
    assert calls == ["night", "day"]


def test_controller_serializes_two_deep_tasks():
    events: list[str] = []
    controller = DeepTaskController(switch=lambda profile: events.append(f"switch:{profile}"), health=lambda profile: True, resource_ok=lambda: True, transaction_free=lambda: True)
    first_started = threading.Event()
    release_first = threading.Event()

    def first():
        first_started.set()
        release_first.wait(1)
        events.append("work:first")

    first_thread = threading.Thread(target=lambda: controller.run(first))
    second_thread = threading.Thread(target=lambda: controller.run(lambda: events.append("work:second")))
    first_thread.start()
    assert first_started.wait(1)
    second_thread.start()
    time.sleep(0.03)
    assert "work:second" not in events
    release_first.set()
    first_thread.join(1)
    second_thread.join(1)
    assert events == ["switch:night", "work:first", "switch:day", "switch:night", "work:second", "switch:day"]


def test_runtime_gate_fails_closed_without_or_with_stale_evidence(tmp_path, monkeypatch):
    monkeypatch.setenv("MAGI_DEEP_RUNTIME_EVIDENCE_PATH", str(tmp_path / "missing.json"))
    assert not runtime_owner_worker_transaction_free(root=str(tmp_path))
    evidence = tmp_path / "evidence.json"
    evidence.write_text(json.dumps({"schema": "magi.v3.runtime-owner-worker-transaction/v1", "owner_verified": True, "transaction_active": False, "active_workers": 0, "observed_at_epoch": time.time()}), encoding="utf-8")
    monkeypatch.setenv("MAGI_DEEP_RUNTIME_EVIDENCE_PATH", str(evidence))
    assert runtime_owner_worker_transaction_free(root=str(tmp_path))


def test_admission_queue_records_only_hash_and_local_provider(tmp_path, monkeypatch):
    monkeypatch.setenv("MAGI_RUNTIME_DIR", str(tmp_path))
    task_id = enqueue_local_deep_admission(task_type="legal_analysis", prompt="個資不得落盤")
    row = json.loads((tmp_path / "metrics" / "local_deep_queue.jsonl").read_text(encoding="utf-8"))
    assert row["task_id"] == task_id
    assert row["provider"] == "omlx"
    assert "個資" not in json.dumps(row, ensure_ascii=False)


def test_natural_complexity_escalates_but_lookup_and_mutation_do_not():
    assert infer_local_deep_task_type("請深入分析這個法律爭點，比較裁判見解與適用要件，並交叉核對理由。") == "legal_analysis"
    assert infer_local_deep_task_type("查明天天氣") == ""
    assert infer_local_deep_task_type("刪除 2026-0049 的檔案") == ""


def test_private_job_queue_binds_prompt_to_opaque_admission(tmp_path, monkeypatch):
    from skills.memory import job_queue

    monkeypatch.setenv("MAGI_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setattr(job_queue, "_DB_DIR", str(tmp_path / "agent" / "jobs"))
    monkeypatch.setattr(job_queue, "_DB_PATH", str(tmp_path / "agent" / "jobs" / "job_queue.db"))
    job_id = enqueue_local_deep_job(
        task_type="legal_analysis",
        prompt="私密案件分析",
        platform="WEB",
        user_id="7",
    )
    job = job_queue.read(job_id)
    admission = json.loads((tmp_path / "runtime" / "metrics" / "local_deep_queue.jsonl").read_text(encoding="utf-8"))
    assert job["user_text"] == "私密案件分析"
    assert admission["secure_task_ref"] == f"job_queue:{job_id}"
    assert "私密" not in json.dumps(admission, ensure_ascii=False)
    assert oct((tmp_path / "agent" / "jobs" / "job_queue.db").stat().st_mode & 0o777) == "0o600"


def test_production_transaction_gate_uses_existing_live_sources(tmp_path, monkeypatch):
    from api.platforms import runtime_dir
    import api.deep_task_control as module

    monkeypatch.delenv("MAGI_DEEP_RUNTIME_EVIDENCE_PATH", raising=False)
    cron = tmp_path / "cron_state.json"
    cron.write_text(json.dumps({"job_local_deep_queue_worker": {"last_status": "running"}, "other": {"last_status": "success"}}), encoding="utf-8")
    monkeypatch.setattr(runtime_dir, "cron_state", lambda: cron)
    payloads = iter([
        {"ready": True, "components": {"release_ownership": "verified", "active_resources": {"total": 0}}},
        {"ok": True, "checks": {"capacity": {"tool_inflight": 0, "inference_inflight": 0}}},
    ])

    class _Response:
        status = 200
        def __init__(self, payload): self.payload = payload
        def __enter__(self): return self
        def __exit__(self, *_args): return False
        def read(self, _limit): return json.dumps(self.payload).encode()

    monkeypatch.setattr(module.urllib.request, "urlopen", lambda *_a, **_k: _Response(next(payloads)))
    assert runtime_owner_worker_transaction_free(root=str(tmp_path)) is True
