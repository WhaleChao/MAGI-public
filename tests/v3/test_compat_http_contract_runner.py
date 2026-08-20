from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from flask import Flask, Response, jsonify, request

import scripts.v3_validation.http_contract_runner as http_contract_runner
from scripts.v3_validation.adapter_spec import LegacyResponse
from scripts.v3_validation.http_contract_runner import (
    InjectedTestClient,
    OfflineIsolationAttestation,
    fixture_sha256,
    run_http_contract,
    verify_contract_evidence,
)
from scripts.v3_validation.inventory import EXPECTED_FINGERPRINT, load_and_validate_runtime_inventory
from scripts.v3_validation.route_reviews import load_route_method_reviews
from scripts.v3_validation.schema import ContractValidationError, load_json
from scripts.v3_validation.side_effects import SIDE_EFFECT_CLASSES


FIXTURE_DIR = Path(__file__).parent / "compat" / "fixtures"
OFFLINE = OfflineIsolationAttestation()


def _write_fixture(tmp_path: Path, fixture: dict[str, object], name: str = "fixture.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(fixture, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def _legacy_response(path: Path, **changes: object) -> LegacyResponse:
    response = copy.deepcopy(load_json(path)["response"])
    response.update(changes)
    return LegacyResponse(
        response["status"],
        response["content_type"],
        response["body"],
        response["headers"],
    )


def test_wsgi_test_client_executes_reviewed_json_contract_and_emits_bound_evidence() -> None:
    fixture_path = FIXTURE_DIR / "reply-success.json"
    fixture = load_json(fixture_path)
    calls: list[dict[str, object]] = []
    app = Flask(__name__)

    @app.post("/api/osc/chat")
    def osc_chat() -> tuple[dict[str, object], int, dict[str, str]]:
        calls.append(
            {
                "query": request.args.to_dict(),
                "headers": dict(request.headers),
                "body": request.get_json(),
            }
        )
        response = fixture["response"]
        return response["body"], response["status"], response["headers"]

    client = InjectedTestClient.wsgi(app.test_client(), isolation=OFFLINE, name="flask-in-process")
    expected_fixture_hash = hashlib.sha256(fixture_path.read_bytes()).hexdigest()
    result = run_http_contract(
        fixture_path,
        client,
        expected_fixture_sha256=expected_fixture_hash,
    )

    assert len(calls) == 1
    assert calls[0]["query"] == fixture["request"]["query"]
    assert calls[0]["body"] == fixture["request"]["body"]
    assert calls[0]["headers"]["Authorization"] == fixture["request"]["headers"]["Authorization"]
    assert result.response.status == 200
    assert result.response.body == fixture["response"]["body"]
    assert result.canonical_envelope["data"] == fixture["response"]["body"]
    assert result.canonical_envelope["meta"]["legacy_shape"] == "reply_json"

    evidence = result.evidence
    assert evidence.inventory_fingerprint == EXPECTED_FINGERPRINT
    assert evidence.fixture_sha256 == expected_fixture_hash == fixture_sha256(fixture_path)
    assert (evidence.service, evidence.rule, evidence.method, evidence.endpoint) == (
        "5002",
        "/api/osc/chat",
        "POST",
        "web_runtime.osc_chat_api",
    )
    assert evidence.request_fidelity == "complete"
    assert evidence.expected_response_sha256 == evidence.observed_response_sha256
    assert len(evidence.evidence_sha256) == 64
    verify_contract_evidence(evidence, fixture_path)


def test_asgi_style_test_client_preserves_exact_sse_stream_and_request_contract() -> None:
    fixture_path = FIXTURE_DIR / "external-sse.json"
    fixture = load_json(fixture_path)
    calls: list[tuple[object, ...]] = []

    class AsgiStyleClient:
        def request(self, method: str, path: str, **kwargs: object) -> SimpleNamespace:
            calls.append((method, path, kwargs))
            response = fixture["response"]
            return SimpleNamespace(
                status_code=response["status"],
                headers={**response["headers"], "Content-Length": str(len(response["body"]))},
                text=response["body"],
            )

    result = run_http_contract(
        fixture_path,
        InjectedTestClient.asgi(AsgiStyleClient(), isolation=OFFLINE, name="asgi-in-process"),
    )

    assert calls == [
        (
            "POST",
            "/osc/external/chat",
            {
                "params": {"stream": "true"},
                "headers": fixture["request"]["headers"],
                "json": {"message": "ping"},
            },
        )
    ]
    assert result.response.body == "data: {\"delta\":\"pong\"}\n\ndata: [DONE]\n\n"
    assert result.canonical_envelope["data"] == {"events": [{"delta": "pong"}], "done": True}
    assert result.evidence.expected_legacy_shape == "sse"
    verify_contract_evidence(result.evidence, fixture_path)


def test_callable_receives_verified_multipart_bytes_and_preserves_plaintext_response() -> None:
    fixture_path = FIXTURE_DIR / "shortcut-error.json"
    seen_files: list[tuple[object, ...]] = []

    def execute(contract_request: object) -> LegacyResponse:
        seen_files.append(contract_request.files)
        return _legacy_response(fixture_path)

    result = run_http_contract(
        fixture_path,
        InjectedTestClient.callable(execute, isolation=OFFLINE, name="multipart-metadata-stub"),
    )

    assert seen_files[0][0].content == b"%PDF-test-fixture\n"
    assert seen_files[0][0].size_bytes == 18
    assert seen_files[0][0].sha256 == "26ba81e059f5547a5f9676a31013093fb984a3838b47528a7a6eacec09acf90c"
    assert result.response.status == 422
    assert result.response.content_type == "text/plain; charset=utf-8"
    assert result.response.body == "[error] invalid_pdf"
    assert result.canonical_envelope["ok"] is False
    assert result.evidence.request_fidelity == "complete"
    verify_contract_evidence(result.evidence, fixture_path)


def _fully_reviewed_route_methods() -> dict[object, object]:
    inventory = load_and_validate_runtime_inventory()
    reviews = load_route_method_reviews(expected_inventory_fingerprint=inventory["fingerprint"])
    assert inventory["review_summary"]["route_method_total"] == 431
    assert inventory["review_summary"]["unreviewed_route_methods"] == 0
    assert len(reviews) == 431
    assert all(review.reviewed for review in reviews.values())
    return reviews


def test_unknown_route_method_is_rejected_before_client_invocation(tmp_path: Path) -> None:
    fixture = load_json(FIXTURE_DIR / "reply-success.json")
    fixture["route"]["endpoint"] = "invented.endpoint"
    fixture_path = _write_fixture(tmp_path, fixture)
    calls = 0

    def execute(_: object) -> LegacyResponse:
        nonlocal calls
        calls += 1
        return _legacy_response(FIXTURE_DIR / "reply-success.json")

    with pytest.raises(ContractValidationError, match="pinned 347-route inventory"):
        run_http_contract(
            fixture_path,
            InjectedTestClient.callable(execute, isolation=OFFLINE),
            allowed_fixture_roots=(tmp_path,),
        )
    assert calls == 0


def test_reviewed_side_effect_mismatch_is_rejected_before_client_invocation(tmp_path: Path) -> None:
    reviews = _fully_reviewed_route_methods()
    fixture = load_json(FIXTURE_DIR / "reply-success.json")
    route = fixture["route"]
    key = next(
        key
        for key in reviews
        if (key.service, key.rule, key.method, key.endpoint)
        == (route["service"], route["rule"], fixture["request"]["method"], route["endpoint"])
    )
    required_effect = reviews[key].side_effect_class
    fixture["side_effect_class"] = next(effect for effect in SIDE_EFFECT_CLASSES if effect != required_effect)
    fixture_path = _write_fixture(tmp_path, fixture)
    calls = 0

    def execute(_: object) -> LegacyResponse:
        nonlocal calls
        calls += 1
        return _legacy_response(FIXTURE_DIR / "reply-success.json")

    with pytest.raises(ContractValidationError, match="fixture side_effect_class must be"):
        run_http_contract(
            fixture_path,
            InjectedTestClient.callable(execute, isolation=OFFLINE),
            allowed_fixture_roots=(tmp_path,),
        )
    assert calls == 0


def test_reviewed_preflight_precedes_multipart_schema_and_payload_decoding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fully_reviewed_route_methods()
    fixture = load_json(FIXTURE_DIR / "shortcut-error.json")
    fixture["request"]["files"][0]["content_base64"] = "not-base64"
    fixture_path = _write_fixture(tmp_path, fixture)
    calls = 0
    preflights = 0
    original_preflight = http_contract_runner._preflight_reviewed_route_method

    def track_preflight(fixture: object, inventory: object) -> object:
        nonlocal preflights
        preflights += 1
        return original_preflight(fixture, inventory)

    monkeypatch.setattr(http_contract_runner, "_preflight_reviewed_route_method", track_preflight)

    def execute(_: object) -> LegacyResponse:
        nonlocal calls
        calls += 1
        return _legacy_response(FIXTURE_DIR / "shortcut-error.json")

    with pytest.raises(ContractValidationError, match="failed schema validation"):
        run_http_contract(
            fixture_path,
            InjectedTestClient.callable(execute, isolation=OFFLINE),
            allowed_fixture_roots=(tmp_path,),
        )
    assert preflights == 1
    assert calls == 0


def test_fixture_hash_and_isolation_gates_run_before_client_invocation() -> None:
    fixture_path = FIXTURE_DIR / "reply-success.json"
    calls = 0

    def execute(_: object) -> LegacyResponse:
        nonlocal calls
        calls += 1
        return _legacy_response(fixture_path)

    safe_client = InjectedTestClient.callable(execute, isolation=OFFLINE)
    with pytest.raises(ContractValidationError, match="fixture SHA-256 mismatch"):
        run_http_contract(fixture_path, safe_client, expected_fixture_sha256="0" * 64)
    assert calls == 0

    unsafe = InjectedTestClient.callable(
        execute,
        isolation=OfflineIsolationAttestation(network_allowed=True),
    )
    with pytest.raises(ContractValidationError, match="strict offline isolation"):
        run_http_contract(fixture_path, unsafe)
    assert calls == 0


def test_path_allowlist_rejects_before_fixture_bytes_are_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fixture_path = _write_fixture(tmp_path, load_json(FIXTURE_DIR / "reply-success.json"))
    reads: list[Path] = []
    original_read_bytes = Path.read_bytes

    def track_read_bytes(path: Path) -> bytes:
        reads.append(path)
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", track_read_bytes)
    client = InjectedTestClient.callable(lambda _: _legacy_response(fixture_path), isolation=OFFLINE)
    with pytest.raises(ContractValidationError, match="outside the allowlisted roots"):
        run_http_contract(fixture_path, client)
    assert reads == []


def test_path_allowlist_resolves_symlink_before_reading_fixture_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    allowed_root = tmp_path / "allowed"
    outside_root = tmp_path / "outside"
    allowed_root.mkdir()
    outside_root.mkdir()
    outside_fixture = _write_fixture(outside_root, load_json(FIXTURE_DIR / "reply-success.json"))
    linked_fixture = allowed_root / "linked.json"
    linked_fixture.symlink_to(outside_fixture)
    reads: list[Path] = []
    original_read_bytes = Path.read_bytes

    def track_read_bytes(path: Path) -> bytes:
        reads.append(path)
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", track_read_bytes)
    client = InjectedTestClient.callable(lambda _: _legacy_response(outside_fixture), isolation=OFFLINE)
    with pytest.raises(ContractValidationError, match="outside the allowlisted roots"):
        run_http_contract(linked_fixture, client, allowed_fixture_roots=(allowed_root,))
    assert reads == []


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"status": 201}, "status mismatch"),
        ({"headers": {"Content-Type": "application/problem+json"}}, "header mismatch"),
        ({"body": {"reply": "changed", "degraded": False}}, "body mismatch"),
    ],
)
def test_status_header_and_body_drift_fail_without_passing_evidence(
    change: dict[str, object],
    message: str,
) -> None:
    fixture_path = FIXTURE_DIR / "reply-success.json"
    client = InjectedTestClient.callable(
        lambda _: _legacy_response(fixture_path, **change),
        isolation=OFFLINE,
    )
    with pytest.raises(ContractValidationError, match=message):
        run_http_contract(fixture_path, client)


def test_sse_requires_exact_legacy_stream_including_done_marker() -> None:
    fixture_path = FIXTURE_DIR / "external-sse.json"
    changed_stream = "data: {\"delta\":\"pong\"}\n\n"
    client = InjectedTestClient.callable(
        lambda _: _legacy_response(fixture_path, body=changed_stream),
        isolation=OFFLINE,
    )
    with pytest.raises(ContractValidationError, match="body mismatch"):
        run_http_contract(fixture_path, client)


def test_declared_header_must_exist_even_if_response_exposes_content_type_property() -> None:
    fixture_path = FIXTURE_DIR / "reply-success.json"
    raw_response = SimpleNamespace(
        status_code=200,
        headers={},
        content_type="application/json",
        json=load_json(fixture_path)["response"]["body"],
    )
    client = InjectedTestClient.callable(lambda _: raw_response, isolation=OFFLINE)
    with pytest.raises(ContractValidationError, match="missing declared header"):
        run_http_contract(fixture_path, client)


def test_wsgi_adapter_sends_verified_multipart_bytes_without_a_socket() -> None:
    fixture_path = FIXTURE_DIR / "shortcut-error.json"
    fixture = load_json(fixture_path)
    received: list[dict[str, object]] = []
    app = Flask(__name__)

    @app.post("/shortcut/pdf_text")
    def shortcut_pdf_text() -> Response:
        uploaded = request.files["file"]
        content = uploaded.read()
        received.append(
            {
                "content": content,
                "sha256": hashlib.sha256(content).hexdigest(),
                "filename": uploaded.filename,
                "content_type": uploaded.content_type,
                "request_content_type": request.mimetype,
            }
        )
        response = fixture["response"]
        return Response(response["body"], status=response["status"], headers=response["headers"])

    result = run_http_contract(
        fixture_path,
        InjectedTestClient.wsgi(app.test_client(), isolation=OFFLINE, name="flask-multipart-in-process"),
    )

    assert received == [
        {
            "content": b"%PDF-test-fixture\n",
            "sha256": "26ba81e059f5547a5f9676a31013093fb984a3838b47528a7a6eacec09acf90c",
            "filename": "<redacted:filename:0f6f01edb879>",
            "content_type": "application/pdf",
            "request_content_type": "multipart/form-data",
        }
    ]
    assert result.response.status == 422
    assert result.evidence.request_fidelity == "complete"


def test_asgi_adapter_passes_verified_multipart_bytes_to_in_process_client() -> None:
    fixture_path = FIXTURE_DIR / "shortcut-error.json"
    calls: list[tuple[object, ...]] = []

    class AsgiStyleClient:
        def request(self, method: str, path: str, **kwargs: object) -> LegacyResponse:
            calls.append((method, path, kwargs))
            return _legacy_response(fixture_path)

    result = run_http_contract(
        fixture_path,
        InjectedTestClient.asgi(AsgiStyleClient(), isolation=OFFLINE, name="asgi-multipart-in-process"),
    )

    method, path, kwargs = calls[0]
    assert (method, path) == ("POST", "/shortcut/pdf_text")
    assert kwargs["data"] == {}
    assert "Content-Type" not in kwargs["headers"]
    field, (filename, content, content_type) = kwargs["files"][0]
    assert field == "file"
    assert filename == "<redacted:filename:0f6f01edb879>"
    assert content == b"%PDF-test-fixture\n"
    assert content_type == "application/pdf"
    assert result.evidence.request_fidelity == "complete"


def test_wsgi_response_preserves_duplicate_set_cookie_headers_in_order() -> None:
    fixture_path = FIXTURE_DIR / "reply-duplicate-headers.json"
    fixture = load_json(fixture_path)
    app = Flask(__name__)

    @app.post("/api/osc/chat")
    def osc_chat_duplicate_headers() -> Response:
        response = jsonify(fixture["response"]["body"])
        response.headers["Content-Type"] = "application/json"
        response.headers.add("Set-Cookie", "<redacted:set-cookie:111111111111>")
        response.headers.add("Set-Cookie", "<redacted:set-cookie:222222222222>")
        return response

    result = run_http_contract(
        fixture_path,
        InjectedTestClient.wsgi(app.test_client(), isolation=OFFLINE, name="flask-duplicate-headers"),
    )

    assert result.response.headers["set-cookie"] == (
        "<redacted:set-cookie:111111111111>",
        "<redacted:set-cookie:222222222222>",
    )
    assert result.evidence.expected_response_sha256 == result.evidence.observed_response_sha256
    verify_contract_evidence(result.evidence, fixture_path)


def test_asgi_style_multi_items_preserve_duplicate_response_headers() -> None:
    fixture_path = FIXTURE_DIR / "reply-duplicate-headers.json"
    fixture = load_json(fixture_path)

    class MultiHeaders:
        def multi_items(self) -> list[tuple[str, str]]:
            return [
                ("Content-Type", "application/json"),
                ("Set-Cookie", "<redacted:set-cookie:111111111111>"),
                ("Set-Cookie", "<redacted:set-cookie:222222222222>"),
            ]

        def items(self) -> list[tuple[str, str]]:
            return [("Set-Cookie", "incorrectly-combined")]

    class AsgiStyleClient:
        def request(self, *_: object, **__: object) -> SimpleNamespace:
            return SimpleNamespace(
                status_code=200,
                headers=MultiHeaders(),
                json=fixture["response"]["body"],
            )

    result = run_http_contract(
        fixture_path,
        InjectedTestClient.asgi(AsgiStyleClient(), isolation=OFFLINE, name="asgi-multi-headers"),
    )
    assert result.response.headers["set-cookie"] == (
        "<redacted:set-cookie:111111111111>",
        "<redacted:set-cookie:222222222222>",
    )


@pytest.mark.parametrize(
    "set_cookie_values",
    [
        ["<redacted:set-cookie:111111111111>"],
        ["<redacted:set-cookie:222222222222>", "<redacted:set-cookie:111111111111>"],
        ["<redacted:set-cookie:111111111111>, <redacted:set-cookie:222222222222>"],
    ],
)
def test_duplicate_set_cookie_missing_reordered_or_combined_values_are_rejected(
    set_cookie_values: list[str],
) -> None:
    fixture_path = FIXTURE_DIR / "reply-duplicate-headers.json"
    headers = [("Content-Type", "application/json"), *(('Set-Cookie', value) for value in set_cookie_values)]
    client = InjectedTestClient.callable(
        lambda _: _legacy_response(fixture_path, headers=headers),
        isolation=OFFLINE,
    )
    with pytest.raises(ContractValidationError, match="header mismatch for set-cookie"):
        run_http_contract(fixture_path, client)


def test_fixture_duplicate_content_type_is_rejected_before_client_invocation(tmp_path: Path) -> None:
    fixture = load_json(FIXTURE_DIR / "reply-duplicate-headers.json")
    fixture["response"]["headers"].append({"header": "content-type", "value": "application/json"})
    fixture_path = _write_fixture(tmp_path, fixture)
    calls = 0

    def execute(_: object) -> LegacyResponse:
        nonlocal calls
        calls += 1
        return _legacy_response(FIXTURE_DIR / "reply-duplicate-headers.json")

    with pytest.raises(ContractValidationError, match="exactly one Content-Type"):
        run_http_contract(
            fixture_path,
            InjectedTestClient.callable(execute, isolation=OFFLINE),
            allowed_fixture_roots=(tmp_path,),
        )
    assert calls == 0


def test_client_exception_cannot_emit_passing_evidence() -> None:
    fixture_path = FIXTURE_DIR / "reply-success.json"

    def fail(_: object) -> None:
        raise RuntimeError("synthetic failure")

    with pytest.raises(ContractValidationError, match="no passing evidence was emitted"):
        run_http_contract(fixture_path, InjectedTestClient.callable(fail, isolation=OFFLINE))


def test_evidence_verification_rejects_evidence_or_fixture_drift(tmp_path: Path) -> None:
    fixture_path = FIXTURE_DIR / "reply-success.json"
    result = run_http_contract(
        fixture_path,
        InjectedTestClient.callable(lambda _: _legacy_response(fixture_path), isolation=OFFLINE),
    )

    tampered_evidence = replace(result.evidence, method="GET")
    with pytest.raises(ContractValidationError, match="evidence SHA-256"):
        verify_contract_evidence(tampered_evidence, fixture_path)

    copied_path = tmp_path / "same-semantics-different-bytes.json"
    copied_path.write_bytes(fixture_path.read_bytes() + b"\n")
    with pytest.raises(ContractValidationError, match="fixture SHA-256"):
        verify_contract_evidence(result.evidence, copied_path, allowed_fixture_roots=(tmp_path,))
