from __future__ import annotations

import json
from pathlib import Path

import pytest
import scripts.v3_python_runtime_snapshot as runtime_snapshot

from scripts.v3_python_runtime_snapshot import (
    BYTECODE_CACHE_POLICY,
    PythonRuntimeBlocked,
    build_runtime_manifest,
    verify_runtime_manifest,
    verify_runtime_manifest_singleflight,
)


def test_runtime_snapshot_uses_cross_platform_lock_backend() -> None:
    assert runtime_snapshot.fcntl.__name__ == "_PortableFileLock"
    assert callable(runtime_snapshot.fcntl.flock)


def _runtime(tmp_path: Path) -> Path:
    root = tmp_path / "venv"
    python = root / "bin" / "python3"
    python.parent.mkdir(parents=True)
    python.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    python.chmod(0o755)
    (root / "pyvenv.cfg").write_text(
        "home = " + str(python.parent) + "\n"
        "include-system-site-packages = false\n"
        "executable = " + str(python) + "\n",
        encoding="utf-8",
    )
    package = root / "lib" / "python3.14" / "site-packages" / "example.py"
    package.parent.mkdir(parents=True)
    package.write_text("VALUE = 1\n", encoding="utf-8")
    return python


def test_complete_runtime_tree_is_hash_bound_and_verifiable(tmp_path: Path) -> None:
    python = _runtime(tmp_path)
    encoded, evidence = build_runtime_manifest(python)
    manifest = tmp_path / "python-runtime-manifest.json"
    manifest.write_bytes(encoded)

    report = verify_runtime_manifest(manifest.resolve())

    payload = json.loads(encoded)
    assert report["status"] == "passed"
    assert report["tree_sha256"] == evidence["tree_sha256"] == payload["tree_sha256"]
    assert payload["file_count"] == 3
    assert payload["bytecode_cache_policy"] == BYTECODE_CACHE_POLICY
    assert any(row["path"].endswith("site-packages/example.py") for row in payload["files"])


def test_default_bytecode_cache_growth_does_not_drift_bound_runtime(tmp_path: Path) -> None:
    python = _runtime(tmp_path)
    cache = python.parent.parent / "lib" / "python3.14" / "__pycache__"
    cache.mkdir()
    (cache / "first.cpython-314.pyc").write_bytes(b"first-cache")
    encoded, evidence = build_runtime_manifest(python)
    manifest = tmp_path / "python-runtime-manifest.json"
    manifest.write_bytes(encoded)
    (cache / "second.cpython-314.pyc").write_bytes(b"second-cache")

    report = verify_runtime_manifest(
        manifest.resolve(),
        expected_tree_sha256=evidence["tree_sha256"],
    )

    payload = json.loads(encoded)
    assert report["status"] == "passed"
    assert not any("__pycache__" in row["path"] for row in payload["files"])
    assert not any("__pycache__" in row["path"] for row in payload["directories"])


def test_excluded_bytecode_cache_cannot_hide_non_bytecode_member(tmp_path: Path) -> None:
    python = _runtime(tmp_path)
    cache = python.parent.parent / "lib" / "python3.14" / "__pycache__"
    cache.mkdir()
    (cache / "startup-hook.py").write_text("raise RuntimeError\n", encoding="utf-8")

    with pytest.raises(PythonRuntimeBlocked, match="forbidden member"):
        build_runtime_manifest(python)


def test_dependency_or_mode_drift_is_rejected(tmp_path: Path) -> None:
    python = _runtime(tmp_path)
    encoded, _ = build_runtime_manifest(python)
    manifest = tmp_path / "python-runtime-manifest.json"
    manifest.write_bytes(encoded)
    package = python.parent.parent / "lib" / "python3.14" / "site-packages" / "example.py"
    package.write_text("VALUE = 2\n", encoding="utf-8")

    with pytest.raises(PythonRuntimeBlocked, match="drift"):
        verify_runtime_manifest(manifest.resolve())


def test_symlinked_runtime_directory_is_rejected(tmp_path: Path) -> None:
    python = _runtime(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (python.parent.parent / "linked").symlink_to(outside, target_is_directory=True)

    with pytest.raises(PythonRuntimeBlocked, match="symlinked directory"):
        build_runtime_manifest(python)


def test_pth_path_must_not_escape_runtime(tmp_path: Path) -> None:
    python = _runtime(tmp_path)
    outside = tmp_path / "live-v2"
    outside.mkdir()
    pth = python.parent.parent / "lib" / "python3.14" / "site-packages" / "foreign.pth"
    pth.write_text(str(outside) + "\n", encoding="utf-8")

    with pytest.raises(PythonRuntimeBlocked, match="pth path escapes"):
        build_runtime_manifest(python)


def test_pth_cannot_import_project_code_before_release(tmp_path: Path) -> None:
    python = _runtime(tmp_path)
    pth = python.parent.parent / "lib" / "python3.14" / "site-packages" / "guard.pth"
    pth.write_text("import api.mysql_connector_guard\n", encoding="utf-8")

    with pytest.raises(PythonRuntimeBlocked, match="executable line"):
        build_runtime_manifest(python)


def test_symlinked_pth_is_validated(tmp_path: Path) -> None:
    python = _runtime(tmp_path)
    site = python.parent.parent / "lib" / "python3.14" / "site-packages"
    payload = site / "guard-hook.txt"
    payload.write_text("import api.mysql_connector_guard\n", encoding="utf-8")
    (site / "guard.pth").symlink_to(payload)

    with pytest.raises(PythonRuntimeBlocked, match="executable line"):
        build_runtime_manifest(python)


def test_package_symlink_must_not_escape_runtime(tmp_path: Path) -> None:
    python = _runtime(tmp_path)
    outside = tmp_path / "foreign.py"
    outside.write_text("VALUE = 1\n", encoding="utf-8")
    package = python.parent.parent / "lib" / "python3.14" / "site-packages" / "foreign.py"
    package.symlink_to(outside)

    with pytest.raises(PythonRuntimeBlocked, match="symlink escapes"):
        build_runtime_manifest(python)


def test_system_site_packages_are_rejected(tmp_path: Path) -> None:
    python = _runtime(tmp_path)
    config = python.parent.parent / "pyvenv.cfg"
    config.write_text(
        config.read_text(encoding="utf-8").replace(
            "include-system-site-packages = false",
            "include-system-site-packages = true",
        ),
        encoding="utf-8",
    )

    with pytest.raises(PythonRuntimeBlocked, match="disable system site-packages"):
        build_runtime_manifest(python)


def test_external_base_runtime_is_hash_bound(tmp_path: Path) -> None:
    base = tmp_path / "base-python"
    real_python = base / "bin" / "python3"
    real_python.parent.mkdir(parents=True)
    real_python.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    real_python.chmod(0o755)
    (base / "INSTALL_RECEIPT.json").write_text("{}\n", encoding="utf-8")
    # A closer marker must not narrow the trusted base inventory to bin/.
    (real_python.parent / "INSTALL_RECEIPT.json").write_text("{}\n", encoding="utf-8")
    stdlib = base / "lib" / "python3.14" / "stdlib.py"
    stdlib.parent.mkdir(parents=True)
    stdlib.write_text("VALUE = 1\n", encoding="utf-8")
    venv = tmp_path / "external-venv"
    python = venv / "bin" / "python3"
    python.parent.mkdir(parents=True)
    python.symlink_to(real_python)
    (venv / "pyvenv.cfg").write_text(
        "home = " + str(real_python.parent) + "\n"
        "include-system-site-packages = false\n"
        "executable = " + str(real_python) + "\n",
        encoding="utf-8",
    )
    package = venv / "lib" / "python3.14" / "site-packages" / "example.py"
    package.parent.mkdir(parents=True)
    package.write_text("VALUE = 1\n", encoding="utf-8")
    encoded, _ = build_runtime_manifest(python)
    manifest = tmp_path / "external-runtime.json"
    manifest.write_bytes(encoded)
    stdlib.write_text("VALUE = 2\n", encoding="utf-8")

    with pytest.raises(PythonRuntimeBlocked, match="drift"):
        verify_runtime_manifest(manifest.resolve())


def test_manifest_declared_runtime_binding_cannot_be_swapped(tmp_path: Path) -> None:
    python = _runtime(tmp_path)
    encoded, _ = build_runtime_manifest(python)
    manifest = tmp_path / "runtime.json"
    manifest.write_bytes(encoded)

    with pytest.raises(PythonRuntimeBlocked, match="declared executable binding mismatch"):
        verify_runtime_manifest(
            manifest.resolve(),
            expected_python_runtime=tmp_path / "other-venv" / "bin" / "python3",
        )


def test_runtime_verification_singleflight_reuses_only_same_fresh_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    python = _runtime(tmp_path)
    encoded, evidence = build_runtime_manifest(python)
    manifest = tmp_path / "runtime.json"
    manifest.write_bytes(encoded)
    cache = tmp_path / "state" / "runtime-verification.json"

    first = verify_runtime_manifest_singleflight(
        manifest.resolve(),
        cache_path=cache.resolve(),
        max_age_seconds=3,
        expected_tree_sha256=evidence["tree_sha256"],
        expected_python_runtime=python,
        expected_python_realpath=python.resolve(),
    )
    monkeypatch.setattr(
        runtime_snapshot,
        "verify_runtime_manifest",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("rehash")),
    )
    second = verify_runtime_manifest_singleflight(
        manifest.resolve(),
        cache_path=cache.resolve(),
        max_age_seconds=3,
        expected_tree_sha256=evidence["tree_sha256"],
        expected_python_runtime=python,
        expected_python_realpath=python.resolve(),
    )

    assert first["verification_mode"] == "full"
    assert second["verification_mode"] == "startup_singleflight_receipt"


def test_expired_runtime_verification_receipt_cannot_hide_tree_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    python = _runtime(tmp_path)
    encoded, evidence = build_runtime_manifest(python)
    manifest = tmp_path / "runtime.json"
    manifest.write_bytes(encoded)
    cache = tmp_path / "state" / "runtime-verification.json"
    verify_runtime_manifest_singleflight(
        manifest.resolve(),
        cache_path=cache.resolve(),
        max_age_seconds=3,
        expected_tree_sha256=evidence["tree_sha256"],
        expected_python_runtime=python,
        expected_python_realpath=python.resolve(),
    )
    monkeypatch.setattr(runtime_snapshot.time, "time", lambda: 10_000.0)
    payload = json.loads(cache.read_text(encoding="utf-8"))
    payload["verified_at_epoch"] = 1.0
    cache.write_text(json.dumps(payload), encoding="utf-8")
    cache.chmod(0o600)
    package = python.parent.parent / "lib" / "python3.14" / "site-packages" / "example.py"
    package.write_text("VALUE = 2\n", encoding="utf-8")

    with pytest.raises(PythonRuntimeBlocked, match="drift"):
        verify_runtime_manifest_singleflight(
            manifest.resolve(),
            cache_path=cache.resolve(),
            max_age_seconds=3,
            expected_tree_sha256=evidence["tree_sha256"],
            expected_python_runtime=python,
            expected_python_realpath=python.resolve(),
        )
