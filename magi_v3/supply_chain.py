"""Reproducible runtime inventory, SBOM and release supply-chain gates."""

from __future__ import annotations

import ast
import hashlib
import importlib.metadata
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping


LOCK_SCHEMA = "magi.python-runtime-lock/v1"
WHEELHOUSE_SCHEMA = "magi.wheelhouse-manifest/v1"
VULNERABILITY_SCHEMA = "magi.vulnerability-receipt/v1"
RELEASE_BINDING_SCHEMA = "magi.supply-chain-binding/v1"
RELEASE_BINDING_PATH = Path("config/v3_supply_chain_binding.json")
_SHA_RE = re.compile(r"[0-9a-f]{64}")
_SECRET_FILE_NAMES = {
    ".env",
    "credentials.json",
    "service-account.json",
    "token.json",
    "id_rsa",
    "id_ed25519",
}
_PRIVATE_KEY_TEXT = re.compile(
    r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"
)
_ASSIGNED_SECRET_TEXT = re.compile(
    r"(?i:(?:api[_-]?key|client[_-]?secret|password|token))"
    r"\s*[:=]\s*['\"](?P<value>[A-Za-z0-9_./+=-]{20,})['\"]"
)
_FIXTURE_SECRET_ROOTS = ("tests/", "scripts/v3_validation/")
_FIXTURE_VALUE_PREFIXES = (
    "attacker-",
    "attestation-",
    "canonical-",
    "dispatcher-",
    "expiring-",
    "fixture-",
    "offline",
    "outer-zero-owner-",
    "support-fixture-",
    "synthetic-",
    "test-",
    "unsafe-",
)


class SupplyChainError(ValueError):
    pass


def _load_json_regular(path: Path, *, label: str) -> Mapping[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise SupplyChainError(f"{label} must be a regular file")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SupplyChainError(f"{label} is invalid JSON") from exc
    if not isinstance(value, Mapping):
        raise SupplyChainError(f"{label} must be an object")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_digest(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def installed_components() -> list[dict[str, Any]]:
    components: list[dict[str, Any]] = []
    for distribution in importlib.metadata.distributions():
        name = str(distribution.metadata.get("Name") or "").strip()
        version = str(distribution.version or "").strip()
        if not name or not version:
            continue
        metadata_path = Path(distribution._path) / "METADATA"  # type: ignore[attr-defined]
        record_path = Path(distribution._path) / "RECORD"  # type: ignore[attr-defined]
        requires = sorted(str(item) for item in (distribution.requires or ()))
        components.append(
            {
                "name": name,
                "normalized_name": re.sub(r"[-_.]+", "-", name).lower(),
                "version": version,
                "purl": f"pkg:pypi/{name.lower()}@{version}",
                "metadata_sha256": _sha256(metadata_path) if metadata_path.is_file() else "",
                "record_sha256": _sha256(record_path) if record_path.is_file() else "",
                "requires_dist": requires,
            }
        )
    return sorted(components, key=lambda item: (item["normalized_name"], item["version"]))


def runtime_lock(*, python_version: str, platform: str, components: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    packages = [
        {
            "name": item["normalized_name"],
            "version": item["version"],
            "metadata_sha256": item.get("metadata_sha256", ""),
            "record_sha256": item.get("record_sha256", ""),
        }
        for item in components
    ]
    value = {
        "schema": LOCK_SCHEMA,
        "python_version": python_version,
        "platform": platform,
        "packages": packages,
    }
    value["packages_sha256"] = canonical_digest({"packages": packages})
    return value


def cyclonedx_sbom(*, components: Iterable[Mapping[str, Any]], serial_seed: str) -> dict[str, Any]:
    rows = []
    for item in components:
        rows.append(
            {
                "type": "library",
                "name": item["name"],
                "version": item["version"],
                "purl": item["purl"],
                "bom-ref": item["purl"],
                "hashes": [
                    {"alg": "SHA-256", "content": digest}
                    for digest in (item.get("metadata_sha256"), item.get("record_sha256"))
                    if digest
                ],
                "properties": [
                    {"name": "magi:requires-dist", "value": requirement}
                    for requirement in item.get("requires_dist", ())
                ],
            }
        )
    serial = hashlib.sha256(serial_seed.encode("utf-8")).hexdigest()
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "serialNumber": f"urn:uuid:{serial[:8]}-{serial[8:12]}-{serial[12:16]}-{serial[16:20]}-{serial[20:32]}",
        "version": 1,
        "components": rows,
    }


def wheelhouse_manifest(wheelhouse: Path) -> dict[str, Any]:
    files = []
    for path in sorted(wheelhouse.rglob("*") if wheelhouse.is_dir() else ()):
        if path.is_file() and path.suffix.lower() in {".whl", ".zip", ".gz"}:
            files.append(
                {
                    "filename": path.relative_to(wheelhouse).as_posix(),
                    "size": path.stat().st_size,
                    "sha256": _sha256(path),
                }
            )
    value = {"schema": WHEELHOUSE_SCHEMA, "files": files}
    value["files_sha256"] = canonical_digest({"files": files})
    return value


def verify_wheelhouse(wheelhouse: Path, manifest: Mapping[str, Any]) -> None:
    if manifest.get("schema") != WHEELHOUSE_SCHEMA or not isinstance(manifest.get("files"), list):
        raise SupplyChainError("invalid wheelhouse manifest")
    observed = wheelhouse_manifest(wheelhouse)
    if observed["files"] != manifest["files"] or observed["files_sha256"] != manifest.get("files_sha256"):
        raise SupplyChainError("wheelhouse content drift")
    if not observed["files"]:
        raise SupplyChainError("wheelhouse is empty")


def scan_release_secrets(root: Path) -> list[str]:
    findings: list[str] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            findings.append(f"symlink:{path.relative_to(root)}")
            continue
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if path.name.lower() in _SECRET_FILE_NAMES or path.suffix.lower() in {".pem", ".p12", ".pfx", ".key"}:
            findings.append(f"secret_filename:{relative}")
            continue
        if path.suffix.lower() not in {".py", ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".sh"}:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        relative_text = relative.as_posix()
        assigned_secret = any(
            not (
                relative_text.startswith(_FIXTURE_SECRET_ROOTS)
                and match.group("value").lower().startswith(_FIXTURE_VALUE_PREFIXES)
            )
            for match in _ASSIGNED_SECRET_TEXT.finditer(text)
        )
        if _PRIVATE_KEY_TEXT.search(text) or assigned_secret:
            findings.append(f"secret_literal:{relative}")
    return findings


def _runtime_install_call(node: ast.Call) -> bool:
    try:
        text = ast.unparse(node).lower()
    except Exception:
        return False
    return (
        ("subprocess.run" in text or "subprocess.popen" in text)
        and (("pip" in text and "install" in text) or ("playwright" in text and "install" in text))
    )


def audit_runtime_install_policy(root: Path) -> list[str]:
    allowed = {
        "magi_v3/selfhost.py": "installer workflow",
        "skills/engine/playwright_wrapper.py": "runtime_install_forbidden_in_sealed_release",
        "skills/evolution/skill_genesis.py": "runtime_install_forbidden_in_sealed_release",
        "skills/management/auto_skill.py": "runtime install forbidden in sealed release",
        "skills/file-review-orchestrator/action.py": "runtime dependency install blocked in sealed release",
    }
    findings: list[str] = []
    for base_name in ("api", "magi_v3", "skills", "casper_ecosystem"):
        base = root / base_name
        for path in sorted(base.rglob("*.py") if base.is_dir() else ()):
            relative = path.relative_to(root).as_posix()
            try:
                source = path.read_text(encoding="utf-8")
                tree = ast.parse(source)
            except (OSError, UnicodeError, SyntaxError):
                continue
            calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call) and _runtime_install_call(node)]
            if not calls:
                continue
            marker = allowed.get(relative)
            if marker is None or marker not in source:
                findings.append(f"runtime_install:{relative}")
    return findings


def validate_vulnerability_receipt(
    receipt: Mapping[str, Any], *, expected_packages_sha256: str
) -> None:
    if receipt.get("schema") != VULNERABILITY_SCHEMA:
        raise SupplyChainError("invalid vulnerability receipt schema")
    if receipt.get("packages_sha256") != expected_packages_sha256:
        raise SupplyChainError("vulnerability receipt dependency drift")
    if receipt.get("scanner") not in {"pip-audit", "osv-scanner"}:
        raise SupplyChainError("unapproved vulnerability scanner")
    generated = str(receipt.get("generated_at") or "")
    try:
        timestamp = datetime.fromisoformat(generated.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SupplyChainError("invalid vulnerability receipt timestamp") from exc
    if timestamp.tzinfo is None:
        raise SupplyChainError("vulnerability receipt timestamp must be timezone-aware")
    age = datetime.now(timezone.utc) - timestamp.astimezone(timezone.utc)
    if age.total_seconds() < 0 or age.days > 7:
        raise SupplyChainError("vulnerability receipt expired")
    vulnerabilities = receipt.get("vulnerabilities")
    if not isinstance(vulnerabilities, list):
        raise SupplyChainError("vulnerability list required")
    if receipt.get("vulnerability_count") != len(vulnerabilities):
        raise SupplyChainError("vulnerability receipt count mismatch")
    expected_ok = not vulnerabilities
    if receipt.get("ok") is not expected_ok:
        raise SupplyChainError("vulnerability receipt status mismatch")
    if not expected_ok:
        raise SupplyChainError("vulnerability receipt is not release-clean")


def validate_release_supply_chain_binding(
    release_root: Path,
    *,
    base_runtime_manifest: Path | None = None,
) -> dict[str, Any]:
    """Verify the immutable base + hashed overlay evidence shipped in a release."""

    root = release_root.expanduser().resolve()
    binding = _load_json_regular(root / RELEASE_BINDING_PATH, label="supply-chain binding")
    if binding.get("schema") != RELEASE_BINDING_SCHEMA:
        raise SupplyChainError("invalid release supply-chain binding schema")
    supplied_digest = str(binding.get("binding_sha256") or "")
    unsigned = dict(binding)
    unsigned.pop("binding_sha256", None)
    if supplied_digest != canonical_digest(unsigned):
        raise SupplyChainError("release supply-chain binding digest mismatch")
    if binding.get("runtime_strategy") != "immutable_base_plus_hashed_offline_overlay":
        raise SupplyChainError("unsupported runtime supply-chain strategy")
    packages_sha = str(binding.get("packages_sha256") or "")
    if _SHA_RE.fullmatch(packages_sha) is None:
        raise SupplyChainError("release package inventory digest is invalid")

    base = binding.get("base_runtime")
    if not isinstance(base, Mapping):
        raise SupplyChainError("base runtime binding is missing")
    base_manifest_sha = str(base.get("manifest_sha256") or "")
    base_tree_sha = str(base.get("tree_sha256") or "")
    if (
        not str(base.get("runtime_id") or "").strip()
        or _SHA_RE.fullmatch(base_manifest_sha) is None
        or _SHA_RE.fullmatch(base_tree_sha) is None
    ):
        raise SupplyChainError("base runtime binding is invalid")
    if base_runtime_manifest is not None:
        external = base_runtime_manifest.expanduser()
        if (
            not external.is_absolute()
            or external.is_symlink()
            or not external.is_file()
            or _sha256(external) != base_manifest_sha
        ):
            raise SupplyChainError("base runtime manifest digest mismatch")
        base_value = _load_json_regular(external, label="base runtime manifest")
        if base_value.get("tree_sha256") != base_tree_sha:
            raise SupplyChainError("base runtime tree digest mismatch")

    raw_artifacts = binding.get("artifacts")
    required = {
        "runtime_lock",
        "sbom",
        "wheelhouse_manifest",
        "vulnerability_receipt",
    }
    if not isinstance(raw_artifacts, Mapping) or set(raw_artifacts) != required:
        raise SupplyChainError("release supply-chain artifact inventory is incomplete")
    loaded: dict[str, Mapping[str, Any]] = {}
    artifact_summary: dict[str, dict[str, Any]] = {}
    for role in sorted(required):
        descriptor = raw_artifacts[role]
        if not isinstance(descriptor, Mapping):
            raise SupplyChainError(f"supply-chain artifact descriptor is invalid: {role}")
        relative_text = str(descriptor.get("path") or "")
        relative = Path(relative_text)
        if (
            not relative_text.startswith("config/supply-chain/")
            or relative.is_absolute()
            or ".." in relative.parts
        ):
            raise SupplyChainError(f"supply-chain artifact path is unsafe: {role}")
        path = root / relative
        try:
            resolved = path.resolve(strict=True)
            resolved.relative_to(root)
        except (OSError, ValueError) as exc:
            raise SupplyChainError(f"supply-chain artifact escapes release: {role}") from exc
        if path.is_symlink() or resolved != path or not path.is_file():
            raise SupplyChainError(f"supply-chain artifact must be a regular file: {role}")
        expected_sha = str(descriptor.get("sha256") or "")
        if (
            _SHA_RE.fullmatch(expected_sha) is None
            or _sha256(path) != expected_sha
            or descriptor.get("size") != path.stat().st_size
        ):
            raise SupplyChainError(f"supply-chain artifact digest mismatch: {role}")
        value = _load_json_regular(path, label=f"supply-chain artifact {role}")
        loaded[role] = value
        artifact_summary[role] = {
            "path": relative_text,
            "sha256": expected_sha,
            "size": path.stat().st_size,
        }

    lock = loaded["runtime_lock"]
    packages = lock.get("packages")
    if (
        lock.get("schema") != LOCK_SCHEMA
        or lock.get("packages_sha256") != packages_sha
        or not isinstance(packages, list)
        or canonical_digest({"packages": packages}) != packages_sha
    ):
        raise SupplyChainError("release runtime lock is invalid")
    vulnerability = loaded["vulnerability_receipt"]
    validate_vulnerability_receipt(vulnerability, expected_packages_sha256=packages_sha)
    wheelhouse = loaded["wheelhouse_manifest"]
    wheel_files = wheelhouse.get("files")
    if (
        wheelhouse.get("schema") != WHEELHOUSE_SCHEMA
        or not isinstance(wheel_files, list)
        or not wheel_files
        or wheelhouse.get("files_sha256") != canonical_digest({"files": wheel_files})
    ):
        raise SupplyChainError("release wheelhouse manifest is invalid")
    sbom = loaded["sbom"]
    components = sbom.get("components")
    if (
        sbom.get("bomFormat") != "CycloneDX"
        or sbom.get("specVersion") != "1.5"
        or not isinstance(components, list)
        or len(components) != len(packages)
    ):
        raise SupplyChainError("release SBOM is invalid")
    locked = {
        (str(item.get("name") or "").lower(), str(item.get("version") or ""))
        for item in packages
        if isinstance(item, Mapping)
    }
    bom_packages = {
        (re.sub(r"[-_.]+", "-", str(item.get("name") or "")).lower(), str(item.get("version") or ""))
        for item in components
        if isinstance(item, Mapping)
    }
    if locked != bom_packages:
        raise SupplyChainError("release SBOM and runtime lock differ")
    return {
        "schema": RELEASE_BINDING_SCHEMA,
        "ok": True,
        "binding_sha256": supplied_digest,
        "packages_sha256": packages_sha,
        "package_count": len(packages),
        "wheel_count": len(wheel_files),
        "vulnerability_count": vulnerability["vulnerability_count"],
        "base_runtime": {
            "runtime_id": base["runtime_id"],
            "manifest_sha256": base_manifest_sha,
            "tree_sha256": base_tree_sha,
            "externally_verified": base_runtime_manifest is not None,
        },
        "artifacts": artifact_summary,
    }


def pip_audit_receipt(
    report: Mapping[str, Any],
    *,
    packages_sha256: str,
    scanner_version: str,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Normalize a pip-audit JSON result into a release-bound receipt."""

    if not _SHA_RE.fullmatch(str(packages_sha256 or "")):
        raise SupplyChainError("packages SHA-256 is invalid")
    dependencies = report.get("dependencies")
    if not isinstance(dependencies, list):
        raise SupplyChainError("pip-audit dependencies list is missing")
    vulnerabilities: list[dict[str, Any]] = []
    for dependency in dependencies:
        if not isinstance(dependency, Mapping):
            continue
        for vulnerability in dependency.get("vulns") or []:
            if not isinstance(vulnerability, Mapping):
                continue
            vulnerabilities.append(
                {
                    "package": str(dependency.get("name") or ""),
                    "version": str(dependency.get("version") or ""),
                    "id": str(vulnerability.get("id") or ""),
                    "aliases": sorted(str(item) for item in vulnerability.get("aliases") or []),
                    "fix_versions": sorted(str(item) for item in vulnerability.get("fix_versions") or []),
                    "severity": "unknown",
                    "status": "unresolved",
                }
            )
    receipt = {
        "schema": VULNERABILITY_SCHEMA,
        "scanner": "pip-audit",
        "scanner_version": str(scanner_version or "").strip(),
        "generated_at": generated_at or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "packages_sha256": packages_sha256,
        "dependency_count": len(dependencies),
        "vulnerability_count": len(vulnerabilities),
        "vulnerabilities": vulnerabilities,
        "ok": not vulnerabilities,
    }
    # Preserve a failed scanner receipt for diagnosis.  A release consumer
    # must still call ``validate_vulnerability_receipt`` and therefore cannot
    # promote any non-clean result.
    if receipt["ok"]:
        validate_vulnerability_receipt(receipt, expected_packages_sha256=packages_sha256)
    return receipt


__all__ = [
    "LOCK_SCHEMA",
    "RELEASE_BINDING_PATH",
    "RELEASE_BINDING_SCHEMA",
    "SupplyChainError",
    "VULNERABILITY_SCHEMA",
    "WHEELHOUSE_SCHEMA",
    "audit_runtime_install_policy",
    "canonical_digest",
    "cyclonedx_sbom",
    "installed_components",
    "pip_audit_receipt",
    "runtime_lock",
    "scan_release_secrets",
    "validate_vulnerability_receipt",
    "validate_release_supply_chain_binding",
    "verify_wheelhouse",
    "wheelhouse_manifest",
]
