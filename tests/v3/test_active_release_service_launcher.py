from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys

import pytest

from scripts.ops.active_release_service_launcher import (
    SERVICE_SPECS,
    ActiveServiceError,
    child_argv,
    child_environment,
    resolve_active_service,
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    releases = tmp_path / "releases"
    release = releases / "v3-test"
    rows: list[dict[str, object]] = []
    for service, spec in SERVICE_SPECS.items():
        script = release / spec.script
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text(f"# {service}\n", encoding="utf-8")
        rows.append(
            {
                "path": spec.script,
                "sha256": _sha256(script),
                "size": script.stat().st_size,
                "mode": "0444",
            }
        )
    manifest = release / "release-manifest.json"
    manifest.write_text(
        json.dumps(
            {"release_id": release.name, "immutable": True, "files": rows},
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    marker = tmp_path / "active-release.json"
    marker.write_text(
        json.dumps(
            {
                "release_id": release.name,
                "release_root": str(release),
                "release_manifest_sha256": _sha256(manifest),
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return marker, releases, release


@pytest.mark.parametrize("service", sorted(SERVICE_SPECS))
def test_resolves_hash_bound_script_from_active_release(
    tmp_path: Path,
    service: str,
) -> None:
    marker, releases, release = _fixture(tmp_path)

    target = resolve_active_service(service, marker, releases)

    assert target.release_id == "v3-test"
    assert target.release_root == release.resolve()
    assert target.script == (release / SERVICE_SPECS[service].script).resolve()
    assert child_argv(target) == [
        sys.executable,
        "-B",
        str(target.script),
        *SERVICE_SPECS[service].arguments,
    ]
    environment = child_environment(target)
    assert environment["MAGI_ROOT"] == str(release.resolve())
    assert environment["MAGI_V3_RELEASE_ID"] == "v3-test"
    assert environment["PYTHONPATH"] == str(release.resolve())


def test_rejects_unknown_service_before_reading_marker(tmp_path: Path) -> None:
    with pytest.raises(ActiveServiceError, match="not allowlisted"):
        resolve_active_service("unknown", tmp_path / "missing", tmp_path / "missing")


def test_rejects_script_drift_after_sealing(tmp_path: Path) -> None:
    marker, releases, release = _fixture(tmp_path)
    script = release / SERVICE_SPECS["memory-watchdog"].script
    script.write_text("drift\n", encoding="utf-8")

    with pytest.raises(ActiveServiceError, match="script hash mismatch"):
        resolve_active_service("memory-watchdog", marker, releases)


def test_rejects_marker_manifest_hash_drift(tmp_path: Path) -> None:
    marker, releases, _release = _fixture(tmp_path)
    payload = json.loads(marker.read_text(encoding="utf-8"))
    payload["release_manifest_sha256"] = "0" * 64
    marker.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ActiveServiceError, match="manifest hash mismatch"):
        resolve_active_service("paperclip-share-gateway", marker, releases)
