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
