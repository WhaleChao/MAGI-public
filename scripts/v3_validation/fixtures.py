from __future__ import annotations

import base64
import binascii
import copy
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from .adapter_spec import LegacyResponse, assert_legacy_shape
from .inventory import load_and_validate_runtime_inventory
from .paths import REPLAY_FIXTURE_SCHEMA_PATH
from .route_reviews import load_route_method_reviews, require_reviewed_route_method
from .schema import ContractValidationError, load_json, validate_json


_SENSITIVE_KEY = re.compile(
    r"(^|_)(authorization|cookie|set_cookie|api_key|apikey|token|secret|password|passwd|session|credential|"
    r"national_id|identity|id_number|person_id|client_name|customer_name|person_name|full_name|name|address|"
    r"case_number|case_no|court_number|drive_id|file_id|channel_id|discord_id|telegram_id|line_user_id|user_id|"
    r"filename|code|state|id)(_|$)",
    re.IGNORECASE,
)
_EMAIL = re.compile(r"(?<![\w.+-])[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}(?![\w.-])")
_BEARER = re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{8,}", re.IGNORECASE)
_PHONE = re.compile(r"(?<!\d)(?:\+?886[- ]?)?0?9\d{2}[- ]?\d{3}[- ]?\d{3}(?!\d)")
_USER_PATH = re.compile(r"/(?:Users|home)/[^/\s]+(?:/[^\s]*)?")
_NON_LOOPBACK_IP = re.compile(r"\b(?!(?:127|0)\.)((?:\d{1,3}\.){3}\d{1,3})\b")
_RAW_SECRET = re.compile(r"\b(?:nvapi-|sk-|ghp_|xox[baprs]-)[A-Za-z0-9_-]{8,}")
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")
_TAIWAN_ID = re.compile(r"(?<![A-Za-z0-9])[A-Z][12A-D]\d{8}(?![A-Za-z0-9])", re.IGNORECASE)
_TAIWAN_CASE = re.compile(r"(?:民國)?\d{2,3}\s*年?度?\s*[\u4e00-\u9fff]{1,8}字\s*第?\s*\d{1,10}\s*號")
_TAIWAN_ADDRESS = re.compile(
    r"(?:台|臺)?[\u4e00-\u9fff]{1,4}(?:縣|市)[\u4e00-\u9fff0-9之\-]{1,30}(?:區|鄉|鎮|市)"
    r"[\u4e00-\u9fff0-9之\-]{0,40}(?:路|街|巷|弄|號)"
)
_TAIWAN_NAME = re.compile(
    r"(?:歐陽|司馬|上官|諸葛)[\u4e00-\u9fff]{1,2}|"
    r"(?:王|李|張|劉|陳|楊|黃|趙|吳|周|徐|孫|林|朱|高|郭|何|羅|鄭|梁|謝|宋|唐|許|鄧|"
    r"馮|韓|曹|曾|彭|蕭|蔡|潘|田|董|袁|于|余|葉|蔣|杜|蘇|魏|程|呂|丁|沈|任|姚|盧|"
    r"傅|鍾|姜|崔|譚|廖|范|汪|陸|金|石|戴|賈|韋|夏|邱|方|侯|鄒|熊|孟|秦|白|江|閻|"
    r"薛|尹|段|雷|黎|史|龍|陶|賀|顧|毛|郝|龔|邵|萬|錢|嚴|賴)[\u4e00-\u9fff]{1,2}"
)
_CHANNEL_ID = re.compile(r"(?<!\d)\d{15,20}(?!\d)")
_DRIVE_URL = re.compile(r"https?://(?:drive|docs)\.google\.com/[^\s]+", re.IGNORECASE)
_DRIVE_ID = re.compile(r"\b(?![a-fA-F0-9]{25,64}\b)[A-Za-z0-9_-]{25,64}\b")
_PLACEHOLDER = re.compile(r"^<redacted:[a-z0-9_.-]+:[a-f0-9]{12}>$")
_ROUTE_VARIABLE = re.compile(r"<(?:(?P<converter>[a-zA-Z_][\w()]*):)?(?P<name>[^>]+)>")
_SENSITIVE_HEADERS = frozenset({"authorization", "cookie", "set_cookie", "x_api_key", "api_key"})
_MAX_TEST_FILE_BYTES = 65536


def _normalized_key(key: str) -> str:
    snake = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(key))
    return re.sub(r"[^a-z0-9]+", "_", snake.lower()).strip("_")


def _tag(kind: str, value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    digest = hashlib.sha256(("magi-v3-fixture-v1\0" + raw).encode("utf-8")).hexdigest()[:12]
    safe_kind = re.sub(r"[^a-z0-9_.-]+", "_", kind.lower()).strip("_") or "value"
    return f"<redacted:{safe_kind}:{digest}>"


def _redact_text(text: str, applied: set[str]) -> str:
    def replace(pattern: re.Pattern[str], kind: str, source: str) -> str:
        def repl(match: re.Match[str]) -> str:
            applied.add(kind)
            return _tag(kind, match.group(0))
        return pattern.sub(repl, source)

    result = text
    result = replace(_BEARER, "bearer", result)
    result = replace(_RAW_SECRET, "secret", result)
    result = replace(_EMAIL, "email", result)
    result = replace(_PHONE, "phone", result)
    result = replace(_USER_PATH, "path", result)
    result = replace(_NON_LOOPBACK_IP, "ip", result)
    result = replace(_JWT, "jwt", result)
    result = replace(_TAIWAN_ID, "taiwan_id", result)
    result = replace(_TAIWAN_CASE, "case_number", result)
    result = replace(_TAIWAN_ADDRESS, "address", result)
    result = replace(_TAIWAN_NAME, "person_name", result)
    result = replace(_CHANNEL_ID, "channel_id", result)
    result = replace(_DRIVE_URL, "drive_url", result)
    result = replace(_DRIVE_ID, "drive_id", result)
    return result


def _anonymize(value: Any, applied: set[str], key: str = "") -> Any:
    normalized_key = _normalized_key(key)
    if normalized_key == "content_base64":
        return value
    if _SENSITIVE_KEY.search(normalized_key):
        applied.add(f"key:{key.lower()}")
        return _tag(key, value)
    if isinstance(value, dict):
        return {str(item_key): _anonymize(item, applied, str(item_key)) for item_key, item in value.items()}
    if isinstance(value, list):
        return [_anonymize(item, applied, key) for item in value]
    if isinstance(value, tuple):
        return [_anonymize(item, applied, key) for item in value]
    if isinstance(value, str):
        return _redact_text(value, applied)
    return value


def anonymize_fixture(raw_fixture: dict[str, Any]) -> dict[str, Any]:
    fixture = copy.deepcopy(raw_fixture)
    applied: set[str] = set()
    for section in ("request", "response"):
        fixture[section] = _anonymize(fixture.get(section, {}), applied, section)
        headers = fixture[section].get("headers", {})
        if isinstance(headers, list):
            for row in headers:
                if not isinstance(row, dict):
                    continue
                header = _normalized_key(str(row.get("header") or ""))
                if header in _SENSITIVE_HEADERS and not _PLACEHOLDER.match(str(row.get("value") or "")):
                    applied.add(f"key:{header}")
                    row["value"] = _tag(header, row.get("value"))
    retains_test_payload = any(
        isinstance(row, dict) and "content_base64" in row
        for row in fixture.get("request", {}).get("files", ())
    )
    fixture["redaction"] = {
        "version": 1,
        "safe_for_offline": True,
        "raw_payload_retained": retains_test_payload,
        "test_payload_only": retains_test_payload,
        "applied": sorted(applied),
    }
    fixture["replay"] = {
        "mode": "offline",
        "network_allowed": False,
        "external_writes_allowed": False,
    }
    return fixture


def _walk(value: Any, path: str = ""):
    if isinstance(value, dict):
        for key, item in value.items():
            child = f"{path}.{key}" if path else str(key)
            yield child, str(key), item
            yield from _walk(item, child)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _walk(item, f"{path}[{index}]")


def assert_fixture_anonymized(fixture: dict[str, Any]) -> None:
    violations: list[str] = []
    for path, key, value in _walk({"request": fixture.get("request"), "response": fixture.get("response")}):
        if _SENSITIVE_KEY.search(_normalized_key(key)) and not (
            isinstance(value, str) and _PLACEHOLDER.match(value)
        ):
            violations.append(f"{path}: sensitive field is not redacted")
        if isinstance(value, str) and not path.endswith(".content_base64") and not _PLACEHOLDER.match(value):
            for name, pattern in (
                ("email", _EMAIL),
                ("bearer", _BEARER),
                ("phone", _PHONE),
                ("user_path", _USER_PATH),
                ("non_loopback_ip", _NON_LOOPBACK_IP),
                ("secret", _RAW_SECRET),
                ("jwt", _JWT),
                ("taiwan_id", _TAIWAN_ID),
                ("case_number", _TAIWAN_CASE),
                ("address", _TAIWAN_ADDRESS),
                ("person_name", _TAIWAN_NAME),
                ("channel_id", _CHANNEL_ID),
                ("drive_url", _DRIVE_URL),
                ("drive_id", _DRIVE_ID),
            ):
                if pattern.search(value):
                    violations.append(f"{path}: contains raw {name}")
    for file_row in fixture.get("request", {}).get("files", ()):
        if any(key in file_row for key in ("content", "bytes", "data", "path")):
            violations.append("request.files: raw file content/path is forbidden")
    for section in ("request", "response"):
        headers = fixture.get(section, {}).get("headers", {})
        if not isinstance(headers, list):
            continue
        for index, row in enumerate(headers):
            if not isinstance(row, dict):
                continue
            header = _normalized_key(str(row.get("header") or ""))
            value = row.get("value")
            if header in _SENSITIVE_HEADERS and not (isinstance(value, str) and _PLACEHOLDER.match(value)):
                violations.append(f"{section}.headers[{index}]: sensitive header value is not redacted")
    if violations:
        raise ContractValidationError("fixture is not safe for offline replay: " + "; ".join(violations[:12]))


def decode_fixture_file_payload(file_row: dict[str, Any]) -> bytes:
    try:
        payload = base64.b64decode(str(file_row.get("content_base64") or ""), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ContractValidationError("fixture multipart content_base64 is not strict Base64") from exc
    if not payload or len(payload) > _MAX_TEST_FILE_BYTES:
        raise ContractValidationError(f"fixture multipart payload must be 1..{_MAX_TEST_FILE_BYTES} bytes")
    if len(payload) != file_row.get("size_bytes"):
        raise ContractValidationError("fixture multipart size_bytes does not match decoded bytes")
    if hashlib.sha256(payload).hexdigest() != file_row.get("sha256"):
        raise ContractValidationError("fixture multipart SHA-256 does not match decoded bytes")
    return payload


def assert_fixture_file_payloads(fixture: dict[str, Any]) -> None:
    files = fixture.get("request", {}).get("files", ())
    retains_payload = bool(files)
    redaction = fixture.get("redaction", {})
    if redaction.get("raw_payload_retained") is not retains_payload:
        raise ContractValidationError("fixture raw_payload_retained must exactly reflect embedded multipart bytes")
    if redaction.get("test_payload_only") is not retains_payload:
        raise ContractValidationError("fixture test_payload_only must exactly reflect embedded multipart bytes")

    text_patterns = (
        ("email", _EMAIL),
        ("bearer", _BEARER),
        ("phone", _PHONE),
        ("user_path", _USER_PATH),
        ("non_loopback_ip", _NON_LOOPBACK_IP),
        ("secret", _RAW_SECRET),
        ("jwt", _JWT),
        ("taiwan_id", _TAIWAN_ID),
        ("case_number", _TAIWAN_CASE),
        ("address", _TAIWAN_ADDRESS),
        ("person_name", _TAIWAN_NAME),
        ("channel_id", _CHANNEL_ID),
        ("drive_url", _DRIVE_URL),
        ("drive_id", _DRIVE_ID),
    )
    for index, file_row in enumerate(files):
        if file_row.get("safe_test_payload") is not True:
            raise ContractValidationError(f"fixture multipart file {index} is not declared as a safe test payload")
        payload = decode_fixture_file_payload(file_row)
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError:
            continue
        for name, pattern in text_patterns:
            if pattern.search(text):
                raise ContractValidationError(f"fixture multipart file {index} contains raw {name}")


def assert_fixture_route_is_pinned(fixture: dict[str, Any]) -> None:
    route = fixture["route"]
    expected_methods = sorted(route["methods"])
    candidates = [
        row
        for row in load_and_validate_runtime_inventory()["coverage"]
        if row["service"] == route["service"]
        and row["rule"] == route["rule"]
        and row["endpoint"] == route["endpoint"]
        and sorted(row["methods"]) == expected_methods
    ]
    if not candidates:
        raise ContractValidationError(
            "fixture route does not exactly match the pinned 347-route inventory: "
            f"{route['service']} {expected_methods} {route['rule']} -> {route['endpoint']}"
        )
    if fixture["request"]["method"] not in expected_methods:
        raise ContractValidationError("fixture request method is not declared by its pinned route")
    request_method = fixture["request"]["method"]
    review = require_reviewed_route_method(
        service=route["service"],
        rule=route["rule"],
        method=request_method,
        endpoint=route["endpoint"],
        reviews=load_route_method_reviews(),
    )
    if not route_rule_matches_path(route["rule"], fixture["request"]["path"]):
        raise ContractValidationError("fixture request path does not match its pinned route rule")
    if fixture["side_effect_class"] != review.side_effect_class:
        raise ContractValidationError(
            f"fixture side_effect_class must be {review.side_effect_class!r} for reviewed route-method, "
            f"got {fixture['side_effect_class']!r}"
        )


def route_rule_matches_path(rule: str, path: str) -> bool:
    parts: list[str] = []
    position = 0
    converter_patterns = {
        "int": r"\d+",
        "float": r"\d+(?:\.\d+)?",
        "path": r".+",
        "uuid": r"[0-9a-fA-F-]{36}",
        "string": r"[^/]+",
    }
    for match in _ROUTE_VARIABLE.finditer(rule):
        parts.append(re.escape(rule[position:match.start()]))
        parts.append(converter_patterns.get(match.group("converter") or "string", r"[^/]+"))
        position = match.end()
    parts.append(re.escape(rule[position:]))
    return re.fullmatch("".join(parts), path) is not None


def assert_fixture_legacy_shape(fixture: dict[str, Any]) -> None:
    response = fixture["response"]
    try:
        assert_legacy_shape(
            LegacyResponse(response["status"], response["content_type"], response["body"], response["headers"]),
            fixture["expected_legacy_shape"],
        )
    except ValueError as exc:
        raise ContractValidationError(f"fixture legacy response shape mismatch: {exc}") from exc


def validate_replay_fixture(fixture: dict[str, Any], *, schema_path: str | Path = REPLAY_FIXTURE_SCHEMA_PATH) -> None:
    validate_json(fixture, load_json(schema_path), label=f"fixture:{fixture.get('fixture_id', '<unknown>')}")
    assert_fixture_file_payloads(fixture)
    assert_fixture_anonymized(fixture)
    assert_fixture_route_is_pinned(fixture)
    assert_fixture_legacy_shape(fixture)


def load_replay_fixture(path: str | Path) -> dict[str, Any]:
    fixture = load_json(path)
    validate_replay_fixture(fixture)
    return fixture
