from __future__ import annotations

import importlib.util
import os
import json
from pathlib import Path


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
