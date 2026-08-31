from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

import pytest

from scripts.v3_release_gate import (
    BoundArtifact,
    EVIDENCE_SPECS,
    _campaign_structured_rows,
    _canonical_json_bytes,
    _configured_value,
    _nonnegative_int,
    _route_attested_seatbelt_workspace,
    _route_seatbelt_attestation,
    _recompute_route_metrics,
    evaluate_evidence,
    validate_evidence,
)
from scripts.v3_validation.worker_soak_evidence import (
    WorkerSoakEvidenceError,
    summarize_worker_soak_measurements,
)

ROOT = Path(__file__).resolve().parents[2]
BASE_CONFIG = json.loads((ROOT / "config" / "v3_cutover_gates.json").read_text())
NOW = datetime(2026, 7, 14, 4, 30, tzinfo=timezone.utc)
CONTEXT = {
    "campaign_id": "campaign-2026-07",
    "release_sha": "release-deadbeef",
    "hardware_id": "magi-mac-studio-01",
    "gate_config_sha256": "a" * 64,
}


def test_release_gate_recomputes_dynamic_route_seatbelt_contract(
    tmp_path: Path,
) -> None:
    from scripts.v3_validation.route_certification import _seatbelt_attestation

    workspace = tmp_path / "route-certification" / "ordinary-week"
    attestation = _route_seatbelt_attestation(workspace)
    assert attestation == _seatbelt_attestation(workspace)
    assert attestation["schema_version"] == 2
    assert attestation["live_magi_mutable_read_write_denied"] is True
    environment = attestation["environment_allowlist"]
    assert "MAGI_V3_ROUTE_CERTIFYING" in environment["formal_base"]
    assert "MAGI_V3_ROUTE_CERTIFYING" in environment["formal_trace"]

    assert _route_attested_seatbelt_workspace(attestation, "ordinary-week") == (
        workspace.resolve()
    )
    attestation["profile_sha256"] = "0" * 64
    assert _route_attested_seatbelt_workspace(attestation, "ordinary-week") is None


def test_worker_soak_metrics_follow_targeted_campaign_pass_count() -> None:
    row = {
        "cycles_requested": 100,
        "cycles_completed": 100,
        "process_groups_gone": 100,
        "active_workers_after": 0,
        "governor_slots_after": 0,
        "fd_drift": 0,
    }

    assert summarize_worker_soak_measurements([row]) == {
        "cycles": 100,
        "unreaped_workers": 0,
        "resource_baseline_restored": True,
    }
    assert summarize_worker_soak_measurements([row] * 7)["cycles"] == 700


def test_worker_soak_metrics_fail_closed_without_hiding_unreaped_workers() -> None:
    row = {
        "cycles_requested": 100,
        "cycles_completed": 100,
        "process_groups_gone": 99,
        "active_workers_after": 0,
        "governor_slots_after": 0,
        "fd_drift": 0,
    }
    assert summarize_worker_soak_measurements([row]) == {
        "cycles": 100,
        "unreaped_workers": 1,
        "resource_baseline_restored": False,
    }
    row["fd_drift"] = True
    with pytest.raises(WorkerSoakEvidenceError, match="non-negative integer"):
        summarize_worker_soak_measurements([row])


def test_release_gate_route_formal_environment_allowlist_fails_closed_on_drift(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "route-certification" / "ordinary-week"
    attestation = _route_seatbelt_attestation(workspace)
    attestation["environment_allowlist"]["formal_base"].append("UNREVIEWED_ENV")

    assert _route_attested_seatbelt_workspace(attestation, "ordinary-week") is None


def test_release_gate_rejects_unbound_route_seatbelt_workspace(
    tmp_path: Path,
) -> None:
    attestation = _route_seatbelt_attestation(tmp_path / "unbound")

    assert _route_attested_seatbelt_workspace(attestation, "ordinary-week") is None


def test_release_gate_conditional_g28_requires_exact_five_fixed_sources(monkeypatch) -> None:
    """Conditional G28 cannot omit or smuggle a preauthorization source."""
    import scripts.v3_validation.human_approval as approval_module
    import scripts.v3_release_gate as gate_module

    def verifier(*, consumption, consumption_sha256, **_kwargs):
        assert consumption == {"machine_evidence": []}
        assert consumption_sha256
        return {
            "approved": True,
            "approver_id": "local:501:ai",
            "approver_role": "authorized_release_owner",
            "approval_scope": "exact_release_and_campaign",
            "authorization_mode": "conditional_daytime_window",
            "conditional_daytime_window": {},
            "conditional_request_sha256": "1" * 64,
            "conditional_receipt_sha256": "2" * 64,
            "conditional_consumption_sha256": "3" * 64,
        }

    monkeypatch.setattr(approval_module, "derive_conditional_human_approval_metrics", verifier)
    def artifact(role: str, value: dict) -> BoundArtifact:
        data = json.dumps(value).encode()
        return BoundArtifact(role, "application/json", f"{role}.json", hashlib.sha256(data).hexdigest(), data)

    request = {"schema": "magi.v3.conditional-human-approval-request/v2"}
    sources = [
        artifact("upstream_conditional_request", request),
        artifact("upstream_conditional_receipt", {}),
        artifact("upstream_conditional_consumption", {"machine_evidence": []}),
        artifact("upstream_approval_gate_report", {}),
        artifact("upstream_approval_gate_config", {}),
    ]
    assert gate_module._authoritative_normalized_metrics(
        "human_go_approval_recorded", sources, CONTEXT, {}, {}
    )["authorization_mode"] == "conditional_daytime_window"
    with pytest.raises(ValueError, match="source roles are not exact"):
        gate_module._authoritative_normalized_metrics(
            "human_go_approval_recorded",
            [*sources, artifact("upstream_unbound", {})],
            CONTEXT,
            {},
            {},
        )


def _campaign_days_for_safety(workload: str, *, network: bool, external: bool) -> list[dict]:
    return [
        {
            "workloads": [
                {
                    "workload": workload,
                    "returncode": 0,
                    "validation_pass": validation_pass,
                    "status": "offline_passed",
                    "structured_evidence": {
                        "schema_version": 1,
                        "workload": workload,
                        "status": "passed",
                        "measurements": {},
                        "network_access_performed": network,
                        "external_network_access_performed": external,
                        "service_start_performed": False,
                        "production_port_access_performed": False,
                        "launchctl_performed": False,
                    },
                }
            ]
        }
        for validation_pass in range(1, 2)
    ]


def test_release_gate_schedule_allows_only_disposable_internal_network() -> None:
    workload = "seven_day_schedule_10x_arrival_2x_duration_replay"
    days = _campaign_days_for_safety(workload, network=True, external=False)

    assert len(_campaign_structured_rows(days, workload)) == 1

    days[0]["workloads"][0]["structured_evidence"][
        "external_network_access_performed"
    ] = True
    with pytest.raises(ValueError, match="safety attestation failed"):
        _campaign_structured_rows(days, workload)


def test_release_gate_non_schedule_workload_still_rejects_any_network() -> None:
    workload = "golden_business_flows"
    days = _campaign_days_for_safety(workload, network=True, external=False)

    with pytest.raises(ValueError, match="safety attestation failed"):
        _campaign_structured_rows(days, workload)


def _set_nested(payload: dict[str, object], dotted_path: str, value: object) -> None:
    current = payload
    parts = dotted_path.split(".")
    for part in parts[:-1]:
        child = current.setdefault(part, {})
        assert isinstance(child, dict)
        current = child
    current[parts[-1]] = value


def passing_metrics(evidence_id: str) -> dict[str, object]:
    metrics: dict[str, object] = {}
    for rule in EVIDENCE_SPECS[evidence_id].rules:
        expected = _configured_value(BASE_CONFIG, rule.expected)
        if rule.operation == "true":
            value: object = True
        elif rule.operation == "false":
            value = False
        elif rule.operation == "nonempty":
            value = "verified-owner"
        elif rule.operation == "contains_all":
            value = list(expected)
        else:
            value = expected
        _set_nested(metrics, rule.path, value)
    return metrics


def evidence(
    evidence_id: str,
    *,
    status: str = "passed",
    metrics: dict[str, object] | None = None,
) -> tuple[dict[str, object], dict[str, object]]:
    spec = EVIDENCE_SPECS[evidence_id]
    metrics = deepcopy(metrics if metrics is not None else passing_metrics(evidence_id))
    metrics_sha256 = hashlib.sha256(_canonical_json_bytes(metrics)).hexdigest()
    report = {
        "schema_version": 1,
        "report_schema": spec.report_schema,
        "evidence_id": evidence_id,
        "status": status,
        "generated_at": "2026-07-14T12:00:00+08:00",
        "producer": spec.producer,
        **CONTEXT,
        "run_context": {
            **CONTEXT,
            "run_id": f"run-{evidence_id}",
            "execution_mode": spec.execution_mode,
            "started_at": "2026-07-14T11:58:00+08:00",
            "completed_at": "2026-07-14T12:00:00+08:00",
        },
        "metrics": metrics,
        "metrics_sha256": metrics_sha256,
    }
    report_bytes = json.dumps(report, ensure_ascii=False, sort_keys=True).encode()
    document = {
        "schema_version": 1,
        "evidence_id": evidence_id,
        "status": status,
        "generated_at": "2026-07-14T12:00:00+08:00",
        "producer": spec.producer,
        **CONTEXT,
        "metrics_sha256": metrics_sha256,
        "artifacts": [
            {
                "role": "producer_report",
                "media_type": "application/json",
                "path": f"reports/{evidence_id}.json",
                "sha256": hashlib.sha256(report_bytes).hexdigest(),
            }
        ],
    }
    return document, report


def write_evidence(
    root: Path,
    bundle: tuple[dict[str, object], dict[str, object]],
    *,
    create_artifact: bool = True,
) -> None:
    document, report = bundle
    if create_artifact:
        artifact_path = root / str(document["artifacts"][0]["path"])
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        artifact_path.write_text(
            json.dumps(report, ensure_ascii=False, sort_keys=True), encoding="utf-8"
        )
    path = root / f"{document['evidence_id']}.json"
    path.write_text(json.dumps(document), encoding="utf-8")


def write_all_valid(root: Path) -> None:
    for evidence_id in EVIDENCE_SPECS:
        write_evidence(root, evidence(evidence_id))


def evaluate(root: Path, **kwargs):
    return evaluate_evidence(
        deepcopy(BASE_CONFIG), root, expected_context=CONTEXT, now=NOW, **kwargs
    )


def _bound(role: str, payload: object, *, raw: bytes | None = None) -> BoundArtifact:
    data = raw if raw is not None else _canonical_json_bytes(payload)
    return BoundArtifact(
        role=role,
        media_type="application/json",
        path=f"sources/{role}.json",
        sha256=hashlib.sha256(data).hexdigest(),
        data=data,
    )


def _route_sources(
    *,
    unarmed: bool = False,
    tamper_role: str | None = None,
    route_count: object = 1,
    release_file_count: object | None = None,
    release_file_size: object | None = None,
):
    route = {
        "schema_version": 1,
        "services": {
            "5002": [{"rule": "/health", "methods": ["GET"], "endpoint": "health"}],
            "5003": [],
        },
        "counts": {"5002": route_count, "5003": 0, "total": route_count},
    }
    normalized = [
        {"service": "5002", "rule": "/health", "methods": ["GET"], "endpoint": "health"}
    ]
    fingerprint = hashlib.sha256(
        json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    reviews = {
        "schema_version": 1,
        "review_policy": "explicit_route_method_only",
        "inventory_fingerprint": fingerprint,
        "reviews": [
            {
                "service": "5002",
                "rule": "/health",
                "method": "GET",
                "endpoint": "health",
                "reviewed": True,
                "reviewed_by": "test",
                "rationale": "bound test route",
            }
        ],
    }
    supplement = {
        "schema_version": 1,
        "review_policy": "explicit_route_method_only",
        "inventory_fingerprint": fingerprint,
        "reviews": [],
    }
    capability = {"schema_version": 1, "capabilities": []}
    route_artifact = _bound("upstream_runtime_route_inventory", route)
    review_artifact = _bound("upstream_route_reviews", reviews)
    supplement_artifact = _bound("upstream_route_review_supplement", supplement)
    capability_artifact = _bound("upstream_capability_manifest", capability)
    runtime = {
        "schema_version": 1,
        "runtime_root": "/bound/runtime",
        "base_runtime_root": "/bound/base-runtime",
        "python_runtime": "/bound/runtime/bin/python",
        "python_runtime_realpath": "/bound/base-runtime/bin/python3",
        "python_runtime_sha256": "1" * 64,
        "tree_sha256": "3" * 64,
        "directories": [
            {"path": "lib/python3.14/site-packages", "mode": "0555"}
        ],
        "base_directories": [],
    }
    runtime_artifact = _bound("upstream_python_runtime_manifest", runtime)
    file_artifacts = {
        "config/v3_capability_manifest.json": capability_artifact,
        "docs/architecture/v3/generated/v2_runtime_routes.json": route_artifact,
        "scripts/v3_validation/actual_route_replay.py": _bound(
            "source", {"source": "actual"}
        ),
        "scripts/v3_validation/route-success-proof-review.json": _bound(
            "source", {"source": "proof"}
        ),
        "scripts/v3_validation/route-method-review-supplement.json": supplement_artifact,
        "scripts/v3_validation/route-method-review.json": review_artifact,
        "scripts/v3_validation/route_certification.py": _bound(
            "source", {"source": "compiler"}
        ),
        "scripts/v3_validation/route_success_trace_plugin.py": _bound(
            "source", {"source": "plugin"}
        ),
    }
    file_rows = [
        {
            "path": path,
            "sha256": artifact.sha256,
            "size": len(artifact.data),
            "mode": "0644",
        }
        for path, artifact in sorted(file_artifacts.items())
    ]
    if release_file_size is not None:
        file_rows[0]["size"] = release_file_size
    release_sha = hashlib.sha256(
        json.dumps(file_rows, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    context = {
        "campaign_id": "route-campaign",
        "release_sha": release_sha,
        "hardware_id": "test-mac",
        "gate_config_sha256": "a" * 64,
    }
    manifest = {
        "schema_version": 1,
        "immutable": True,
        "release_id": "route-release",
        "commit": "b" * 40,
        "release_sha256": release_sha,
        "source_snapshot_sha256": release_sha,
        "source_file_count": (
            len(file_rows) if release_file_count is None else release_file_count
        ),
        "files": file_rows,
    }
    manifest_artifact = _bound("upstream_release_manifest", manifest)
    marker = {
        "schema_version": 1,
        "release_id": manifest["release_id"],
        "commit": manifest["commit"],
        "manifest": "release-manifest.json",
        "manifest_sha256": manifest_artifact.sha256,
        "release_sha256": release_sha,
        "source_snapshot_sha256": release_sha,
        "source_file_count": (
            len(file_rows) if release_file_count is None else release_file_count
        ),
    }
    campaign = {
        "schema_version": 1,
        **context,
        "evidence_class": "immutable_release_offline_campaign",
        "release_id": manifest["release_id"],
        "release_commit": manifest["commit"],
        "release_manifest_sha256": manifest_artifact.sha256,
        "live_execution_performed": False,
        "cutover_execution_performed": False,
        "armed": not unarmed,
        "certifying": not unarmed,
        "harness_certified": not unarmed,
        "offline_complete": not unarmed,
        "decision": "NO_GO" if unarmed else "GO",
        "execution_backend": "release_launcher",
        "python_runtime_path": runtime["python_runtime"],
        "python_runtime_realpath": runtime["python_runtime_realpath"],
        "python_runtime_sha256": runtime["python_runtime_sha256"],
        "python_runtime_manifest": "/bound/python-runtime-manifest.json",
        "python_runtime_manifest_sha256": runtime_artifact.sha256,
        "python_runtime_tree_sha256": runtime["tree_sha256"],
        "fail_closed": unarmed,
        "required_independent_passes": 1,
        "artifacts": [],
    }
    if tamper_role == "upstream_route_reviews":
        review_artifact = _bound(tamper_role, reviews, raw=review_artifact.data + b"\n")
    if tamper_role == "upstream_capability_manifest":
        capability_artifact = _bound(
            tamper_role, capability, raw=capability_artifact.data + b"\n"
        )
    if tamper_role == "upstream_route_review_supplement":
        supplement_artifact = _bound(
            tamper_role, supplement, raw=supplement_artifact.data + b"\n"
        )
    certification_artifacts = [
        _bound("upstream_route_certification_report", {"invalid_fixture": index})
        for index in range(1)
    ]
    campaign_day_artifact = _bound("upstream_campaign_day", {"invalid_fixture": True})
    campaign["artifacts"] = [
        {"path": "artifacts/day.json", "sha256": campaign_day_artifact.sha256}
    ]
    artifacts = [
        _bound("upstream_release_marker", marker),
        manifest_artifact,
        _bound("upstream_campaign_report", campaign),
        campaign_day_artifact,
        route_artifact,
        review_artifact,
        supplement_artifact,
        capability_artifact,
        runtime_artifact,
        *certification_artifacts,
    ]
    by_role: dict[str, list[BoundArtifact]] = {}
    for artifact in artifacts:
        by_role.setdefault(artifact.role, []).append(artifact)
    return by_role, context


@pytest.mark.parametrize(
    ("role", "message"),
    [
        ("upstream_route_reviews", "route review is not bound"),
        (
            "upstream_route_review_supplement",
            "route review supplement is not bound",
        ),
        ("upstream_capability_manifest", "capability manifest is not bound"),
    ],
)
def test_route_review_and_capability_bytes_must_match_release_manifest(
    role: str, message: str
) -> None:
    by_role, context = _route_sources(tamper_role=role)

    with pytest.raises(ValueError, match=message):
        _recompute_route_metrics(by_role, context)


def test_unarmed_campaign_cannot_certify_release_route_evidence() -> None:
    by_role, context = _route_sources(unarmed=True)

    with pytest.raises(ValueError, match="not an armed completed certifying"):
        _recompute_route_metrics(by_role, context)


@pytest.mark.parametrize("value", [True, -1])
def test_route_declared_counts_reject_bool_and_negative(value: object) -> None:
    by_role, context = _route_sources(route_count=value)

    with pytest.raises(ValueError, match="non-negative integer"):
        _recompute_route_metrics(by_role, context)


@pytest.mark.parametrize("field", ["count", "size"])
@pytest.mark.parametrize("value", [True, -1])
def test_release_manifest_counts_and_sizes_reject_bool_and_negative(
    field: str, value: object
) -> None:
    kwargs = {"release_file_count": value} if field == "count" else {"release_file_size": value}
    by_role, context = _route_sources(**kwargs)

    with pytest.raises(ValueError, match="non-negative integer"):
        _recompute_route_metrics(by_role, context)


@pytest.mark.parametrize("value", [-1, True, False, 1.5, "0"])
def test_authoritative_counter_rejects_negative_bool_and_non_integer(value: object) -> None:
    with pytest.raises(ValueError, match="non-negative integer"):
        _nonnegative_int(value, "fault counter")


def test_handwritten_semantic_envelopes_cannot_be_go(tmp_path: Path) -> None:
    assert list(EVIDENCE_SPECS) == BASE_CONFIG["required_evidence"]
    write_all_valid(tmp_path)

    report = evaluate(tmp_path)

    assert report["decision"] == "NO_GO"
    assert report["passed_count"] == 0
    assert len(report["invalid"]) == len(EVIDENCE_SPECS) == 14
    assert all(
        any("no code-owned authoritative verifier" in error for error in errors)
        for errors in report["invalid"].values()
    )


def test_registered_machine_producers_resolve_to_release_source() -> None:
    for spec in EVIDENCE_SPECS.values():
        if spec.producer.startswith("human."):
            continue
        producer_path = ROOT / spec.producer.replace(".", "/")
        assert producer_path.with_suffix(".py").is_file() or (
            producer_path / "__init__.py"
        ).is_file(), spec.producer


def test_missing_failed_and_invalid_evidence_are_no_go(tmp_path: Path) -> None:
    failed_id = "v3_unit_contract_integration_e2e_passed"
    invalid_id = "runtime_route_inventory_current"
    write_evidence(tmp_path, evidence(failed_id, status="failed"))
    invalid, invalid_report = evidence(invalid_id)
    invalid["artifacts"] = [{"path": "x", "sha256": "not-a-digest"}]
    write_evidence(tmp_path, (invalid, invalid_report))

    report = evaluate(tmp_path)

    assert failed_id in report["invalid"]
    assert invalid_id in report["invalid"]
    assert set(report["no_go_reasons"]) == {
        "required_evidence_missing",
        "required_evidence_invalid",
    }


def test_evidence_timestamp_must_have_timezone() -> None:
    document, _ = evidence("portable_source_inventory_current")
    document["generated_at"] = "2026-07-14T12:00:00"

    assert "generated_at must include a timezone" in validate_evidence(
        document, "portable_source_inventory_current", expected_context=CONTEXT, now=NOW
    )


def test_missing_or_tampered_artifact_is_invalid(tmp_path: Path) -> None:
    evidence_id = "portable_source_inventory_current"
    write_evidence(tmp_path, evidence(evidence_id), create_artifact=False)
    missing = evaluate(tmp_path)
    assert "is missing" in missing["invalid"][evidence_id][0]

    artifact = tmp_path / "reports" / f"{evidence_id}.json"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"tampered")
    tampered = evaluate(tmp_path)
    assert "SHA-256 mismatch" in tampered["invalid"][evidence_id][0]


def test_artifact_path_cannot_escape_evidence_directory(tmp_path: Path) -> None:
    evidence_id = "portable_source_inventory_current"
    document, report = evidence(evidence_id)
    document["artifacts"] = [
        {
            "role": "producer_report",
            "media_type": "application/json",
            "path": "../outside.json",
            "sha256": hashlib.sha256(b"example").hexdigest(),
        }
    ]
    write_evidence(tmp_path, (document, report), create_artifact=False)

    result = evaluate(tmp_path)

    assert "must stay inside" in result["invalid"][evidence_id][0]


def test_required_evidence_must_exactly_match_registered_semantic_specs(tmp_path: Path) -> None:
    config = deepcopy(BASE_CONFIG)
    config["required_evidence"] = config["required_evidence"][:-1]
    with pytest.raises(ValueError, match="must exactly match semantic evidence specs"):
        evaluate_evidence(config, tmp_path, expected_context=CONTEXT, now=NOW)

    config = deepcopy(BASE_CONFIG)
    config["required_evidence"][0] = "handwritten_generic_pass"
    with pytest.raises(ValueError, match="unregistered"):
        evaluate_evidence(config, tmp_path, expected_context=CONTEXT, now=NOW)


def test_duplicate_required_evidence_is_configuration_error(tmp_path: Path) -> None:
    config = deepcopy(BASE_CONFIG)
    config["required_evidence"].append(config["required_evidence"][0])
    with pytest.raises(ValueError, match="duplicates"):
        evaluate_evidence(config, tmp_path, expected_context=CONTEXT, now=NOW)


def test_required_evidence_must_not_be_empty(tmp_path: Path) -> None:
    config = deepcopy(BASE_CONFIG)
    config["required_evidence"] = []
    with pytest.raises(ValueError, match="must not be empty"):
        evaluate_evidence(config, tmp_path, expected_context=CONTEXT, now=NOW)


def test_evidence_must_declare_at_least_one_artifact(tmp_path: Path) -> None:
    evidence_id = "portable_source_inventory_current"
    document, report = evidence(evidence_id)
    document["artifacts"] = []
    write_evidence(tmp_path, (document, report), create_artifact=False)

    result = evaluate(tmp_path)

    assert "at least one artifact" in result["invalid"][evidence_id][0]


@pytest.mark.parametrize(
    "field", ["campaign_id", "release_sha", "hardware_id", "gate_config_sha256"]
)
def test_evidence_is_bound_to_exact_release_context(tmp_path: Path, field: str) -> None:
    evidence_id = "portable_source_inventory_current"
    document, report = evidence(evidence_id)
    document[field] = "wrong" if field != "gate_config_sha256" else "b" * 64
    write_evidence(tmp_path, (document, report))

    result = evaluate(tmp_path)

    assert any(field in error for error in result["invalid"][evidence_id])


def test_producer_report_context_and_run_context_are_both_bound(tmp_path: Path) -> None:
    evidence_id = "runtime_route_inventory_current"
    document, report = evidence(evidence_id)
    report["release_sha"] = "another-release"
    report["run_context"]["campaign_id"] = "another-campaign"
    _rewrite_report_binding(document, report)
    write_evidence(tmp_path, (document, report))

    result = evaluate(tmp_path)

    errors = result["invalid"][evidence_id]
    assert any("producer_report release_sha" in error for error in errors)
    assert any("run_context.campaign_id" in error for error in errors)


def _rewrite_report_binding(document: dict[str, object], report: dict[str, object]) -> None:
    report_bytes = json.dumps(report, ensure_ascii=False, sort_keys=True).encode()
    document["artifacts"][0]["sha256"] = hashlib.sha256(report_bytes).hexdigest()


def test_registered_producer_and_schema_are_not_free_form(tmp_path: Path) -> None:
    evidence_id = "portable_source_inventory_current"
    document, report = evidence(evidence_id)
    document["producer"] = "pytest"
    report["producer"] = "pytest"
    report["report_schema"] = "generic-passed/v1"
    _rewrite_report_binding(document, report)
    write_evidence(tmp_path, (document, report))

    result = evaluate(tmp_path)

    errors = result["invalid"][evidence_id]
    assert any("registered producer" in error for error in errors)


def test_threshold_failure_cannot_be_hidden_by_passed_status(tmp_path: Path) -> None:
    evidence_id = "atomic_release_switch_and_cold_rollback_drill_passed"
    document, report = evidence(evidence_id)
    report["metrics"]["rollback_rto_seconds"] = 121
    metrics_digest = hashlib.sha256(_canonical_json_bytes(report["metrics"])).hexdigest()
    report["metrics_sha256"] = metrics_digest
    document["metrics_sha256"] = metrics_digest
    _rewrite_report_binding(document, report)
    write_evidence(tmp_path, (document, report))

    result = evaluate(tmp_path)

    errors = result["invalid"][evidence_id]
    assert any("rollback_rto_seconds" in error and "le 120" in error for error in errors)


def test_metrics_digest_must_bind_envelope_to_producer_report(tmp_path: Path) -> None:
    evidence_id = "v3_unit_contract_integration_e2e_passed"
    document, report = evidence(evidence_id)
    document["metrics_sha256"] = "b" * 64
    write_evidence(tmp_path, (document, report))

    result = evaluate(tmp_path)

    assert any("evidence metrics_sha256" in error for error in result["invalid"][evidence_id])


def test_stale_and_future_evidence_are_invalid(tmp_path: Path) -> None:
    stale_id = "portable_source_inventory_current"
    future_id = "runtime_route_inventory_current"
    stale, stale_report = evidence(stale_id)
    stale["generated_at"] = "2026-07-12T00:00:00Z"
    future, future_report = evidence(future_id)
    future["generated_at"] = "2026-07-14T05:00:00Z"
    write_evidence(tmp_path, (stale, stale_report))
    write_evidence(tmp_path, (future, future_report))

    result = evaluate(tmp_path, max_age_hours=24)

    assert any("maximum evidence age" in item for item in result["invalid"][stale_id])
    assert any("future" in item for item in result["invalid"][future_id])


@pytest.mark.parametrize("max_age", [0, float("inf"), float("nan")])
def test_invalid_freshness_window_is_rejected(tmp_path: Path, max_age: float) -> None:
    with pytest.raises(ValueError, match="finite and greater than zero"):
        evaluate(tmp_path, max_age_hours=max_age)
