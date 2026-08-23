from __future__ import annotations

import importlib
from pathlib import Path

from magi_v3.process_monitor import (
    ZombiePersistence,
    classify_process_rows,
    parse_ps_rows,
)


ROOT = Path("/Applications/MAGI/releases/v3-test")
PYTHON = "/opt/homebrew/opt/python@3.14/bin/python3.14"
WORKER = "skills/file-review-orchestrator/action.py"


def _snapshot(*lines: str, tracker=None, now=10.0):
    return classify_process_rows(
        parse_ps_rows("\n".join(lines)),
        magi_root=ROOT,
        zombie_tracker=tracker,
        monotonic_now=now,
    )


def test_rc600_shell_launcher_is_not_a_worker_but_its_unmanaged_child_is_orphaned():
    result = _snapshot(
        "25721 1 03-05:19:36 Ss /bin/zsh -lc env -u PYTHONPATH "
        f"{PYTHON} {WORKER} --task help",
        "25732 25721 03-05:19:36 R "
        f"{PYTHON} /old-evidence/{WORKER} --task help",
    )

    assert result["summary"] == {
        "core_count": 0,
        "worker_count": 1,
        "orphan_count": 1,
        "zombie_count": 0,
        "duplicate_groups": 0,
        "anomaly_count": 1,
    }
    assert [row["pid"] for row in result["workers"]] == [25732]
    assert [row["pid"] for row in result["orphans"]] == [25732]
    assert result["orphans"][0]["orphan_reason"] == "unmanaged_ancestry"


def test_worker_below_canonical_supervisor_ancestry_is_not_an_orphan():
    result = _snapshot(
        f"100 1 00:04:00 S {PYTHON} {ROOT}/magi_v3/legacy_background_service.py --legacy-root .",
        f"101 100 00:02:00 S {PYTHON} {ROOT}/{WORKER} --task scan",
    )

    assert result["summary"]["worker_count"] == 1
    assert result["summary"]["orphan_count"] == 0
    assert result["summary"]["anomaly_count"] == 0


def test_direct_init_worker_and_exact_duplicate_group_are_reported_once():
    command = f"{PYTHON} /old/{WORKER} --task scan"
    result = _snapshot(
        f"201 1 00:10:00 S {command}",
        f"202 1 00:09:00 S {command}",
    )

    assert result["summary"]["worker_count"] == 2
    assert result["summary"]["orphan_count"] == 2
    assert result["summary"]["duplicate_groups"] == 1
    assert result["summary"]["anomaly_count"] == 3
    assert result["duplicates"][0]["pids"] == [201, 202]


def test_transient_zombie_requires_same_five_second_persistence_on_shared_contract():
    tracker = ZombiePersistence(persistence_seconds=5.0)
    lines = (
        f"300 1 00:05:00 S {PYTHON} {ROOT}/api/server.py",
        "301 300 00:00:01 Z (Python)",
    )

    first = _snapshot(*lines, tracker=tracker, now=10.0)
    persistent = _snapshot(*lines, tracker=tracker, now=15.1)
    reaped = _snapshot(lines[0], tracker=tracker, now=16.0)

    assert first["summary"]["zombie_count"] == 0
    assert persistent["summary"]["zombie_count"] == 1
    assert persistent["summary"]["anomaly_count"] == 1
    assert reaped["summary"]["zombie_count"] == 0


def test_web_and_menubar_use_identical_snapshot_and_summary(monkeypatch, tmp_path):
    monkeypatch.setenv("MAGI_MENUBAR_NO_APPKIT", "1")
    web = importlib.import_module("api.blueprints.web_runtime")
    menubar = importlib.import_module("gui.magi_menubar")

    stdout = "\n".join(
        (
            f"400 1 00:20:00 S {PYTHON} {ROOT}/api/server.py",
            f"401 1 00:10:00 S /bin/zsh -lc {PYTHON} {WORKER} --task help",
            f"402 401 00:10:00 R {PYTHON} /old/{WORKER} --task help",
        )
    )

    class Done:
        pass

    done = Done()
    done.stdout = stdout
    fake_run = lambda *args, **kwargs: done
    monkeypatch.setattr(web.subprocess, "run", fake_run)
    monkeypatch.setattr(menubar.subprocess, "run", fake_run)
    monkeypatch.setattr(web, "_WEB_PROCESS_ZOMBIE_TRACKER", ZombiePersistence(0.0))
    monkeypatch.setattr(menubar, "_MENUBAR_PROCESS_ZOMBIE_TRACKER", ZombiePersistence(0.0))
    monkeypatch.setattr(menubar, "MAGI_ROOT", str(ROOT))

    web_result = web._collect_process_monitor(
        process_monitor_state_path=tmp_path / "missing.json",
        magi_root=ROOT,
    )
    menubar_result = menubar._collect_process_health()

    assert web_result["summary"] == menubar_result["summary"]
    assert web_result["summary"]["worker_count"] == 1
    assert web_result["summary"]["orphan_count"] == 1
    assert menubar._process_anomaly_detail(menubar_result) == "孤兒 1／殭屍 0／重複 0"


def test_process_monitor_read_failure_is_an_attention_state(monkeypatch):
    monkeypatch.setenv("MAGI_MENUBAR_NO_APPKIT", "1")
    menubar = importlib.import_module("gui.magi_menubar")
    cache = {
        "services": {name: True for name, _patterns in menubar.SERVICES},
        "process_health": {
            "ok": False,
            "summary": {"anomaly_count": 1},
        },
    }
    assert menubar._overall_state(cache) == "attention"
