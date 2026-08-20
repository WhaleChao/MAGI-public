import json

import pytest

from magi_v3.business_outcome_eval import anonymise_finding, evaluate_outcome_slo, merge_manual_finding, write_manual_finding


def _raw(**overrides):
    row = {"domain": "file_review", "source_category": "manual_audit", "expected_outcome": "quarantined", "failure_mode": "cross_case_mismatch", "detail": "wrong PDF bound to another case", "source_reference": "operator-note-001"}
    row.update(overrides)
    return row


def test_manual_finding_is_deidentified_deduplicated_and_test_root_only(tmp_path):
    corpus = {"schema": "magi.business-outcome-regression/v1", "cases": []}
    merged, added = merge_manual_finding(corpus, _raw())
    repeated, repeat_added = merge_manual_finding(merged, _raw())
    assert added and not repeat_added and len(repeated["cases"]) == 1
    assert "wrong PDF" not in json.dumps(repeated) and "operator-note" not in json.dumps(repeated)
    assert "detail_hash" not in json.dumps(repeated)
    assert "source_reference_hash" not in json.dumps(repeated)
    path = tmp_path / "evals" / "corpus.json"; path.parent.mkdir(); path.write_text(json.dumps(corpus), encoding="utf-8")
    _, written = write_manual_finding(path, _raw(), test_root=tmp_path)
    assert written
    with pytest.raises(ValueError):
        write_manual_finding(tmp_path / "outside.json", _raw(), test_root=tmp_path / "evals")


def test_manual_finding_rejects_pii_before_persistence():
    with pytest.raises(ValueError):
        anonymise_finding(_raw(failure_mode="漏通知給王小明 A123456789"))
    entry = anonymise_finding(_raw(detail="王小明 A123456789 未收到通知"))
    assert "王小明" not in json.dumps(entry)
    assert "A123456789" not in json.dumps(entry)
    with pytest.raises(ValueError):
        anonymise_finding(_raw(failure_mode="missed_王小明"))


def test_outcome_slo_requires_receipts_and_handles_cross_case_notification_and_defer():
    report = evaluate_outcome_slo([
        {"id": "cross-case", "domain": "file_review", "expected_outcome": "quarantined", "observed_outcome": "quarantined", "receipts": [{"kind": "file_receipt", "receipt_id": "r1"}]},
        {"id": "missed-notice", "domain": "dispatch", "expected_outcome": "notified", "observed_outcome": "notified", "receipts": []},
        {"id": "duplicate", "domain": "payment", "expected_outcome": "deduplicated", "observed_outcome": "deduplicated", "receipts": [{"kind": "payment_receipt", "receipt_id": "r2"}]},
        {"id": "defer", "domain": "deadline", "expected_outcome": "deferred", "observed_outcome": "deferred", "receipts": []},
        {"id": "archive", "domain": "archive", "expected_outcome": "archived", "observed_outcome": "archived", "receipts": [{"kind": "archive_receipt", "receipt_id": "r3"}]},
    ])
    assert report["failed"] == 1
    assert report["results"][0]["ok"] is True
    assert report["results"][1]["receipt_ok"] is False
    assert report["results"][3]["ok"] is True
