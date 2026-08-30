from __future__ import annotations

import hashlib
import io
import json
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

from .adapter_spec import LegacyResponse, adapt_legacy_response, assert_legacy_shape
from .fixtures import decode_fixture_file_payload, load_replay_fixture, validate_replay_fixture
from .inventory import load_and_validate_runtime_inventory
from .paths import REPO_ROOT
from .route_reviews import load_route_method_reviews, require_reviewed_route_method
from .schema import ContractValidationError, load_json


_CLIENT_KINDS = frozenset({"wsgi_test_client", "asgi_test_client", "callable_test_client"})
DEFAULT_FIXTURE_ROOT = REPO_ROOT / "tests" / "v3" / "compat" / "fixtures"


def _canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ContractValidationError(f"HTTP contract value is not canonical JSON: {exc}") from exc


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical_hash(value: Any) -> str:
    return _sha256(_canonical_json(value))


def _normalized_headers(headers: Any) -> dict[str, tuple[str, ...]]:
    if headers is None:
        return {}
    if hasattr(headers, "multi_items") and callable(headers.multi_items):
        rows = headers.multi_items()
    elif isinstance(headers, Mapping):
        rows = headers.items()
    elif hasattr(headers, "items") and callable(headers.items):
        rows = headers.items()
    else:
        try:
            rows = iter(headers)
        except TypeError as exc:
            raise ContractValidationError(
                "headers must be a mapping, iterable of pairs, or fixture header/value rows"
            ) from exc

    result: dict[str, list[str]] = {}
    for row in rows:
        if isinstance(row, Mapping):
            if set(row) != {"header", "value"}:
                raise ContractValidationError("fixture header rows must contain exactly header/value")
            raw_name, raw_value = row["header"], row["value"]
        else:
            try:
                raw_name, raw_value = row
            except (TypeError, ValueError) as exc:
                raise ContractValidationError("headers contain a malformed row") from exc
        name = str(raw_name).strip().lower()
        if not name:
            raise ContractValidationError("headers contain an empty header name")
        values = raw_value if isinstance(raw_value, (list, tuple)) else (raw_value,)
        result.setdefault(name, []).extend(str(value).strip() for value in values)
    return {name: tuple(values) for name, values in result.items()}


@dataclass(frozen=True)
class OfflineIsolationAttestation:
    """Caller attestation required before an injected client may be invoked.

    The runner never opens a socket. The application under test can still own
    dependencies, so callers must explicitly attest that those dependencies are
    stubbed and cannot reach production state.
    """

    transport: str = "in_process"
    network_allowed: bool = False
    external_writes_allowed: bool = False
    production_state_allowed: bool = False
    dependencies_stubbed: bool = True

    def validate(self) -> None:
        expected = {
            "transport": "in_process",
            "network_allowed": False,
            "external_writes_allowed": False,
            "production_state_allowed": False,
            "dependencies_stubbed": True,
        }
        if asdict(self) != expected:
            raise ContractValidationError(
                "injected client lacks the strict offline isolation attestation: "
                "in_process transport, stubbed dependencies, no network/external writes/production state"
            )


@dataclass(frozen=True)
class MultipartFile:
    field: str
    filename: str
    content_type: str
    size_bytes: int
    sha256: str
    content: bytes


@dataclass(frozen=True)
class ContractRequest:
    service: str
    rule: str
    endpoint: str
    method: str
    path: str
    query: dict[str, Any]
    headers: dict[str, str]
    content_type: str
    body: Any
    files: tuple[MultipartFile, ...]
    request_fidelity: str


@dataclass(frozen=True)
class InjectedTestClient:
    kind: str
    name: str
    isolation: OfflineIsolationAttestation
    _invoke: Callable[[ContractRequest], Any]

    @classmethod
    def wsgi(
        cls,
        client: Any,
        *,
        isolation: OfflineIsolationAttestation,
        name: str = "wsgi-test-client",
    ) -> "InjectedTestClient":
        def invoke(request: ContractRequest) -> Any:
            open_request = getattr(client, "open", None)
            if not callable(open_request):
                raise ContractValidationError("injected WSGI test client must expose callable open()")
            headers = dict(request.headers)
            kwargs: dict[str, Any] = {
                "method": request.method,
                "query_string": request.query,
                "headers": headers,
            }
            if request.files:
                if not isinstance(request.body, Mapping):
                    raise ContractValidationError("multipart fixture body must be an object")
                data: dict[str, Any] = dict(request.body)
                for file in request.files:
                    upload = (io.BytesIO(file.content), file.filename, file.content_type)
                    existing = data.get(file.field)
                    if existing is None:
                        data[file.field] = upload
                    elif isinstance(existing, list):
                        existing.append(upload)
                    else:
                        data[file.field] = [existing, upload]
                for header in tuple(headers):
                    if header.lower() == "content-type":
                        headers.pop(header)
                kwargs["data"] = data
                kwargs["content_type"] = request.content_type
            elif "json" in request.content_type.lower():
                kwargs["json"] = request.body
            else:
                kwargs["data"] = request.body
            return open_request(request.path, **kwargs)

        return cls("wsgi_test_client", name, isolation, invoke)

    @classmethod
    def asgi(
        cls,
        client: Any,
        *,
        isolation: OfflineIsolationAttestation,
        name: str = "asgi-test-client",
    ) -> "InjectedTestClient":
        def invoke(request: ContractRequest) -> Any:
            send_request = getattr(client, "request", None)
            if not callable(send_request):
                raise ContractValidationError("injected ASGI test client must expose callable request()")
            headers = dict(request.headers)
            kwargs: dict[str, Any] = {"params": request.query, "headers": headers}
            if request.files:
                if not isinstance(request.body, Mapping):
                    raise ContractValidationError("multipart fixture body must be an object")
                for header in tuple(headers):
                    if header.lower() == "content-type":
                        headers.pop(header)
                kwargs["data"] = dict(request.body)
                kwargs["files"] = [
                    (file.field, (file.filename, file.content, file.content_type)) for file in request.files
                ]
            elif "json" in request.content_type.lower():
                kwargs["json"] = request.body
            elif isinstance(request.body, (str, bytes)):
                kwargs["content"] = request.body
            else:
                kwargs["data"] = request.body
            return send_request(request.method, request.path, **kwargs)

        return cls("asgi_test_client", name, isolation, invoke)

    @classmethod
    def callable(
        cls,
        invoke: Callable[[ContractRequest], Any],
        *,
        isolation: OfflineIsolationAttestation,
        name: str = "callable-test-client",
    ) -> "InjectedTestClient":
        if not callable(invoke):
            raise ContractValidationError("injected test client executor must be callable")
        return cls("callable_test_client", name, isolation, invoke)


@dataclass(frozen=True)
class ContractEvidence:
    schema_version: int
    inventory_fingerprint: str
    fixture_id: str
    fixture_sha256: str
    service: str
    rule: str
    method: str
    endpoint: str
    reviewed_by: str
    side_effect_class: str
    expected_legacy_shape: str
    client_kind: str
    client_name: str
    isolation: OfflineIsolationAttestation
    request_fidelity: str
    request_sha256: str
    expected_response_sha256: str
    observed_response_sha256: str
    canonical_envelope_sha256: str
    passed: bool
    evidence_sha256: str

    def unsigned_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload.pop("evidence_sha256")
        return payload

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ContractRunResult:
    response: LegacyResponse
    canonical_envelope: dict[str, Any]
    evidence: ContractEvidence


def _body_from_response(raw_response: Any, content_type: str) -> Any:
    if isinstance(raw_response, LegacyResponse):
        return raw_response.body
    if "json" in content_type.lower():
        get_json = getattr(raw_response, "get_json", None)
        if callable(get_json):
            return get_json()
        json_value = getattr(raw_response, "json", None)
        if callable(json_value):
            return json_value()
        if json_value is not None:
            return json_value

    marker = object()
    body = getattr(raw_response, "text", marker)
    if body is marker:
        body = getattr(raw_response, "data", marker)
    if body is marker:
        body = getattr(raw_response, "content", marker)
    if body is marker:
        raise ContractValidationError("injected response exposes no text, data, content, get_json(), or json")
    if isinstance(body, bytes):
        try:
            body = body.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ContractValidationError("injected response body is not UTF-8") from exc
    if "json" in content_type.lower() and isinstance(body, str):
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise ContractValidationError("injected JSON response body is not valid JSON") from exc
    return body


def _coerce_response(raw_response: Any) -> LegacyResponse:
    if isinstance(raw_response, LegacyResponse):
        headers = _normalized_headers(raw_response.headers)
        return LegacyResponse(raw_response.status, raw_response.content_type.strip(), raw_response.body, headers)
    status = getattr(raw_response, "status_code", getattr(raw_response, "status", None))
    if not isinstance(status, int) or isinstance(status, bool):
        raise ContractValidationError("injected response must expose integer status_code or status")
    headers = _normalized_headers(getattr(raw_response, "headers", None))
    content_types = headers.get("content-type")
    if content_types is None:
        raw_content_type = getattr(raw_response, "content_type", None)
        if raw_content_type is None:
            raise ContractValidationError("injected response must expose Content-Type")
        content_type = str(raw_content_type).strip()
    else:
        if len(content_types) != 1:
            raise ContractValidationError("injected response must expose exactly one Content-Type value")
        content_type = content_types[0]
    body = _body_from_response(raw_response, content_type)
    return LegacyResponse(status, content_type, body, headers)


def _response_projection(response: LegacyResponse, declared_headers: Any) -> dict[str, Any]:
    observed_headers = _normalized_headers(response.headers)
    expected_headers = _normalized_headers(declared_headers)
    projected_headers: dict[str, list[str]] = {}
    for name, expected_values in expected_headers.items():
        actual_values = observed_headers.get(name)
        if actual_values is None:
            raise ContractValidationError(f"legacy response is missing declared header: {name}")
        if actual_values != expected_values:
            raise ContractValidationError(
                f"legacy response header mismatch for {name}: expected {expected_values!r}, got {actual_values!r}"
            )
        projected_headers[name] = list(actual_values)
    return {
        "status": response.status,
        "headers": projected_headers,
        "content_type": response.content_type,
        "body": response.body,
    }


def _expected_response_projection(response: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "status": response["status"],
        "headers": {name: list(values) for name, values in _normalized_headers(response["headers"]).items()},
        "content_type": str(response["content_type"]).strip(),
        "body": response["body"],
    }


def _assert_fixture_content_type_header(response: Mapping[str, Any]) -> None:
    content_types = _normalized_headers(response["headers"]).get("content-type")
    expected = (str(response["content_type"]).strip(),)
    if content_types != expected:
        raise ContractValidationError(
            f"fixture must declare exactly one Content-Type header equal to response.content_type: {expected[0]!r}"
        )


def _request_from_fixture(fixture: Mapping[str, Any]) -> ContractRequest:
    route = fixture["route"]
    request = fixture["request"]
    files = tuple(
        MultipartFile(
            field=item["field"],
            filename=item["filename"],
            content_type=item["content_type"],
            size_bytes=item["size_bytes"],
            sha256=item["sha256"],
            content=decode_fixture_file_payload(item),
        )
        for item in request["files"]
    )
    return ContractRequest(
        service=route["service"],
        rule=route["rule"],
        endpoint=route["endpoint"],
        method=request["method"],
        path=request["path"],
        query=dict(request["query"]),
        headers=dict(request["headers"]),
        content_type=request["content_type"],
        body=request["body"],
        files=files,
        request_fidelity="complete",
    )


def _resolve_allowed_fixture_path(
    path: str | Path,
    allowed_fixture_roots: tuple[str | Path, ...] | None,
) -> Path:
    roots = allowed_fixture_roots if allowed_fixture_roots is not None else (DEFAULT_FIXTURE_ROOT,)
    if not roots:
        raise ContractValidationError("HTTP contract fixture allowlist must not be empty")
    resolved_roots = tuple(Path(root).expanduser().resolve() for root in roots)
    resolved_path = Path(path).expanduser().resolve()
    if resolved_path.suffix.lower() != ".json":
        raise ContractValidationError("HTTP contract fixture must be an allowlisted .json file")
    if not any(resolved_path.is_relative_to(root) for root in resolved_roots):
        raise ContractValidationError("HTTP contract fixture path is outside the allowlisted roots")
    if not resolved_path.is_file():
        raise ContractValidationError("HTTP contract fixture path is not a regular file")
    return resolved_path


def fixture_sha256(
    path: str | Path,
    *,
    allowed_fixture_roots: tuple[str | Path, ...] | None = None,
) -> str:
    resolved_path = _resolve_allowed_fixture_path(path, allowed_fixture_roots)
    return _sha256(resolved_path.read_bytes())


def _preflight_reviewed_route_method(
    fixture: Any,
    inventory: Mapping[str, Any],
) -> tuple[Mapping[str, Any], str, Any]:
    try:
        route = fixture["route"]
        request = fixture["request"]
        service = str(route["service"])
        rule = str(route["rule"])
        methods = sorted(str(value).upper() for value in route["methods"])
        endpoint = str(route["endpoint"])
        method = str(request["method"]).upper()
    except (KeyError, TypeError) as exc:
        raise ContractValidationError("fixture lacks a route/method identity for preflight review") from exc
    pinned = any(
        row["service"] == service
        and row["rule"] == rule
        and sorted(row["methods"]) == methods
        and row["endpoint"] == endpoint
        for row in inventory["coverage"]
    )
    if not pinned:
        raise ContractValidationError(
            "fixture route does not exactly match the pinned 347-route inventory: "
            f"{service} {methods} {rule} -> {endpoint}"
        )
    reviews = load_route_method_reviews(expected_inventory_fingerprint=inventory["fingerprint"])
    review = require_reviewed_route_method(
        service=service,
        rule=rule,
        method=method,
        endpoint=endpoint,
        reviews=reviews,
    )
    return route, method, review


def run_http_contract(
    fixture_path: str | Path,
    client: InjectedTestClient,
    *,
    expected_fixture_sha256: str | None = None,
    allowed_fixture_roots: tuple[str | Path, ...] | None = None,
) -> ContractRunResult:
    """Run one reviewed fixture through an injected, in-process test client.

    All inventory, review, fixture, hash, and isolation gates execute before the
    client callback. This function contains no URL/host/port option and performs
    no transport or persistence I/O beyond reading the fixture and pinned files.
    """

    if not isinstance(client, InjectedTestClient):
        raise ContractValidationError("HTTP contract runner requires an InjectedTestClient")
    if client.kind not in _CLIENT_KINDS or not client.name.strip():
        raise ContractValidationError("injected test client has invalid kind/name")
    client.isolation.validate()

    path = _resolve_allowed_fixture_path(fixture_path, allowed_fixture_roots)
    raw_fixture_hash = fixture_sha256(path, allowed_fixture_roots=allowed_fixture_roots)
    if expected_fixture_sha256 is not None and raw_fixture_hash != expected_fixture_sha256:
        raise ContractValidationError(
            f"fixture SHA-256 mismatch: expected {expected_fixture_sha256}, got {raw_fixture_hash}"
        )

    inventory = load_and_validate_runtime_inventory()
    fixture = load_json(path)
    route, method, review = _preflight_reviewed_route_method(fixture, inventory)
    validate_replay_fixture(fixture)
    _assert_fixture_content_type_header(fixture["response"])

    request = _request_from_fixture(fixture)
    try:
        raw_response = client._invoke(request)
    except ContractValidationError:
        raise
    except Exception as exc:
        raise ContractValidationError(
            f"injected {client.kind} raised {type(exc).__name__}; no passing evidence was emitted"
        ) from exc
    observed = _coerce_response(raw_response)
    expected = fixture["response"]

    if observed.status != expected["status"]:
        raise ContractValidationError(
            f"legacy response status mismatch: expected {expected['status']}, got {observed.status}"
        )
    if observed.content_type != str(expected["content_type"]).strip():
        raise ContractValidationError(
            "legacy response content-type mismatch: "
            f"expected {expected['content_type']!r}, got {observed.content_type!r}"
        )
    observed_projection = _response_projection(observed, expected["headers"])
    expected_projection = _expected_response_projection(expected)
    if _canonical_hash(observed.body) != _canonical_hash(expected["body"]):
        raise ContractValidationError("legacy response body mismatch")
    try:
        assert_legacy_shape(observed, fixture["expected_legacy_shape"])
    except ValueError as exc:
        raise ContractValidationError(f"observed legacy response shape mismatch: {exc}") from exc

    request_id = f"contract-{fixture['fixture_id']}-{raw_fixture_hash[:16]}"
    envelope = adapt_legacy_response(
        observed,
        request_id=request_id,
        expected_shape=fixture["expected_legacy_shape"],
    )
    evidence = ContractEvidence(
        schema_version=1,
        inventory_fingerprint=inventory["fingerprint"],
        fixture_id=fixture["fixture_id"],
        fixture_sha256=raw_fixture_hash,
        service=route["service"],
        rule=route["rule"],
        method=method,
        endpoint=route["endpoint"],
        reviewed_by=review.reviewed_by,
        side_effect_class=review.side_effect_class,
        expected_legacy_shape=fixture["expected_legacy_shape"],
        client_kind=client.kind,
        client_name=client.name,
        isolation=client.isolation,
        request_fidelity=request.request_fidelity,
        request_sha256=_canonical_hash(fixture["request"]),
        expected_response_sha256=_canonical_hash(expected_projection),
        observed_response_sha256=_canonical_hash(observed_projection),
        canonical_envelope_sha256=_canonical_hash(envelope),
        passed=True,
        evidence_sha256="",
    )
    evidence = replace(evidence, evidence_sha256=_canonical_hash(evidence.unsigned_dict()))
    return ContractRunResult(response=observed, canonical_envelope=envelope, evidence=evidence)


def verify_contract_evidence(
    evidence: ContractEvidence,
    fixture_path: str | Path,
    *,
    allowed_fixture_roots: tuple[str | Path, ...] | None = None,
) -> None:
    """Revalidate deterministic evidence against current pinned inputs."""

    if not isinstance(evidence, ContractEvidence):
        raise ContractValidationError("contract evidence must be a ContractEvidence instance")
    if evidence.schema_version != 1 or evidence.passed is not True:
        raise ContractValidationError("contract evidence is not a passing schema_version 1 record")
    if evidence.client_kind not in _CLIENT_KINDS or not evidence.client_name.strip():
        raise ContractValidationError("contract evidence has invalid client identity")
    evidence.isolation.validate()
    if evidence.evidence_sha256 != _canonical_hash(evidence.unsigned_dict()):
        raise ContractValidationError("contract evidence SHA-256 is invalid")

    inventory = load_and_validate_runtime_inventory()
    if evidence.inventory_fingerprint != inventory["fingerprint"]:
        raise ContractValidationError("contract evidence is not pinned to the current inventory fingerprint")
    path = _resolve_allowed_fixture_path(fixture_path, allowed_fixture_roots)
    fixture = load_replay_fixture(path)
    _assert_fixture_content_type_header(fixture["response"])
    if evidence.fixture_sha256 != fixture_sha256(path, allowed_fixture_roots=allowed_fixture_roots):
        raise ContractValidationError("contract evidence fixture SHA-256 does not match fixture bytes")

    route = fixture["route"]
    method = fixture["request"]["method"]
    expected_identity = (
        fixture["fixture_id"],
        route["service"],
        route["rule"],
        method,
        route["endpoint"],
        fixture["side_effect_class"],
        fixture["expected_legacy_shape"],
    )
    actual_identity = (
        evidence.fixture_id,
        evidence.service,
        evidence.rule,
        evidence.method,
        evidence.endpoint,
        evidence.side_effect_class,
        evidence.expected_legacy_shape,
    )
    if actual_identity != expected_identity:
        raise ContractValidationError("contract evidence route/method/fixture identity does not match fixture")

    review = require_reviewed_route_method(
        service=route["service"],
        rule=route["rule"],
        method=method,
        endpoint=route["endpoint"],
        reviews=load_route_method_reviews(expected_inventory_fingerprint=inventory["fingerprint"]),
    )
    if evidence.reviewed_by != review.reviewed_by:
        raise ContractValidationError("contract evidence reviewer does not match current completed review")
    expected_response_hash = _canonical_hash(_expected_response_projection(fixture["response"]))
    if evidence.request_sha256 != _canonical_hash(fixture["request"]):
        raise ContractValidationError("contract evidence request SHA-256 does not match fixture")
    if evidence.expected_response_sha256 != expected_response_hash:
        raise ContractValidationError("contract evidence expected response SHA-256 does not match fixture")
    if evidence.observed_response_sha256 != expected_response_hash:
        raise ContractValidationError("contract evidence observed response does not satisfy fixture projection")

    expected_fidelity = "complete"
    if evidence.request_fidelity != expected_fidelity:
        raise ContractValidationError("contract evidence request fidelity does not match fixture")
    expected_response = fixture["response"]
    request_id = f"contract-{fixture['fixture_id']}-{evidence.fixture_sha256[:16]}"
    expected_envelope = adapt_legacy_response(
        LegacyResponse(
            expected_response["status"],
            expected_response["content_type"],
            expected_response["body"],
            expected_response["headers"],
        ),
        request_id=request_id,
        expected_shape=fixture["expected_legacy_shape"],
    )
    if evidence.canonical_envelope_sha256 != _canonical_hash(expected_envelope):
        raise ContractValidationError("contract evidence canonical envelope SHA-256 does not match fixture")
