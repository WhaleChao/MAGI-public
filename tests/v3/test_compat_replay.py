from __future__ import annotations

import base64
import copy
import hashlib
import json
from pathlib import Path

import pytest

from scripts.v3_validation.adapter_spec import LegacyResponse, adapt_legacy_response
from scripts.v3_validation.fixtures import (
    anonymize_fixture,
    decode_fixture_file_payload,
    load_replay_fixture,
    validate_replay_fixture,
)
from scripts.v3_validation.golden_flows import run_osc_file_golden_flow
from scripts.v3_validation.schema import ContractValidationError, load_json


FIXTURE_DIR = Path(__file__).parent / "compat" / "fixtures"
BEHAVIOR_FIXTURE_DIR = Path(__file__).parent / "compat" / "behavior_fixtures"


def test_osc_preview_range_download_golden_flow_has_bound_end_to_end_outcomes(
    tmp_path: Path,
) -> None:
    evidence = run_osc_file_golden_flow(
        BEHAVIOR_FIXTURE_DIR / "osc-file-content.json",
        tmp_path / "flow-sandbox",
    )

    assert evidence["passed"] is True
    assert evidence["transport"] == "in_process_wsgi_v3_compat"
    assert evidence["inventory_counts"] == {"5002": 280, "5003": 67, "total": 347}
    assert evidence["expected_outcomes_sha256"] == evidence["observed_outcomes_sha256"]
    assert evidence["outcomes"]["anonymous_preview_status"] == 302
    assert evidence["outcomes"]["preview_status"] == 200
    assert evidence["outcomes"]["range_status"] == 206
    assert evidence["outcomes"]["full_download_status"] == 200
    assert evidence["outcomes"]["missing_status"] == 404
    assert evidence["outcomes"]["forbidden_status"] == 403
    assert evidence["network_access_performed"] is False
    assert evidence["external_writes_performed"] is False
    assert evidence["sandbox_writes_only"] is True
    assert evidence["staged_files_remaining"] == 0
    assert evidence["production_state_accessed"] is False
    assert evidence["service_start_performed"] is False
    assert len(evidence["fixture_sha256"]) == len(evidence["evidence_sha256"]) == 64


def test_osc_golden_flow_expected_outcome_drift_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts.v3_validation import golden_flows

    fixture_path = BEHAVIOR_FIXTURE_DIR / "osc-file-content.json"
    altered = json.loads(fixture_path.read_text(encoding="utf-8"))
    altered["golden_flow"]["expected_outcomes"]["range_status"] = 200
    original_load = golden_flows.load_json

    def changed_load(path):
        if Path(path).resolve() == fixture_path.resolve():
            return copy.deepcopy(altered)
        return original_load(path)

    monkeypatch.setattr(golden_flows, "load_json", changed_load)
    with pytest.raises(ContractValidationError, match="outcome drift"):
        run_osc_file_golden_flow(fixture_path, tmp_path / "drift-sandbox")


@pytest.mark.parametrize("path", sorted(FIXTURE_DIR.glob("*.json")), ids=lambda path: path.stem)
def test_recorded_fixtures_are_schema_valid_and_anonymized(path: Path) -> None:
    fixture = load_replay_fixture(path)
    assert fixture["replay"] == {
        "mode": "offline",
        "network_allowed": False,
        "external_writes_allowed": False,
    }


def test_anonymizer_redacts_sensitive_keys_and_inline_identifiers() -> None:
    raw = load_json(FIXTURE_DIR / "reply-success.json")
    raw["request"]["headers"]["X-API-Key"] = "sk-example-secret-value"
    raw["request"]["body"] = {
        "email": "person@example.com",
        "note": "Bearer abcdefghijklmnop from /Users/alice/private at 8.8.8.8, call 0912-345-678",
    }

    safe = anonymize_fixture(raw)
    rendered = str(safe)
    for forbidden in (
        "sk-example-secret-value",
        "person@example.com",
        "abcdefghijklmnop",
        "/Users/alice/private",
        "8.8.8.8",
        "0912-345-678",
    ):
        assert forbidden not in rendered
    validate_replay_fixture(safe)


@pytest.mark.parametrize(
    "private_value",
    [
        "王小明",
        "A123456789",
        "臺北市中正區某路1號",
        "112年度訴字第12345號",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.signature123",
        "123456789012345678",
        "https://drive.google.com/file/d/private-drive-id/view",
        "1AbCdEfGhIjKlMnOpQrStUvWxYz12345",
    ],
)
def test_fixture_rejects_unredacted_taiwan_pii_and_channel_identifiers(private_value: str) -> None:
    fixture = load_json(FIXTURE_DIR / "reply-success.json")
    fixture["request"]["body"] = {"note": private_value}
    with pytest.raises(ContractValidationError, match="not safe for offline replay"):
        validate_replay_fixture(fixture)


def test_fixture_rejects_sensitive_identity_keys_by_default() -> None:
    fixture = load_json(FIXTURE_DIR / "reply-success.json")
    fixture["request"]["body"] = {"clientName": "raw-client", "drive_id": "raw-drive-id"}
    with pytest.raises(ContractValidationError, match="sensitive field"):
        validate_replay_fixture(fixture)

    safe = anonymize_fixture(fixture)
    assert "raw-client" not in str(safe)
    assert "raw-drive-id" not in str(safe)
    validate_replay_fixture(safe)


def test_fixture_rejects_raw_sensitive_header_and_file_content() -> None:
    fixture = load_json(FIXTURE_DIR / "shortcut-error.json")
    fixture["request"]["headers"]["X-API-Key"] = "raw-credential"
    with pytest.raises(ContractValidationError, match="sensitive field"):
        validate_replay_fixture(fixture)

    fixture = load_json(FIXTURE_DIR / "shortcut-error.json")
    fixture["request"]["files"][0]["content"] = "raw bytes"
    with pytest.raises(ContractValidationError):
        validate_replay_fixture(fixture)


def test_fixture_embeds_small_verified_non_pii_multipart_payload() -> None:
    fixture = load_replay_fixture(FIXTURE_DIR / "shortcut-error.json")
    file_row = fixture["request"]["files"][0]
    payload = decode_fixture_file_payload(file_row)

    assert payload == b"%PDF-test-fixture\n"
    assert len(payload) == file_row["size_bytes"] == 18
    assert hashlib.sha256(payload).hexdigest() == file_row["sha256"]
    assert file_row["safe_test_payload"] is True
    assert fixture["redaction"]["raw_payload_retained"] is True
    assert fixture["redaction"]["test_payload_only"] is True


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("size_bytes", 17, "size_bytes"),
        ("sha256", "0" * 64, "SHA-256"),
    ],
)
def test_fixture_rejects_multipart_size_or_hash_drift(field: str, value: object, message: str) -> None:
    fixture = load_json(FIXTURE_DIR / "shortcut-error.json")
    fixture["request"]["files"][0][field] = value
    with pytest.raises(ContractValidationError, match=message):
        validate_replay_fixture(fixture)


def test_fixture_rejects_pii_inside_embedded_test_payload() -> None:
    fixture = load_json(FIXTURE_DIR / "shortcut-error.json")
    payload = b"person@example.com"
    file_row = fixture["request"]["files"][0]
    file_row["content_base64"] = base64.b64encode(payload).decode("ascii")
    file_row["size_bytes"] = len(payload)
    file_row["sha256"] = hashlib.sha256(payload).hexdigest()
    with pytest.raises(ContractValidationError, match="contains raw email"):
        validate_replay_fixture(fixture)


def test_fixture_rejects_false_embedded_payload_retention_claim() -> None:
    fixture = load_json(FIXTURE_DIR / "shortcut-error.json")
    fixture["redaction"]["raw_payload_retained"] = False
    with pytest.raises(ContractValidationError, match="raw_payload_retained"):
        validate_replay_fixture(fixture)


def test_duplicate_header_rows_do_not_bypass_sensitive_header_redaction() -> None:
    fixture = load_json(FIXTURE_DIR / "reply-duplicate-headers.json")
    fixture["response"]["headers"][1]["value"] = "raw-session-cookie"
    with pytest.raises(ContractValidationError, match="sensitive header value"):
        validate_replay_fixture(fixture)

    safe = anonymize_fixture(fixture)
    assert "raw-session-cookie" not in str(safe)
    validate_replay_fixture(safe)


def test_fixture_rejects_unpinned_route_or_side_effect_claim() -> None:
    fixture = load_json(FIXTURE_DIR / "reply-success.json")
    fixture["route"]["endpoint"] = "invented.endpoint"
    with pytest.raises(ContractValidationError, match="pinned 347-route"):
        validate_replay_fixture(fixture)

    fixture = load_json(FIXTURE_DIR / "reply-success.json")
    fixture["side_effect_class"] = "read_only"
    with pytest.raises(ContractValidationError, match="side_effect_class"):
        validate_replay_fixture(fixture)


def test_fixture_rejects_request_path_that_does_not_match_pinned_rule() -> None:
    fixture = load_json(FIXTURE_DIR / "reply-success.json")
    fixture["request"]["path"] = "/totally/unrelated"
    with pytest.raises(ContractValidationError, match="request path"):
        validate_replay_fixture(fixture)


def test_fixture_rejects_declared_legacy_shape_that_does_not_match_response() -> None:
    fixture = load_json(FIXTURE_DIR / "reply-success.json")
    fixture["expected_legacy_shape"] = "json_ok"
    with pytest.raises(ContractValidationError, match="legacy response shape mismatch"):
        validate_replay_fixture(fixture)

@pytest.mark.parametrize(
    ("shape", "body"),
    [
        ("json_ok", {"ok": True, "value": 1}),
        ("json_success", {"success": True, "value": 1}),
        ("reply_json", {"reply": "done"}),
        ("json_bare", {"value": 1}),
    ],
)
def test_json_legacy_shapes_adapt_to_official_envelope(shape: str, body: object) -> None:
    result = adapt_legacy_response(
        LegacyResponse(200, "application/json", body),
        request_id="req-fixture",
        expected_shape=shape,
    )
    assert result["ok"] is True
    assert result["data"] == body
    assert result["error"] is None
    assert result["meta"]["compat_version"] == "v2"


def test_plaintext_error_preserves_legacy_status_and_has_canonical_error() -> None:
    result = adapt_legacy_response(
        LegacyResponse(200, "text/plain", "[error] unavailable"),
        request_id="req-text",
        expected_shape="text_plain",
    )
    assert result["ok"] is False
    assert result["data"] == {"text": "[error] unavailable"}
    assert result["error"]["message"] == "[error] unavailable"
    assert result["meta"]["legacy_status"] == 200


def test_sse_shape_preserves_events_and_done_marker() -> None:
    fixture = load_replay_fixture(FIXTURE_DIR / "external-sse.json")
    response = fixture["response"]
    result = adapt_legacy_response(
        LegacyResponse(response["status"], response["content_type"], response["body"]),
        request_id="req-sse",
        expected_shape="sse",
    )
    assert result["data"] == {"events": [{"delta": "pong"}], "done": True}


def test_declared_false_result_is_failure_even_with_http_200() -> None:
    body = {"ok": False, "error": "dependency_unavailable", "degraded": True}
    result = adapt_legacy_response(
        LegacyResponse(200, "application/json", body),
        request_id="req-degraded",
        expected_shape="json_ok",
    )
    assert result["ok"] is False
    assert result["meta"]["degraded"] is True
    assert result["error"]["code"] == "dependency_unavailable"


@pytest.mark.parametrize("status", [302, 503])
def test_http_redirect_or_error_cannot_be_overridden_by_body_ok(status: int) -> None:
    result = adapt_legacy_response(
        LegacyResponse(status, "application/json", {"ok": True}),
        request_id="req-http-failure",
        expected_shape="json_ok",
    )
    assert result["ok"] is False
    assert result["error"] is not None


def test_json_legacy_shape_requires_json_content_type() -> None:
    with pytest.raises(ValueError, match="json_ok"):
        adapt_legacy_response(
            LegacyResponse(200, "text/html", {"ok": True}),
            request_id="req-wrong-content-type",
            expected_shape="json_ok",
        )


def test_wrong_declared_legacy_shape_is_rejected() -> None:
    with pytest.raises(ValueError, match="reply_json"):
        adapt_legacy_response(
            LegacyResponse(200, "application/json", {"ok": True}),
            request_id="req-wrong-shape",
            expected_shape="reply_json",
        )
