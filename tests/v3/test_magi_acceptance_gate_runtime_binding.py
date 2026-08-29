from __future__ import annotations

import json
from pathlib import Path

from scripts.ops import magi_acceptance_gate as gate


def test_doctor_environment_follows_active_release_and_shared_runtime(
    tmp_path: Path, monkeypatch
) -> None:
    release = tmp_path / "releases" / "v3-rc643-test"
    release.mkdir(parents=True)
    marker = tmp_path / "runtime" / "active-release.json"
    marker.parent.mkdir()
    marker.write_text(
        json.dumps(
            {
                "release": "v3",
                "release_id": release.name,
                "release_root": str(release),
            }
        ),
        encoding="utf-8",
    )
    state_dir = marker.parent / "MAGI_v3" / "shared" / "runtime"
    state_dir.mkdir(parents=True)
    monkeypatch.setenv("MAGI_V3_ACTIVE_RELEASE_MARKER", str(marker))
    monkeypatch.delenv("MAGI_LIVE_RUNTIME_ROOT", raising=False)
    monkeypatch.delenv("MAGI_V3_RELEASE_ID", raising=False)
    monkeypatch.delenv("MAGI_ROOT", raising=False)
    monkeypatch.delenv("MAGI_ROOT_DIR", raising=False)
    monkeypatch.setattr(gate, "_RUNTIME_OVERRIDE", "")
    monkeypatch.setattr(gate, "RUNTIME_DIR", tmp_path / "source" / ".runtime")

    assert gate._live_runtime_root() == release
    assert gate._live_runtime_state_dir(release) == state_dir
    assert gate._live_runtime_artifact("live_conflict_audit_ci_latest.json") == (
        state_dir / "live_conflict_audit_ci_latest.json"
    )
    environment = gate._doctor_environment()
    assert environment.items() >= {
        "MAGI_RUNTIME_DIR": str(state_dir),
        "MAGI_V3_ACTIVE_RELEASE_MARKER": str(marker),
        "MAGI_V3_RELEASE_ID": release.name,
        "MAGI_ROOT": str(release),
        "MAGI_ROOT_DIR": str(release),
        "MAGI_LIVE_RUNTIME_ROOT": str(release),
        "MAGI_SHARED_STATE_DIR": str(state_dir.parent),
        "MAGI_V3_SHARED_STATE_DIR": str(state_dir.parent),
        "MAGI_FILE_REVIEW_STATE_DIR": str(state_dir.parent / "file-review"),
        "MAGI_PAYMENT_REGISTRY_PATH": str(
            state_dir.parent / "file-review" / "downloads" / "payment_registry.json"
        ),
    }.items()


def test_source_root_uses_local_runtime_state(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "source"
    source.mkdir()
    monkeypatch.setattr(gate, "MAGI_ROOT", source)
    monkeypatch.setattr(gate, "_RUNTIME_OVERRIDE", "")

    assert gate._live_runtime_state_dir(source) == source / ".runtime"
