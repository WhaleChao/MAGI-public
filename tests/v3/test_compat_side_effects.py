from __future__ import annotations

import copy

import pytest

from scripts.v3_validation.paths import JOB_ENVELOPE_SCHEMA_PATH
from scripts.v3_validation.schema import ContractValidationError, load_json, validate_json
from scripts.v3_validation.side_effects import SIDE_EFFECT_CLASSES, evaluate_side_effect


def _job(side_effect_class: str) -> dict[str, object]:
    return {
        "schema_version": "3.0",
        "job_id": "job-contract-test",
        "capability": "operations_health_security",
        "operation": "contract_test",
        "worker_class": "light",
        "side_effect_class": side_effect_class,
        "priority_class": "P3",
        "status": "queued",
        "created_at": "2026-07-14T02:00:00Z",
        "scheduled_for": "2026-07-14T02:00:00Z",
        "latest_start_at": "2026-07-14T02:01:00Z",
        "deadline_at": "2026-07-14T02:02:00Z",
        "not_before": "2026-07-14T02:00:00Z",
        "attempt": 0,
        "max_attempts": 1,
        "timeout_sec": 30,
        "queue_ttl_sec": 60,
        "preemptible": True,
        "resource_claim": {
            "memory_mb": 16,
            "metal_mb": 0,
            "cpu_percent": 10,
            "disk_io": "none",
            "nas_io": "none",
            "network": "none",
            "browser_tokens": 0,
        },
        "commit_phase": "not_applicable",
        "input": {},
        "business_completed": False,
        "artifacts": [],
        "side_effect_receipts": [],
        "metrics": {},
    }


@pytest.mark.parametrize("effect", sorted(SIDE_EFFECT_CLASSES))
def test_offline_replay_never_executes(effect: str) -> None:
    decision = evaluate_side_effect(effect)
    assert decision.allowed is True
    assert decision.execute is False


@pytest.mark.parametrize("effect", ["none", "read_only"])
def test_isolated_live_allows_only_read_only_classes_by_default(effect: str) -> None:
    decision = evaluate_side_effect(effect, phase="isolated_live_validation")
    assert decision.allowed is True
    assert decision.execute is True


@pytest.mark.parametrize("effect", ["local_draft", "reversible_write", "external_commit", "destructive"])
def test_isolated_live_blocks_writes_by_default(effect: str) -> None:
    decision = evaluate_side_effect(effect, phase="isolated_live_validation")
    assert decision.allowed is False
    assert decision.execute is False


def test_explicit_sandbox_can_only_enable_local_or_reversible_writes() -> None:
    for effect in ("local_draft", "reversible_write"):
        assert evaluate_side_effect(
            effect,
            phase="isolated_live_validation",
            sandboxed=True,
            allow_sandbox_writes=True,
        ).execute
    for effect in ("external_commit", "destructive"):
        assert not evaluate_side_effect(
            effect,
            phase="isolated_live_validation",
            sandboxed=True,
            allow_sandbox_writes=True,
        ).allowed


def test_job_contract_requires_idempotency_and_confirmation_for_external_commit() -> None:
    job = _job("external_commit")
    schema = load_json(JOB_ENVELOPE_SCHEMA_PATH)
    with pytest.raises(ContractValidationError):
        validate_json(job, schema, label="job")

    valid = copy.deepcopy(job)
    valid["idempotency_key"] = "fixture-only-key"
    valid["confirmation"] = {"required": True, "reason": "external delivery"}
    validate_json(valid, schema, label="job")
