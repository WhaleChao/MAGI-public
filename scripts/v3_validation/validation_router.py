"""Read-only change-risk validation routing.

``change_scope`` answers one deliberately small question: can a *development*
feedback loop be scoped?  It does not select test nodes, bind the selection to
the release sources, or describe the mandatory promotion gate.  This module is
the thin layer that supplies those missing pieces.

The router only builds a plan and a receipt.  It never starts pytest, services,
or a LIVE probe.  An absent or malformed manifest/inventory always routes to a
full development plan or a blocked formal plan; it can never silently become a
scoped plan.
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

if __package__ in {None, ""}:  # Support ``python scripts/v3_validation/validation_router.py``.
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from scripts.v3_validation.change_scope import FULL_SCOPE, SCOPED_SCOPE, classify_paths
else:
    from .change_scope import FULL_SCOPE, SCOPED_SCOPE, classify_paths


ROUTER_SCHEMA = "magi.v3.validation-router/v1"
RECEIPT_SCHEMA = "magi.v3.validation-router-receipt/v1"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
MODES = frozenset({"development", "formal_promotion", "live"})
FORMAL_MODES = frozenset({"formal_promotion", "live"})
CORE_SECTIONS = (
    "v3_suites",
    "quality_contract_groups",
    "golden_sets",
    "side_effect_test_targets",
)
DEV_SMOKE_PATH = "tests/v3/test_change_scope.py"
SAFE_RESOURCE_POLICY = {
    "network": False,
    "live_state_access": False,
    "external_writes": False,
    "max_workers": 1,
    "runtime_db_nas_access": False,
}


class ValidationRouterError(ValueError):
    """Raised when a routing input cannot be trusted."""


@dataclass(frozen=True)
class ValidationNode:
    nodeid: str
    path: str
    suite: str
    duration_seconds: float
    risk_tags: tuple[str, ...]
    source_paths: tuple[str, ...]
    source_sha256: str | None = None


@dataclass(frozen=True)
class ValidationPlan:
    mode: str
    development_scope: str
    promotion_scope: str
    status: str
    selected_paths: tuple[str, ...]
    selected_nodeids: tuple[str, ...]
    selected_suites: tuple[str, ...]
    mandatory_sections: tuple[str, ...]
    reasons: tuple[str, ...]
    timing: Mapping[str, Any]

    @property
    def blocked(self) -> bool:
        return self.status != "ready"

    def pytest_args(self) -> tuple[str, ...]:
        """Return deterministic nodeid/path args; callers decide when to run."""

        # Only scoped development may narrow to measured nodeids.  Full and
        # formal plans intentionally return every expanded file path so a
        # crafted/incomplete node inventory cannot omit another node in the
        # same file.
        if (
            self.mode == "development"
            and self.development_scope == SCOPED_SCOPE
            and self.selected_nodeids
        ):
            return self.selected_nodeids
        return self.selected_paths


def _normalise(value: str) -> str:
    """Validate a repo-relative path; never repair unsafe input."""

    raw = str(value)
    text = raw.replace("\\", "/")
    if (
        not text
        or "\x00" in text
        or text.startswith("/")
        or re.fullmatch(r"[A-Za-z]:/.*", text)
    ):
        raise ValidationRouterError(f"unsafe path: {raw!r}")
    parts = text.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValidationRouterError(f"unsafe path traversal: {raw!r}")
    return "/".join(parts)


def _normalise_glob(value: str) -> str:
    """Validate a repo-relative glob without treating it as a source path."""

    raw = str(value)
    text = raw.replace("\\", "/")
    if (
        not text
        or "\x00" in text
        or text.startswith("/")
        or re.fullmatch(r"[A-Za-z]:/.*", text)
    ):
        raise ValidationRouterError(f"unsafe glob: {raw!r}")
    parts = text.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValidationRouterError(f"unsafe glob traversal: {raw!r}")
    return "/".join(parts)


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    try:
        return _sha256_bytes(path.read_bytes())
    except OSError as exc:
        raise ValidationRouterError(f"source is unavailable: {path}") from exc


def _bound_source(root: Path, relative: str) -> Path:
    """Resolve a receipt source without permitting traversal or symlink escape."""

    relative = _normalise(relative)
    root = root.resolve()
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValidationRouterError(f"source escapes workspace: {relative}") from exc
    return candidate


def _as_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValidationRouterError(f"{label} must be an object")
    return value


def _expand_v2_globs(root: Path, globs: Sequence[str]) -> tuple[str, ...]:
    """Expand V2 release globs against this workspace, not a marker list."""

    patterns = _validate_v2_globs(globs)
    matches: set[str] = set()
    for pattern in patterns:
        # Path.glob is bounded by the declared tests/ pattern.  Do not walk
        # the entire repository just to expand a small release glob.
        try:
            candidates = root.glob(pattern)
        except (OSError, ValueError) as exc:
            raise ValidationRouterError(f"v2 glob cannot be expanded: {pattern}") from exc
        for candidate in candidates:
            if candidate.is_symlink():
                raise ValidationRouterError(
                    f"v2 glob matched symlink: {candidate}"
                )
            if not candidate.is_file():
                continue
            try:
                relative = candidate.relative_to(root).as_posix()
                bound = _bound_source(root, relative)
            except (OSError, ValueError, ValidationRouterError) as exc:
                # A matching symlink outside the workspace is an input-
                # integrity failure, not a reason to silently omit a test.
                if isinstance(exc, ValidationRouterError) and "escapes workspace" in str(exc):
                    raise
                continue
            if (
                relative.startswith("tests/")
                and bound.is_file()
                and not bound.is_symlink()
                and fnmatch.fnmatchcase(relative, pattern)
            ):
                matches.add(_normalise(relative))
    if not matches:
        raise ValidationRouterError("v2_regression.include_globs matched no workspace tests")
    return tuple(sorted(matches))


def _validate_v2_globs(globs: Sequence[str]) -> tuple[str, ...]:
    patterns = tuple(_normalise_glob(pattern) for pattern in globs)
    for pattern in patterns:
        filename_pattern = Path(pattern).name
        if (
            not pattern.startswith("tests/")
            or not pattern.endswith(".py")
            or not fnmatch.fnmatchcase(filename_pattern, "test_*.py")
        ):
            raise ValidationRouterError(
                "v2_regression.include_globs must target tests/**/test_*.py"
            )
    return patterns


def _manifest_paths(
    manifest: Mapping[str, Any], *, root: Path | None = None
) -> tuple[dict[str, tuple[str, ...]], tuple[str, ...]]:
    """Validate the release manifest and return unique paths by section."""

    legacy = _as_mapping(manifest.get("legacy_v2_validation"), "legacy_v2_validation")
    if legacy.get("mode") != "disabled":
        raise ValidationRouterError("legacy_v2_validation must be disabled")
    if "v2_regression" in manifest:
        raise ValidationRouterError("v2_regression is retired from the active validation matrix")
    sections: dict[str, tuple[str, ...]] = {}
    for section in CORE_SECTIONS:
        value = manifest.get(section)
        if section == "side_effect_test_targets":
            rows = value
        else:
            rows = [path for values in _as_mapping(value, section).values() for path in (values if isinstance(values, list) else [])]
            if not _as_mapping(value, section) or any(not isinstance(values, list) for values in _as_mapping(value, section).values()):
                raise ValidationRouterError(f"{section} entries are invalid")
        if not isinstance(rows, list) or not rows or any(not isinstance(path, str) or not path.startswith("tests/") or not path.endswith(".py") for path in rows):
            raise ValidationRouterError(f"{section} contains invalid test paths")
        normalised = tuple(sorted({_normalise(path) for path in rows}))
        if len(normalised) != len(rows):
            # Duplicated entries create duplicate execution and ambiguous
            # receipts, so formal routing blocks instead of de-duplicating a
            # malformed release manifest.
            raise ValidationRouterError(f"{section} contains duplicate test paths")
        sections[section] = normalised
    core_paths = tuple(sorted({path for paths in sections.values() for path in paths}))
    if not core_paths:
        raise ValidationRouterError("formal core has no test paths")
    return sections, core_paths


def _node_from_mapping(value: Any, index: int) -> ValidationNode:
    row = _as_mapping(value, f"node_inventory[{index}]")
    nodeid = row.get("nodeid")
    path = row.get("path")
    suite = row.get("suite")
    duration = row.get("duration_seconds")
    tags = row.get("risk_tags", [])
    sources = row.get("source_paths", [])
    digest = row.get("source_sha256")
    if not isinstance(nodeid, str) or not nodeid or not isinstance(path, str) or not path.startswith("tests/"):
        raise ValidationRouterError(f"node_inventory[{index}] identity is invalid")
    if not isinstance(suite, str) or not suite:
        raise ValidationRouterError(f"node_inventory[{index}] suite is invalid")
    if type(duration) not in (int, float) or isinstance(duration, bool) or not math.isfinite(float(duration)) or float(duration) < 0:
        raise ValidationRouterError(f"node_inventory[{index}] duration is invalid")
    if not isinstance(tags, list) or any(not isinstance(tag, str) or not tag for tag in tags):
        raise ValidationRouterError(f"node_inventory[{index}] risk_tags are invalid")
    if not isinstance(sources, list) or any(not isinstance(source, str) or not source for source in sources):
        raise ValidationRouterError(f"node_inventory[{index}] source_paths are invalid")
    if digest is not None and (not isinstance(digest, str) or len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest)):
        raise ValidationRouterError(f"node_inventory[{index}] source_sha256 is invalid")
    return ValidationNode(
        nodeid=nodeid,
        path=_normalise(path),
        suite=suite,
        duration_seconds=float(duration),
        risk_tags=tuple(sorted(set(tags))),
        source_paths=tuple(sorted({_normalise(source) for source in sources})),
        source_sha256=digest,
    )


def _inventory_rows(value: Any) -> tuple[Mapping[str, Any], ...]:
    if value is None:
        return tuple()
    if isinstance(value, Mapping):
        rows = value.get("nodes")
        if not isinstance(rows, list):
            raise ValidationRouterError("node inventory object must contain a nodes list")
        value = rows
    if not isinstance(value, list):
        raise ValidationRouterError("node inventory must be a JSON list or object")
    if any(not isinstance(row, Mapping) for row in value):
        raise ValidationRouterError("node inventory rows must be objects")
    return tuple(value)


def load_nodes(value: Any) -> tuple[ValidationNode, ...]:
    if value is None:
        return tuple()
    rows = tuple(
        _node_from_mapping(row, index)
        for index, row in enumerate(_inventory_rows(value))
    )
    ids = [row.nodeid for row in rows]
    if len(ids) != len(set(ids)):
        raise ValidationRouterError("node inventory contains duplicate nodeids")
    return tuple(sorted(rows, key=lambda row: row.nodeid))


def _validate_node_sources(
    nodes: Sequence[ValidationNode], *, root: Path | None, require_sources: bool
) -> None:
    if not nodes:
        return
    if require_sources and root is None:
        raise ValidationRouterError("workspace root is required to bind node sources")
    for node in nodes:
        if require_sources and not node.source_paths:
            raise ValidationRouterError(f"node has no source_paths: {node.nodeid}")
        if not node.source_paths:
            if node.source_sha256:
                raise ValidationRouterError(
                    f"node source_sha256 has no source path: {node.nodeid}"
                )
            continue
        if root is None:
            if node.source_sha256:
                raise ValidationRouterError(
                    f"workspace root required for node source_sha256: {node.nodeid}"
                )
            continue
        digests = {
            source: _sha256_file(_bound_source(root, source))
            for source in node.source_paths
        }
        if node.source_sha256:
            if len(node.source_paths) != 1:
                raise ValidationRouterError(
                    f"source_sha256 is ambiguous for multi-source node: {node.nodeid}"
                )
            observed = digests[node.source_paths[0]]
            if observed != node.source_sha256:
                raise ValidationRouterError(
                    f"node source_sha256 does not match source: {node.nodeid}"
                )


def _timing(nodes: Sequence[ValidationNode]) -> dict[str, Any]:
    durations = sorted(row.duration_seconds for row in nodes)
    total = sum(durations)
    if not durations:
        return {"node_count": 0, "measured_total_seconds": 0.0, "p50_seconds": 0.0, "p95_seconds": 0.0}
    def percentile(index: float) -> float:
        return durations[min(len(durations) - 1, max(0, math.ceil(index * len(durations)) - 1))]
    return {
        "node_count": len(durations),
        "measured_total_seconds": round(total, 6),
        "p50_seconds": round(percentile(0.50), 6),
        "p95_seconds": round(percentile(0.95), 6),
    }


def _selected_nodes(nodes: Sequence[ValidationNode], paths: set[str], sources: set[str]) -> tuple[ValidationNode, ...]:
    return tuple(row for row in nodes if row.path in paths or bool(set(row.source_paths) & sources))


def _validate_core_files(root: Path | None, paths: Sequence[str]) -> None:
    if root is None:
        return
    for path in paths:
        candidate = _bound_source(root, path)
        if not candidate.is_file():
            raise ValidationRouterError(f"core test source is unavailable: {path}")


def _release_binding_reasons(
    binding: Mapping[str, Any] | None,
    *,
    root: Path | None,
    manifest_path: str,
) -> tuple[str, ...]:
    """Verify promotion identity against files in the bound workspace."""

    required = (
        "release_sha",
        "campaign_id",
        "source_snapshot_sha256",
        "source_commit",
        "gate_config_sha256",
        "release_manifest_path",
        "release_manifest_sha256",
    )
    if not isinstance(binding, Mapping) or any(
        not isinstance(binding.get(key), str) or not binding.get(key)
        for key in required
    ):
        return ("formal-release-binding-required",)
    if any(
        not SHA256_RE.fullmatch(str(binding[key]))
        for key in (
            "release_sha",
            "source_snapshot_sha256",
            "gate_config_sha256",
            "release_manifest_sha256",
        )
    ) or not GIT_SHA_RE.fullmatch(str(binding["source_commit"])):
        return ("formal-release-binding-format-invalid",)
    if root is None:
        return ("formal-release-binding-workspace-required",)
    reasons: list[str] = []
    try:
        gate_path = _normalise(manifest_path)
        gate_file = _bound_source(root, gate_path)
        gate_digest = _sha256_file(gate_file)
        if binding["gate_config_sha256"] != gate_digest:
            reasons.append("formal-gate-config-sha256-mismatch")

        release_path = _normalise(str(binding["release_manifest_path"]))
        release_file = _bound_source(root, release_path)
        release_digest = _sha256_file(release_file)
        if binding["release_manifest_sha256"] != release_digest:
            reasons.append("formal-release-manifest-sha256-mismatch")
        try:
            release_object = json.loads(release_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValidationRouterError("release manifest is not valid JSON") from exc
        if not isinstance(release_object, Mapping):
            reasons.append("formal-release-manifest-identity-required")
        else:
            # The release artifact schema is release_id/release_sha256/
            # source_snapshot_sha256/commit.  The binding keeps the router's
            # stable release_sha/campaign_id names, but they must be exact
            # aliases of those artifact identities.
            for artifact_key, binding_key in (
                ("release_sha256", "release_sha"),
                ("release_id", "campaign_id"),
                ("source_snapshot_sha256", "source_snapshot_sha256"),
                ("commit", "source_commit"),
            ):
                if not isinstance(release_object.get(artifact_key), str) or not release_object.get(artifact_key):
                    reasons.append(f"formal-release-manifest-missing:{artifact_key}")
                elif binding.get(binding_key) != release_object[artifact_key]:
                    reasons.append(f"formal-release-identity-mismatch:{artifact_key}")
            if (
                not SHA256_RE.fullmatch(str(release_object.get("release_sha256", "")))
                or not SHA256_RE.fullmatch(str(release_object.get("source_snapshot_sha256", "")))
                or not GIT_SHA_RE.fullmatch(str(release_object.get("commit", "")))
            ):
                reasons.append("formal-release-manifest-format-invalid")
            if release_object.get("source_snapshot_sha256") != release_object.get("release_sha256"):
                reasons.append("formal-release-source-snapshot-mismatch")
    except (OSError, ValidationRouterError, TypeError, ValueError):
        reasons.append("formal-release-binding-artifact-unavailable")
    return tuple(dict.fromkeys(reasons))


def route(
    mode: str,
    changed_files: Iterable[str],
    *,
    manifest: Mapping[str, Any],
    nodes: Iterable[Mapping[str, Any]] | None = None,
    release_binding: Mapping[str, str] | None = None,
    root: Path | None = None,
    manifest_path: str = "config/v3_release_quality_suites.json",
) -> ValidationPlan:
    """Build a fail-closed plan without executing any validation command."""

    if mode not in MODES:
        raise ValidationRouterError(f"unsupported validation mode: {mode!r}")
    changed = tuple(sorted({_normalise(path) for path in changed_files}))
    if root is not None:
        for path in changed:
            _bound_source(root, path)
    decision = classify_paths(changed, root=root)
    manifest_root = root if mode in FORMAL_MODES or decision.development_scope == FULL_SCOPE else None
    sections, core_paths = _manifest_paths(manifest, root=manifest_root)
    if manifest_root is not None:
        _validate_core_files(manifest_root, core_paths)
    loaded_nodes = load_nodes(nodes)
    reasons = list(decision.reasons)
    if mode in FORMAL_MODES:
        binding_reasons = _release_binding_reasons(
            release_binding, root=root, manifest_path=manifest_path
        )
        if root is None:
            reasons.extend(binding_reasons)
            return ValidationPlan(
                mode,
                FULL_SCOPE,
                FULL_SCOPE,
                "blocked",
                core_paths,
                tuple(),
                tuple(),
                CORE_SECTIONS,
                tuple(reasons + ["formal-workspace-root-required"]),
                _timing(tuple()),
            )
        _validate_node_sources(loaded_nodes, root=root, require_sources=True)
        if binding_reasons:
            return ValidationPlan(mode, FULL_SCOPE, FULL_SCOPE, "blocked", core_paths, tuple(), tuple(), CORE_SECTIONS, tuple(reasons + list(binding_reasons)), _timing(tuple()))
        selected_paths = core_paths
        selected_nodes = tuple(row for row in loaded_nodes if row.path in set(core_paths))
        status = "ready"
        if not loaded_nodes:
            status = "blocked"
            reasons.append("formal-node-inventory-required")
        elif {row.path for row in loaded_nodes} != set(core_paths):
            status = "blocked"
            reasons.append("formal-node-inventory-does-not-cover-core")
            if not set(core_paths).issubset({row.path for row in loaded_nodes}):
                reasons.append("formal-node-inventory-missing-core-tests")
            if not {row.path for row in loaded_nodes}.issubset(set(core_paths)):
                reasons.append("formal-node-inventory-has-non-core-tests")
        return ValidationPlan(mode, decision.development_scope, FULL_SCOPE, status, selected_paths, tuple(row.nodeid for row in selected_nodes), tuple(sorted({row.suite for row in selected_nodes})), CORE_SECTIONS, tuple(reasons), _timing(selected_nodes))

    # A full development decision is still useful for local feedback, but it
    # must not pretend to certify a promotion.
    if decision.development_scope == FULL_SCOPE:
        selected_paths = core_paths
        selected_nodes = tuple(row for row in loaded_nodes if row.path in set(core_paths))
        status = "ready"
        if root is None:
            status = "blocked"
            reasons.append("full-workspace-root-required")
        if loaded_nodes and not selected_nodes:
            status = "blocked"
            reasons.append("development-node-inventory-does-not-cover-core")
        elif loaded_nodes and {row.path for row in selected_nodes} != set(core_paths):
            status = "blocked"
            reasons.append("development-node-inventory-incomplete-core")
        return ValidationPlan(mode, FULL_SCOPE, FULL_SCOPE, status, selected_paths, tuple(row.nodeid for row in selected_nodes), tuple(sorted({row.suite for row in selected_nodes})), tuple(), tuple(reasons), _timing(selected_nodes))

    changed_set = set(changed)
    selected_nodes = _selected_nodes(loaded_nodes, changed_set, changed_set)
    selected_paths = tuple(sorted({row.path for row in selected_nodes}))
    if not selected_paths:
        # Documentation/style-only changes do not need a production suite, but
        # still run the policy smoke test when it is part of the manifest.
        smoke = DEV_SMOKE_PATH if (root is None or (root / DEV_SMOKE_PATH).is_file()) else ""
        selected_paths = (smoke,) if smoke else tuple()
        reasons.append("safe-content-only")
    status = "ready" if selected_paths else "blocked"
    if not selected_paths:
        reasons.append("scoped-selection-empty")
    return ValidationPlan(mode, SCOPED_SCOPE, FULL_SCOPE, status, selected_paths, tuple(row.nodeid for row in selected_nodes), tuple(sorted({row.suite for row in selected_nodes})), tuple(), tuple(reasons), _timing(selected_nodes))


def build_receipt(
    plan: ValidationPlan,
    *,
    changed_files: Iterable[str],
    manifest: Mapping[str, Any],
    root: Path,
    node_inventory: Any = None,
    inventory_source_sha256: str | None = None,
    manifest_path: str = "config/v3_release_quality_suites.json",
    base: str | None = None,
    head: str | None = None,
    release_binding: Mapping[str, str] | None = None,
    release_binding_source_sha256: str | None = None,
) -> dict[str, Any]:
    """Build a hash-bound receipt suitable for attaching to a validation run."""

    root = root.resolve()
    changed = tuple(sorted({_normalise(path) for path in changed_files}))
    manifest_rel = _normalise(manifest_path)
    manifest_file = _bound_source(root, manifest_rel)
    if not manifest_file.is_file():
        raise ValidationRouterError(f"manifest source is unavailable: {manifest_rel}")
    try:
        source_manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValidationRouterError(f"manifest source is invalid: {manifest_rel}") from exc
    if source_manifest != manifest:
        raise ValidationRouterError("manifest object is not identical to the bound source")
    inventory_nodes = load_nodes(node_inventory)
    _validate_node_sources(
        inventory_nodes,
        root=root,
        require_sources=plan.mode in FORMAL_MODES,
    )
    inventory_ids = tuple(row.nodeid for row in inventory_nodes)
    selected_ids = tuple(sorted(plan.selected_nodeids))
    if plan.mode in FORMAL_MODES and plan.status == "ready":
        if not inventory_nodes or selected_ids != inventory_ids:
            raise ValidationRouterError("formal receipt inventory is not the selected complete inventory")
    if plan.development_scope == FULL_SCOPE and plan.status == "ready" and inventory_nodes:
        if selected_ids != inventory_ids:
            raise ValidationRouterError("full receipt inventory is not the selected complete inventory")
    binding = dict(release_binding or {})
    release_manifest_path: str | None = None
    if plan.mode in FORMAL_MODES and plan.status == "ready":
        binding_reasons = _release_binding_reasons(
            binding, root=root, manifest_path=manifest_rel
        )
        if binding_reasons:
            raise ValidationRouterError(
                "formal receipt release binding is invalid: "
                + ",".join(binding_reasons)
            )
        release_manifest_path = _normalise(str(binding["release_manifest_path"]))
    inventory_source_paths = {
        source
        for node in inventory_nodes
        for source in node.source_paths
    }
    source_paths = sorted(
        set(changed)
        | set(plan.selected_paths)
        | inventory_source_paths
        | {
            manifest_rel,
            "scripts/v3_validation/change_scope.py",
            "scripts/v3_validation/validation_router.py",
        }
        | ({release_manifest_path} if release_manifest_path else set())
    )
    source_sha256: dict[str, str] = {}
    for path in source_paths:
        source_sha256[path] = _sha256_file(_bound_source(root, path))
    inventory_hash = (
        _sha256_bytes(_canonical(node_inventory)) if node_inventory is not None else None
    )
    binding_hash = _sha256_bytes(_canonical(binding)) if binding else None
    for label, digest in (
        ("inventory_source_sha256", inventory_source_sha256),
        ("release_binding_source_sha256", release_binding_source_sha256),
    ):
        if digest is not None and not SHA256_RE.fullmatch(str(digest)):
            raise ValidationRouterError(f"{label} is not a SHA-256 digest")
    receipt: dict[str, Any] = {
        "schema": RECEIPT_SCHEMA,
        "router_schema": ROUTER_SCHEMA,
        "base": base,
        "head": head,
        "mode": plan.mode,
        "status": plan.status,
        "changed_files": list(changed),
        "development_scope": plan.development_scope,
        "promotion_scope": FULL_SCOPE,
        "promotion_requires_full_release_quality": True,
        "mandatory_sections": list(plan.mandatory_sections),
        "selected_paths": list(plan.selected_paths),
        "selected_nodeids": list(plan.selected_nodeids),
        "selected_suites": list(plan.selected_suites),
        "reasons": list(plan.reasons),
        "timing": dict(plan.timing),
        "resource_policy": dict(SAFE_RESOURCE_POLICY),
        "source_sha256": source_sha256,
        "manifest_sha256": source_sha256[manifest_rel],
        "inventory_sha256": inventory_hash,
        "inventory_source_sha256": inventory_source_sha256,
        "inventory_nodeids": list(inventory_ids),
        "inventory_scope": (
            "full"
            if plan.development_scope == FULL_SCOPE or plan.mode in FORMAL_MODES
            else "selected"
        ),
        "release_binding_sha256": binding_hash,
        "release_binding_source_sha256": release_binding_source_sha256,
        "release_binding": binding,
    }
    receipt["receipt_sha256"] = _sha256_bytes(_canonical(receipt))
    return receipt


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValidationRouterError(f"invalid JSON source: {path}") from exc


def _bound_cli_input(root: Path, path: Path, label: str) -> tuple[str, Path]:
    """Bind an installed-release input to the workspace containing the executable."""

    root = root.resolve()
    candidate = path.resolve()
    try:
        relative = candidate.relative_to(root).as_posix()
    except ValueError as exc:
        raise ValidationRouterError(f"{label} is outside executable workspace") from exc
    return _normalise(relative), candidate


def _read_evidence_json(path: Path, label: str) -> tuple[Any, str]:
    """Read external evidence once and reject symlink/non-regular inputs."""

    if path.is_symlink() or not path.is_file():
        raise ValidationRouterError(f"{label} must be a regular file")
    try:
        payload = path.read_bytes()
        value = json.loads(payload.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationRouterError(f"invalid JSON source: {path}") from exc
    return value, _sha256_bytes(payload)


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a read-only change-risk validation plan")
    parser.add_argument("--mode", choices=sorted(MODES), required=True)
    parser.add_argument("--changed-file", action="append", default=[])
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--nodes", type=Path)
    parser.add_argument("--release-binding", "--binding", dest="binding", type=Path)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--base")
    parser.add_argument("--head")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    manifest_rel, manifest_file = _bound_cli_input(root, args.manifest, "manifest")
    if manifest_rel != "config/v3_release_quality_suites.json":
        raise ValidationRouterError("manifest must use canonical config path")
    node_file = args.nodes if args.nodes else None
    binding_file = args.binding if args.binding else None
    nodes, inventory_source_sha256 = (
        _read_evidence_json(node_file, "node inventory")
        if node_file
        else (None, None)
    )
    binding, release_binding_source_sha256 = (
        _read_evidence_json(binding_file, "release binding")
        if binding_file
        else (None, None)
    )
    receipt_file = args.receipt
    if receipt_file.exists() and receipt_file.is_symlink():
        raise ValidationRouterError("receipt must not be a symlink")
    manifest = _load_json(manifest_file)
    if binding is not None and not isinstance(binding, Mapping):
        raise ValidationRouterError("release binding must be a JSON object")
    plan = route(
        args.mode,
        args.changed_file,
        manifest=manifest,
        nodes=nodes,
        release_binding=binding,
        root=root,
        manifest_path=manifest_rel,
    )
    receipt = build_receipt(
        plan,
        changed_files=args.changed_file,
        manifest=manifest,
        root=root,
        node_inventory=nodes,
        inventory_source_sha256=inventory_source_sha256,
        release_binding=binding,
        release_binding_source_sha256=(
            release_binding_source_sha256
        ),
        manifest_path=manifest_rel,
        base=args.base,
        head=args.head,
    )
    receipt_file.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0 if plan.status == "ready" else 2


if __name__ == "__main__":
    raise SystemExit(main())
