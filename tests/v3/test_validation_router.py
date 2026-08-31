from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.v3_validation.validation_router import (
    CORE_SECTIONS,
    FULL_SCOPE,
    RECEIPT_SCHEMA,
    SCOPED_SCOPE,
    ValidationRouterError,
    build_receipt,
    route,
)


def _manifest() -> dict[str, object]:
    return {
        "legacy_v2_validation": {
            "mode": "disabled",
            "reason": "V2 is retired from active promotion",
        },
        "v3_suites": {
            "unit": ["tests/v3/test_unit.py"],
            "contract": ["tests/v3/test_contract.py"],
            "integration": ["tests/v3/test_integration.py"],
            "e2e": ["tests/v3/test_change_scope.py"],
        },
        "quality_contract_groups": {
            "interaction": ["tests/test_interaction.py"],
            "agent_kernel": ["tests/test_kernel.py"],
            "memory": ["tests/test_memory.py"],
            "quality": ["tests/test_quality.py"],
        },
        "golden_sets": {
            "context": ["tests/test_context.py"],
            "memory": ["tests/test_memory.py"],
            "tool": ["tests/test_tool.py"],
            "plan": ["tests/test_plan.py"],
            "answer": ["tests/test_answer.py"],
        },
        "side_effect_test_targets": ["tests/v3/test_side_effects.py"],
    }


def _nodes() -> list[dict[str, object]]:
    return [
        {
            "nodeid": "tests/v3/test_unit.py::test_format",
            "path": "tests/v3/test_unit.py",
            "suite": "unit",
            "duration_seconds": 0.25,
            "risk_tags": ["pure"],
            "source_paths": ["lib/formatting.py"],
        },
        {
            "nodeid": "tests/v3/test_contract.py::test_route",
            "path": "tests/v3/test_contract.py",
            "suite": "contract",
            "duration_seconds": 1.5,
            "risk_tags": ["route"],
            "source_paths": ["api/route.py"],
        },
    ]


def _materialize_core_workspace(tmp_path: Path, manifest: dict[str, object]) -> tuple[tuple[str, ...], list[dict[str, object]]]:
    """Create a tiny source-only workspace for the active V3 test core."""

    paths: set[str] = set()
    for section, values in manifest.items():
        if section == "legacy_v2_validation":
            continue
        if section == "side_effect_test_targets":
            paths.update(values)  # type: ignore[arg-type]
        else:
            for rows in values.values():  # type: ignore[union-attr]
                paths.update(rows)
    for path in paths:
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("def test_one():\n    pass\n", encoding="utf-8")
    nodes = []
    for path in sorted(paths):
        digest = hashlib.sha256((tmp_path / path).read_bytes()).hexdigest()
        nodes.append(
            {
                "nodeid": f"{path}::test_one",
                "path": path,
                "suite": "core",
                "duration_seconds": 0.1,
                "risk_tags": [],
                "source_paths": [path],
                "source_sha256": digest,
            }
        )
    return tuple(sorted(paths)), nodes


def _write_cli_sources(tmp_path: Path, manifest: dict[str, object]) -> tuple[Path, Path, Path]:
    manifest_path = tmp_path / "config" / "v3_release_quality_suites.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    repo_root = Path(__file__).resolve().parents[2]
    for path in ("scripts/v3_validation/change_scope.py", "scripts/v3_validation/validation_router.py"):
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((repo_root / path).read_bytes())
    target = tmp_path / "docs" / "release.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("docs/release.md", encoding="utf-8")
    return manifest_path, tmp_path / "docs" / "release.md", tmp_path / "receipt.json"


def _formal_binding(tmp_path: Path, manifest_path: Path) -> dict[str, str]:
    release_path = tmp_path / "config" / "release_manifest.json"
    release_sha = "a" * 64
    release = {
        "release_id": "campaign",
        "release_sha256": release_sha,
        "source_snapshot_sha256": release_sha,
        "commit": "b" * 40,
    }
    release_path.write_text(json.dumps(release, sort_keys=True), encoding="utf-8")
    return {
        "release_sha": release_sha,
        "campaign_id": release["release_id"],
        "source_snapshot_sha256": release["source_snapshot_sha256"],
        "source_commit": release["commit"],
        "gate_config_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        "release_manifest_path": "config/release_manifest.json",
        "release_manifest_sha256": hashlib.sha256(release_path.read_bytes()).hexdigest(),
    }


def test_pure_change_routes_one_measured_node_without_promotion_downgrade(tmp_path: Path) -> None:
    source = tmp_path / "lib" / "formatting.py"
    source.parent.mkdir()
    source.write_text("# magi-validation-scope: pure-function\n", encoding="utf-8")
    plan = route(
        "development",
        ["lib/formatting.py"],
        manifest=_manifest(),
        nodes=_nodes(),
        root=tmp_path,
    )
    assert plan.status == "ready"
    assert plan.development_scope == SCOPED_SCOPE
    assert plan.promotion_scope == FULL_SCOPE
    assert plan.selected_nodeids == ("tests/v3/test_unit.py::test_format",)
    assert plan.pytest_args() == plan.selected_nodeids
    assert plan.timing["node_count"] == 1
    assert plan.timing["measured_total_seconds"] == 0.25


def test_operational_change_routes_full_core_and_unknown_inventory_does_not_scope() -> None:
    plan = route("development", ["api/route.py"], manifest=_manifest(), nodes=_nodes())
    assert plan.status == "blocked"
    assert "development-node-inventory-incomplete-core" in plan.reasons
    assert plan.development_scope == FULL_SCOPE
    assert plan.selected_paths == tuple(sorted({
        "tests/v3/test_unit.py",
        "tests/v3/test_contract.py",
        "tests/v3/test_integration.py",
        "tests/v3/test_change_scope.py",
        "tests/test_interaction.py",
        "tests/test_kernel.py",
        "tests/test_memory.py",
        "tests/test_quality.py",
        "tests/test_context.py",
        "tests/test_tool.py",
        "tests/test_plan.py",
        "tests/test_answer.py",
        "tests/v3/test_side_effects.py",
    }))


def test_formal_modes_require_release_binding_and_keep_all_core_sections(tmp_path: Path) -> None:
    blocked = route("live", ["docs/release.md"], manifest=_manifest(), nodes=_nodes())
    assert blocked.status == "blocked"
    assert blocked.promotion_scope == FULL_SCOPE
    assert "formal-release-binding-required" in blocked.reasons

    manifest = _manifest()
    # The formal router binds every active V3 core path to a measured node and
    # source hash; retired V2 tests are deliberately absent.
    core_paths, complete_nodes = _materialize_core_workspace(tmp_path, manifest)
    manifest_path = tmp_path / "config" / "v3_release_quality_suites.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    binding = _formal_binding(tmp_path, manifest_path)
    ready = route(
        "formal_promotion",
        ["docs/release.md"],
        manifest=manifest,
        nodes=complete_nodes,
        release_binding=binding,
        root=tmp_path,
    )
    assert ready.status == "ready"
    assert ready.mandatory_sections == CORE_SECTIONS
    assert len(ready.selected_paths) == len(set(ready.selected_paths))
    assert ready.selected_paths == core_paths
    assert ready.pytest_args() == ready.selected_paths
    assert ready.promotion_scope == FULL_SCOPE


def test_formal_missing_core_inventory_is_blocked(tmp_path: Path) -> None:
    manifest = _manifest()
    core_paths, nodes = _materialize_core_workspace(tmp_path, manifest)
    manifest_path = tmp_path / "config" / "v3_release_quality_suites.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    binding = _formal_binding(tmp_path, manifest_path)
    plan = route(
        "formal_promotion",
        ["docs/release.md"],
        manifest=manifest,
        nodes=nodes[:-1],
        release_binding=binding,
        root=tmp_path,
    )
    assert plan.status == "blocked"
    assert len(plan.selected_paths) == len(core_paths)
    assert "formal-node-inventory-missing-core-tests" in plan.reasons
    assert plan.pytest_args() == plan.selected_paths


def test_receipt_hashes_sources_and_declares_safe_low_resource_policy(tmp_path: Path) -> None:
    manifest_path = tmp_path / "config" / "v3_release_quality_suites.json"
    manifest_path.parent.mkdir()
    import json

    manifest_path.write_text(json.dumps(_manifest()), encoding="utf-8")
    for path in ("scripts/v3_validation/change_scope.py", "scripts/v3_validation/validation_router.py", "docs/release.md", "tests/v3/test_change_scope.py"):
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(path, encoding="utf-8")
    plan = route("development", ["docs/release.md"], manifest=_manifest())
    receipt = build_receipt(
        plan,
        changed_files=["docs/release.md"],
        manifest=_manifest(),
        root=tmp_path,
        release_binding={"release_sha": "r", "campaign_id": "c", "gate_config_sha256": "g"},
    )
    assert receipt["schema"] == RECEIPT_SCHEMA
    assert receipt["promotion_requires_full_release_quality"] is True
    assert receipt["resource_policy"]["max_workers"] == 1
    assert receipt["resource_policy"]["network"] is False
    assert receipt["source_sha256"]["docs/release.md"] == hashlib.sha256(b"docs/release.md").hexdigest()
    assert len(receipt["receipt_sha256"]) == 64


def test_malformed_node_inventory_fails_closed() -> None:
    with pytest.raises(ValidationRouterError, match="duration"):
        route(
            "development",
            ["lib/formatting.py"],
            manifest=_manifest(),
            nodes=[{"nodeid": "tests/v3/test_unit.py::test", "path": "tests/v3/test_unit.py", "suite": "unit", "duration_seconds": "slow"}],
        )


def test_receipt_rejects_workspace_traversal(tmp_path: Path) -> None:
    manifest_path = tmp_path / "config" / "v3_release_quality_suites.json"
    manifest_path.parent.mkdir()
    import json

    manifest_path.write_text(json.dumps(_manifest()), encoding="utf-8")
    for path in ("scripts/v3_validation/change_scope.py", "scripts/v3_validation/validation_router.py"):
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(path, encoding="utf-8")
    plan = route("development", ["docs/release.md"], manifest=_manifest())
    with pytest.raises(ValidationRouterError, match="unsafe path traversal"):
        build_receipt(plan, changed_files=["../outside.py"], manifest=_manifest(), root=tmp_path)


def test_source_hash_mismatch_and_symlink_escape_are_rejected(tmp_path: Path) -> None:
    manifest = _manifest()
    manifest_path = tmp_path / "config" / "v3_release_quality_suites.json"
    manifest_path.parent.mkdir()
    import json

    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    source = tmp_path / "lib" / "pure.py"
    source.parent.mkdir()
    source.write_text("# magi-validation-scope: pure-function\n", encoding="utf-8")
    plan = route(
        "development",
        ["lib/pure.py"],
        manifest=manifest,
        root=tmp_path,
        nodes=[
            {
                "nodeid": "tests/v3/test_unit.py::test",
                "path": "tests/v3/test_unit.py",
                "suite": "unit",
                "duration_seconds": 0.1,
                "risk_tags": [],
                "source_paths": ["lib/pure.py"],
                "source_sha256": "0" * 64,
            }
        ],
    )
    with pytest.raises(ValidationRouterError, match="source_sha256"):
        build_receipt(
            plan,
            changed_files=["lib/pure.py"],
            manifest=manifest,
            root=tmp_path,
            node_inventory=[
                {
                    "nodeid": "tests/v3/test_unit.py::test",
                    "path": "tests/v3/test_unit.py",
                    "suite": "unit",
                    "duration_seconds": 0.1,
                    "risk_tags": [],
                    "source_paths": ["lib/pure.py"],
                    "source_sha256": "0" * 64,
                }
            ],
        )

    outside = tmp_path.parent / f"{tmp_path.name}-outside.py"
    outside.write_text("outside\n", encoding="utf-8")
    (tmp_path / "lib" / "link.py").symlink_to(outside)
    with pytest.raises(ValidationRouterError, match="escapes workspace"):
        route("development", ["lib/link.py"], manifest=manifest, root=tmp_path)


def test_change_scope_rejects_standalone_traversal_and_absolute_paths() -> None:
    from scripts.v3_validation.change_scope import classify_paths

    posix_absolute = "/" + "tmp/outside.py"
    windows_absolute = "C:" + "/outside.py"
    for unsafe in ("../outside.py", "./docs/readme.md", posix_absolute, windows_absolute):
        with pytest.raises(ValueError):
            classify_paths([unsafe])


def test_cli_formal_binds_json_inventory_manifest_and_release_binding(tmp_path: Path) -> None:
    manifest = _manifest()
    core_paths, nodes = _materialize_core_workspace(tmp_path, manifest)
    manifest_path, _changed, receipt_path = _write_cli_sources(tmp_path, manifest)
    evidence = tmp_path.parent / f"{tmp_path.name}-evidence"
    evidence.mkdir()
    inventory_path = evidence / "inventory.json"
    binding_path = evidence / "binding.json"
    receipt_path = evidence / "receipt.json"
    inventory_path.write_text(json.dumps(nodes), encoding="utf-8")
    binding = _formal_binding(tmp_path, manifest_path)
    binding_path.write_text(json.dumps(binding), encoding="utf-8")
    script = tmp_path / "scripts" / "v3_validation" / "validation_router.py"
    completed = subprocess.run(
        [
            sys.executable,
            str(script),
            "--mode",
            "formal_promotion",
            "--changed-file",
            "docs/release.md",
            "--manifest",
            str(manifest_path),
            "--nodes",
            str(inventory_path),
            "--release-binding",
            str(binding_path),
            "--receipt",
            str(receipt_path),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["status"] == "ready"
    assert receipt["selected_paths"] == list(core_paths)
    assert receipt["inventory_scope"] == "full"
    assert len(receipt["inventory_sha256"]) == 64
    assert receipt["inventory_source_sha256"] == hashlib.sha256(inventory_path.read_bytes()).hexdigest()
    assert len(receipt["manifest_sha256"]) == 64
    assert len(receipt["release_binding_sha256"]) == 64
    assert receipt["release_binding_source_sha256"] == hashlib.sha256(binding_path.read_bytes()).hexdigest()
    assert receipt["source_sha256"]["config/release_manifest.json"] == binding["release_manifest_sha256"]
    assert receipt["source_sha256"]["scripts/v3_validation/validation_router.py"] == hashlib.sha256(script.read_bytes()).hexdigest()


def test_formal_release_identity_and_manifest_escape_blocked(tmp_path: Path) -> None:
    manifest = _manifest()
    core_paths, nodes = _materialize_core_workspace(tmp_path, manifest)
    manifest_path = tmp_path / "config" / "v3_release_quality_suites.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    binding = _formal_binding(tmp_path, manifest_path)
    binding["release_sha"] = "c" * 64
    plan = route(
        "formal_promotion",
        ["docs/release.md"],
        manifest=manifest,
        nodes=nodes,
        release_binding=binding,
        root=tmp_path,
    )
    assert plan.status == "blocked"
    assert "formal-release-identity-mismatch:release_sha256" in plan.reasons
    binding["release_sha"] = "a" * 64
    binding["release_manifest_path"] = "../release.json"
    escaped = route(
        "formal_promotion",
        ["docs/release.md"],
        manifest=manifest,
        nodes=nodes,
        release_binding=binding,
        root=tmp_path,
    )
    assert escaped.status == "blocked"
    assert "formal-release-binding-artifact-unavailable" in escaped.reasons


def test_retired_v2_cannot_be_reenabled_in_active_matrix(tmp_path: Path) -> None:
    manifest = _manifest()
    invalid = dict(manifest)
    invalid["legacy_v2_validation"] = {"mode": "active"}
    with pytest.raises(ValidationRouterError, match="must be disabled"):
        route("formal_promotion", ["docs/release.md"], manifest=invalid, root=tmp_path)

    stale = dict(manifest)
    stale["v2_regression"] = {"include_globs": ["tests/test_*.py"]}
    with pytest.raises(ValidationRouterError, match="retired"):
        route("formal_promotion", ["docs/release.md"], manifest=stale, root=tmp_path)


def test_formal_binding_rejects_invalid_digest_and_commit_formats(tmp_path: Path) -> None:
    manifest = _manifest()
    _materialize_core_workspace(tmp_path, manifest)
    manifest_path = tmp_path / "config" / "v3_release_quality_suites.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    binding = _formal_binding(tmp_path, manifest_path)
    binding["gate_config_sha256"] = "A" * 64
    plan = route(
        "formal_promotion",
        ["docs/release.md"],
        manifest=manifest,
        nodes=None,
        release_binding=binding,
        root=tmp_path,
    )
    assert plan.status == "blocked"
    assert "formal-release-binding-format-invalid" in plan.reasons
    binding = _formal_binding(tmp_path, manifest_path)
    binding["source_commit"] = "d" * 39
    plan = route(
        "formal_promotion",
        ["docs/release.md"],
        manifest=manifest,
        nodes=None,
        release_binding=binding,
        root=tmp_path,
    )
    assert plan.status == "blocked"
    assert "formal-release-binding-format-invalid" in plan.reasons


def test_cli_formal_without_binding_is_blocked_not_silently_usable(tmp_path: Path) -> None:
    manifest = _manifest()
    _materialize_core_workspace(tmp_path, manifest)
    manifest_path, _changed, receipt_path = _write_cli_sources(tmp_path, manifest)
    script = tmp_path / "scripts" / "v3_validation" / "validation_router.py"
    completed = subprocess.run(
        [
            sys.executable,
            str(script),
            "--mode",
            "live",
            "--changed-file",
            "docs/release.md",
            "--manifest",
            str(manifest_path),
            "--receipt",
            str(receipt_path),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 2
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["status"] == "blocked"
    assert "formal-release-binding-required" in receipt["reasons"]
