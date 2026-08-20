from __future__ import annotations

import hashlib
import json
import plistlib
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

import scripts.v3_evidence_compiler as compiler
import scripts.v3_release_gate as release_gate
from scripts.architecture.generate_v2_inventory import (
    build_inventory,
    collect_daemon_children,
    collect_launchagents,
    collect_routes,
    collect_skills,
)
from scripts.v3_backup_prepare import prepare_backup
from scripts.v3_campaign.runner import ReleaseBundle
from scripts.v3_evidence_compiler import (
    CompileContext,
    EvidenceCompileError,
    compile_backup_evidence,
    compile_deploy_evidence,
    compile_evidence,
)
from scripts.v3_release_gate import (
    BoundArtifact,
    EVIDENCE_SPECS,
    _canonical_json_bytes,
    _recompute_backup_metrics,
    _recompute_deploy_metrics,
    evaluate_evidence,
)


ROOT = Path(__file__).resolve().parents[2]
GATE_PATH = ROOT / "config" / "v3_cutover_gates.json"
GATE_BYTES = GATE_PATH.read_bytes()
GATE_SHA = hashlib.sha256(GATE_BYTES).hexdigest()
CONFIG = json.loads(GATE_BYTES)
NOW = datetime(2026, 7, 15, 15, 0, tzinfo=timezone.utc)


def test_checked_in_portable_inventory_matches_candidate_source_surfaces() -> None:
    """Fail quality certification before a newly shipped source can stale inventory."""

    inventory = json.loads(
        (ROOT / "docs/architecture/v3/generated/v2_inventory.json").read_text(
            encoding="utf-8"
        )
    )
    tests = sorted(
        str(path.relative_to(ROOT)) for path in (ROOT / "tests").glob("test_*.py")
    )
    launchagents = collect_launchagents(ROOT, include_installed=False)

    assert inventory["http_routes"] == collect_routes(ROOT)
    assert inventory["skill_entrypoints"] == collect_skills(ROOT)
    assert inventory["daemon_children"] == collect_daemon_children(ROOT)
    assert inventory["launchagents"] == launchagents
    assert inventory["test_modules"] == tests
    assert inventory["counts"]["http_routes"] == len(inventory["http_routes"])
    assert inventory["counts"]["skill_entrypoints"] == len(
        inventory["skill_entrypoints"]
    )
    assert inventory["counts"]["daemon_child_declarations"] == len(
        inventory["daemon_children"]
    )
    assert inventory["counts"]["checked_in_launchagents"] == len(
        launchagents["checked_in"]
    )
    assert inventory["counts"]["installed_launchagents"] == 0
    assert inventory["counts"]["test_modules"] == len(tests)


def test_repository_source_contract_resolves_the_live_runtime_not_the_checkout() -> None:
    contract = compiler._source_contract(CONFIG)

    assert contract["schema_version"] == 2
    assert contract["root_kind"] == "current_user_application_support_magi_v2"
    assert contract["v2_root"].endswith(
        "/Library/Application Support/MAGI/runtime/MAGI_v2"
    )
    assert contract["website_root"] == f'{contract["v2_root"]}/whalechao.github.io'
    assert "/Desktop/MAGI_v2" not in contract["v2_root"]


def test_compile_evidence_routes_physical_fault_inputs_only_to_campaign_compiler(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Physical drill inputs belong to campaign evidence, not release inventory."""
    context = CompileContext("campaign-routing", "a" * 64, "test-mac", GATE_SHA)
    release_root = tmp_path / "release"
    release_root.mkdir()
    campaign_report = tmp_path / "campaign.json"
    _write_json(campaign_report, {})
    physical_report = tmp_path / "physical-report.json"
    physical_plan = tmp_path / "physical-plan.json"
    physical_authorization = tmp_path / "physical-authorization.json"
    for path in (physical_report, physical_plan, physical_authorization):
        _write_json(path, {})

    calls: dict[str, dict[str, object]] = {}

    def fake_release(
        *,
        release_root: Path,
        campaign_report: Path,
        output: Path,
        context: CompileContext,
        config: dict[str, object],
    ) -> dict[str, str]:
        calls["release"] = {
            "release_root": release_root,
            "campaign_report": campaign_report,
            "output": output,
            "context": context,
            "config": config,
        }
        return {}

    def fake_campaign(
        *,
        report_path: Path,
        release_root: Path,
        output: Path,
        context: CompileContext,
        config: dict[str, object],
        physical_fault_report: Path | None = None,
        physical_fault_plan: Path | None = None,
        physical_fault_authorization: Path | None = None,
    ) -> dict[str, str]:
        calls["campaign"] = {
            "physical_fault_report": physical_fault_report,
            "physical_fault_plan": physical_fault_plan,
            "physical_fault_authorization": physical_fault_authorization,
        }
        return {}

    monkeypatch.setattr(compiler, "compile_release_evidence", fake_release)
    monkeypatch.setattr(compiler, "compile_campaign_evidence", fake_campaign)

    summary = compile_evidence(
        output=tmp_path / "evidence",
        context=context,
        gate_config=GATE_PATH,
        release_root=release_root,
        campaign_report=campaign_report,
        physical_fault_report=physical_report,
        physical_fault_plan=physical_plan,
        physical_fault_authorization=physical_authorization,
    )

    assert "release" in calls
    assert calls["campaign"] == {
        "physical_fault_report": physical_report,
        "physical_fault_plan": physical_plan,
        "physical_fault_authorization": physical_authorization,
    }
    assert "release" not in summary["rejected_sources"]


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_canonical_json_bytes(payload))


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _formal_backup(
    tmp_path: Path,
    context: CompileContext,
    *,
    database_count: int = 4,
    empty_website: bool = False,
) -> Path:
    source = tmp_path / "MAGI_v2"
    database_relatives = [
        Path(".agent/jobs/job_queue.db"),
        Path(".agent/mq/message_queue.db"),
        Path(".runtime/conversation_history.sqlite3"),
        Path(".runtime/taiwan_legal_mcp/cache.sqlite3"),
    ][:database_count]
    databases: list[Path] = []
    for relative in database_relatives:
        database = source / relative
        database.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(database) as connection:
            connection.execute("CREATE TABLE state (id INTEGER PRIMARY KEY, value TEXT)")
            connection.execute("INSERT INTO state(value) VALUES (?)", (relative.as_posix(),))
        databases.append(database)
    website = (
        tmp_path
        / "Library"
        / "Application Support"
        / "MAGI"
        / "runtime"
        / "MAGI_v2"
        / "whalechao.github.io"
    )
    (website / "data").mkdir(parents=True)
    (website / "assets").mkdir()
    if not empty_website:
        (website / "data" / "content.json").write_text("{}", encoding="utf-8")
        (website / "assets" / "app.js").write_text("ok", encoding="utf-8")
    backup = tmp_path / "backup"
    prepare_backup(
        source_root=source,
        database_paths=databases,
        website_root=website,
        output_dir=backup,
        campaign_id=context.campaign_id,
        release_sha=context.release_sha,
        hardware_id=context.hardware_id,
        gate_config_sha256=context.gate_config_sha256,
        now=NOW,
    )
    return backup


def _test_backup_config(tmp_path: Path) -> dict[str, object]:
    config = json.loads(json.dumps(CONFIG))
    config["source_contract"] = {
        "schema_version": 1,
        "contract_id": "magi.v3.formal-v2-backup-sources/v1",
        "v2_root": str(tmp_path / "MAGI_v2"),
        "website_root": str(
            tmp_path
            / "Library"
            / "Application Support"
            / "MAGI"
            / "runtime"
            / "MAGI_v2"
            / "whalechao.github.io"
        ),
        "database_relatives": sorted(compiler.FORMAL_BACKUP_DATABASES),
    }
    return config


def test_formal_four_database_backup_is_authoritatively_recomputed(tmp_path: Path) -> None:
    context = CompileContext("campaign-backup", "a" * 64, "test-mac", GATE_SHA)
    backup = _formal_backup(tmp_path, context)
    output = tmp_path / "evidence"
    config = _test_backup_config(tmp_path)

    statuses = compile_backup_evidence(
        metadata_path=backup / "backup-metadata.json",
        output=output,
        context=context,
        config=config,
    )
    decision = evaluate_evidence(
        config,
        output,
        expected_context=context.as_dict(),
        now=NOW,
    )

    assert statuses == {
        "database_backup_restore_drill_passed": "passed",
        "runtime_state_snapshot_verified": "passed",
    }
    assert set(decision["passed"]) == set(statuses)
    assert not decision["invalid"]


def test_tmp_four_database_backup_cannot_impersonate_formal_machine_roots(
    tmp_path: Path,
) -> None:
    context = CompileContext("campaign-fake-root", "a" * 64, "test-mac", GATE_SHA)
    backup = _formal_backup(tmp_path, context)

    with pytest.raises(EvidenceCompileError, match="roots do not match"):
        compile_backup_evidence(
            metadata_path=backup / "backup-metadata.json",
            output=tmp_path / "evidence",
            context=context,
            config=CONFIG,
        )


def test_toy_one_database_backup_cannot_clear_formal_gate(tmp_path: Path) -> None:
    context = CompileContext("campaign-toy", "a" * 64, "test-mac", GATE_SHA)
    backup = _formal_backup(tmp_path, context, database_count=1)
    config = _test_backup_config(tmp_path)

    with pytest.raises(EvidenceCompileError, match="exact four formal V2 databases"):
        compile_backup_evidence(
            metadata_path=backup / "backup-metadata.json",
            output=tmp_path / "evidence",
            context=context,
            config=config,
        )


def _bound_file(role: str, path: Path, media_type: str = "application/json") -> BoundArtifact:
    data = path.read_bytes()
    return BoundArtifact(
        role=role,
        media_type=media_type,
        path=path.name,
        sha256=hashlib.sha256(data).hexdigest(),
        data=data,
    )


def test_portable_inventory_is_rebuilt_from_exact_release_bound_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "release-source"
    _write_json(
        source / "cron_jobs.json",
        [
            {
                "id": "heartbeat",
                "command": f"{source}/venv/bin/python3 {source}/scripts/heartbeat.py",
                "enabled": True,
            }
        ],
    )
    (source / "api").mkdir()
    (source / "api" / "service.py").write_text(
        "@app.get('/health')\ndef health():\n    return {}\n",
        encoding="utf-8",
    )
    (source / "skills" / "demo").mkdir(parents=True)
    (source / "skills" / "demo" / "action.py").write_text("VALUE = 1\n", encoding="utf-8")
    (source / "skills" / ".versions" / "old").mkdir(parents=True)
    (source / "skills" / ".versions" / "old" / "action.py").write_text(
        "VALUE = 0\n", encoding="utf-8"
    )
    (source / "daemon.py").write_text("VALUE = 1\n", encoding="utf-8")
    launchagents = source / "config" / "launchagents"
    launchagents.mkdir(parents=True)
    (launchagents / "com.magi.test.plist").write_bytes(
        plistlib.dumps({"Label": "com.magi.test", "RunAtLoad": False})
    )
    (source / "tests").mkdir()
    (source / "tests" / "test_demo.py").write_text("def test_demo(): pass\n", encoding="utf-8")
    (source / "tests" / "test_not_shipped.py").write_text(
        "def test_not_shipped(): pass\n", encoding="utf-8"
    )
    inventory = build_inventory(source, include_installed_launchagents=False)

    excluded = {
        "skills/.versions/old/action.py",
        "tests/test_not_shipped.py",
    }
    selected = [
        path
        for path in compiler._portable_inventory_source_inputs(source)
        if path.relative_to(source).as_posix() not in excluded
    ]
    rows = [
        {
            "path": path.relative_to(source).as_posix(),
            "sha256": _sha(path),
            "size": path.stat().st_size,
            "mode": f"{path.stat().st_mode & 0o777:04o}",
        }
        for path in selected
    ]
    inventory_bytes = _canonical_json_bytes(inventory)
    cron_bytes = (source / "cron_jobs.json").read_bytes()

    def artifact(role: str, data: bytes, name: str) -> BoundArtifact:
        return BoundArtifact(role, "application/json", name, hashlib.sha256(data).hexdigest(), data)

    by_role = {
        "upstream_release_marker": [artifact("upstream_release_marker", b"{}", "marker")],
        "upstream_release_manifest": [artifact("upstream_release_manifest", b"{}", "manifest")],
        "upstream_campaign_report": [artifact("upstream_campaign_report", b"{}", "campaign")],
        "upstream_portable_inventory": [
            artifact("upstream_portable_inventory", inventory_bytes, "inventory")
        ],
        "upstream_campaign_cron_snapshot": [
            artifact("upstream_campaign_cron_snapshot", cron_bytes, "cron")
        ],
        "upstream_campaign_cron_source": [
            artifact("upstream_campaign_cron_source", cron_bytes, "cron-source")
        ],
        "upstream_portable_inventory_input_manifest": [
            artifact(
                "upstream_portable_inventory_input_manifest",
                _canonical_json_bytes(
                    {
                        "schema_version": 1,
                        "selection": "magi.v3.portable-inventory-inputs/v1",
                        "inputs": rows,
                    }
                ),
                "inputs",
            )
        ],
        "upstream_portable_inventory_input": [
            artifact("upstream_portable_inventory_input", path.read_bytes(), path.name)
            for path in selected
        ],
    }
    files = {row["path"]: row for row in rows}
    files["docs/architecture/v3/generated/v2_inventory.json"] = {
        "path": "docs/architecture/v3/generated/v2_inventory.json",
        "sha256": hashlib.sha256(inventory_bytes).hexdigest(),
        "size": len(inventory_bytes),
        "mode": "0644",
    }
    monkeypatch.setattr(
        release_gate,
        "_verify_release_control_sources",
        lambda *_args, **_kwargs: ({}, {}, files),
    )
    monkeypatch.setattr(
        release_gate,
        "_verify_campaign_context_source",
        lambda *_args, **_kwargs: {
            "cron_jobs_sha256": hashlib.sha256(cron_bytes).hexdigest(),
            "cron_jobs_source_sha256": hashlib.sha256(cron_bytes).hexdigest(),
        },
    )

    assert release_gate._recompute_portable_inventory_metrics(by_role, {}) == {
        "inventory_sha_matches": True,
        "unmapped_interfaces": 0,
        "unapproved_source_runtime_drift": 0,
    }

    original = by_role["upstream_portable_inventory_input"][0]
    by_role["upstream_portable_inventory_input"][0] = artifact(
        original.role,
        original.data + b"tampered",
        original.path,
    )
    with pytest.raises(ValueError, match="source artifact mismatch"):
        release_gate._recompute_portable_inventory_metrics(by_role, {})


@pytest.mark.parametrize(
    ("target", "value"),
    [
        ("metadata", False),
        ("metadata", -1),
        ("drill", False),
        ("drill", -1),
    ],
)
def test_backup_zero_counts_reject_bool_and_negative_in_compiler_and_gate(
    tmp_path: Path, target: str, value: object
) -> None:
    context = CompileContext(f"campaign-count-{target}-{value}", "a" * 64, "test-mac", GATE_SHA)
    backup = _formal_backup(tmp_path, context, empty_website=True)
    config = _test_backup_config(tmp_path)
    metadata_path = backup / "backup-metadata.json"
    drill_path = backup / "restore-drill.json"
    metadata = json.loads(metadata_path.read_text())
    if target == "metadata":
        metadata["mutable_file_count"] = value
    else:
        drill = json.loads(drill_path.read_text())
        drill["verified_mutable_files"] = value
        _write_json(drill_path, drill)
        metadata["restore_drill"]["verified_mutable_files"] = value
        metadata["restore_drill"]["evidence_sha256"] = _sha(drill_path)
    _write_json(metadata_path, metadata)

    with pytest.raises(EvidenceCompileError, match="non-negative integer"):
        compile_backup_evidence(
            metadata_path=metadata_path,
            output=tmp_path / "evidence",
            context=context,
            config=config,
        )

    by_role = {
        artifact.role: [artifact]
        for artifact in (
            _bound_file("upstream_backup_metadata", metadata_path),
            _bound_file("upstream_backup_content_manifest", backup / "backup-content.json"),
            _bound_file("upstream_restore_drill", drill_path),
            _bound_file(
                "upstream_backup_archive",
                backup / "v2-state-and-website-backup.tar.gz",
                "application/gzip",
            ),
        )
    }
    with pytest.raises(ValueError, match="non-negative integer"):
        _recompute_backup_metrics(by_role, context.as_dict(), config["source_contract"])


def _release_control(tmp_path: Path) -> tuple[Path, CompileContext, ReleaseBundle]:
    release = tmp_path / "release"
    release.mkdir()
    file_row = {
        "path": "placeholder.txt",
        "sha256": hashlib.sha256(b"bound").hexdigest(),
        "size": 5,
        "mode": "0644",
    }
    snapshot_sha = hashlib.sha256(
        json.dumps([file_row], sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    context = CompileContext("campaign-deploy", snapshot_sha, "test-mac", GATE_SHA)
    manifest = {
        "schema_version": 1,
        "immutable": True,
        "release_id": "release-test",
        "commit": "b" * 40,
        "release_sha256": snapshot_sha,
        "source_snapshot_sha256": snapshot_sha,
        "source_file_count": 1,
        "files": [file_row],
    }
    manifest_path = release / "release-manifest.json"
    _write_json(manifest_path, manifest)
    marker = {
        "schema_version": 1,
        "release_id": manifest["release_id"],
        "commit": manifest["commit"],
        "manifest": "release-manifest.json",
        "manifest_sha256": _sha(manifest_path),
        "release_sha256": snapshot_sha,
        "source_snapshot_sha256": snapshot_sha,
        "source_file_count": 1,
    }
    _write_json(release / "RELEASE_COMPLETE.json", marker)
    bundle = ReleaseBundle(
        release,
        str(manifest["release_id"]),
        str(manifest["commit"]),
        snapshot_sha,
        _sha(manifest_path),
        (),
    )
    return release, context, bundle


def test_three_role_deploy_is_recomputed_from_plists_and_release_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    release, context, bundle = _release_control(tmp_path)
    campaign_path = tmp_path / "campaign.json"
    campaign = {
        "schema_version": 1,
        **context.as_dict(),
        "evidence_class": "immutable_release_offline_campaign",
        "release_id": bundle.release_id,
        "release_commit": bundle.commit,
        "release_manifest_sha256": bundle.manifest_sha256,
        "live_execution_performed": False,
        "cutover_execution_performed": False,
        "armed": True,
        "certifying": True,
        "harness_certified": True,
        "offline_complete": True,
        "decision": "GO",
        "execution_backend": "release_launcher",
        "fail_closed": False,
        "required_independent_passes": 7,
    }
    _write_json(campaign_path, campaign)
    monkeypatch.setattr(compiler, "_verify_release", lambda *_args: bundle)
    monkeypatch.setattr(
        compiler,
        "_verify_campaign",
        lambda *_args: (campaign, [], [campaign_path]),
    )
    deploy = tmp_path / "deploy"
    artifact_rows = []
    roles = []
    for role in ("control", "gateway", "supervisor"):
        label = f"com.magi.v3.{role}"
        plist = deploy / "launchagents" / f"{label}.plist"
        plist.parent.mkdir(parents=True, exist_ok=True)
        plist.write_bytes(plistlib.dumps({"Label": label, "ProgramArguments": ["/bin/false"]}))
        artifact_rows.append(
            {
                "path": f"launchagents/{label}.plist",
                "sha256": _sha(plist),
                "size": plist.stat().st_size,
            }
        )
        roles.append({"role": role, "label": label})
    manifest_path = deploy / "deploy-manifest.json"
    _write_json(
        manifest_path,
        {
            "schema_version": 1,
            "status": "prepared_not_installed",
            "mutation_performed": False,
            "release_id": bundle.release_id,
            "release_manifest_sha256": bundle.manifest_sha256,
            "release_manifest": str(release / "release-manifest.json"),
            "generated_at": NOW.isoformat(),
            "artifacts": artifact_rows,
            "roles": roles,
        },
    )
    marker_path = deploy / "DEPLOY_PREPARED.json"
    _write_json(
        marker_path,
        {
            "schema_version": 1,
            "status": "prepared_not_installed",
            "ready_to_install": True,
            "mutation_performed": False,
            "release_id": bundle.release_id,
            "release_manifest_sha256": bundle.manifest_sha256,
            "manifest": manifest_path.name,
            "manifest_sha256": _sha(manifest_path),
        },
    )
    output = tmp_path / "evidence"

    statuses = compile_deploy_evidence(
        marker_path=marker_path,
        release_root=release,
        campaign_report=campaign_path,
        output=output,
        context=context,
        config=CONFIG,
    )
    decision = evaluate_evidence(
        CONFIG,
        output,
        expected_context=context.as_dict(),
        now=NOW,
    )

    assert statuses == {"rendered_launchagent_manifest_checksums_saved": "passed"}
    assert "rendered_launchagent_manifest_checksums_saved" in decision["passed"]
    assert not decision["invalid"]

    envelope = json.loads(
        (output / "rendered_launchagent_manifest_checksums_saved.json").read_text()
    )
    frozen_sources: list[BoundArtifact] = []
    for artifact_row in envelope["artifacts"]:
        if artifact_row["role"] == "producer_report":
            continue
        path = output / artifact_row["path"]
        frozen_sources.append(_bound_file(artifact_row["role"], path, artifact_row["media_type"]))
    deploy_index = next(
        index for index, item in enumerate(frozen_sources) if item.role == "upstream_deploy_manifest"
    )
    marker_index = next(
        index for index, item in enumerate(frozen_sources) if item.role == "upstream_deploy_marker"
    )
    copied_deploy = json.loads(frozen_sources[deploy_index].data)
    copied_deploy["artifacts"][0]["size"] = True
    deploy_bytes = _canonical_json_bytes(copied_deploy)
    frozen_sources[deploy_index] = BoundArtifact(
        "upstream_deploy_manifest",
        "application/json",
        frozen_sources[deploy_index].path,
        hashlib.sha256(deploy_bytes).hexdigest(),
        deploy_bytes,
    )
    copied_marker = json.loads(frozen_sources[marker_index].data)
    copied_marker["manifest_sha256"] = frozen_sources[deploy_index].sha256
    marker_bytes = _canonical_json_bytes(copied_marker)
    frozen_sources[marker_index] = BoundArtifact(
        "upstream_deploy_marker",
        "application/json",
        frozen_sources[marker_index].path,
        hashlib.sha256(marker_bytes).hexdigest(),
        marker_bytes,
    )
    by_role = {item.role: [] for item in frozen_sources}
    for item in frozen_sources:
        by_role[item.role].append(item)
    with pytest.raises(ValueError, match="non-negative integer"):
        _recompute_deploy_metrics(by_role, context.as_dict())

    campaign["armed"] = False
    with pytest.raises(EvidenceCompileError, match="not an armed completed certifying"):
        compile_deploy_evidence(
            marker_path=marker_path,
            release_root=release,
            campaign_report=campaign_path,
            output=tmp_path / "unarmed-evidence",
            context=context,
            config=CONFIG,
        )

    campaign["armed"] = True
    changed_manifest = json.loads(manifest_path.read_text())
    first_path = deploy / changed_manifest["artifacts"][0]["path"]
    first_path.write_bytes(b"x")
    changed_manifest["artifacts"][0]["sha256"] = _sha(first_path)
    changed_manifest["artifacts"][0]["size"] = True
    _write_json(manifest_path, changed_manifest)
    changed_marker = json.loads(marker_path.read_text())
    changed_marker["manifest_sha256"] = _sha(manifest_path)
    _write_json(marker_path, changed_marker)
    with pytest.raises(EvidenceCompileError, match="non-negative integer"):
        compile_deploy_evidence(
            marker_path=marker_path,
            release_root=release,
            campaign_report=campaign_path,
            output=tmp_path / "bool-size-evidence",
            context=context,
            config=CONFIG,
        )


def test_human_normalizer_rejects_arbitrary_upstream_without_exact_approval_chain(
    tmp_path: Path,
) -> None:
    context = CompileContext("campaign-human", "a" * 64, "test-mac", GATE_SHA)
    evidence_id = "human_go_approval_recorded"
    spec = EVIDENCE_SPECS[evidence_id]
    source_path = tmp_path / "sources" / evidence_id / "00-arbitrary.json"
    _write_json(source_path, {"approved": True})
    source = {
        "role": "upstream_arbitrary",
        "media_type": "application/json",
        "path": str(source_path.relative_to(tmp_path)),
        "sha256": _sha(source_path),
    }
    metrics = {
        "approved": True,
        "approver_id": "fake",
        "approver_role": "authorized_release_owner",
        "approval_scope": "exact_release_and_campaign",
    }
    metrics_sha = hashlib.sha256(_canonical_json_bytes(metrics)).hexdigest()
    report = {
        "schema_version": 1,
        "report_schema": spec.report_schema,
        "evidence_id": evidence_id,
        "status": "passed",
        "generated_at": NOW.isoformat(),
        "producer": spec.producer,
        **context.as_dict(),
        "normalized_by": "scripts.v3_evidence_compiler",
        "normalizer_schema": "magi.v3.trusted-evidence-normalizer/v1",
        "source_artifacts": [source],
        "run_context": {
            **context.as_dict(),
            "run_id": "fake-human",
            "execution_mode": spec.execution_mode,
            "started_at": NOW.isoformat(),
            "completed_at": NOW.isoformat(),
        },
        "metrics": metrics,
        "metrics_sha256": metrics_sha,
        "normalization": {
            "defaults_used": False,
            "live_state_accessed": False,
            "service_start_performed": False,
        },
    }
    report_path = tmp_path / "reports" / f"{evidence_id}.json"
    _write_json(report_path, report)
    envelope = {
        "schema_version": 1,
        "evidence_id": evidence_id,
        "status": "passed",
        "generated_at": NOW.isoformat(),
        "producer": spec.producer,
        **context.as_dict(),
        "metrics_sha256": metrics_sha,
        "artifacts": [
            {
                "role": "producer_report",
                "media_type": "application/json",
                "path": str(report_path.relative_to(tmp_path)),
                "sha256": _sha(report_path),
            },
            source,
        ],
    }
    _write_json(tmp_path / f"{evidence_id}.json", envelope)

    decision = evaluate_evidence(
        CONFIG,
        tmp_path,
        expected_context=context.as_dict(),
        now=NOW,
    )

    assert evidence_id in decision["invalid"]
    assert any("fixed source roles" in error for error in decision["invalid"][evidence_id])


def test_second_public_compile_rereads_mutated_source_instead_of_using_stale_cache(
    tmp_path: Path,
) -> None:
    context = CompileContext("campaign-cache", "a" * 64, "test-mac", GATE_SHA)
    backup = _formal_backup(tmp_path, context)
    config = _test_backup_config(tmp_path)
    metadata_path = backup / "backup-metadata.json"

    compile_backup_evidence(
        metadata_path=metadata_path,
        output=tmp_path / "first-evidence",
        context=context,
        config=config,
    )
    metadata = json.loads(metadata_path.read_text())
    metadata["coverage"] = []
    _write_json(metadata_path, metadata)

    with pytest.raises(EvidenceCompileError, match="does not cover exact formal"):
        compile_backup_evidence(
            metadata_path=metadata_path,
            output=tmp_path / "second-evidence",
            context=context,
            config=config,
        )


@pytest.mark.parametrize("module_mode", [False, True])
def test_cli_and_module_invocation_exit_nonzero_for_incomplete_evidence(
    tmp_path: Path, module_mode: bool
) -> None:
    output = tmp_path / ("module" if module_mode else "direct")
    common = [
        "--output",
        str(output),
        "--campaign-id",
        "campaign-empty",
        "--release-sha",
        "a" * 64,
        "--hardware-id",
        "test-mac",
        "--gate-config-sha256",
        GATE_SHA,
        "--gate-config",
        str(GATE_PATH),
    ]
    command = (
        [sys.executable, "-m", "scripts.v3_evidence_compiler", *common]
        if module_mode
        else [sys.executable, str(ROOT / "scripts" / "v3_evidence_compiler.py"), *common]
    )

    result = subprocess.run(
        command,
        cwd=ROOT if module_mode else tmp_path,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 1
    summary = json.loads(result.stdout)
    assert summary["decision"] == "EVIDENCE_INCOMPLETE"
    assert len(summary["unavailable"]) == 28
