from __future__ import annotations

import copy
from pathlib import Path

import pytest

from scripts.v3_validation.inventory import (
    EXPECTED_COUNTS,
    EXPECTED_FINGERPRINT,
    load_and_validate_runtime_inventory,
    validate_inventory,
)
from scripts.v3_validation.paths import ROUTE_METHOD_REVIEW_PATH, RUNTIME_ROUTES_PATH
from scripts.v3_validation.schema import load_json
from scripts.v3_validation.side_effects import SIDE_EFFECT_CLASSES
from scripts.v3_validation.source_anchor_refresh import refresh_route_review_sources


def test_runtime_inventory_and_explicit_side_effect_reviews_cover_every_method() -> None:
    result = load_and_validate_runtime_inventory()

    assert result["counts"] == EXPECTED_COUNTS == {"5002": 280, "5003": 67, "total": 347}
    assert result["fingerprint"] == EXPECTED_FINGERPRINT
    assert len(result["coverage"]) == 347
    assert result["inventory_valid"] is True
    assert result["implementation_coverage_complete"] is True
    assert result["ok"] is True
    assert all(row["suggested_capability"] for row in result["coverage"])
    summary = result["review_summary"]
    assert summary == {
        "route_inventory_total": 347,
        "route_method_total": 431,
        "reviewed_route_methods": 431,
        "unreviewed_route_methods": 0,
        "fully_reviewed_routes": 347,
        "unreviewed_routes": 0,
        "implementation_coverage_complete": True,
    }
    reviewed_effects = {
        reviewed["side_effect_class"]
        for row in result["coverage"]
        for reviewed in row["reviewed_methods"]
    }
    assert reviewed_effects <= SIDE_EFFECT_CLASSES


def test_get_oauth_callback_is_explicitly_reviewed_as_external_commit() -> None:
    result = load_and_validate_runtime_inventory()
    row = next(item for item in result["coverage"] if item["rule"] == "/api/osc/gcal/auth/callback")
    assert row["reviewed_methods"] == [
        {
            "method": "GET",
            "side_effect_class": "external_commit",
            "reviewed_by": "phase1-security-review",
        }
    ]
    # This is a side-effect review only; it is not evidence that a V3 handler
    # implements the same compatibility behavior.
    assert result["review_summary"]["implementation_coverage_complete"] is True


def test_osc_file_routes_are_explicitly_reviewed_as_sandboxed_reversible_writes() -> None:
    result = load_and_validate_runtime_inventory()
    for rule in ("/api/osc/files/preview", "/api/osc/files/content"):
        row = next(item for item in result["coverage"] if item["rule"] == rule)
        assert row["reviewed_methods"] == [
            {
                "method": "GET",
                "side_effect_class": "reversible_write",
                "reviewed_by": "osc-file-offline-contract-review",
            }
        ]


def test_service_5003_offline_review_partitions_every_pinned_route_method() -> None:
    manifest = load_json(ROUTE_METHOD_REVIEW_PATH)
    inventory = load_json(RUNTIME_ROUTES_PATH)

    inventory_keys = {
        (row["service"], row["rule"], method, row["endpoint"])
        for row in inventory["services"]["5003"]
        for method in row["methods"]
    }
    reviewed_rows = [
        row
        for row in manifest["reviews"]
        if row["service"] == "5003" and row.get("reviewed") is True
    ]
    unreviewed_rows = manifest["unreviewed"]
    reviewed_keys = {
        (row["service"], row["rule"], row["method"], row["endpoint"])
        for row in reviewed_rows
    }
    unreviewed_keys = {
        (row["service"], row["rule"], row["method"], row["endpoint"])
        for row in unreviewed_rows
    }

    assert len(inventory_keys) == 67
    assert len(reviewed_keys) == 25
    assert len(unreviewed_keys) == 42
    assert reviewed_keys.isdisjoint(unreviewed_keys)
    assert reviewed_keys | unreviewed_keys == inventory_keys
    assert all(row.get("reviewed") is False for row in unreviewed_rows)


def test_every_review_row_has_current_handler_source_identity() -> None:
    manifest = load_json(ROUTE_METHOD_REVIEW_PATH)
    repo_root = Path(__file__).resolve().parents[2]

    # Resolve every endpoint through the AST.  Line movement alone is not an
    # interface change and must not force a new release iteration.
    refreshed = refresh_route_review_sources(manifest, repo_root)

    original_rows = [*manifest["reviews"], *manifest["unreviewed"]]
    refreshed_rows = [*refreshed["reviews"], *refreshed["unreviewed"]]
    assert len(original_rows) == len(refreshed_rows)
    for row, resolved in zip(original_rows, refreshed_rows, strict=True):
        source_path, source_line = resolved["handler_source"].rsplit(":", 1)
        line_number = int(source_line)
        lines = (repo_root / source_path).read_text(encoding="utf-8").splitlines()
        endpoint_name = row["endpoint"].rsplit(".", 1)[-1]
        declared_path, _declared_line = row["handler_source"].rsplit(":", 1)

        assert row["service"] in {"5002", "5003"}
        assert row["rule"].startswith("/")
        assert row["method"] in {"GET", "POST", "PUT", "PATCH", "DELETE"}
        assert row["rationale"].strip()
        assert declared_path == source_path
        assert f"def {endpoint_name}(" in lines[line_number - 1]

    for row in manifest["reviews"]:
        assert row["reviewed"] is True
        assert row["side_effect_class"] in SIDE_EFFECT_CLASSES
        assert row["reviewed_by"].strip()


def test_inventory_change_fails_closed_even_when_declared_counts_are_adjusted() -> None:
    payload = copy.deepcopy(load_json(RUNTIME_ROUTES_PATH))
    payload["services"]["5002"][0]["endpoint"] += "_changed"

    with pytest.raises(ValueError, match="fingerprint changed"):
        validate_inventory(payload)


def test_inventory_missing_route_fails_count_gate() -> None:
    payload = copy.deepcopy(load_json(RUNTIME_ROUTES_PATH))
    payload["services"]["5003"].pop()

    with pytest.raises(ValueError, match="counts changed"):
        validate_inventory(payload)


@pytest.mark.parametrize(
    ("field", "value"),
    [("service", "5003"), ("methods", []), ("rule", "relative"), ("endpoint", "")],
)
def test_inventory_rejects_malformed_route_rows_before_fingerprinting(field: str, value: object) -> None:
    payload = copy.deepcopy(load_json(RUNTIME_ROUTES_PATH))
    payload["services"]["5002"][0][field] = value
    with pytest.raises(ValueError, match="runtime route"):
        validate_inventory(payload)
