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
