"""PII-safe regression corpus and receipt-based business outcome evaluator.

This module deliberately has no database, network, or production-runtime
dependency.  It turns a human-discovered miss into an anonymised test corpus
entry, then evaluates fixture receipts for the five business boundaries.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Iterable


SCHEMA = "magi.business-outcome-regression/v1"
DOMAINS = {"dispatch", "deadline", "payment", "file_review", "archive"}
RECEIPT_KIND = {
    "dispatch": "assignment_receipt",
    "deadline": "deadline_receipt",
    "payment": "payment_receipt",
    "file_review": "file_receipt",
    "archive": "archive_receipt",
}
_PII = re.compile(
    r"(?:\b[A-Z][12]\d{8}\b|\b09\d{8}\b|[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}|"
    r"(?:當事人|原告|被告|客戶|姓名)\s*[:：]?\s*[\u4e00-\u9fff]{2,4})",
    re.IGNORECASE,
)
_SLUG = re.compile(r"^[a-z][a-z0-9_]{1,63}$")


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _contains_pii(value: Any) -> bool:
    if isinstance(value, str):
        return bool(_PII.search(value))
    if isinstance(value, dict):
        return any(_contains_pii(key) or _contains_pii(item) for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return any(_contains_pii(item) for item in value)
    return False


def anonymise_finding(raw: dict[str, Any]) -> dict[str, Any]:
    """Create the only corpus representation accepted from a human finding.

    Free-form details and external/case IDs are hashed, never persisted.  PII
    in structured fields fails closed rather than relying on redaction.
    """
    domain = str(raw.get("domain") or "").strip()
    source_category = str(raw.get("source_category") or "").strip()
    expected = str(raw.get("expected_outcome") or "").strip()
    failure_mode = str(raw.get("failure_mode") or "").strip()
    if domain not in DOMAINS or not source_category or not expected or not failure_mode:
        raise ValueError("domain, source_category, expected_outcome and failure_mode are required")
    if any(_SLUG.fullmatch(value) is None for value in (source_category, expected, failure_mode)):
        raise ValueError("regression fields must use anonymous lowercase slug codes")
    forbidden = {key: value for key, value in raw.items() if key not in {"detail", "source_reference"}}
    if _contains_pii(forbidden):
        raise ValueError("PII is not permitted in regression corpus fields")
    signature = {
        "domain": domain,
        "source_category": source_category,
        "expected_outcome": expected,
        "failure_mode": failure_mode,
    }
    return {
        "id": "manual-" + _hash(signature)[:20],
        "domain": domain,
        "source_category": source_category,
        "expected_outcome": expected,
        "failure_mode": failure_mode,
    }


def merge_manual_finding(corpus: dict[str, Any], raw: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Return a new corpus and whether the anonymised finding was added."""
    if corpus.get("schema") != SCHEMA:
        raise ValueError("unexpected corpus schema")
    entry = anonymise_finding(raw)
    cases = list(corpus.get("cases") or [])
    if any(str(case.get("id")) == entry["id"] for case in cases if isinstance(case, dict)):
        return {**corpus, "cases": cases}, False
    return {**corpus, "cases": cases + [entry]}, True


def write_manual_finding(corpus_path: str | Path, raw: dict[str, Any], *, test_root: str | Path) -> tuple[dict[str, Any], bool]:
    """Persist only under the explicitly supplied test/eval root."""
    target = Path(corpus_path).resolve()
    root = Path(test_root).resolve()
    if root not in target.parents:
        raise ValueError("regression corpus may only be written under the test/eval root")
    corpus = json.loads(target.read_text(encoding="utf-8"))
    merged, added = merge_manual_finding(corpus, raw)
    if added:
        target.write_text(json.dumps(merged, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return merged, added


def evaluate_outcome_slo(records: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Check expected outcome and committed receipt completeness per domain."""
    results: list[dict[str, Any]] = []
    for record in records:
        domain = str(record.get("domain") or "")
        expected = str(record.get("expected_outcome") or "")
        observed = str(record.get("observed_outcome") or "")
        receipt_kinds = {str(row.get("kind") or "") for row in (record.get("receipts") or []) if isinstance(row, dict) and row.get("receipt_id")}
        required = RECEIPT_KIND.get(domain, "")
        receipt_ok = observed == "deferred" or required in receipt_kinds
        outcome_ok = bool(domain in DOMAINS and expected and expected == observed)
        results.append({"id": str(record.get("id") or ""), "domain": domain, "outcome_ok": outcome_ok, "receipt_ok": receipt_ok, "ok": outcome_ok and receipt_ok})
    return {"schema": "magi.business-outcome-slo/v1", "sample_size": len(results), "failed": sum(not row["ok"] for row in results), "ok": all(row["ok"] for row in results), "results": results}
