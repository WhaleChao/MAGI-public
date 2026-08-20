from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.v3_python_runtime_snapshot import build_runtime_manifest
from scripts.v3_validation.quality_input_builder import (
    QualityInputBuildError,
    build_quality_inputs,
)
from scripts.v3_validation import quality_input_builder as builder
from tests.v3 import test_campaign_runner as campaign_fixtures


def _write(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


def _release(tmp_path: Path) -> Path:
    return campaign_fixtures.create_release(tmp_path)[0]


def _runtime_manifest(tmp_path: Path) -> tuple[Path, Path, str]:
    venv = tmp_path / "runtime-venv"
    subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
    python = venv / "bin" / "python"
    encoded, report = build_runtime_manifest(python)
    manifest = tmp_path / "runtime-manifest.json"
    manifest.write_bytes(encoded)
    return python, manifest, str(report["tree_sha256"])


def _inputs(tmp_path: Path) -> tuple[Path, Path]:
    source = tmp_path / "legacy"
    cron = source / "cron.json"
    _write(
        cron,
        json.dumps(
            [{"id": "job", "enabled": True, "cron": "0 * * * *", "command": "@MAGI validation"}]
        ),
    )
    website = tmp_path / "website"
    _write(website / "admin" / "admin_server.py", "class Admin: pass\n")
    _write(website / "js" / "main.js", "console.log('ok')\n")
    _write(website / "data" / "client.json", '{"client":"must-not-copy"}')
    _write(website / ".admin_config.json", '{"password":"must-not-copy"}')
    _write(website / "drive-token.json", "must-not-copy")
    return cron, website


def test_builds_code_only_release_bound_quality_inputs(tmp_path: Path) -> None:
    release = _release(tmp_path)
    cron, website = _inputs(tmp_path)
    python, runtime_manifest, tree_sha = _runtime_manifest(tmp_path)
    output = tmp_path / "quality"
    manifest = build_quality_inputs(
        output_root=output,
        cron_source=cron,
        website_source=website,
        release_root=release,
        runtime_root=tmp_path / "runtime",
        python_runtime=python,
        python_runtime_manifest=runtime_manifest,
        python_runtime_tree_sha256=tree_sha,
        expected_job_count=1,
    )
    assert manifest["secret_source_read"] is False
    assert manifest["release"]["release_id"] == "v3-campaign-fixture"
    assert manifest["runtime"]["python_runtime_tree_sha256"] == tree_sha
    assert manifest["cron"]["job_count"] == 1
    assert (output / "cron_jobs.json").is_file()
    assert (output / "cron_jobs.v3.json").is_file()
    assert (output / "website/admin/admin_server.py").is_file()
    assert not (output / "website/data/client.json").exists()
    assert not (output / "website/.admin_config.json").exists()
    assert not (output / "website/drive-token.json").exists()
    assert json.loads((output / "quality-input-manifest.json").read_text())["website"]["admin_sha256"] == hashlib.sha256((output / "website/admin/admin_server.py").read_bytes()).hexdigest()


def test_fails_closed_for_existing_output_or_wrong_count(tmp_path: Path) -> None:
    release = _release(tmp_path)
    cron, website = _inputs(tmp_path)
    python, runtime_manifest, tree_sha = _runtime_manifest(tmp_path)
    with pytest.raises(QualityInputBuildError, match="job count"):
        build_quality_inputs(
            output_root=tmp_path / "bad",
            cron_source=cron,
            website_source=website,
            release_root=release,
            runtime_root=tmp_path / "runtime",
            python_runtime=python,
            python_runtime_manifest=runtime_manifest,
            python_runtime_tree_sha256=tree_sha,
            expected_job_count=102,
        )
    occupied = tmp_path / "occupied"
    occupied.mkdir()
    with pytest.raises(QualityInputBuildError, match="new absolute"):
        build_quality_inputs(
            output_root=occupied,
            cron_source=cron,
            website_source=website,
            release_root=release,
            runtime_root=tmp_path / "runtime",
            python_runtime=python,
            python_runtime_manifest=runtime_manifest,
            python_runtime_tree_sha256=tree_sha,
            expected_job_count=1,
        )


def test_runtime_tree_tampering_fails_closed(tmp_path: Path) -> None:
    release = _release(tmp_path)
    cron, website = _inputs(tmp_path)
    python, runtime_manifest, tree_sha = _runtime_manifest(tmp_path)
    with pytest.raises(QualityInputBuildError, match="runtime binding"):
        build_quality_inputs(
            output_root=tmp_path / "quality",
            cron_source=cron,
            website_source=website,
            release_root=release,
            runtime_root=tmp_path / "runtime",
            python_runtime=python,
            python_runtime_manifest=runtime_manifest,
            python_runtime_tree_sha256="0" * 64 if tree_sha != "0" * 64 else "1" * 64,
            expected_job_count=1,
        )


def test_website_path_replacement_during_descriptor_read_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "admin_server.py"
    source.write_bytes(b"A" * 4096)
    replacement = tmp_path / "replacement.py"
    replacement.write_bytes(b"B" * 4096)
    original_read = builder.os.read
    replaced = False

    def read_and_replace(descriptor: int, size: int) -> bytes:
        nonlocal replaced
        value = original_read(descriptor, size)
        if value and not replaced:
            replacement.replace(source)
            replaced = True
        return value

    monkeypatch.setattr(builder.os, "read", read_and_replace)
    with pytest.raises(QualityInputBuildError, match="path changed"):
        builder._read_regular_bytes(source)
