from __future__ import annotations

import importlib.util
import os
import json
from pathlib import Path
from types import SimpleNamespace


def load_worker_module():
    path = Path(__file__).resolve().parents[1] / "scripts" / "drive_case_sync_worker.py"
    spec = importlib.util.spec_from_file_location("drive_case_sync_worker_for_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_worker_lock_skips_when_existing_pid_is_alive(tmp_path, monkeypatch):
    worker = load_worker_module()
    monkeypatch.setattr(worker, "runtime_dir", lambda: tmp_path)
    (tmp_path / "drive_case_sync_worker.pid").write_text("1\n", encoding="utf-8")

    result = worker.acquire_worker_lock()

    assert result["acquired"] is False
    assert result["status"] == "already_running"
    assert result["active_pid"] == 1
    assert (tmp_path / "drive_case_sync_worker.pid").read_text(encoding="utf-8") == "1\n"


def test_worker_lock_replaces_stale_pid_and_releases(tmp_path, monkeypatch):
    worker = load_worker_module()
    monkeypatch.setattr(worker, "runtime_dir", lambda: tmp_path)
    (tmp_path / "drive_case_sync_worker.pid").write_text("999999\n", encoding="utf-8")

    result = worker.acquire_worker_lock()

    assert result["acquired"] is True
    assert result["pid"] == os.getpid()
    assert result["stale_lock"]["previous_status"] == "stale_lock_cleared"
    assert result["lock"]["metadata"]["owner"] == "drive_case_sync_worker"
    assert (tmp_path / "drive_case_sync_worker.pid").read_text(encoding="utf-8") == f"{os.getpid()}\n"
    assert (tmp_path / "drive_case_sync_worker.pid.json").exists()

    worker._release_worker_lock()
    assert not (tmp_path / "drive_case_sync_worker.pid").exists()


def test_worker_lock_precisely_fails_when_flock_owner_metadata_is_stale(tmp_path, monkeypatch):
    worker = load_worker_module()
    monkeypatch.setattr(worker, "runtime_dir", lambda: tmp_path)
    monkeypatch.setattr(worker, "_pid_is_alive", lambda pid: False)

    class FakeLock:
        acquired = False
        active_owner = {"pid": 999999, "owner": "old"}

        def as_dict(self):
            return {"acquired": False, "active_owner": self.active_owner}

    monkeypatch.setattr(worker, "acquire_lock", lambda *args, **kwargs: FakeLock())

    result = worker.acquire_worker_lock()

    assert result["acquired"] is False
    assert result["status"] == "lock_held_unknown_owner"
    assert result["stale_lock_audit"]["action"] == "precise_fail"


def test_worker_status_writes_general_and_kind_specific_latest(tmp_path, monkeypatch):
    worker = load_worker_module()
    monkeypatch.setattr(worker, "runtime_dir", lambda: tmp_path)

    worker.save_worker_status({"ok": True, "status": "ok"}, kind="all_files")

    general = tmp_path / "drive_case_sync_worker_status_latest.json"
    all_files = tmp_path / "drive_case_sync_worker_status_all_files_latest.json"
    assert general.exists()
    assert all_files.exists()
    assert '"worker_kind": "all_files"' in all_files.read_text(encoding="utf-8")
    payload = json.loads(general.read_text(encoding="utf-8"))
    assert payload.get("status_by_kind", {}).get("all_files", {}).get("status") == "ok"


def test_worker_status_keeps_status_by_kind_without_overwriting(tmp_path, monkeypatch):
    worker = load_worker_module()
    monkeypatch.setattr(worker, "runtime_dir", lambda: tmp_path)

    worker.save_worker_status({"ok": True, "status": "ok", "status_code": "all_files_ok"}, kind="all_files")
    worker.save_worker_status({"ok": True, "status": "running", "status_code": "priority_running"}, kind="priority")

    general = json.loads((tmp_path / "drive_case_sync_worker_status_latest.json").read_text(encoding="utf-8"))
    assert general.get("status") == "running"
    assert general.get("worker_kind") == "priority"
    status_by_kind = general.get("status_by_kind") or {}
    assert status_by_kind.get("all_files", {}).get("status_code") == "all_files_ok"
    assert status_by_kind.get("priority", {}).get("status_code") == "priority_running"


def test_clear_stale_running_status_keeps_other_kinds(tmp_path, monkeypatch):
    worker = load_worker_module()
    monkeypatch.setattr(worker, "runtime_dir", lambda: tmp_path)
    worker_path = tmp_path / "drive_case_sync_worker_status_latest.json"
    worker_path.write_text(
        json.dumps(
            {
                "worker_kind": "priority",
                "status": "running",
                "pid": 999999,
                "status_by_kind": {
                    "all_files": {"status": "ok", "status_code": "all_files_ok"},
                    "priority": {"status": "running", "pid": 999999},
                },
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(worker, "_pid_is_alive", lambda pid: False)

    stale = worker.clear_stale_running_status()

    payload = json.loads(worker_path.read_text(encoding="utf-8"))
    assert stale["status"] == "stale_running_cleared"
    assert payload.get("status_by_kind", {}).get("all_files", {}).get("status_code") == "all_files_ok"
    assert payload.get("status_by_kind", {}).get("priority", {}).get("status") == "stale_running_cleared"
    assert payload.get("worker_kind") == "priority"


def test_worker_state_keeps_status_by_kind_and_kind_specific_file(tmp_path, monkeypatch):
    worker = load_worker_module()
    monkeypatch.setattr(worker, "runtime_dir", lambda: tmp_path)

    worker.save_state({"matched_case_offset": 7, "last_status": {"status": "ok", "status_code": "priority_ok"}}, kind="priority")
    worker.save_state({"all_case_offset": 9, "last_status": {"status": "ok", "status_code": "all_files_ok"}}, kind="all_files")

    general = json.loads((tmp_path / "worker_state.json").read_text(encoding="utf-8"))
    assert general["status_by_kind"]["priority"]["status_code"] == "priority_ok"
    assert general["status_by_kind"]["all_files"]["status_code"] == "all_files_ok"
    assert (tmp_path / "worker_state_priority.json").exists()
    assert (tmp_path / "worker_state_all_files.json").exists()


def test_termination_status_marks_interrupted_with_offsets():
    worker = load_worker_module()
    worker._CURRENT_RUN_CONTEXT.clear()
    worker._CURRENT_RUN_CONTEXT.update(
        {
            "worker_kind": "all_files",
            "started_at": "2026-06-26T01:12:58+08:00",
            "matched_case_offset": 24,
            "all_case_offset": 1,
            "all_case_total": 207,
        }
    )

    status = worker._termination_status(15)

    assert status["ok"] is False
    assert status["status"] == "interrupted"
    assert status["worker_kind"] == "all_files"
    assert status["signal"] == 15
    assert status["matched_case_offset"] == 24
    assert status["all_case_total"] == 207


def test_terminal_status_for_current_process_preserves_completed_worker_state(tmp_path, monkeypatch):
    worker = load_worker_module()
    monkeypatch.setattr(worker, "runtime_dir", lambda: tmp_path)
    worker.save_worker_status(
        {
            "ok": True,
            "status": "ok",
            "pid": os.getpid(),
            "finished_at": "2026-06-30T08:13:23+08:00",
            "summary": {"matched_case_folders": 23},
        },
        kind="all_files",
    )

    status = worker._terminal_status_for_current_process("all_files")

    assert status["status"] == "ok"
    assert worker._terminal_status_exit_code(status) == 0


def test_terminal_status_for_partial_failure_exits_nonzero(tmp_path, monkeypatch):
    worker = load_worker_module()
    monkeypatch.setattr(worker, "runtime_dir", lambda: tmp_path)
    worker.save_worker_status(
        {
            "ok": False,
            "status": "partial_failure",
            "action_required": True,
            "pid": os.getpid(),
            "finished_at": "2026-07-04T16:13:23+08:00",
            "execution_summary": {"pending_unverified": 1},
        },
        kind="all_files",
    )

    status = worker._terminal_status_for_current_process("all_files")

    assert status["status"] == "partial_failure"
    assert worker._terminal_status_exit_code(status) == 1


def test_terminal_status_for_current_process_ignores_interrupted_state(tmp_path, monkeypatch):
    worker = load_worker_module()
    monkeypatch.setattr(worker, "runtime_dir", lambda: tmp_path)
    worker.save_worker_status(
        {
            "ok": False,
            "status": "interrupted",
            "pid": os.getpid(),
            "finished_at": "2026-06-30T08:13:23+08:00",
        },
        kind="all_files",
    )

    assert worker._terminal_status_for_current_process("all_files") == {}


def test_timeout_status_does_not_reuse_previous_run_summary(tmp_path, monkeypatch):
    worker = load_worker_module()
    monkeypatch.setattr(worker, "runtime_dir", lambda: tmp_path)
    worker.save_worker_status(
        {
            "ok": True,
            "status": "ok",
            "pid": 111,
            "finished_at": "2026-07-04T12:50:41+08:00",
            "summary": {"matched_case_folders": 23},
            "file_sync_summary": {"matched_cases_scanned": 23},
            "execution_summary": {"downloaded": 3},
        },
        kind="all_files",
    )
    worker.save_worker_status(
        {
            "ok": False,
            "status": "timeout",
            "pid": 222,
            "finished_at": "2026-07-04T14:32:23+08:00",
        },
        kind="all_files",
    )

    general = json.loads((tmp_path / "drive_case_sync_worker_status_latest.json").read_text(encoding="utf-8"))
    assert general["status"] == "timeout"
    assert "summary" not in general
    assert "file_sync_summary" not in general
    assert "execution_summary" not in general
    assert "summary" not in general["status_by_kind"]["all_files"]


def test_running_status_does_not_reuse_previous_terminal_fields(tmp_path, monkeypatch):
    worker = load_worker_module()
    monkeypatch.setattr(worker, "runtime_dir", lambda: tmp_path)
    worker.save_worker_status(
        {
            "ok": False,
            "status": "interrupted",
            "pid": 111,
            "finished_at": "2026-07-09T10:44:30+08:00",
            "message": "previous run interrupted",
            "signal": 15,
        },
        kind="all_files",
    )
    worker.save_worker_status(
        {
            "ok": None,
            "status": "direct_all_case_sync_running",
            "pid": 222,
            "started_at": "2026-07-09T10:47:42+08:00",
            "all_case_numbers": ["2025-0001"],
        },
        kind="all_files",
    )

    general = json.loads((tmp_path / "drive_case_sync_worker_status_latest.json").read_text(encoding="utf-8"))
    assert general["status"] == "direct_all_case_sync_running"
    assert general["pid"] == 222
    assert "finished_at" not in general
    assert "message" not in general
    assert "signal" not in general
    assert "finished_at" not in general["status_by_kind"]["all_files"]
    assert "message" not in general["status_by_kind"]["all_files"]


def test_success_status_drops_previous_timeout_fields(tmp_path, monkeypatch):
    worker = load_worker_module()
    monkeypatch.setattr(worker, "runtime_dir", lambda: tmp_path)
    worker.save_worker_status(
        {
            "ok": False,
            "status": "timeout",
            "pid": 111,
            "active_worker_pid": 222,
            "previous_status": "direct_all_case_sync_running",
            "previous_pid": 333,
            "previous_started_at": "2026-07-04T03:35:10+08:00",
            "next_step": "retry later",
            "stale_lock_audit": {"stale": True},
            "signal": 15,
            "lock_path": "/tmp/old.pid",
            "finished_at": "2026-07-04T14:32:23+08:00",
        },
        kind="all_files",
    )
    worker.save_worker_status(
        {
            "ok": True,
            "status": "ok",
            "pid": 444,
            "finished_at": "2026-07-04T16:04:41+08:00",
            "summary": {"matched_case_folders": 1},
        },
        kind="all_files",
    )

    general = json.loads((tmp_path / "drive_case_sync_worker_status_latest.json").read_text(encoding="utf-8"))
    assert general["status"] == "ok"
    assert general["summary"] == {"matched_case_folders": 1}
    for key in (
        "active_worker_pid",
        "previous_status",
        "previous_pid",
        "previous_started_at",
        "next_step",
        "stale_lock_audit",
        "signal",
        "lock_path",
    ):
        assert key not in general


def test_imported_folder_repair_defaults_to_dry_run(monkeypatch):
    worker = load_worker_module()
    calls = []
    monkeypatch.setattr(
        worker,
        "repair_imported_drive_alias_folders",
        lambda report, **kwargs: calls.append(kwargs) or {"enabled": True, "summary": {"mode": "dry_run"}},
    )
    args = SimpleNamespace(
        no_repair_imported_folders=False,
        repair_imported_folders_apply=False,
        repair_delete_duplicates=False,
        repair_max_cases=80,
        repair_max_files_per_case=300,
        repair_max_seconds_per_case=60,
    )

    result = worker.run_imported_folder_repair({"file_sync_plan": {"cases": []}}, args)

    assert calls[0]["apply"] is False
    assert calls[0]["delete_duplicate"] is False
    assert result["safety"]["default_mode"] == "dry_run"


def test_imported_folder_repair_requires_explicit_delete_flag(monkeypatch):
    worker = load_worker_module()
    calls = []
    monkeypatch.setattr(
        worker,
        "repair_imported_drive_alias_folders",
        lambda report, **kwargs: calls.append(kwargs) or {"enabled": True, "summary": {"mode": "apply"}},
    )
    args = SimpleNamespace(
        no_repair_imported_folders=False,
        repair_imported_folders_apply=True,
        repair_delete_duplicates=False,
        repair_max_cases=80,
        repair_max_files_per_case=300,
        repair_max_seconds_per_case=60,
    )

    result = worker.run_imported_folder_repair({"file_sync_plan": {"cases": []}}, args)

    assert calls[0]["apply"] is True
    assert calls[0]["delete_duplicate"] is False
    assert result["safety"]["delete_duplicate"] is False
