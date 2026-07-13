from __future__ import annotations

import pytest


def test_case_file_operation_lock_blocks_live_peer(monkeypatch, tmp_path):
    from api.domains import case_file_operation_lock as lock_mod

    monkeypatch.setenv("MAGI_RUNTIME_DIR", str(tmp_path))
    path = lock_mod.case_file_operation_lock_path()
    path.write_text("424242\npeer\n", encoding="utf-8")
    monkeypatch.setattr(lock_mod.os, "kill", lambda pid, sig: None)

    result = lock_mod.acquire_case_file_operation_lock(owner="probe")

    assert result["acquired"] is False
    assert result["active_pid"] == 424242
    assert result["lock_path"] == str(path)


def test_case_file_operation_lock_clears_stale_peer(monkeypatch, tmp_path):
    from api.domains import case_file_operation_lock as lock_mod

    monkeypatch.setenv("MAGI_RUNTIME_DIR", str(tmp_path))
    path = lock_mod.case_file_operation_lock_path()
    path.write_text("424242\npeer\n", encoding="utf-8")

    def fake_kill(_pid, _sig):
        raise ProcessLookupError

    monkeypatch.setattr(lock_mod.os, "kill", fake_kill)

    result = lock_mod.acquire_case_file_operation_lock(owner="probe")

    assert result["acquired"] is True
    assert result["stale_lock_cleared"] is True


def test_empty_shell_cleanup_skips_when_case_file_lock_is_busy(monkeypatch):
    from scripts.ops import cleanup_synology_empty_case_shells as mod

    monkeypatch.setattr(
        mod,
        "acquire_case_file_operation_lock",
        lambda owner: {"acquired": False, "active_pid": 999, "lock_path": "/tmp/case.pid"},
    )

    report = mod.run(apply=True, limit=1, max_seconds=1)

    assert report["ok"] is True
    assert report["skipped"] is True
    assert report["reason"] == "case_file_operation_already_running"


def test_empty_shell_cleanup_releases_case_file_lock_on_error(monkeypatch):
    from scripts.ops import cleanup_synology_empty_case_shells as mod

    released = []
    monkeypatch.setattr(mod, "acquire_case_file_operation_lock", lambda owner: {"acquired": True})
    monkeypatch.setattr(mod, "release_case_file_operation_lock", lambda: released.append(True))
    monkeypatch.setattr(mod, "_closed_cases", lambda _limit: (_ for _ in ()).throw(RuntimeError("boom")))

    with pytest.raises(RuntimeError, match="boom"):
        mod.run(apply=True, limit=1, max_seconds=1)

    assert released == [True]


def test_slow_archive_releases_case_file_lock_on_error(monkeypatch, tmp_path):
    from scripts.ops import slow_archive_closed_cases as mod

    released = []
    monkeypatch.setattr(mod, "acquire_case_file_operation_lock", lambda owner: {"acquired": True})
    monkeypatch.setattr(mod, "release_case_file_operation_lock", lambda: released.append(True))
    monkeypatch.setattr(mod, "_archive_root", lambda allow_cloud_target=False: tmp_path)
    monkeypatch.setattr(mod, "_closed_rows", lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(
        mod.sys,
        "argv",
        [
            "slow_archive_closed_cases.py",
            "--apply",
            "--allow-now",
            "--limit",
            "1",
            "--min-size-mb",
            "0",
        ],
    )

    with pytest.raises(RuntimeError, match="boom"):
        mod.main()

    assert released == [True]
