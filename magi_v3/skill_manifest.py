"""SkillManifest v1 validation and release-bound approval.

Imported or model-generated skills are mutable code and are not trusted merely
because they exist on disk. Their manifest declares the narrow execution
boundary; a sealed production release additionally requires the exact manifest
digest in its approved catalog.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Mapping


SCHEMA = "magi.skill-manifest/v1"
MANIFEST_NAME = "skill-manifest.json"
CATALOG_SCHEMA = "magi.approved-skill-catalog/v1"
_ID_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}")
_SHA_RE = re.compile(r"[0-9a-f]{64}")
_HOST_RE = re.compile(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)*[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_SECRET_RE = re.compile(r"[A-Z][A-Z0-9_]{1,127}")


class SkillManifestError(ValueError):
    pass


def canonical_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _unsigned(value: Mapping[str, Any]) -> dict[str, Any]:
    result = json.loads(json.dumps(value))
    result.pop("signature", None)
    return result


def manifest_digest(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_bytes(_unsigned(value))).hexdigest()


def _safe_root(value: Any, *, field: str) -> str:
    raw = str(value or "").strip()
    if not raw or "\x00" in raw or "*" in raw or "?" in raw:
        raise SkillManifestError(f"{field}: invalid root")
    path = Path(raw).expanduser()
    if any(part == ".." for part in path.parts):
        raise SkillManifestError(f"{field}: traversal forbidden")
    if not path.is_absolute():
        raise SkillManifestError(f"{field}: absolute path required")
    return str(path.resolve(strict=False))


def validate_manifest(value: Mapping[str, Any], *, skill_dir: Path | None = None) -> dict[str, Any]:
    if not isinstance(value, Mapping) or value.get("schema") != SCHEMA:
        raise SkillManifestError("unsupported SkillManifest schema")
    skill_id = str(value.get("skill_id") or "").strip().lower()
    version = str(value.get("version") or "").strip()
    if not _ID_RE.fullmatch(skill_id) or not version or len(version) > 128:
        raise SkillManifestError("invalid skill identity")

    source = value.get("source")
    if not isinstance(source, Mapping):
        raise SkillManifestError("source declaration required")
    action_sha = str(source.get("action_sha256") or "").lower()
    if not _SHA_RE.fullmatch(action_sha):
        raise SkillManifestError("source.action_sha256 must be lowercase SHA-256")
    commit = str(source.get("commit") or "").strip()
    if not commit or len(commit) > 128:
        raise SkillManifestError("source.commit required")

    permissions = value.get("permissions")
    if not isinstance(permissions, Mapping):
        raise SkillManifestError("permissions required")
    filesystem = permissions.get("filesystem")
    network = permissions.get("network")
    if not isinstance(filesystem, Mapping) or not isinstance(network, Mapping):
        raise SkillManifestError("filesystem and network permissions required")
    read_roots = tuple(_safe_root(item, field="read_roots") for item in filesystem.get("read_roots") or ())
    write_roots = tuple(_safe_root(item, field="write_roots") for item in filesystem.get("write_roots") or ())
    mode = str(network.get("mode") or "none")
    hosts = tuple(str(item or "").strip().lower().rstrip(".") for item in network.get("hosts") or ())
    if mode not in {"none", "allowlist"}:
        raise SkillManifestError("network.mode must be none or allowlist")
    if mode == "none" and hosts:
        raise SkillManifestError("network hosts forbidden when mode=none")
    if any(not _HOST_RE.fullmatch(host) for host in hosts):
        raise SkillManifestError("invalid network host")
    secrets = tuple(str(item or "").strip() for item in permissions.get("secrets") or ())
    if any(not _SECRET_RE.fullmatch(item) for item in secrets):
        raise SkillManifestError("invalid secret declaration")
    if type(permissions.get("subprocess")) is not bool:
        raise SkillManifestError("permissions.subprocess must be boolean")

    dependencies = value.get("dependencies")
    if not isinstance(dependencies, Mapping):
        raise SkillManifestError("dependencies required")
    lock_sha = str(dependencies.get("lock_sha256") or "").lower()
    if lock_sha and not _SHA_RE.fullmatch(lock_sha):
        raise SkillManifestError("invalid dependency lock SHA-256")
    lock_file = str(dependencies.get("lock_file") or "").strip()
    if lock_file and (Path(lock_file).name != lock_file or lock_file in {".", ".."}):
        raise SkillManifestError("dependency lock_file must be a local filename")
    packages = dependencies.get("packages") or []
    if not isinstance(packages, list):
        raise SkillManifestError("dependencies.packages must be a list")
    if packages and not lock_sha:
        raise SkillManifestError("dependency lock SHA-256 required when packages are declared")
    for package in packages:
        if not isinstance(package, Mapping):
            raise SkillManifestError("invalid dependency package record")
        name = str(package.get("name") or "").strip().lower()
        pinned = str(package.get("version") or "").strip()
        wheel_sha = str(package.get("sha256") or "").lower()
        if not _ID_RE.fullmatch(name) or not pinned or not _SHA_RE.fullmatch(wheel_sha):
            raise SkillManifestError("dependency packages require pinned version and wheel SHA-256")

    approval = value.get("approval")
    if not isinstance(approval, Mapping) or approval.get("status") not in {"candidate", "approved", "disabled"}:
        raise SkillManifestError("invalid approval status")
    signature = value.get("signature")
    expected = manifest_digest(value)
    if not isinstance(signature, Mapping) or signature.get("algorithm") != "release-bound-sha256":
        raise SkillManifestError("release-bound signature required")
    if signature.get("value") != expected:
        raise SkillManifestError("SkillManifest signature mismatch")

    if skill_dir is not None:
        action = skill_dir / "action.py"
        if not action.is_file() or hashlib.sha256(action.read_bytes()).hexdigest() != action_sha:
            raise SkillManifestError("action.py digest mismatch")

    normalized = json.loads(json.dumps(value))
    normalized["skill_id"] = skill_id
    normalized["permissions"]["filesystem"]["read_roots"] = list(read_roots)
    normalized["permissions"]["filesystem"]["write_roots"] = list(write_roots)
    normalized["permissions"]["network"]["hosts"] = list(hosts)
    normalized["permissions"]["secrets"] = list(secrets)
    return normalized


def load_manifest(skill_dir: Path) -> dict[str, Any]:
    path = skill_dir / MANIFEST_NAME
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SkillManifestError(f"SkillManifest unavailable: {exc}") from exc
    return validate_manifest(value, skill_dir=skill_dir)


def build_candidate_manifest(
    *, skill_dir: Path, skill_id: str, source_commit: str = "runtime-generated"
) -> dict[str, Any]:
    action = skill_dir / "action.py"
    if not action.is_file():
        raise SkillManifestError("action.py required before manifest generation")
    root = str(skill_dir.resolve())
    value: dict[str, Any] = {
        "schema": SCHEMA,
        "skill_id": skill_id.strip().lower(),
        "version": "candidate-1",
        "source": {
            "kind": "model-generated",
            "commit": source_commit,
            "action_sha256": hashlib.sha256(action.read_bytes()).hexdigest(),
        },
        "permissions": {
            "filesystem": {"read_roots": [root], "write_roots": [root]},
            "network": {"mode": "none", "hosts": []},
            "secrets": [],
            "subprocess": False,
        },
        "dependencies": {"lock_file": "", "lock_sha256": "", "packages": []},
        "approval": {"status": "candidate", "approved_by": "", "approved_at": ""},
    }
    value["signature"] = {"algorithm": "release-bound-sha256", "value": manifest_digest(value)}
    return validate_manifest(value, skill_dir=skill_dir)


def write_candidate_manifest(*, skill_dir: Path, skill_id: str) -> Path:
    value = build_candidate_manifest(skill_dir=skill_dir, skill_id=skill_id)
    path = skill_dir / MANIFEST_NAME
    temporary = path.with_name(path.name + f".tmp-{os.getpid()}")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)
    return path


def verify_catalog_approval(manifest: Mapping[str, Any], *, catalog_path: Path) -> None:
    try:
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SkillManifestError(f"approved skill catalog unavailable: {exc}") from exc
    if catalog.get("schema") != CATALOG_SCHEMA or not isinstance(catalog.get("skills"), Mapping):
        raise SkillManifestError("invalid approved skill catalog")
    record = catalog["skills"].get(manifest["skill_id"])
    if not isinstance(record, Mapping) or record.get("enabled") is not True:
        raise SkillManifestError("skill is not enabled in approved catalog")
    if record.get("manifest_sha256") != manifest_digest(manifest):
        raise SkillManifestError("approved catalog digest mismatch")


__all__ = [
    "CATALOG_SCHEMA",
    "MANIFEST_NAME",
    "SCHEMA",
    "SkillManifestError",
    "build_candidate_manifest",
    "load_manifest",
    "manifest_digest",
    "validate_manifest",
    "verify_catalog_approval",
    "write_candidate_manifest",
]
