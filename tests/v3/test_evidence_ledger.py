from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone

import pytest

from magi_v3.evidence_ledger import (
    ACTIVE_POINTER_SCHEMA,
    EVIDENCE_ENVELOPE_SCHEMA,
    EvidenceEnvelope,
    EvidenceLedger,
    EvidenceLedgerError,
    envelope_health_view,
)


NOW = datetime(2026, 8, 29, 4, 0, tzinfo=timezone.utc)


def _envelope(
    release_id: str,
    outcome: str = "failed",
    *,
    status_class: str = "live_health",
    generated_at: datetime = NOW,
) -> EvidenceEnvelope:
    return EvidenceEnvelope.create(
        release_id=release_id,
        source_commit="a" * 40,
        producer={"name": "commercial-readiness", "version": "2"},
        validator={"name": "function-health", "version": "2"},
        generated_at=generated_at,
        expires_at=generated_at + timedelta(hours=2),
        subject="commercial_readiness_live",
        status_class=status_class,
        outcome=outcome,
        reason_code="checks_failed" if outcome == "failed" else "verified",
        trace_id="b" * 32,
        receipt={"ok": outcome != "failed", "checks": 11, "failures": int(outcome == "failed")},
    )


def test_predecessor_failure_remains_history_but_not_active_latest(tmp_path) -> None:
    ledger = EvidenceLedger(tmp_path / "evidence.sqlite3")
    ledger.initialize()
    old = _envelope(
        "v3-20260828-rc643-r37",
        generated_at=NOW - timedelta(hours=1),
    )
    current = _envelope("v3-20260829-rc643-r59", "passed")
    ledger.append(old)
    ledger.append(current)
    pointer = ledger.bind_active_release(
        release_id="v3-20260829-rc643-r59",
        source_commit="a" * 40,
        marker_sha256="c" * 64,
        observed_at=NOW,
    )

    assert pointer["schema"] == ACTIVE_POINTER_SCHEMA
    assert ledger.latest("commercial_readiness_live")["evidence_id"] == current.evidence_id
    assert [row["evidence_id"] for row in ledger.history("commercial_readiness_live")] == [
        current.evidence_id,
        old.evidence_id,
    ]


def test_legacy_latest_is_projection_of_active_release_only(tmp_path) -> None:
    ledger = EvidenceLedger(tmp_path / "evidence.sqlite3")
    ledger.initialize()
    ledger.append(_envelope("v3-old"))
    current = _envelope("v3-current", "passed")
    ledger.append(current)
    ledger.bind_active_release(
        release_id="v3-current",
        source_commit="a" * 40,
        marker_sha256="c" * 64,
        observed_at=NOW,
    )
    target = tmp_path / "commercial_readiness_live_latest.json"

    projected = ledger.project_legacy_latest("commercial_readiness_live", target)

    assert projected["ok"] is True
    assert projected["release_id"] == "v3-current"
    assert projected["evidence_id"] == current.evidence_id
    assert json.loads(target.read_text(encoding="utf-8")) == projected


def test_business_attention_never_becomes_system_failure() -> None:
    envelope = _envelope("v3-current", "attention", status_class="business_backlog")
    view = envelope_health_view(envelope.as_dict(), active_release_id="v3-current", now=NOW)
    assert view["status"] == "observed"
    assert view["ok"] is True
    assert view["status_class"] == "business_backlog"


def test_old_release_failure_is_superseded_and_current_failure_is_failed() -> None:
    old = envelope_health_view(_envelope("v3-old").as_dict(), active_release_id="v3-current", now=NOW)
    current = envelope_health_view(_envelope("v3-current").as_dict(), active_release_id="v3-current", now=NOW)
    assert old["status"] == "superseded" and old["ok"] is True
    assert current["status"] == "failed" and current["ok"] is False


def test_envelope_rejects_tampering_and_sensitive_receipt_fields() -> None:
    value = _envelope("v3-current", "passed").as_dict()
    value["receipt"]["ok"] = False
    with pytest.raises(EvidenceLedgerError, match="SHA-256|evidence_id"):
        EvidenceEnvelope.from_mapping(value)

    with pytest.raises(EvidenceLedgerError, match="forbidden field"):
        EvidenceEnvelope.create(
            release_id="v3-current",
            source_commit="a" * 40,
            producer={"name": "p", "version": "1"},
            validator={"name": "v", "version": "1"},
            generated_at=NOW,
            subject="health",
            status_class="live_health",
            outcome="passed",
            reason_code="verified",
            trace_id="b" * 32,
            receipt={"case_content": "must-not-enter-ledger"},
        ).as_dict()


def test_duplicate_append_is_idempotent(tmp_path) -> None:
    ledger = EvidenceLedger(tmp_path / "evidence.sqlite3")
    ledger.initialize()
    value = _envelope("v3-current", "passed")
    assert ledger.append(value) == ledger.append(value)
    assert len(ledger.history("commercial_readiness_live")) == 1


def test_projection_refuses_symlink(tmp_path) -> None:
    ledger = EvidenceLedger(tmp_path / "evidence.sqlite3")
    ledger.initialize()
    ledger.append(_envelope("v3-current", "passed"))
    ledger.bind_active_release(
        release_id="v3-current",
        source_commit="a" * 40,
        marker_sha256=hashlib.sha256(b"marker").hexdigest(),
        observed_at=NOW,
    )
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    link = tmp_path / "latest.json"
    link.symlink_to(outside)
    with pytest.raises(EvidenceLedgerError, match="symlink"):
        ledger.project_legacy_latest("commercial_readiness_live", link)


def test_envelope_schema_is_explicit() -> None:
    assert _envelope("v3-current", "passed").as_dict()["schema"] == EVIDENCE_ENVELOPE_SCHEMA


def test_active_pointer_is_derived_from_hash_bound_deployment_marker(tmp_path) -> None:
    release = tmp_path / "releases" / "v3-current"
    release.mkdir(parents=True)
    manifest = release / "release-manifest.json"
    manifest.write_text(
        json.dumps({"release_id": "v3-current", "commit": "a" * 40}),
        encoding="utf-8",
    )
    marker = tmp_path / "active-release.json"
    marker.write_text(
        json.dumps(
            {
                "schema": "magi.v3.active-release/v1",
                "release_id": "v3-current",
                "release_root": str(release),
                "release_manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
            }
        ),
        encoding="utf-8",
    )
    ledger = EvidenceLedger(tmp_path / "evidence.sqlite3")
    ledger.initialize()
    pointer = ledger.bind_active_marker(marker, observed_at=NOW)
    assert pointer["release_id"] == "v3-current"
    assert pointer["source_commit"] == "a" * 40

    tampered = json.loads(marker.read_text(encoding="utf-8"))
    tampered["release_manifest_sha256"] = "0" * 64
    marker.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(EvidenceLedgerError, match="identity mismatch"):
        ledger.bind_active_marker(marker, observed_at=NOW)
