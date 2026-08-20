#!/usr/bin/env python3
"""Start the input-method watchdog from the verified active MAGI release.

This host-level launcher deliberately contains no release-specific path.  It
validates the active marker, release manifest, and watchdog file hash before
executing the current immutable implementation.  A failed validation exits
closed instead of falling back to an older release.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
from typing import Sequence


APP_ROOT = Path.home() / "Library" / "Application Support" / "MAGI"
ACTIVE_MARKER = APP_ROOT / "runtime" / "active-release.json"
RELEASES_ROOT = APP_ROOT / "releases"
WATCHDOG_RELATIVE = "scripts/ops/input_method_watchdog.py"
RELEASE_ID_RE = re.compile(r"v3-[A-Za-z0-9._-]+")
SHA256_RE = re.compile(r"[0-9a-f]{64}")


class ActiveWatchdogError(RuntimeError):
    """The active release cannot be trusted for watchdog execution."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _regular_file(path: Path, label: str) -> Path:
    try:
        info = path.lstat()
    except OSError as exc:
        raise ActiveWatchdogError(f"{label} is unavailable") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ActiveWatchdogError(f"{label} is not a regular file")
    return path.resolve(strict=True)


def resolve_active_watchdog(
    marker_path: Path = ACTIVE_MARKER,
    releases_root: Path = RELEASES_ROOT,
) -> tuple[Path, Path, str]:
    """Return ``(release_root, script, release_id)`` after full hash binding."""

    marker_file = _regular_file(marker_path, "active release marker")
    try:
        marker = json.loads(marker_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ActiveWatchdogError("active release marker is invalid") from exc

    release_id = str(marker.get("release_id") or "")
    manifest_sha = str(marker.get("release_manifest_sha256") or "").lower()
    if not RELEASE_ID_RE.fullmatch(release_id) or not SHA256_RE.fullmatch(manifest_sha):
        raise ActiveWatchdogError("active release marker is incomplete")

    releases = releases_root.expanduser().resolve(strict=True)
    declared = Path(str(marker.get("release_root") or "")).expanduser()
    if not declared.is_absolute() or declared.is_symlink():
        raise ActiveWatchdogError("active release root is unsafe")
    try:
        release = declared.resolve(strict=True)
    except OSError as exc:
        raise ActiveWatchdogError("active release root is unavailable") from exc
    if release.parent != releases or release.name != release_id:
        raise ActiveWatchdogError("active release root is outside the release store")

    manifest_path = _regular_file(release / "release-manifest.json", "release manifest")
    if _sha256(manifest_path) != manifest_sha:
        raise ActiveWatchdogError("release manifest hash mismatch")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ActiveWatchdogError("release manifest is invalid") from exc
    if manifest.get("release_id") != release_id:
        raise ActiveWatchdogError("release manifest identity mismatch")
    inventory = {
        str(row.get("path") or ""): str(row.get("sha256") or "").lower()
        for row in manifest.get("files") or []
        if isinstance(row, dict)
    }
    expected_script_sha = inventory.get(WATCHDOG_RELATIVE, "")
    if not SHA256_RE.fullmatch(expected_script_sha):
        raise ActiveWatchdogError("watchdog is absent from the release manifest")
    script = _regular_file(release / WATCHDOG_RELATIVE, "watchdog script")
    if _sha256(script) != expected_script_sha:
        raise ActiveWatchdogError("watchdog script hash mismatch")
    return release, script, release_id


def main(argv: Sequence[str] | None = None) -> int:
    release, script, release_id = resolve_active_watchdog()
    os.environ["MAGI_ROOT"] = str(release)
    os.environ["MAGI_ROOT_DIR"] = str(release)
    os.environ["MAGI_V3_RELEASE_ID"] = release_id
    arguments = [sys.executable, str(script), *(list(argv) if argv is not None else sys.argv[1:])]
    os.execv(sys.executable, arguments)
    return 70


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ActiveWatchdogError as exc:
        print(f"MAGI input-method watchdog launcher blocked: {exc}", file=sys.stderr)
        raise SystemExit(78)
