from __future__ import annotations

import plistlib
from pathlib import Path

import pytest

from scripts.ops.v3_host_singleton_migration import (
    ACTIVE_SERVICE_LABELS,
    LABELS,
    HostSingletonMigrationError,
    render_migrated_plist,
    stage_migrations,
)


def _release(tmp_path: Path) -> tuple[Path, Path, Path]:
    release = tmp_path / "releases" / "v3-test"
    (release / "scripts/ops").mkdir(parents=True)
    (release / "release-manifest.json").write_text("{}", encoding="utf-8")
    (release / "scripts/ops/input_method_watchdog.py").write_text(
        "pass\n", encoding="utf-8"
    )
    python = tmp_path / "runtime" / "venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("#!/bin/sh\n", encoding="utf-8")
    python.chmod(0o755)
    runtime = tmp_path / "runtime" / "MAGI_v3"
    runtime.mkdir(parents=True)
    application_root = runtime.parent.parent
    stable_watchdog = application_root / "bin" / "magi-active-input-method-watchdog.py"
    stable_watchdog.parent.mkdir(parents=True, exist_ok=True)
    stable_watchdog.write_text("pass\n", encoding="utf-8")
    stable_launcher = application_root / "bin" / "magi-active-release-service.py"
    stable_launcher.write_text("pass\n", encoding="utf-8")
    return release, python, runtime


def _plist(label: str) -> dict:
    legacy_desktop = str(Path("/Users") / "ai" / "Desktop" / "MAGI_v2")
    legacy_runtime_bin = str(
        Path("/Users")
        / "ai"
        / "Library"
        / "Application Support"
        / "MAGI"
        / "runtime"
        / "MAGI_v2"
        / "venv"
        / "bin"
    )
    stale_v3_runtime_bin = str(
        Path("/Users")
        / "ai"
        / "Library"
        / "Application Support"
        / "MAGI"
        / "runtimes"
        / "runtime-v3-old"
        / "bin"
    )
    return {
        "Label": label,
        "ProgramArguments": ["/bin/example"],
        "EnvironmentVariables": {
            "PATH": f"{legacy_runtime_bin}:{stale_v3_runtime_bin}:/usr/bin:/bin",
            "MAGI_ROOT": legacy_desktop,
            "MAGI_ROOT_DIR": legacy_desktop,
        },
    }


@pytest.mark.parametrize("label", LABELS)
def test_rendered_host_singleton_contains_no_v2_reference(
    tmp_path: Path,
    label: str,
) -> None:
    release, python, runtime = _release(tmp_path)
    rendered = render_migrated_plist(
        label,
        _plist(label),
        release_root=release,
        python_runtime=python,
        runtime_root=runtime,
    )

    assert "magi_v2" not in repr(rendered).lower()
    assert "/usr/bin" in rendered["EnvironmentVariables"]["PATH"].split(":")
    assert "runtime-v3-old" not in rendered["EnvironmentVariables"]["PATH"]
    if label == "com.magi.input-method-watchdog":
        assert rendered["ProgramArguments"][:2] == [
            str(python.resolve()),
            str(runtime.parent.parent / "bin" / "magi-active-input-method-watchdog.py"),
        ]
        assert "MAGI_ROOT" not in rendered["EnvironmentVariables"]
        assert rendered["WorkingDirectory"] == str(runtime.parent.parent)
    if label in ACTIVE_SERVICE_LABELS:
        assert rendered["ProgramArguments"] == [
            str(python.resolve()),
            str(runtime.parent.parent / "bin" / "magi-active-release-service.py"),
            ACTIVE_SERVICE_LABELS[label],
        ]
        assert "MAGI_ROOT" not in rendered["EnvironmentVariables"]
        assert "MAGI_ROOT_DIR" not in rendered["EnvironmentVariables"]
        assert rendered["WorkingDirectory"] == str(runtime.parent.parent)
    if label == "com.magi.omlx-watchdog":
        assert rendered["EnvironmentVariables"]["MAGI_TRAINING_LOCK_PATH"].endswith(
            "MAGI_v3/shared/static/training.lock"
        )
    if label == "com.magi.rpc":
        assert rendered["EnvironmentVariables"]["MAGI_ROOT"].endswith(
            "MAGI_v3/shared"
        )


def test_staging_is_non_mutating_and_hash_bound(tmp_path: Path) -> None:
    release, python, runtime = _release(tmp_path)
    launchagents = tmp_path / "LaunchAgents"
    launchagents.mkdir()
    before: dict[str, bytes] = {}
    for label in LABELS:
        path = launchagents / f"{label}.plist"
        path.write_bytes(plistlib.dumps(_plist(label)))
        before[label] = path.read_bytes()

    output = tmp_path / "staged"
    result = stage_migrations(
        launchagents_root=launchagents,
        output_root=output,
        release_root=release,
        python_runtime=python,
        runtime_root=runtime,
    )

    assert result["status"] == "staged_not_installed"
    assert result["v2_references"] == 0
    assert result["immutable_release_references"] == 0
    assert len(result["plists"]) == len(LABELS)
    for label in LABELS:
        assert (launchagents / f"{label}.plist").read_bytes() == before[label]
        staged = output / f"{label}.plist"
        assert staged.is_file()
        assert "magi_v2" not in repr(plistlib.loads(staged.read_bytes())).lower()


def test_omlx_normal_runtime_drops_release_specific_python_override(tmp_path: Path) -> None:
    release, python, runtime = _release(tmp_path)
    current = _plist("com.magi.omlx")
    current["EnvironmentVariables"].update(
        {
            "OMLX_GEMMA4_UNIFIED_RUNTIME": "0",
            "MAGI_OMLX_GEMMA4_PYTHON": "/old/release/bin/magi-v3-python",
        }
    )
    rendered = render_migrated_plist(
        "com.magi.omlx",
        current,
        release_root=release,
        python_runtime=python,
        runtime_root=runtime,
    )
    assert "MAGI_OMLX_GEMMA4_PYTHON" not in rendered["EnvironmentVariables"]


def test_omlx_unified_runtime_rebinds_python_override(tmp_path: Path) -> None:
    release, python, runtime = _release(tmp_path)
    current = _plist("com.magi.omlx")
    current["EnvironmentVariables"].update(
        {
            "OMLX_GEMMA4_UNIFIED_RUNTIME": "1",
            "MAGI_OMLX_GEMMA4_PYTHON": "/old/release/bin/magi-v3-python",
        }
    )
    rendered = render_migrated_plist(
        "com.magi.omlx",
        current,
        release_root=release,
        python_runtime=python,
        runtime_root=runtime,
    )
    assert rendered["EnvironmentVariables"]["MAGI_OMLX_GEMMA4_PYTHON"] == str(
        python.resolve()
    )


def test_renderer_rejects_unversioned_release(tmp_path: Path) -> None:
    release, python, runtime = _release(tmp_path)
    unversioned = release.parent / "current"
    release.rename(unversioned)

    with pytest.raises(HostSingletonMigrationError, match="versioned V3"):
        render_migrated_plist(
            "com.magi.rpc",
            _plist("com.magi.rpc"),
            release_root=unversioned,
            python_runtime=python,
            runtime_root=runtime,
        )
