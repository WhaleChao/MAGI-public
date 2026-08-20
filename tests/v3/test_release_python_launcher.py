from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

from scripts.v3_python_runtime_snapshot import build_runtime_manifest


ROOT = Path(__file__).resolve().parents[2]
SOURCE_LAUNCHER = ROOT / "bin" / "magi-v3-python"


@pytest.fixture(autouse=True)
def _unlock_synthetic_release_after_test(tmp_path: Path):
    yield
    release = tmp_path / "release"
    if release.is_dir() and not release.is_symlink():
        for directory, _directory_names, _file_names in os.walk(
            release, topdown=False, followlinks=False
        ):
            Path(directory).chmod(0o755)


def _sealed_release(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    release = (tmp_path / "release").resolve()
    launcher = release / "bin" / "magi-v3-python"
    runtime_verifier = release / "scripts" / "v3_python_runtime_snapshot.py"
    launcher.parent.mkdir(parents=True)
    runtime_verifier.parent.mkdir(parents=True)
    launcher.write_bytes(SOURCE_LAUNCHER.read_bytes())
    runtime_verifier.write_bytes((ROOT / "scripts" / runtime_verifier.name).read_bytes())
    launcher.chmod(0o555)
    runtime_verifier.chmod(0o444)
    rows = []
    for path, relative, mode in (
        (launcher, "bin/magi-v3-python", "0555"),
        (runtime_verifier, "scripts/v3_python_runtime_snapshot.py", "0444"),
    ):
        rows.append(
            {
                "path": relative,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "size": path.stat().st_size,
                "mode": mode,
            }
        )
    rows.sort(key=lambda row: row["path"])
    release_sha = hashlib.sha256(
        json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    manifest = {
        "schema_version": 1,
        "release_id": "test-release",
        "commit": "a" * 40,
        "immutable": True,
        "source_file_count": len(rows),
        "source_snapshot_sha256": release_sha,
        "release_sha256": release_sha,
        "files": rows,
    }
    manifest_path = release / "release-manifest.json"
    manifest_path.write_text(json.dumps(manifest, sort_keys=True) + "\n", encoding="utf-8")
    manifest_sha = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    marker = {
        "schema_version": 1,
        "release_id": "test-release",
        "commit": "a" * 40,
        "manifest": "release-manifest.json",
        "manifest_sha256": manifest_sha,
        "source_file_count": len(rows),
        "source_snapshot_sha256": release_sha,
        "release_sha256": release_sha,
    }
    marker_path = release / "RELEASE_COMPLETE.json"
    marker_path.write_text(json.dumps(marker, sort_keys=True) + "\n", encoding="utf-8")
    manifest_path.chmod(0o444)
    marker_path.chmod(0o444)
    for directory, _directory_names, _file_names in os.walk(release, topdown=False):
        Path(directory).chmod(0o555)
    return launcher, {
        "MAGI_V3_RELEASE_ID": "test-release",
        "MAGI_V3_RELEASE_MANIFEST": str(manifest_path),
        "MAGI_V3_RELEASE_MANIFEST_SHA256": manifest_sha,
    }


def _runtime(tmp_path: Path) -> Path:
    runtime = tmp_path / "external-venv" / "bin" / "python"
    runtime.parent.mkdir(parents=True)
    runtime.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' \"$PYTHONDONTWRITEBYTECODE\" \"$MAGI_AGENT_DIR\" "
        "\"$MAGI_EXPORTS_DIR\" \"$PYTHONPATH\" \"$PYTHONNOUSERSITE\" "
        "\"$PYTHONSAFEPATH\" \"$MAGI_ROOT_DIR\" \"$MAGI_ORCH_DIR\" "
        "\"$MAGI_JSON_DIR\" \"$MAGI_SKILL_PYTHON\" \"$MAGI_SKILL_OVERLAY_DIR\" "
        "\"$MAGI_SKILL_RUNTIME_SITE_PACKAGES\" \"$MAGI_SKILL_EVENTS_FILE\" "
        "\"$MAGI_SKILL_USAGE_TRACKER_FILE\" \"$MAGI_SKILL_INTERVIEW_HISTORY_FILE\" "
        "\"$MAGI_IRON_DOME_STATE_DIR\" \"$MAGI_IRON_DOME_DYNAMIC_RULES_PATH\" "
        "\"$MAGI_IRON_DOME_PATTERNS_CACHE_FILE\" \"$MAGI_IRON_DOME_UPSTREAM_STATE_FILE\" "
        "\"$*\" > \"$LAUNCHER_TEST_OUTPUT\"\n",
        encoding="utf-8",
    )
    runtime.chmod(0o755)
    (runtime.parent.parent / "pyvenv.cfg").write_text(
        "home = " + str(runtime.parent) + "\n"
        "include-system-site-packages = false\n"
        "executable = " + str(runtime) + "\n",
        encoding="utf-8",
    )
    package = runtime.parent.parent / "lib" / "python3.14" / "site-packages" / "example.py"
    package.parent.mkdir(parents=True)
    package.write_text("VALUE = 1\n", encoding="utf-8")
    return runtime


def _cron_environment(tmp_path: Path) -> dict[str, str]:
    cron_jobs = tmp_path / "cron_jobs.json"
    cron_jobs.write_text('[{"id":"heartbeat"}]\n', encoding="utf-8")
    cron_sha = hashlib.sha256(cron_jobs.read_bytes()).hexdigest()
    return {
        "MAGI_CRON_JOBS_FILE": str(cron_jobs),
        "MAGI_CRON_JOBS_SHA256": cron_sha,
        "MAGI_CRON_JOBS_SOURCE_SHA256": cron_sha,
    }


def _runtime_manifest_environment(tmp_path: Path, runtime: Path) -> dict[str, str]:
    manifest = tmp_path / "python-runtime-manifest.json"
    encoded, evidence = build_runtime_manifest(runtime)
    manifest.write_bytes(encoded)
    home = tmp_path / "home"
    runtime_root = home / "Library" / "Application Support" / "MAGI" / "runtime" / "MAGI_v3"
    state = runtime_root / "state" / "gateway"
    shared = runtime_root / "shared"
    external = tmp_path / "external-config"
    state.mkdir(parents=True)
    shared.mkdir()
    external.mkdir()
    _launcher, release_environment = _sealed_release(tmp_path)
    return {
        "HOME": str(home),
        "MAGI_V3_STATE_DIR": str(state),
        "MAGI_V3_SHARED_STATE_DIR": str(shared),
        "MAGI_V3_PYTHON_RUNTIME_MANIFEST": str(manifest),
        "MAGI_V3_PYTHON_RUNTIME_MANIFEST_SHA256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
        "MAGI_V3_PYTHON_RUNTIME_TREE_SHA256": evidence["tree_sha256"],
        "MAGI_JSON_DIR": str(external),
        **release_environment,
    }


def _launcher_from_environment(environment: dict[str, str]) -> Path:
    return Path(environment["MAGI_V3_RELEASE_MANIFEST"]).parent / "bin" / "magi-v3-python"


def test_launcher_hash_binds_external_runtime_and_redirects_mutable_paths(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    output = tmp_path / "observed.txt"
    state = tmp_path / "state"
    environment = {
        **os.environ,
        "MAGI_V3_PYTHON_RUNTIME": str(runtime),
        "MAGI_V3_PYTHON_RUNTIME_REALPATH": str(runtime.resolve()),
        "MAGI_V3_PYTHON_RUNTIME_SHA256": hashlib.sha256(runtime.read_bytes()).hexdigest(),
        "MAGI_V3_STATE_DIR": str(state),
        "MAGI_AGENT_DIR": "/live-v2/.agent",
        "MAGI_ROOT_DIR": "/live-v2",
        "MAGI_ORCH_DIR": "/live-v2/orch",
        "MAGI_JSON_DIR": "/live-v2/json",
        "MAGI_SKILL_PYTHON": "/live-v2/venv/bin/python",
        "MAGI_SKILL_OVERLAY_DIR": "/live-v2/skills",
        "MAGI_SKILL_RUNTIME_SITE_PACKAGES": "/live-v2/site-packages",
        "MAGI_SKILL_EVENTS_FILE": "/live-v2/logs/events.jsonl",
        "MAGI_SKILL_USAGE_TRACKER_FILE": "/live-v2/logs/usage.jsonl",
        "MAGI_SKILL_INTERVIEW_HISTORY_FILE": "/live-v2/logs/interviews.jsonl",
        "MAGI_IRON_DOME_STATE_DIR": "/live-v2/skills/.iron-dome",
        "MAGI_IRON_DOME_DYNAMIC_RULES_PATH": "/live-v2/skills/evolution/iron_dome_dynamic_rules.json",
        "MAGI_IRON_DOME_PATTERNS_CACHE_FILE": "/live-v2/static/iron_dome_patterns.json",
        "MAGI_IRON_DOME_UPSTREAM_STATE_FILE": "/live-v2/static/iron_dome_upstream_last.json",
        "LAUNCHER_TEST_OUTPUT": str(output),
        **_cron_environment(tmp_path),
        **_runtime_manifest_environment(tmp_path, runtime),
    }
    for name in ("MAGI_EXPORTS_DIR", "MAGI_RUNTIME_DIR"):
        environment.pop(name, None)
    shared = Path(environment["MAGI_V3_SHARED_STATE_DIR"])
    launcher = _launcher_from_environment(environment)
    release_root = launcher.parent.parent

    result = subprocess.run(
        [str(launcher), "-m", "magi_v3.gateway"],
        env=environment,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    assert result.returncode == 0
    assert output.read_text(encoding="utf-8").splitlines() == [
        "1",
        str(shared / "agent"),
        str(shared / "exports"),
        str(release_root),
        "1",
        "1",
        str(release_root),
        str(release_root / "casper_ecosystem" / "law_firm_orchestrators"),
        environment["MAGI_JSON_DIR"],
        str(runtime),
        str(shared / "skill-overlays"),
        str(shared / "skill-overlays" / ".runtime-site-packages"),
        str(shared / "skill-overlays" / ".logs" / "skill_runtime_events.jsonl"),
        str(shared / "skill-overlays" / ".logs" / "skill_usage_events.jsonl"),
        str(shared / "skill-overlays" / ".logs" / "skill_interview_history.jsonl"),
        str(shared / "skill-overlays" / ".iron-dome"),
        str(shared / "skill-overlays" / ".iron-dome" / "dynamic_rules.json"),
        str(shared / "skill-overlays" / ".iron-dome" / "patterns_cache.json"),
        str(shared / "skill-overlays" / ".iron-dome" / "upstream_last.json"),
        "-m magi_v3.gateway",
    ]


def test_launcher_refuses_hash_mismatch_without_executing_runtime(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    output = tmp_path / "must-not-exist.txt"
    environment = {
        **os.environ,
        "MAGI_V3_PYTHON_RUNTIME": str(runtime),
        "MAGI_V3_PYTHON_RUNTIME_REALPATH": str(runtime.resolve()),
        "MAGI_V3_PYTHON_RUNTIME_SHA256": "0" * 64,
        "MAGI_V3_STATE_DIR": str(tmp_path / "state"),
        "LAUNCHER_TEST_OUTPUT": str(output),
        **_cron_environment(tmp_path),
        **_runtime_manifest_environment(tmp_path, runtime),
    }
    for name in ("MAGI_AGENT_DIR", "MAGI_EXPORTS_DIR", "MAGI_RUNTIME_DIR"):
        environment.pop(name, None)
    launcher = _launcher_from_environment(environment)

    result = subprocess.run(
        [str(launcher), "-m", "magi_v3.gateway"],
        env=environment,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    assert result.returncode == 126
    assert "hash mismatch" in result.stderr
    assert not output.exists()


def test_launcher_refuses_json_directory_inside_immutable_release(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    output = tmp_path / "must-not-exist.txt"
    environment = {
        **os.environ,
        "MAGI_V3_PYTHON_RUNTIME": str(runtime),
        "MAGI_V3_PYTHON_RUNTIME_REALPATH": str(runtime.resolve()),
        "MAGI_V3_PYTHON_RUNTIME_SHA256": hashlib.sha256(runtime.read_bytes()).hexdigest(),
        "LAUNCHER_TEST_OUTPUT": str(output),
        **_cron_environment(tmp_path),
        **_runtime_manifest_environment(tmp_path, runtime),
    }
    launcher = _launcher_from_environment(environment)
    environment["MAGI_JSON_DIR"] = str(launcher.parent)

    result = subprocess.run(
        [str(launcher), "-m", "magi_v3.gateway"],
        env=environment,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    assert result.returncode == 126
    assert "MAGI_JSON_DIR overlaps immutable release" in result.stderr
    assert not output.exists()


def test_launcher_refuses_cron_hash_drift_without_executing_runtime(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    output = tmp_path / "must-not-exist.txt"
    environment = {
        **os.environ,
        "MAGI_V3_PYTHON_RUNTIME": str(runtime),
        "MAGI_V3_PYTHON_RUNTIME_REALPATH": str(runtime.resolve()),
        "MAGI_V3_PYTHON_RUNTIME_SHA256": hashlib.sha256(runtime.read_bytes()).hexdigest(),
        "MAGI_V3_STATE_DIR": str(tmp_path / "state"),
        "LAUNCHER_TEST_OUTPUT": str(output),
        **_cron_environment(tmp_path),
        **_runtime_manifest_environment(tmp_path, runtime),
    }
    Path(environment["MAGI_CRON_JOBS_FILE"]).write_text("[]\n", encoding="utf-8")
    launcher = _launcher_from_environment(environment)

    result = subprocess.run(
        [str(launcher), "-m", "magi_v3.gateway"],
        env=environment,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    assert result.returncode == 126
    assert "cron jobs file hash mismatch" in result.stderr
    assert not output.exists()


def test_launcher_refuses_incomplete_cron_source_binding(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    output = tmp_path / "must-not-exist.txt"
    environment = {
        **os.environ,
        "MAGI_V3_PYTHON_RUNTIME": str(runtime),
        "MAGI_V3_PYTHON_RUNTIME_REALPATH": str(runtime.resolve()),
        "MAGI_V3_PYTHON_RUNTIME_SHA256": hashlib.sha256(runtime.read_bytes()).hexdigest(),
        "MAGI_V3_STATE_DIR": str(tmp_path / "state"),
        "LAUNCHER_TEST_OUTPUT": str(output),
        **_cron_environment(tmp_path),
        **_runtime_manifest_environment(tmp_path, runtime),
    }
    environment.pop("MAGI_CRON_JOBS_SOURCE_SHA256")
    launcher = _launcher_from_environment(environment)

    result = subprocess.run(
        [str(launcher), "-m", "magi_v3.gateway"],
        env=environment,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    assert result.returncode == 126
    assert "MAGI_CRON_JOBS_SOURCE_SHA256" in result.stderr
    assert not output.exists()


def test_launcher_refuses_python_runtime_manifest_hash_drift(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    output = tmp_path / "must-not-exist.txt"
    environment = {
        **os.environ,
        "MAGI_V3_PYTHON_RUNTIME": str(runtime),
        "MAGI_V3_PYTHON_RUNTIME_REALPATH": str(runtime.resolve()),
        "MAGI_V3_PYTHON_RUNTIME_SHA256": hashlib.sha256(runtime.read_bytes()).hexdigest(),
        "MAGI_V3_STATE_DIR": str(tmp_path / "state"),
        "LAUNCHER_TEST_OUTPUT": str(output),
        **_cron_environment(tmp_path),
        **_runtime_manifest_environment(tmp_path, runtime),
    }
    Path(environment["MAGI_V3_PYTHON_RUNTIME_MANIFEST"]).write_text("{}\n", encoding="utf-8")
    launcher = _launcher_from_environment(environment)

    result = subprocess.run(
        [str(launcher), "-m", "magi_v3.gateway"],
        env=environment,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    assert result.returncode == 126
    assert "runtime manifest hash mismatch" in result.stderr
    assert not output.exists()


def test_launcher_refuses_runtime_tree_verifier_failure(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    output = tmp_path / "must-not-exist.txt"
    environment = {
        **os.environ,
        "MAGI_V3_PYTHON_RUNTIME": str(runtime),
        "MAGI_V3_PYTHON_RUNTIME_REALPATH": str(runtime.resolve()),
        "MAGI_V3_PYTHON_RUNTIME_SHA256": hashlib.sha256(runtime.read_bytes()).hexdigest(),
        "MAGI_V3_STATE_DIR": str(tmp_path / "state"),
        "MAGI_V3_SHARED_STATE_DIR": str(tmp_path / "shared"),
        "LAUNCHER_TEST_OUTPUT": str(output),
        **_cron_environment(tmp_path),
        **_runtime_manifest_environment(tmp_path, runtime),
    }
    package = runtime.parent.parent / "lib" / "python3.14" / "site-packages" / "example.py"
    package.write_text("VALUE = 2\n", encoding="utf-8")
    launcher = _launcher_from_environment(environment)

    result = subprocess.run(
        [str(launcher), "-m", "magi_v3.gateway"],
        env=environment,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    assert result.returncode == 126
    assert "runtime tree verification failed" in result.stderr
    assert not output.exists()


def test_launcher_refuses_symlinked_shared_state_before_runtime_executes(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    output = tmp_path / "must-not-exist.txt"
    environment = {
        **os.environ,
        "MAGI_V3_PYTHON_RUNTIME": str(runtime),
        "MAGI_V3_PYTHON_RUNTIME_REALPATH": str(runtime.resolve()),
        "MAGI_V3_PYTHON_RUNTIME_SHA256": hashlib.sha256(runtime.read_bytes()).hexdigest(),
        "LAUNCHER_TEST_OUTPUT": str(output),
        **_cron_environment(tmp_path),
        **_runtime_manifest_environment(tmp_path, runtime),
    }
    shared = Path(environment["MAGI_V3_SHARED_STATE_DIR"])
    shared.rmdir()
    launcher = _launcher_from_environment(environment)
    shared.symlink_to(launcher.parent.parent, target_is_directory=True)

    result = subprocess.run(
        [str(launcher), "-m", "magi_v3.gateway"],
        env=environment,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    assert result.returncode == 126
    assert "shared state directory is symlinked" in result.stderr
    assert not output.exists()


def test_launcher_refuses_symlinked_shared_child_before_runtime_executes(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    output = tmp_path / "must-not-exist.txt"
    environment = {
        **os.environ,
        "MAGI_V3_PYTHON_RUNTIME": str(runtime),
        "MAGI_V3_PYTHON_RUNTIME_REALPATH": str(runtime.resolve()),
        "MAGI_V3_PYTHON_RUNTIME_SHA256": hashlib.sha256(runtime.read_bytes()).hexdigest(),
        "LAUNCHER_TEST_OUTPUT": str(output),
        **_cron_environment(tmp_path),
        **_runtime_manifest_environment(tmp_path, runtime),
    }
    shared = Path(environment["MAGI_V3_SHARED_STATE_DIR"])
    escape = tmp_path / "v2-or-release-escape"
    escape.mkdir()
    (shared / "runtime").symlink_to(escape, target_is_directory=True)
    launcher = _launcher_from_environment(environment)

    result = subprocess.run(
        [str(launcher), "-m", "magi_v3.gateway"],
        env=environment,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    assert result.returncode == 126
    assert "shared state child is symlinked: runtime" in result.stderr
    assert not output.exists()
    assert not any(escape.iterdir())


@pytest.mark.parametrize("tamper", ["hash", "marker", "extra", "mode", "symlink"])
def test_trusted_launcher_refuses_release_tamper_extra_mode_or_symlink_before_runtime(
    tmp_path: Path,
    tamper: str,
) -> None:
    runtime = _runtime(tmp_path)
    output = tmp_path / "must-not-exist.txt"
    environment = {
        **os.environ,
        "MAGI_V3_PYTHON_RUNTIME": str(runtime),
        "MAGI_V3_PYTHON_RUNTIME_REALPATH": str(runtime.resolve()),
        "MAGI_V3_PYTHON_RUNTIME_SHA256": hashlib.sha256(runtime.read_bytes()).hexdigest(),
        "LAUNCHER_TEST_OUTPUT": str(output),
        **_cron_environment(tmp_path),
        **_runtime_manifest_environment(tmp_path, runtime),
    }
    launcher = _launcher_from_environment(environment)
    release = launcher.parent.parent
    verifier = release / "scripts" / "v3_python_runtime_snapshot.py"
    if tamper == "hash":
        verifier.chmod(0o644)
        verifier.write_bytes(verifier.read_bytes() + b"\n# tampered\n")
        verifier.chmod(0o444)
    elif tamper == "marker":
        marker = release / "RELEASE_COMPLETE.json"
        marker.chmod(0o644)
        payload = json.loads(marker.read_text())
        payload["release_id"] = "other-release"
        marker.write_text(json.dumps(payload) + "\n", encoding="utf-8")
        marker.chmod(0o444)
    elif tamper == "extra":
        release.chmod(0o755)
        (release / "unexpected.py").write_text("raise SystemExit(9)\n", encoding="utf-8")
        (release / "unexpected.py").chmod(0o444)
        release.chmod(0o555)
    elif tamper == "mode":
        verifier.chmod(0o644)
    else:
        release.chmod(0o755)
        (release / "escape.py").symlink_to(runtime)
        release.chmod(0o555)

    result = subprocess.run(
        [str(launcher), "-m", "magi_v3.gateway"],
        env=environment,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    assert result.returncode == 126
    assert "release verification failed" in result.stderr
    assert not output.exists()
