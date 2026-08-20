from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from scripts.v3_pdf_namer_handoff import (
    DEFAULT_DESTINATION,
    DEFAULT_SOURCE,
    HandoffError,
    finalize,
    precopy,
    verify_manifest,
)
from scripts.v3_source_contract import LIVE_V2_HOME_RELATIVE, account_home


def _json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def _paths(tmp_path: Path) -> tuple[Path, Path, Path]:
    return tmp_path / "v2-pdf-state", tmp_path / "v3-pdf-state", tmp_path / "evidence" / "handoff.json"


def test_default_paths_bind_live_v2_and_the_deployed_v3_shared_state() -> None:
    home = account_home()

    assert DEFAULT_SOURCE == home / LIVE_V2_HOME_RELATIVE / "skills" / "pdf-namer"
    assert DEFAULT_DESTINATION == (
        home / "Library/Application Support/MAGI/runtime/MAGI_v3/shared/pdf-namer"
    )
    assert not DEFAULT_SOURCE.is_relative_to(Path(__file__).resolve().parents[2])
    assert "/MAGI_v3/runtime/shared/" not in str(DEFAULT_DESTINATION)


def test_precopy_is_allowlisted_private_and_public_evidence_has_no_case_payload_or_file_names(
    tmp_path: Path,
) -> None:
    source, destination, manifest = _paths(tmp_path)
    source.mkdir()
    private_case = "王小明-v-林小美"
    _json(source / "training_data.json", [{"case": private_case}])
    _json(source / "arbitrary.json", {"must_not_copy": private_case})
    (source / "action.py").write_text("raise SystemExit('must not copy')", encoding="utf-8")

    dry = precopy(source, destination, manifest, apply=False)
    assert dry["mutation_performed"] is False
    assert not destination.exists() and not manifest.exists()

    report = precopy(source, destination, manifest, apply=True)

    assert report["status"] == "precopy_complete"
    assert (destination / "training_data.json").is_file()
    assert not (destination / "arbitrary.json").exists()
    assert not (destination / "action.py").exists()
    assert stat.S_IMODE((destination / "training_data.json").stat().st_mode) == 0o600
    assert stat.S_IMODE(manifest.stat().st_mode) == 0o600
    public = manifest.read_text(encoding="utf-8")
    assert private_case not in public
    assert "training_data.json" not in public
    assert "arbitrary.json" not in public
    assert json.loads(public)["contains_business_payload"] is False
    assert json.loads(public)["contains_file_names"] is False


def test_final_refresh_accepts_normal_post_precopy_v2_learning_update(tmp_path: Path) -> None:
    source, destination, manifest = _paths(tmp_path)
    source.mkdir()
    _json(source / "training_data.json", [{"learned": 1}])
    precopy(source, destination, manifest, apply=True)
    _json(source / "training_data.json", [{"learned": 1}, {"learned": 2}])
    _json(source / "_corrections.json", {"synthetic": "correction"})

    report = finalize(source, destination, manifest)

    assert report["status"] == "complete"
    assert json.loads((destination / "training_data.json").read_text()) == [
        {"learned": 1},
        {"learned": 2},
    ]
    assert (destination / "_corrections.json").is_file()
    verified = verify_manifest(
        manifest,
        source=source,
        destination=destination,
        allowed_statuses={"complete"},
    )
    assert verified["record_count"] == 3


def test_final_refresh_rejects_destination_drift_without_touching_v2(tmp_path: Path) -> None:
    source, destination, manifest = _paths(tmp_path)
    source.mkdir()
    source_state = source / "training_data.json"
    _json(source_state, [{"learned": 1}])
    precopy(source, destination, manifest, apply=True)
    original_source = source_state.read_bytes()
    (destination / "training_data.json").write_text("[]", encoding="utf-8")
    (destination / "training_data.json").chmod(0o600)
    _json(source_state, [{"learned": 1}, {"learned": 2}])

    with pytest.raises(HandoffError, match="precopy snapshot"):
        finalize(source, destination, manifest)

    assert source_state.read_bytes() != original_source
    assert json.loads(source_state.read_text()) == [{"learned": 1}, {"learned": 2}]
    assert json.loads(manifest.read_text())["status"] == "precopy_complete"


def test_direct_final_apply_and_repeat_are_idempotent(tmp_path: Path) -> None:
    source, destination, manifest = _paths(tmp_path)
    source.mkdir()
    _json(source / "_learned_filename_rules.json", {"rule": "synthetic"})

    first = finalize(source, destination, manifest)
    second = finalize(source, destination, manifest)

    assert first["status"] == second["status"] == "complete"
    assert second["idempotent"] is True
    assert second["mutation_performed"] is False


def test_symlink_escape_hardlink_and_different_overwrite_are_rejected(tmp_path: Path) -> None:
    source, destination, manifest = _paths(tmp_path)
    source.mkdir()
    outside = tmp_path / "outside.json"
    _json(outside, [])
    (source / "training_data.json").symlink_to(outside)
    with pytest.raises(HandoffError, match="symlink"):
        precopy(source, destination, manifest, apply=True)

    (source / "training_data.json").unlink()
    os.link(outside, source / "training_data.json")
    with pytest.raises(HandoffError, match="private regular"):
        precopy(source, destination, manifest, apply=True)

    (source / "training_data.json").unlink()
    _json(source / "training_data.json", [])
    destination.mkdir()
    (destination / "training_data.json").write_text("{}", encoding="utf-8")
    (destination / "training_data.json").chmod(0o600)
    with pytest.raises(HandoffError, match="differs"):
        precopy(source, destination, manifest, apply=True)


def test_manifest_or_destination_symlink_is_rejected(tmp_path: Path) -> None:
    source, destination, manifest = _paths(tmp_path)
    source.mkdir()
    _json(source / "training_data.json", [])
    real_destination = tmp_path / "real-destination"
    real_destination.mkdir()
    destination.symlink_to(real_destination, target_is_directory=True)
    with pytest.raises(HandoffError, match="symlink"):
        precopy(source, destination, manifest, apply=True)

    destination.unlink()
    evidence = manifest.parent
    evidence.mkdir()
    real_manifest = tmp_path / "real-manifest.json"
    real_manifest.write_text("{}", encoding="utf-8")
    manifest.symlink_to(real_manifest)
    with pytest.raises(HandoffError, match="symlink"):
        precopy(source, destination, manifest, apply=True)
