from __future__ import annotations

import json

import pytest

from magi_v3 import controlled_evolution as ce
from magi_v3.quality_ledger import QualityOutcomeLedger, attest_release, canonical_quality_signal


def _attestation():
    return attest_release(
        release_id="rc559-test",
        commit_sha="a" * 40,
        manifest_bytes=b'{"release_id":"rc559-test","schema":1}',
    )


def _signal(**overrides):
    item = {
        "kind": "calendar_import_source_gap", "owner": "calendar.reconciler",
        "evidence_hash": "b" * 64, "actionability": "human_review",
        "state": "waiting_human", "retry_at": None, "deadline_at": "2026-08-16T00:00:00Z",
        "human_required": True,
    }
    item.update(overrides)
    return item


@pytest.mark.parametrize("kind", ["transcript_retry_pending", "calendar_import_source_gap", "pdf_bookmark_backlog", "judgment_quality_backlog"])
def test_all_business_quality_kinds_are_canonical_and_deidentified(kind):
    result = canonical_quality_signal(_signal(kind=kind))
    assert result["kind"] == kind
    assert result["outcome_id"].startswith("qo-")
    assert set(result) == {"kind", "owner", "evidence_hash", "actionability", "state", "retry_at", "deadline_at", "human_required", "outcome_id"}


def test_unknown_or_raw_data_is_rejected_and_never_persisted(tmp_path):
    ledger = QualityOutcomeLedger(tmp_path / "quality.sqlite3")
    private_prefix = "/" + "Users/"
    with pytest.raises(ValueError):
        ledger.upsert(
            {**_signal(), "evidence": private_prefix + "person/case.pdf 王小明"},
            attestation=_attestation(),
        )
    saved = ledger.upsert(_signal(), attestation=_attestation())
    raw = (tmp_path / "quality.sqlite3").read_bytes()
    assert saved["attestation"]["auto_deploy"] is False
    assert private_prefix.encode() not in raw and "王小明" not in raw.decode("utf-8", "ignore")


def test_release_attestation_is_bound_and_never_deploy_authority():
    attestation = _attestation()
    assert attestation["manifest_sha256"] != "a" * 64
    assert attestation["auto_deploy"] is False and attestation["human_required"] is True
    with pytest.raises(ValueError):
        attest_release(release_id="bad/path", commit_sha="a" * 40, manifest_bytes=b"x")
    with pytest.raises(ValueError):
        attest_release(
            release_id="rc559-test",
            commit_sha="a" * 40,
            manifest_bytes=b'{"release_id":"different"}',
        )


def test_ledger_rejects_a_forged_release_attestation(tmp_path):
    ledger = QualityOutcomeLedger(tmp_path / "quality.sqlite3")
    forged = dict(_attestation())
    forged["release_id"] = "other-release"
    with pytest.raises(ValueError):
        ledger.upsert(_signal(), attestation=forged)


def test_quality_outcome_feeds_controlled_evolution_without_raw_evidence(tmp_path):
    store = ce.EvolutionStore(tmp_path / "evolution.sqlite3")
    proposals = ce.ingest_quality_outcomes([_signal()], root=tmp_path, release_id="rc559-test", store=store)
    assert len(proposals) == 1
    serialized = json.dumps(proposals[0])
    assert proposals[0]["component"] == "calendar_todos"
    assert "calendar.reconciler" not in serialized and "b" * 64 not in serialized
    assert proposals[0]["auto_deploy"] is False


def test_auto_retry_requires_retry_and_human_review_requires_human():
    with pytest.raises(ValueError):
        canonical_quality_signal(_signal(actionability="auto_retry", human_required=False, retry_at=None))
    with pytest.raises(ValueError):
        canonical_quality_signal(_signal(human_required=False))
