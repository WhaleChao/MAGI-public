from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
from pathlib import Path


def test_background_lock_reports_active_owner(tmp_path, monkeypatch):
    monkeypatch.setenv("MAGI_BACKGROUND_LOCK_DIR", str(tmp_path))
    from scripts.ops import background_task_locks as locks

    first = locks.acquire_lock("unit-domain", owner="first", kind="test")
    out = tmp_path / "child.json"
    env = {**os.environ.copy(), "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
    try:
        code = (
            "import json, pathlib; "
            "from scripts.ops import background_task_locks as locks; "
            "lock=locks.acquire_lock('unit-domain', owner='second', kind='test'); "
            f"pathlib.Path({str(out)!r}).write_text(json.dumps(lock.as_dict()), encoding='utf-8')"
        )
        subprocess.run([sys.executable, "-c", code], cwd=str(Path(__file__).resolve().parents[1]), env=env, check=True)
        second = json.loads(out.read_text(encoding="utf-8"))

        assert first.acquired is True
        assert second["acquired"] is False
        assert second["active_owner"]["owner"] == "first"
        assert second["active_owner"]["pid"] == os.getpid()
    finally:
        first.release()


def test_background_lock_release_clears_owner_body(tmp_path, monkeypatch):
    monkeypatch.setenv("MAGI_BACKGROUND_LOCK_DIR", str(tmp_path))
    from scripts.ops import background_task_locks as locks

    lock = locks.acquire_lock("unit-cleanup", owner="first", kind="test")
    lock_path = tmp_path / "unit-cleanup.lock"
    meta_path = tmp_path / "unit-cleanup.lock.json"

    assert lock.acquired is True
    assert "first" in lock_path.read_text(encoding="utf-8")
    assert meta_path.exists()

    lock.release()

    assert lock_path.read_text(encoding="utf-8") == ""
    assert not meta_path.exists()


def test_case_file_operation_lock_is_flock_backed(tmp_path, monkeypatch):
    monkeypatch.setenv("MAGI_RUNTIME_DIR", str(tmp_path))
    from api.domains import case_file_operation_lock as mod

    mod = importlib.reload(mod)
    first = mod.acquire_case_file_operation_lock(owner="first")
    out = tmp_path / "case_child.json"
    env = {**os.environ.copy(), "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
    try:
        code = (
            "import json, pathlib; "
            "from api.domains.case_file_operation_lock import acquire_case_file_operation_lock; "
            "result=acquire_case_file_operation_lock(owner='second'); "
            f"pathlib.Path({str(out)!r}).write_text(json.dumps(result), encoding='utf-8')"
        )
        subprocess.run([sys.executable, "-c", code], cwd=str(Path(__file__).resolve().parents[1]), env=env, check=True)
        second = json.loads(out.read_text(encoding="utf-8"))

        assert first["acquired"] is True
        assert first["lock"]["metadata"]["owner"] == "first"
        assert second["acquired"] is False
        assert second["active_pid"] == os.getpid()
        assert (tmp_path / "case_file_mutation.pid.json").exists()
    finally:
        mod.release_case_file_operation_lock()
        assert not (tmp_path / "case_file_mutation.pid").exists()
