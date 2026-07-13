from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_process_hygiene():
    root = Path(__file__).resolve().parents[1]
    path = root / "skills" / "process-hygiene" / "action.py"
    spec = importlib.util.spec_from_file_location("process_hygiene_action_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_launchd_managed_wrappers_are_not_reported_as_orphans_or_stuck():
    module = _load_process_hygiene()
    procs = [
        {
            "pid": 100,
            "ppid": 1,
            "stat": "S",
            "etime": "02:00:00",
            "command": "/usr/bin/python -S /app/scripts/ops/run_daemon_no_site.py",
        },
        {
            "pid": 101,
            "ppid": 1,
            "stat": "S",
            "etime": "02:00:00",
            "command": "/usr/bin/python -S /app/scripts/ops/run_menubar_no_site.py",
        },
        {
            "pid": 102,
            "ppid": 1,
            "stat": "S",
            "etime": "02:00:00",
            "command": "/usr/bin/python /app/scripts/ops/osc_shell_nas_helper.py",
        },
    ]

    assert module.scan_orphans(procs) == []
    assert module.scan_stuck(procs) == []


def test_ps_all_requests_wide_command_output(monkeypatch):
    module = _load_process_hygiene()
    calls = []

    class Proc:
        stdout = (
            "123 1 S 02:00:00 /usr/bin/python -S "
            "/Users/ai/Library/Application Support/MAGI/runtime/MAGI_v2/scripts/ops/run_daemon_no_site.py\n"
        )

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return Proc()

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    procs = module._ps_all()

    assert calls[0][0][:2] == ["ps", "axww"]
    assert calls[0][1]["env"]["COLUMNS"] == "4096"
    assert procs[0]["command"].endswith("scripts/ops/run_daemon_no_site.py")


def test_drive_case_sync_worker_uses_guarded_timeout_window():
    module = _load_process_hygiene()
    procs = [
        {
            "pid": 200,
            "ppid": 1,
            "stat": "S",
            "etime": "01:05:00",
            "command": "/usr/bin/python /app/scripts/drive_case_sync_worker.py --direct-all-cases",
        }
    ]

    assert module.scan_orphans(procs) == []
    assert module.scan_stuck(procs) == []

    procs[0]["etime"] = "01:35:00"

    assert module.scan_orphans(procs)
    assert module.scan_stuck(procs)


def test_transcript_sync_detached_cron_uses_its_two_hour_timeout_window():
    module = _load_process_hygiene()
    procs = [
        {
            "pid": 210,
            "ppid": 1,
            "stat": "S",
            "etime": "01:20:00",
            "command": "/usr/bin/python /app/skills/transcript-downloader/action.py --task sync",
        }
    ]

    assert module.scan_orphans(procs) == []
    assert module.scan_stuck(procs) == []

    procs[0]["etime"] = "02:06:00"

    assert module.scan_orphans(procs)
    assert module.scan_stuck(procs)


def test_duplicate_scan_only_matches_actual_python_script_processes():
    module = _load_process_hygiene()

    shell_probe = (
        "/bin/zsh -lc ./venv/bin/python3 skills/process-hygiene/action.py --task dedup; "
        "pgrep -fl 'api/discord_bot.py|api/server.py'"
    )

    assert not module._command_executes_python_script(shell_probe, "api/server.py")
    assert module._command_executes_python_script("/usr/bin/python3 api/server.py", "api/server.py")
    assert module._command_executes_python_script(
        "/Users/ai/Desktop/MAGI_v2/venv/bin/python -m api.server",
        "api/server.py",
    )
    assert module._command_executes_python_script(
        "/opt/homebrew/bin/python3 /Users/ai/Library/Application Support/MAGI/runtime/MAGI_v2/api/discord_bot.py",
        "api/discord_bot.py",
    )

    procs = [
        {
            "pid": 300,
            "ppid": 1,
            "stat": "S",
            "etime": "00:00:05",
            "command": shell_probe,
        },
        {
            "pid": 301,
            "ppid": 1,
            "stat": "S",
            "etime": "00:01:00",
            "command": "/usr/bin/python3 /app/api/server.py",
        },
        {
            "pid": 302,
            "ppid": 1,
            "stat": "S",
            "etime": "00:02:00",
            "command": "/usr/bin/python3 /app/api/server.py",
        },
    ]

    duplicates = module.scan_duplicates(procs)

    assert len(duplicates) == 1
    assert duplicates[0]["script"] == "api/server.py"
    assert duplicates[0]["pids"] == [301, 302]


def test_duplicate_scan_prefers_daemon_child_over_orphan():
    module = _load_process_hygiene()
    procs = [
        {
            "pid": 400,
            "ppid": 1,
            "stat": "S",
            "etime": "00:10:00",
            "command": "/usr/bin/python3 /app/api/discord_bot.py",
        },
        {
            "pid": 401,
            "ppid": 399,
            "stat": "S",
            "etime": "02:00:00",
            "command": "/usr/bin/python3 api/discord_bot.py",
        },
    ]

    duplicates = module.scan_duplicates(procs)

    assert duplicates[0]["script"] == "api/discord_bot.py"
    assert duplicates[0]["keep_pid"] == 401
    assert duplicates[0]["kill_pids"] == [400]
