#!/usr/bin/env python3
"""Supervise selected host services from the verified active MAGI release.

The launcher lives in MAGI's stable host-owned ``bin`` directory. It verifies
the active marker, release manifest, and selected script hash before starting a
child, then performs a bounded restart when a new valid release becomes active.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import time
from typing import Mapping, Sequence


APP_ROOT = Path.home() / "Library" / "Application Support" / "MAGI"
ACTIVE_MARKER = APP_ROOT / "runtime" / "active-release.json"
RELEASES_ROOT = APP_ROOT / "releases"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
RELEASE_ID_RE = re.compile(r"^v3-[A-Za-z0-9._-]+$")


@dataclass(frozen=True)
class ServiceSpec:
    script: str
    arguments: tuple[str, ...] = ()


SERVICE_SPECS: Mapping[str, ServiceSpec] = {
    "memory-watchdog": ServiceSpec("scripts/ops/memory_watchdog.py"),
    "mlx-mtp": ServiceSpec(
        "scripts/serve_mlx_mtp.py",
        ("--host", "127.0.0.1", "--port", "8090"),
    ),
    "paperclip-share-gateway": ServiceSpec(
        "scripts/share_gateway.py",
        ("--port", "5014"),
    ),
    "paperclip-share-tunnel": ServiceSpec("scripts/share_tunnel_supervisor.py"),
}


class ActiveServiceError(RuntimeError):
    """The active release or requested host service cannot be trusted."""


@dataclass(frozen=True)
class ActiveServiceTarget:
    service: str
    release_id: str
    release_root: Path
    script: Path
    script_sha256: str
    manifest_sha256: str
    arguments: tuple[str, ...]

    @property
    def identity(self) -> tuple[str, str, str]:
        return (self.release_id, self.manifest_sha256, self.script_sha256)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _regular_file(path: Path, label: str) -> Path:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ActiveServiceError(f"{label} is unavailable") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise ActiveServiceError(f"{label} is not a regular file")
    return path.resolve(strict=True)


def _json_file(path: Path, label: str) -> tuple[Path, dict, bytes]:
    regular = _regular_file(path, label)
    try:
        encoded = regular.read_bytes()
        payload = json.loads(encoded.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ActiveServiceError(f"{label} is invalid") from exc
    if not isinstance(payload, dict):
        raise ActiveServiceError(f"{label} is not an object")
    return regular, payload, encoded


def resolve_active_service(
    service: str,
    marker_path: Path = ACTIVE_MARKER,
    releases_root: Path = RELEASES_ROOT,
) -> ActiveServiceTarget:
    """Resolve one allowlisted service through a hash-bound active marker."""

    spec = SERVICE_SPECS.get(service)
    if spec is None:
        raise ActiveServiceError("service is not allowlisted")
    marker_file, marker, marker_bytes = _json_file(marker_path, "active release marker")
    marker_sha = hashlib.sha256(marker_bytes).hexdigest()
    release_id = str(marker.get("release_id") or "")
    manifest_sha = str(marker.get("release_manifest_sha256") or "").lower()
    if not RELEASE_ID_RE.fullmatch(release_id) or not SHA256_RE.fullmatch(manifest_sha):
        raise ActiveServiceError("active release marker is incomplete")

    releases = releases_root.expanduser().resolve(strict=True)
    declared = Path(str(marker.get("release_root") or "")).expanduser()
    if not declared.is_absolute() or declared.is_symlink():
        raise ActiveServiceError("active release root is unsafe")
    try:
        release = declared.resolve(strict=True)
    except OSError as exc:
        raise ActiveServiceError("active release root is unavailable") from exc
    if release.parent != releases or release.name != release_id:
        raise ActiveServiceError("active release root is outside the release store")

    _manifest_file, manifest, manifest_bytes = _json_file(
        release / "release-manifest.json", "release manifest"
    )
    if hashlib.sha256(manifest_bytes).hexdigest() != manifest_sha:
        raise ActiveServiceError("release manifest hash mismatch")
    if manifest.get("release_id") != release_id or manifest.get("immutable") is not True:
        raise ActiveServiceError("release manifest identity mismatch")
    inventory = {
        str(row.get("path") or ""): str(row.get("sha256") or "").lower()
        for row in manifest.get("files") or []
        if isinstance(row, dict)
    }
    expected_script_sha = inventory.get(spec.script, "")
    if not SHA256_RE.fullmatch(expected_script_sha):
        raise ActiveServiceError("service script is absent from the release manifest")
    script = _regular_file(release / spec.script, "service script")
    if _sha256(script) != expected_script_sha:
        raise ActiveServiceError("service script hash mismatch")
    if _sha256(marker_file) != marker_sha:
        raise ActiveServiceError("active release marker changed during verification")
    return ActiveServiceTarget(
        service=service,
        release_id=release_id,
        release_root=release,
        script=script,
        script_sha256=expected_script_sha,
        manifest_sha256=manifest_sha,
        arguments=spec.arguments,
    )


def child_environment(target: ActiveServiceTarget) -> dict[str, str]:
    environment = dict(os.environ)
    environment.update(
        {
            "MAGI_ROOT": str(target.release_root),
            "MAGI_ROOT_DIR": str(target.release_root),
            "MAGI_V3_RELEASE_ID": target.release_id,
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
            "PYTHONSAFEPATH": "1",
            "PYTHONPATH": str(target.release_root),
        }
    )
    environment.pop("PYTHONHOME", None)
    return environment


def child_argv(target: ActiveServiceTarget) -> list[str]:
    return [sys.executable, "-B", str(target.script), *target.arguments]


def _terminate_child(child: subprocess.Popen, *, timeout_seconds: float = 15.0) -> None:
    if child.poll() is not None:
        return
    try:
        os.killpg(child.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        child.wait(timeout=timeout_seconds)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(child.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    child.wait(timeout=5)


def supervise(
    service: str,
    *,
    marker_path: Path = ACTIVE_MARKER,
    releases_root: Path = RELEASES_ROOT,
    poll_seconds: float = 2.0,
) -> int:
    if poll_seconds < 0.2 or poll_seconds > 60:
        raise ActiveServiceError("poll interval is outside the safe range")
    stop_signal: list[int] = []

    def request_stop(signum: int, _frame) -> None:
        if not stop_signal:
            stop_signal.append(signum)

    previous_handlers = {
        signum: signal.getsignal(signum) for signum in (signal.SIGTERM, signal.SIGINT)
    }
    for signum in previous_handlers:
        signal.signal(signum, request_stop)

    child: subprocess.Popen | None = None
    try:
        target = resolve_active_service(service, marker_path, releases_root)
        while True:
            print(
                f"[active-service] starting {service} from {target.release_id} "
                f"sha256={target.script_sha256[:12]}",
                flush=True,
            )
            child = subprocess.Popen(
                child_argv(target),
                cwd=str(target.release_root),
                env=child_environment(target),
                start_new_session=True,
                close_fds=True,
            )
            while True:
                if stop_signal:
                    _terminate_child(child)
                    return 128 + stop_signal[0]
                result = child.poll()
                if result is not None:
                    return int(result)
                time.sleep(poll_seconds)
                next_target = resolve_active_service(service, marker_path, releases_root)
                if next_target.identity == target.identity:
                    continue
                print(
                    f"[active-service] rebinding {service}: "
                    f"{target.release_id} -> {next_target.release_id}",
                    flush=True,
                )
                _terminate_child(child)
                child = None
                target = next_target
                break
    finally:
        if child is not None:
            _terminate_child(child)
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("service", choices=sorted(SERVICE_SPECS))
    parser.add_argument(
        "--poll-seconds",
        type=float,
        default=float(os.environ.get("MAGI_ACTIVE_SERVICE_POLL_SECONDS", "2")),
    )
    args = parser.parse_args(argv)
    marker = Path(os.environ.get("MAGI_ACTIVE_RELEASE_MARKER") or ACTIVE_MARKER).expanduser()
    releases = Path(os.environ.get("MAGI_RELEASES_ROOT") or RELEASES_ROOT).expanduser()
    return supervise(
        args.service,
        marker_path=marker,
        releases_root=releases,
        poll_seconds=args.poll_seconds,
    )


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ActiveServiceError as exc:
        print(f"MAGI active service launcher blocked: {exc}", file=sys.stderr)
        raise SystemExit(78)
