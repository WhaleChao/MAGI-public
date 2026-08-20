from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from magi_v3.config import load_settings
from magi_v3.instance import SingleActiveError, SingleActiveGuard
from magi_v3.runtime import CoreRuntime


def test_liveness_is_ready_without_initializing_state(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    runtime = CoreRuntime.build(load_settings({"MAGI_V3_STATE_DIR": str(state_dir)}))

    assert runtime.health.liveness().ready is True
    assert runtime.health.readiness().ready is False
    assert not state_dir.exists()


def test_readiness_checks_only_initialized_local_core(tmp_path: Path) -> None:
    runtime = CoreRuntime.build(
        load_settings(
            {
                "MAGI_V3_STATE_DIR": str(tmp_path / "state"),
                "MAGI_V3_HOST_ACTIVE_LOCK_PATH": str(tmp_path / "active-release.lock"),
            }
        )
    )
    runtime.initialize()
    inactive = runtime.health.readiness()
    assert inactive.ready is False
    assert inactive.components["active_release_lock"] == "not_owned"

    runtime.activate()
    report = runtime.health.readiness()

    assert report.ready is True
    assert report.components["ledger"] == "ok"
    assert report.components["active_release_lock"] == "owned"
    assert report.components["model_probe_performed"] is False
    runtime.close()


def test_importing_health_does_not_import_heavy_frameworks() -> None:
    root = Path(__file__).resolve().parents[2]
    code = (
        "import sys;sys.dont_write_bytecode=True;"
        f"sys.path.insert(0,{str(root)!r});"
        "import magi_v3.health;"
        "bad=[n for n in sys.modules if n.split('.')[0] in "
        "{'mlx','torch','playwright','fitz','pymupdf','whisper'}];"
        "print(','.join(sorted(bad)))"
    )
    proc = subprocess.run(
        [sys.executable, "-I", "-c", code],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    assert proc.stdout.strip() == ""


def test_cli_default_does_not_create_state_or_bind(tmp_path: Path) -> None:
    state_dir = tmp_path / "not-created"
    env = dict(os.environ)
    env["MAGI_V3_STATE_DIR"] = str(state_dir)
    proc = subprocess.run(
        [sys.executable, "-m", "magi_v3"],
        cwd=Path(__file__).resolve().parents[2],
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    payload = json.loads(proc.stdout)
    assert payload["status"] == "live"
    assert not state_dir.exists()


def test_cli_initialize_does_not_claim_production_readiness(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    env = dict(os.environ)
    env["MAGI_V3_STATE_DIR"] = str(state_dir)
    proc = subprocess.run(
        [sys.executable, "-m", "magi_v3", "--initialize-ledger", "--ready"],
        cwd=Path(__file__).resolve().parents[2],
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert proc.returncode == 1
    payload = json.loads(proc.stdout)
    assert payload["ready"] is False
    assert payload["components"]["active_release_lock"] == "not_owned"
    assert (state_dir / "ledger.sqlite3").is_file()


def test_single_active_guard_rejects_second_owner(tmp_path: Path) -> None:
    lock_path = tmp_path / "locks" / "active.lock"
    first = SingleActiveGuard(lock_path, instance_id="one")
    second = SingleActiveGuard(lock_path, instance_id="two")
    first.acquire()
    try:
        with pytest.raises(SingleActiveError):
            second.acquire()
    finally:
        first.release()
    second.acquire()
    second.release()
