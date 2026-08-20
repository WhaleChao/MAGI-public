from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from scripts.ops.active_release_input_method_watchdog import (
    ActiveWatchdogError,
    WATCHDOG_RELATIVE,
    resolve_active_watchdog,
)


def _fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    releases = tmp_path / "releases"
    release = releases / "v3-test"
    script = release / WATCHDOG_RELATIVE
    script.parent.mkdir(parents=True)
    script.write_text("print('ok')\n", encoding="utf-8")
    script_sha = hashlib.sha256(script.read_bytes()).hexdigest()
    manifest = {
        "release_id": "v3-test",
        "files": [{"path": WATCHDOG_RELATIVE, "sha256": script_sha}],
    }
    manifest_path = release / "release-manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    marker = tmp_path / "active-release.json"
    marker.write_text(
        json.dumps(
            {
                "release_id": "v3-test",
                "release_root": str(release),
                "release_manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
            }
        ),
        encoding="utf-8",
    )
    return marker, releases, script


def test_active_watchdog_is_manifest_and_hash_bound(tmp_path: Path) -> None:
    marker, releases, script = _fixture(tmp_path)
    release, resolved, release_id = resolve_active_watchdog(marker, releases)
    assert release == script.parents[2].resolve()
    assert resolved == script.resolve()
    assert release_id == "v3-test"


def test_active_watchdog_rejects_script_drift(tmp_path: Path) -> None:
    marker, releases, script = _fixture(tmp_path)
    script.write_text("print('changed')\n", encoding="utf-8")
    with pytest.raises(ActiveWatchdogError, match="hash mismatch"):
        resolve_active_watchdog(marker, releases)


def test_active_watchdog_rejects_release_outside_store(tmp_path: Path) -> None:
    marker, releases, _script = _fixture(tmp_path)
    payload = json.loads(marker.read_text())
    payload["release_root"] = str(tmp_path / "other" / "v3-test")
    marker.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ActiveWatchdogError, match="unavailable|outside"):
        resolve_active_watchdog(marker, releases)
