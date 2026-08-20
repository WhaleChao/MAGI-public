"""Canonical identity for one scheduled command across release-root rebasing."""

from __future__ import annotations

import hashlib
import json
import os
import shlex
from pathlib import Path
from typing import Any, Mapping


_CODE_ROOT_ANCHORS = frozenset(
    {"api", "casper_ecosystem", "config", "gui", "magi_v3", "scripts", "skills"}
)
_V3_MUTABLE_ROOT_ALIASES = {
    "agent": ".agent",
    "runtime": ".runtime",
}
_V3_SHARED_ROOT_ALIASES = {
    "agent": ".agent",
    "runtime": ".runtime",
    "static": "static",
    "exports": "exports",
    "metrics": "_metrics",
    "autopilot-runs": "_autopilot_runs",
}


class CronCommandIdentityError(ValueError):
    pass


def _bound_release_root() -> Path | None:
    """Return the explicitly sealed release root, without guessing path names."""

    raw_manifest = os.environ.get("MAGI_V3_RELEASE_MANIFEST", "").strip()
    release_id = os.environ.get("MAGI_V3_RELEASE_ID", "").strip()
    if not raw_manifest or not release_id:
        return None
    manifest = Path(raw_manifest)
    if (
        not manifest.is_absolute()
        or ".." in manifest.parts
        or manifest.name != "release-manifest.json"
        or manifest.parent.name != release_id
    ):
        return None
    return manifest.parent


def _root_relative_parts(path: Path) -> tuple[str, ...] | None:
    bound_root = _bound_release_root()
    if bound_root is not None:
        try:
            relative = tuple(path.relative_to(bound_root).parts)
            if relative and relative[0] in _V3_MUTABLE_ROOT_ALIASES:
                relative = (_V3_MUTABLE_ROOT_ALIASES[relative[0]], *relative[1:])
            return relative
        except ValueError:
            pass
    # Checked-in schedule definitions are maintained in the V3 source tree
    # before they are rebound into an immutable release.  Recognize that
    # checkout by file identity instead of requiring its directory name to be
    # literally ``MAGI_v3``.  This keeps command semantics stable when the
    # V2 source-of-truth path is retired.
    checkout_root = Path(__file__).resolve().parents[2]
    try:
        relative = tuple(path.relative_to(checkout_root).parts)
        if relative and relative[0] in _V3_MUTABLE_ROOT_ALIASES:
            relative = (_V3_MUTABLE_ROOT_ALIASES[relative[0]], *relative[1:])
        return relative
    except ValueError:
        pass
    parts = path.parts
    for root_name in ("MAGI_v2", "MAGI_v3"):
        indexes = [index for index, part in enumerate(parts) if part == root_name]
        if indexes:
            index = indexes[-1] + 1
            if index < len(parts) and parts[index] == "shared":
                index += 1
            relative = tuple(parts[index:])
            if (
                root_name == "MAGI_v3"
                and relative
                and relative[0] in _V3_MUTABLE_ROOT_ALIASES
            ):
                relative = (_V3_MUTABLE_ROOT_ALIASES[relative[0]], *relative[1:])
            return relative
    return None


def _bound_runtime_shared_parts(path: Path) -> tuple[str, ...] | None:
    """Map only the explicitly bound V3 runtime's shared mutable paths.

    Cron snapshots move legacy mutable paths out of the sealed release and into
    ``<runtime>/shared``.  That runtime is separate from the release root, so it
    must be recognized through the already release-bound Python executable
    rather than by guessing directory names.
    """

    raw_runtime = os.environ.get("MAGI_V3_PYTHON_RUNTIME", "").strip()
    if not raw_runtime:
        return None
    runtime = Path(raw_runtime)
    if (
        not runtime.is_absolute()
        or ".." in runtime.parts
        or runtime.parent.name != "bin"
        or not runtime.name.startswith("python")
    ):
        return None
    shared_root = runtime.parent.parent / "shared"
    try:
        relative = tuple(path.relative_to(shared_root).parts)
    except ValueError:
        return None
    if not relative:
        return None
    alias = _V3_SHARED_ROOT_ALIASES.get(relative[0])
    if alias is None:
        return None
    return (alias, *relative[1:])


def _canonical_path(value: str, *, inferred_root: Path | None = None) -> str:
    path = Path(value)
    if not path.is_absolute():
        if any(
            value == anchor or value.startswith(anchor + "/")
            for anchor in _CODE_ROOT_ANCHORS
        ):
            return "<MAGI_ROOT>/" + value
        return value
    if inferred_root is not None:
        try:
            relative = tuple(path.relative_to(inferred_root).parts)
        except ValueError:
            relative = ()
        if relative:
            if (
                len(relative) >= 3
                and relative[-3] in {"venv", ".venv"}
                and relative[-2] == "bin"
                and relative[-1].startswith("python")
            ):
                return "<PYTHON>"
            if relative[0] in _V3_MUTABLE_ROOT_ALIASES:
                relative = (_V3_MUTABLE_ROOT_ALIASES[relative[0]], *relative[1:])
            return "<MAGI_ROOT>/" + "/".join(relative)
    runtime = os.environ.get("MAGI_V3_PYTHON_RUNTIME", "").strip()
    if runtime:
        declared_runtime = Path(runtime)
        if (
            declared_runtime.is_absolute()
            and ".." not in declared_runtime.parts
            and path == declared_runtime
        ):
            return "<PYTHON>"
    shared_relative = _bound_runtime_shared_parts(path)
    if shared_relative is not None:
        return "<MAGI_ROOT>/" + "/".join(shared_relative)
    relative = _root_relative_parts(path)
    if relative is not None:
        if not relative:
            return "<MAGI_ROOT>"
        if (
            len(relative) >= 3
            and relative[-3] in {"venv", ".venv"}
            and relative[-2] == "bin"
            and relative[-1].startswith("python")
        ):
            return "<PYTHON>"
        return "<MAGI_ROOT>/" + "/".join(relative)
    return value


def _canonical_token(value: str, *, inferred_root: Path | None = None) -> str:
    if "=" in value:
        key, raw = value.split("=", 1)
        if Path(raw).is_absolute():
            return key + "=" + _canonical_path(raw, inferred_root=inferred_root)
    return _canonical_path(value, inferred_root=inferred_root)


def _infer_rebased_checkout_root(parsed: list[str]) -> Path | None:
    """Infer a checkout root only from a complete interpreter+code pair.

    An absolute ``scripts/task.py`` path by itself remains external and must
    not be collapsed.  A command which names both ``<root>/venv/bin/python``
    and a known MAGI code anchor under the same root is a self-contained
    checkout command; rebasing that pair does not change its semantics.
    """

    paths: list[Path] = []
    for token in parsed:
        raw = token.split("=", 1)[1] if "=" in token else token
        candidate = Path(raw)
        if candidate.is_absolute():
            paths.append(candidate)
    for executable in paths:
        parts = executable.parts
        if (
            len(parts) < 4
            or executable.parent.name != "bin"
            or executable.parent.parent.name not in {"venv", ".venv"}
            or not executable.name.startswith("python")
        ):
            continue
        root = executable.parent.parent.parent
        for candidate in paths:
            try:
                relative = candidate.relative_to(root)
            except ValueError:
                continue
            if relative.parts and relative.parts[0] in _CODE_ROOT_ANCHORS:
                return root
    return None


def canonical_command_tokens(command: str) -> list[str]:
    try:
        parsed = shlex.split(str(command or ""), posix=True)
    except ValueError as exc:
        raise CronCommandIdentityError(f"cron command is not parseable: {exc}") from exc
    if len(parsed) >= 3 and parsed[0] == "cd" and parsed[2] == "&&":
        parsed = parsed[3:]
    inferred_root = _infer_rebased_checkout_root(parsed)
    return [_canonical_token(token, inferred_root=inferred_root) for token in parsed]


def command_definition_sha256(job_or_command: Mapping[str, Any] | str) -> str:
    command = (
        str(job_or_command.get("command") or "")
        if isinstance(job_or_command, Mapping)
        else str(job_or_command or "")
    )
    return hashlib.sha256(
        json.dumps(
            canonical_command_tokens(command),
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
