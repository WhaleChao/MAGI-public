from __future__ import annotations

import importlib.util
import os
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
    assert (tmp_path / "drive_case_sync_worker.pid").read_text(encoding="utf-8") == f"{os.getpid()}\n"

    worker._release_worker_lock()
    assert not (tmp_path / "drive_case_sync_worker.pid").exists()
