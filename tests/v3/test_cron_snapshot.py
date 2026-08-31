from __future__ import annotations

import hashlib
import json
import shlex
from pathlib import Path

import pytest

from scripts.v3_cron_snapshot import CronSnapshotBlocked, render_snapshot
from scripts import v3_cron_snapshot as cron_snapshot


def _release(tmp_path: Path) -> Path:
    root = tmp_path / "candidate"
    files = []
    for relative in (
        "skills/example/action.py",
        "scripts/ops/run_with_env.py",
        "config/bin/model.sh",
    ):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("pass\n", encoding="utf-8")
        files.append(
            {
                "path": relative,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "size": path.stat().st_size,
                "mode": "0644",
            }
        )
    (root / "release-manifest.json").write_text(
        json.dumps({"schema_version": 1, "files": files}),
        encoding="utf-8",
    )
    return root


def _python(tmp_path: Path) -> Path:
    executable = tmp_path / "runtime" / "bin" / "python3"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    return executable


def test_snapshot_rebases_code_python_and_mutable_outputs(tmp_path: Path) -> None:
    release = _release(tmp_path)
    python = _python(tmp_path)
    v2 = Path("/Users/test/Library/Application Support/MAGI/runtime/MAGI_v2")
    source = tmp_path / "cron_jobs.json"
    rows = [
        {
            "id": "process",
            "cron": "0 9 * * *",
            "command": shlex.join(
                [
                    str(v2 / "venv/bin/python3"),
                    str(v2 / "skills/example/action.py"),
                    "--json-out",
                    str(v2 / ".runtime/example.json"),
                ]
            ),
            "enabled": True,
            "last_error": "must disappear",
            "result_evidence": "must also disappear",
        },
        {
            "id": "static-output",
            "cron": "0 10 * * *",
            "command": shlex.join(
                [
                    str(v2 / "venv/bin/python3"),
                    str(v2 / "scripts/ops/run_with_env.py"),
                    "--json-out",
                    str(v2 / "static/example_latest.json"),
                ]
            ),
            "enabled": True,
        },
        {"id": "macro", "cron": "0 11 * * *", "command": "@MAGI health", "enabled": True},
    ]
    source.write_text(json.dumps(rows), encoding="utf-8")
    runtime_root = tmp_path / "v3-runtime"

    encoded, report = render_snapshot(
        source=source.resolve(),
        release_root=release,
        runtime_root=runtime_root,
        python_runtime=python,
    )

    snapshot = json.loads(encoded)
    assert report["job_count"] == 3
    assert report["process_job_count"] == 2
    assert report["macro_job_count"] == 1
    assert "MAGI_v2" not in encoded.decode()
    assert "last_error" not in snapshot[0]
    assert "result_evidence" not in snapshot[0]
    first = shlex.split(snapshot[0]["command"])
    assert first[0] == str(python)
    assert first[1] == str(release / "skills/example/action.py")
    assert first[-1] == str(runtime_root / "shared/runtime/example.json")
    second = shlex.split(snapshot[1]["command"])
    assert second[-1] == str(runtime_root / "shared/static/example_latest.json")


def test_snapshot_converts_legacy_cd_prefix_to_release_cwd(tmp_path: Path) -> None:
    release = _release(tmp_path)
    python = _python(tmp_path)
    v2 = Path("/opt/MAGI_v2")
    source = tmp_path / "cron_jobs.json"
    source.write_text(
        json.dumps(
            [
                {
                    "id": "legacy-cd",
                    "cron": "0 9 * * *",
                    "command": f"cd {shlex.quote(str(v2))} && {shlex.quote(str(v2 / 'venv/bin/python3'))} skills/example/action.py",
                }
            ]
        ),
        encoding="utf-8",
    )

    encoded, _ = render_snapshot(
        source=source.resolve(),
        release_root=release,
        runtime_root=tmp_path / "runtime-root",
        python_runtime=python,
    )

    argv = shlex.split(json.loads(encoded)[0]["command"])
    assert argv == [str(python), "skills/example/action.py"]


def test_snapshot_blocks_v2_files_missing_from_release(tmp_path: Path) -> None:
    release = _release(tmp_path)
    python = _python(tmp_path)
    v2 = Path("/opt/MAGI_v2")
    source = tmp_path / "cron_jobs.json"
    source.write_text(
        json.dumps(
            [
                {
                    "id": "missing",
                    "cron": "0 9 * * *",
                    "command": shlex.join(
                        [str(v2 / "venv/bin/python3"), str(v2 / "scripts/missing.py")]
                    ),
                }
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(CronSnapshotBlocked, match="absent from V3 release"):
        render_snapshot(
            source=source.resolve(),
            release_root=release,
            runtime_root=tmp_path / "runtime-root",
            python_runtime=python,
        )


def test_snapshot_blocks_model_switch_schedule_policy_conflict(tmp_path: Path) -> None:
    release = _release(tmp_path)
    python = _python(tmp_path)
    source = tmp_path / "cron_jobs.json"
    source.write_text(
        json.dumps(
            [
                {
                    "id": "job_omlx_switch_night",
                    "cron": "10 17 * * *",
                    "command": "@MAGI model night",
                    "enabled": True,
                }
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(CronSnapshotBlocked, match="model profile policy"):
        render_snapshot(
            source=source.resolve(),
            release_root=release,
            runtime_root=tmp_path / "runtime-root",
            python_runtime=python,
        )


def test_snapshot_blocks_source_path_replacement_after_descriptor_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    release = _release(tmp_path)
    python = _python(tmp_path)
    source = tmp_path / "cron_jobs.json"
    source.write_text(
        json.dumps([{"id": "old", "command": "@MAGI old", "enabled": True}]),
        encoding="utf-8",
    )
    replacement = tmp_path / "replacement.json"
    replacement.write_text(
        json.dumps([{"id": "new", "command": "@MAGI new", "enabled": True}]),
        encoding="utf-8",
    )
    original_read = cron_snapshot.os.read
    replaced = False

    def read_and_replace(descriptor: int, size: int) -> bytes:
        nonlocal replaced
        value = original_read(descriptor, size)
        if value and not replaced:
            replacement.replace(source)
            replaced = True
        return value

    monkeypatch.setattr(cron_snapshot.os, "read", read_and_replace)
    with pytest.raises(CronSnapshotBlocked, match="cron source changed"):
        render_snapshot(
            source=source.resolve(),
            release_root=release,
            runtime_root=tmp_path / "runtime-root",
            python_runtime=python,
        )
