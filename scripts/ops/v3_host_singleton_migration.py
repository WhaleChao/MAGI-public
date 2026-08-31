#!/usr/bin/env python3
"""Stage active-release replacements for legacy host-singleton LaunchAgents.

The command is deliberately non-mutating: it reads existing plists and
writes reviewed replacements to a caller-owned empty directory.  Installation
is a separate, rollback-capable maintenance transaction.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import plistlib
import stat
from pathlib import Path
from typing import Any, Mapping, Sequence


REQUIRED_LABELS = (
    "com.magi.input-method-watchdog",
    "com.magi.omlx",
    "com.magi.omlx-watchdog",
    "com.magi.rpc",
)
ACTIVE_SERVICE_LABELS = {
    "com.magi.memory-watchdog": "memory-watchdog",
    "com.magi.mlx-mtp": "mlx-mtp",
    "com.magi.paperclip-share-gateway": "paperclip-share-gateway",
    "com.magi.paperclip-share-tunnel": "paperclip-share-tunnel",
}
OPTIONAL_LABELS = tuple(ACTIVE_SERVICE_LABELS)
LABELS = REQUIRED_LABELS + OPTIONAL_LABELS


class HostSingletonMigrationError(ValueError):
    """A host-singleton plist cannot be safely rebound to V3."""


def _contains_v2(value: Any) -> bool:
    if isinstance(value, Mapping):
        return any(_contains_v2(key) or _contains_v2(item) for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return any(_contains_v2(item) for item in value)
    return "magi_v2" in str(value).lower() or "magi-v2" in str(value).lower()


def _contains_versioned_release(value: Any) -> bool:
    if isinstance(value, Mapping):
        return any(
            _contains_versioned_release(key) or _contains_versioned_release(item)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(_contains_versioned_release(item) for item in value)
    normalized = str(value).replace("\\", "/")
    return "/Application Support/MAGI/releases/v3-" in normalized


def _path(path: Path, *, kind: str) -> Path:
    raw = path.expanduser()
    if not raw.is_absolute() or raw.is_symlink():
        raise HostSingletonMigrationError(f"{kind} must be an absolute non-symlink path")
    try:
        resolved = raw.resolve(strict=True)
    except OSError as exc:
        raise HostSingletonMigrationError(f"{kind} is unavailable") from exc
    return resolved


def render_migrated_plist(
    label: str,
    current: Mapping[str, Any],
    *,
    release_root: Path,
    python_runtime: Path,
    runtime_root: Path,
    stable_launcher_source: Path | None = None,
) -> dict[str, Any]:
    """Return one V3-only plist while preserving unrelated host settings."""

    if label not in LABELS or current.get("Label") != label:
        raise HostSingletonMigrationError("host-singleton label mismatch")
    release = _path(release_root, kind="release root")
    python = _path(python_runtime, kind="Python runtime")
    runtime = runtime_root.expanduser()
    if not runtime.is_absolute() or runtime.is_symlink():
        raise HostSingletonMigrationError("runtime root must be an absolute non-symlink path")
    runtime = runtime.resolve(strict=False)
    if release.name.startswith("v3-") is False:
        raise HostSingletonMigrationError("release root is not a versioned V3 release")
    if not (release / "release-manifest.json").is_file():
        raise HostSingletonMigrationError("release manifest is missing")

    rendered = plistlib.loads(plistlib.dumps(dict(current), sort_keys=True))
    environment = dict(rendered.get("EnvironmentVariables") or {})
    for key in tuple(environment):
        if _contains_v2(environment[key]):
            environment.pop(key)
    python_bin = str(python.parent)
    existing_path = str(environment.get("PATH") or "")
    path_parts = [
        part
        for part in existing_path.split(":")
        if part
        and "magi_v2" not in part.lower()
        and "magi-v2" not in part.lower()
        and not (
            "/Application Support/MAGI/runtimes/runtime-v3-" in part.replace("\\", "/")
            and Path(part) != python.parent
        )
    ]
    environment["PATH"] = ":".join(
        dict.fromkeys(
            [
                python_bin,
                *path_parts,
                "/opt/homebrew/bin",
                "/usr/local/bin",
                "/usr/bin",
                "/bin",
                "/usr/sbin",
                "/sbin",
            ]
        )
    )

    application_root = runtime.parent.parent
    if label in ACTIVE_SERVICE_LABELS:
        launcher = application_root / "bin" / "magi-active-release-service.py"
        launcher_validation = (
            _path(stable_launcher_source, kind="stable active-service launcher")
            if stable_launcher_source is not None
            else launcher
        )
        if launcher_validation.is_symlink() or not launcher_validation.is_file():
            raise HostSingletonMigrationError("stable active-service launcher is missing")
        rendered["ProgramArguments"] = [
            str(python),
            str(launcher),
            ACTIVE_SERVICE_LABELS[label],
        ]
        rendered["WorkingDirectory"] = str(application_root)
        environment.pop("MAGI_ROOT", None)
        environment.pop("MAGI_ROOT_DIR", None)
        environment["MAGI_RUNTIME_DIR"] = str(runtime / "shared" / "runtime")
        environment["MAGI_ACTIVE_RELEASE_MARKER"] = str(
            application_root / "runtime" / "active-release.json"
        )
    elif label == "com.magi.input-method-watchdog":
        # The input-method watchdog is a host singleton and must survive release
        # archival.  Bind launchd to the stable active-release shim instead of
        # creating a new old-release reference after every V3 upgrade.
        script = application_root / "bin" / "magi-active-input-method-watchdog.py"
        if script.is_symlink() or not script.is_file():
            raise HostSingletonMigrationError(
                "stable V3 input-method watchdog shim is missing"
            )
        rendered["ProgramArguments"] = [
            str(python),
            str(script),
            "--rss-limit-mb",
            "512",
            "--cpu-limit",
            "85",
            "--strikes",
            "3",
        ]
        rendered["WorkingDirectory"] = str(application_root)
        environment.pop("MAGI_ROOT", None)
        environment["MAGI_RUNTIME_DIR"] = str(runtime / "shared" / "runtime")
    elif label == "com.magi.omlx-watchdog":
        environment.pop("MAGI_ROOT_DIR", None)
        environment["MAGI_TRAINING_LOCK_PATH"] = str(
            runtime / "shared" / "static" / "training.lock"
        )
    elif label == "com.magi.omlx":
        environment.pop("MAGI_ROOT_DIR", None)
        # The normal Homebrew oMLX path does not import MAGI release code.
        # Keeping an unused release-specific Python override here makes this
        # host singleton drift after every V3 upgrade and can break a later
        # restart after the old release is archived.  The explicitly enabled
        # unified overlay also runs as this host singleton, so it must use the
        # verified stable runtime rather than a sealed release launcher whose
        # manifest bindings are unavailable inside this LaunchAgent.
        unified = str(
            environment.get("OMLX_GEMMA4_UNIFIED_RUNTIME") or "0"
        ).strip().lower()
        if unified not in {"1", "true", "yes", "on"}:
            environment.pop("MAGI_OMLX_GEMMA4_PYTHON", None)
        else:
            environment["MAGI_OMLX_GEMMA4_PYTHON"] = str(python)
    else:
        environment["MAGI_ROOT"] = str(runtime / "shared")

    rendered["EnvironmentVariables"] = environment
    if _contains_v2(rendered):
        raise HostSingletonMigrationError(f"{label} still contains a V2 reference")
    if _contains_versioned_release(rendered):
        raise HostSingletonMigrationError(f"{label} still contains an immutable release reference")
    return rendered


def stage_migrations(
    *,
    launchagents_root: Path,
    output_root: Path,
    release_root: Path,
    python_runtime: Path,
    runtime_root: Path,
    labels: Sequence[str] | None = None,
    stable_launcher_source: Path | None = None,
) -> dict[str, Any]:
    source_root = _path(launchagents_root, kind="LaunchAgents root")
    output = output_root.expanduser()
    if not output.is_absolute() or output.is_symlink():
        raise HostSingletonMigrationError("output root must be an absolute non-symlink path")
    output = output.resolve(strict=False)
    if output.exists():
        if not output.is_dir() or any(output.iterdir()):
            raise HostSingletonMigrationError("output root must be an empty directory")
    else:
        output.mkdir(parents=True, mode=0o700)
    os.chmod(output, 0o700)

    requested = tuple(labels) if labels is not None else LABELS
    if not requested or len(requested) != len(set(requested)):
        raise HostSingletonMigrationError("requested labels are empty or duplicated")
    unknown = sorted(set(requested) - set(LABELS))
    if unknown:
        raise HostSingletonMigrationError(f"requested labels are unsupported: {unknown}")

    rows: list[dict[str, str]] = []
    absent_optional: list[str] = []
    for label in requested:
        source = source_root / f"{label}.plist"
        if source.is_symlink() or not source.is_file():
            if labels is None and label in OPTIONAL_LABELS:
                absent_optional.append(label)
                continue
            raise HostSingletonMigrationError(f"{label} source plist is unavailable")
        try:
            current = plistlib.loads(source.read_bytes())
        except (OSError, plistlib.InvalidFileException) as exc:
            raise HostSingletonMigrationError(f"{label} source plist is invalid") from exc
        rendered = render_migrated_plist(
            label,
            current,
            release_root=release_root,
            python_runtime=python_runtime,
            runtime_root=runtime_root,
            stable_launcher_source=stable_launcher_source,
        )
        encoded = plistlib.dumps(rendered, fmt=plistlib.FMT_XML, sort_keys=True)
        target = output / f"{label}.plist"
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "wb", closefd=False) as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            os.close(descriptor)
        rows.append(
            {
                "label": label,
                "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                "staged_sha256": hashlib.sha256(encoded).hexdigest(),
            }
        )
    return {
        "schema": "magi.v3.host-singleton-migration/v1",
        "status": "staged_not_installed",
        "release_root": str(release_root.resolve(strict=True)),
        "runtime_root": str(runtime_root.resolve(strict=False)),
        "v2_references": 0,
        "immutable_release_references": 0,
        "absent_optional_labels": absent_optional,
        "plists": rows,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--launchagents-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--release-root", type=Path, required=True)
    parser.add_argument("--python-runtime", type=Path, required=True)
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--stable-launcher-source", type=Path)
    parser.add_argument("--label", action="append", choices=LABELS)
    args = parser.parse_args(argv)
    try:
        result = stage_migrations(
            launchagents_root=args.launchagents_root,
            output_root=args.output_root,
            release_root=args.release_root,
            python_runtime=args.python_runtime,
            runtime_root=args.runtime_root,
            labels=args.label,
            stable_launcher_source=args.stable_launcher_source,
        )
    except (OSError, HostSingletonMigrationError) as exc:
        print(json.dumps({"status": "blocked", "error": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
