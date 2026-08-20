from __future__ import annotations

import sqlite3
import stat
import time

import pytest

from magi_v3.business_events import BusinessEventLedger


def test_event_is_idempotent_and_contains_no_document_body(tmp_path):
    ledger = BusinessEventLedger(tmp_path / "events.sqlite3")
    first = ledger.emit(
        event_type="case_evidence_changed",
        domain="transcript",
        case_number="2026-0049",
        source="immutable-receipt-1",
        payload={"evidence_kind": "transcript", "body": "sensitive", "path": "/secret"},
    )
    second = ledger.emit(
        event_type="case_evidence_changed",
        domain="transcript",
        case_number="2026-0049",
        source="immutable-receipt-1",
    )
    assert first["inserted"] is True
    assert second["inserted"] is False
    assert first["event_id"] == second["event_id"]
    with sqlite3.connect(ledger.path) as conn:
        payload = conn.execute("SELECT payload_json FROM business_events").fetchone()[0]
    assert "sensitive" not in payload
    assert "/secret" not in payload
    assert stat.S_IMODE(ledger.path.stat().st_mode) == 0o600


def test_claim_complete_and_crash_lease_recovery(tmp_path):
    ledger = BusinessEventLedger(tmp_path / "events.sqlite3")
    event = ledger.emit(
        event_type="case_evidence_changed",
        domain="laf",
        case_number="2026-0049",
        source="receipt-a",
    )
    claimed = ledger.claim(limit=1, lease_seconds=30)
    assert [row["event_id"] for row in claimed] == [event["event_id"]]
    with sqlite3.connect(ledger.path) as conn:
        conn.execute(
            "UPDATE business_events SET lease_until=? WHERE event_id=?",
            (time.time() - 1, event["event_id"]),
        )
    reclaimed = ledger.claim(limit=1)
    assert reclaimed[0]["attempts"] == 2
    ledger.complete(event["event_id"], {"scanned": 1})
    assert ledger.health()["counts"]["succeeded"] == 1


def test_invalid_case_number_fails_closed(tmp_path):
    ledger = BusinessEventLedger(tmp_path / "events.sqlite3")
    with pytest.raises(ValueError):
        ledger.emit(
            event_type="case_evidence_changed",
            domain="laf",
            case_number="114年度訴字123號",
            source="receipt",
        )


def test_claim_does_not_consume_observation_events(tmp_path):
    ledger = BusinessEventLedger(tmp_path / "events.sqlite3")
    ledger.emit(event_type="cron_outcome", domain="scheduler", source="one")
    assert ledger.claim() == []
