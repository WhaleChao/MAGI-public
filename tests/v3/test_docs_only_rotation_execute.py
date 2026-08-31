from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from scripts.v3_cutover.core import CutoverError
from scripts.v3_cutover.v3_docs_only_rotation_execute import (
    MANUAL_NAMES,
    MANUAL_PREFIX,
    _allowed,
    build_docs_only_impact,
)
from scripts.v3_cutover.v3_rotation_execute import BoundDeployment, BoundFile


def _row(path: str, value: str) -> dict[str, object]:
    raw = value.encode()
    return {
        "path": path,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "size": len(raw),
        "mode": "0444",
    }


def _deployment(tmp_path: Path, release_id: str, rows: list[dict[str, object]]) -> BoundDeployment:
    root = tmp_path / release_id
    root.mkdir()
    manifest = {
        "schema_version": 1,
        "release_id": release_id,
        "release_sha256": "a" * 64,
        "source_snapshot_sha256": "a" * 64,
        "files": sorted(rows, key=lambda row: str(row["path"])),
    }
    path = root / "release-manifest.json"
    raw = json.dumps(manifest, sort_keys=True).encode()
    path.write_bytes(raw)
    bound = BoundFile(path, hashlib.sha256(raw).hexdigest(), len(raw))
    dummy = BoundFile(path, bound.sha256, bound.size)
    return BoundDeployment(
        root=root,
        marker=dummy,
        manifest=dummy,
        release_id=release_id,
        release_root=root,
        release_manifest=bound,
        release_sha="a" * 64,
        ownership_source=dummy,
        ownership_target=root / "ownership.json",
        plists={},
        ports_by_role={},
    )


def _manual_rows(version: str) -> list[dict[str, object]]:
    return [_row(MANUAL_PREFIX + name, version + name) for name in sorted(MANUAL_NAMES)]


def test_docs_only_policy_is_narrow() -> None:
    assert _allowed("docs/architecture/v3/V3_IMPLEMENTATION_STATUS.md")
    assert _allowed("scripts/docs/build_magi_encyclopedia.py")
    assert _allowed(MANUAL_PREFIX + "MAGI_V3_維修百科全書_rc643.pdf")
    assert not _allowed("api/blueprints/dashboard_pages.py")
    assert not _allowed("config/v3_service_manifest.json")
    assert not _allowed(MANUAL_PREFIX + "nested/manual.pdf")


def test_exact_manual_delta_keeps_operational_members_identical(tmp_path: Path) -> None:
    common = [_row("api/server.py", "same"), _row("config/v3_service_manifest.json", "same")]
    previous = _deployment(tmp_path, "previous", common + _manual_rows("old"))
    candidate = _deployment(tmp_path, "candidate", common + _manual_rows("new"))
    impact = build_docs_only_impact(previous, candidate)
    assert impact["operational_members_byte_identical"] is True
    assert impact["changed_member_count"] == 4
    assert impact["unchanged_member_count"] == 2
    assert len(impact["impact_sha256"]) == 64


def test_operational_delta_fails_closed(tmp_path: Path) -> None:
    previous = _deployment(tmp_path, "previous", [_row("api/server.py", "old"), *_manual_rows("old")])
    candidate = _deployment(tmp_path, "candidate", [_row("api/server.py", "new"), *_manual_rows("new")])
    with pytest.raises(CutoverError, match="operational member"):
        build_docs_only_impact(previous, candidate)


def test_missing_or_legacy_candidate_manual_fails_closed(tmp_path: Path) -> None:
    previous = _deployment(tmp_path, "previous", [_row("api/server.py", "same"), *_manual_rows("old")])
    candidate_rows = [_row("api/server.py", "same"), *_manual_rows("new")]
    candidate_rows.append(_row(MANUAL_PREFIX + "MAGI_V3_維修百科全書_rc641.md", "legacy"))
    candidate = _deployment(tmp_path, "candidate", candidate_rows)
    with pytest.raises(CutoverError, match="allowlist is not exact"):
        build_docs_only_impact(previous, candidate)


def test_no_delta_fails_closed(tmp_path: Path) -> None:
    rows = [_row("api/server.py", "same"), *_manual_rows("same")]
    previous = _deployment(tmp_path, "previous", rows)
    candidate = _deployment(tmp_path, "candidate", rows)
    with pytest.raises(CutoverError, match="no documentation delta"):
        build_docs_only_impact(previous, candidate)

