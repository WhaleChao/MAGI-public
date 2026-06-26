from __future__ import annotations

import json
import sys
import time

from scripts.ops import resource_governor as rg
from scripts.ops import resource_guarded_run as guarded


def _decision(level: str, disk: float = 80, free_inactive: float = 10) -> rg.ResourceDecision:
    snap = rg.ResourceSnapshot(
        disk_free_gb=disk,
        disk_total_gb=460,
        swap_used_gb=1,
        free_gb=4,
        inactive_gb=max(0, free_inactive - 4),
        free_plus_inactive_gb=free_inactive,
    )
    return rg.ResourceDecision(
        ok=level != "critical",
        level=level,
        reasons=[],
        actions=[],
        snapshot=snap,
    )


def test_should_block_at_configured_level():
    blocked, reasons = guarded._should_block(
        _decision("core_only"),
        block_at="core_only",
        require_disk_free_gb=None,
        require_free_inactive_gb=None,
    )
    assert blocked is True
    assert reasons == ["resource_level>=core_only:core_only"]


def test_should_not_block_lower_level():
    blocked, reasons = guarded._should_block(
        _decision("throttle"),
        block_at="core_only",
        require_disk_free_gb=None,
        require_free_inactive_gb=None,
    )
    assert blocked is False
    assert reasons == []


def test_explicit_disk_requirement_blocks_even_when_level_is_normal():
    blocked, reasons = guarded._should_block(
        _decision("normal", disk=44),
        block_at="critical",
        require_disk_free_gb=60,
        require_free_inactive_gb=None,
    )
    assert blocked is True
    assert reasons == ["disk_free<60GB:44GB"]


def test_split_env_prefix_extracts_leading_assignments():
    env, command = guarded._split_env_prefix(
        ["FOO=bar", "MAGI_TEST=1", sys.executable, "-c", "print('ok')"]
    )

    assert env == {"FOO": "bar", "MAGI_TEST": "1"}
    assert command[:2] == [sys.executable, "-c"]


def test_guarded_run_accepts_env_prefix(monkeypatch, tmp_path):
    monkeypatch.setattr(guarded.resource_governor, "collect_snapshot", lambda: _decision("normal").snapshot)
    monkeypatch.setattr(guarded.resource_governor, "classify", lambda _snapshot: _decision("normal"))
    monkeypatch.setattr(guarded, "_append_event", lambda _payload: None)
    out_path = tmp_path / "env.json"

    rc = guarded.main(
        [
            "--job-id",
            "test_env_prefix",
            "--block-at",
            "critical",
            "--",
            "MAGI_TEST_ENV_PREFIX=works",
            sys.executable,
            "-c",
            (
                "import os, json, pathlib; "
                f"pathlib.Path({str(out_path)!r}).write_text("
                "json.dumps({'value': os.environ.get('MAGI_TEST_ENV_PREFIX')}), "
                "encoding='utf-8')"
            ),
        ]
    )

    assert rc == 0
    assert json.loads(out_path.read_text(encoding="utf-8"))["value"] == "works"


def test_guarded_run_timeout_kills_child_process_group(monkeypatch, tmp_path):
    monkeypatch.setattr(guarded.resource_governor, "collect_snapshot", lambda: _decision("normal").snapshot)
    monkeypatch.setattr(guarded.resource_governor, "classify", lambda _snapshot: _decision("normal"))
    monkeypatch.setattr(guarded, "_append_event", lambda _payload: None)
    monkeypatch.setattr(guarded, "_mark_drive_sync_guard_timeout", lambda *args, **kwargs: None)
    marker = tmp_path / "guarded-child-survived.txt"
    code = (
        "import subprocess,sys,time; "
        "subprocess.Popen([sys.executable,'-c',"
        f"\"import pathlib,time; time.sleep(3); pathlib.Path({str(marker)!r}).write_text('alive')\"]); "
        "time.sleep(30)"
    )

    rc = guarded.main(
        [
            "--job-id",
            "test_timeout_group",
            "--block-at",
            "critical",
            "--timeout-sec",
            "1",
            "--",
            sys.executable,
            "-c",
            code,
        ]
    )
    time.sleep(3.5)

    assert rc == 124
    assert not marker.exists()
