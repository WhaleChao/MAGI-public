"""PII-safe W3C trace context and local-first GenAI span export.

MAGI does not record prompts, tool arguments, model output, case identifiers,
filesystem paths, or browser profile data in spans.  The default production
export is a local mode-0600 JSONL spool.  An OTLP/HTTP JSON endpoint may be
enabled only on loopback so a local Collector can forward to self-hosted
Phoenix without exposing legal data to a third party.
"""

from __future__ import annotations

import contextvars
import hashlib
import json
import os
import re
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Protocol
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


TRACE_SCHEMA = "magi.otel-span/v1"
TRACEPARENT_RE = re.compile(r"^00-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$")
TRACE_ID_RE = re.compile(r"^[0-9a-f]{32}$")
SPAN_ID_RE = re.compile(r"^[0-9a-f]{16}$")
SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9_.:/-]{1,160}$")
SAFE_ROUTE_RE = re.compile(r"^/[A-Za-z0-9_.:/{}-]{0,159}$")
ALLOWED_ATTRIBUTES = frozenset(
    {
        "gen_ai.operation.name",
        "gen_ai.agent.name",
        "gen_ai.system",
        "gen_ai.request.model",
        "gen_ai.response.model",
        "magi.release_id",
        "magi.component",
        "magi.tool.name",
        "magi.side_effect.class",
        "magi.outcome",
        "magi.reason_code",
        "magi.receipt.sha256",
        "magi.retry.count",
        "http.request.method",
        "http.route",
        "http.response.status_code",
        "rpc.system",
        "rpc.method",
        "error.type",
    }
)


class TelemetryError(ValueError):
    """Trace context or exporter configuration is unsafe."""


@dataclass(frozen=True, slots=True)
class TraceContext:
    trace_id: str
    span_id: str
    sampled: bool = True

    def __post_init__(self) -> None:
        if not TRACE_ID_RE.fullmatch(self.trace_id) or self.trace_id == "0" * 32:
            raise TelemetryError("trace_id is invalid")
        if not SPAN_ID_RE.fullmatch(self.span_id) or self.span_id == "0" * 16:
            raise TelemetryError("span_id is invalid")

    @property
    def traceparent(self) -> str:
        return f"00-{self.trace_id}-{self.span_id}-{'01' if self.sampled else '00'}"

    @classmethod
    def parse(cls, value: str | None) -> "TraceContext | None":
        if not value:
            return None
        matched = TRACEPARENT_RE.fullmatch(str(value).strip().lower())
        if not matched:
            return None
        try:
            return cls(matched.group(1), matched.group(2), bool(int(matched.group(3), 16) & 1))
        except TelemetryError:
            return None


def new_trace_context(*, trace_id: str | None = None, sampled: bool = True) -> TraceContext:
    return TraceContext(trace_id or secrets.token_hex(16), secrets.token_hex(8), sampled)


def child_trace_context(parent: TraceContext | None) -> TraceContext:
    return new_trace_context(trace_id=parent.trace_id if parent else None, sampled=parent.sampled if parent else True)


def _safe_name(value: Any, field_name: str) -> str:
    text = str(value or "").strip()
    if not SAFE_NAME_RE.fullmatch(text):
        raise TelemetryError(f"{field_name} is not a safe categorical label")
    return text


def _safe_attributes(attributes: Mapping[str, Any] | None) -> dict[str, str | int | float | bool]:
    result: dict[str, str | int | float | bool] = {}
    for key, value in dict(attributes or {}).items():
        if key not in ALLOWED_ATTRIBUTES:
            raise TelemetryError(f"trace attribute is not allowlisted: {key}")
        if isinstance(value, bool):
            result[key] = value
        elif isinstance(value, int) and not isinstance(value, bool):
            result[key] = value
        elif isinstance(value, float) and value == value and abs(value) != float("inf"):
            result[key] = value
        else:
            text = str(value or "").strip()
            if key == "http.route":
                if "?" in text or "#" in text or not SAFE_ROUTE_RE.fullmatch(text):
                    raise TelemetryError("http.route is not a safe route template")
                result[key] = text
            else:
                if text.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:[\\/]", text):
                    raise TelemetryError(f"{key} is not a safe categorical label")
                result[key] = _safe_name(text, key)
    return result


class SpanExporter(Protocol):
    def export(self, record: Mapping[str, Any]) -> None: ...


class NoopExporter:
    def export(self, record: Mapping[str, Any]) -> None:
        del record


class JsonlSpanExporter:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path).expanduser()

    def export(self, record: Mapping[str, Any]) -> None:
        encoded = (json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        if len(encoded) > 16 * 1024:
            raise TelemetryError("span exceeds 16 KiB")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(self.path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            os.write(descriptor, encoded)
        finally:
            os.close(descriptor)
        try:
            self.path.chmod(0o600)
        except OSError:
            pass


class OtlpHttpJsonExporter:
    """Minimal OTLP/HTTP JSON exporter restricted to a local Collector."""

    def __init__(self, endpoint: str, *, timeout_sec: float = 0.75) -> None:
        parsed = urlsplit(str(endpoint or "").strip())
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "::1", "localhost"}
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise TelemetryError("OTLP endpoint must be credential-free loopback HTTP")
        self.endpoint = str(endpoint).rstrip("/")
        self.timeout_sec = max(0.1, min(2.0, float(timeout_sec)))

    def export(self, record: Mapping[str, Any]) -> None:
        attributes = [
            {"key": key, "value": _otlp_value(value)}
            for key, value in dict(record.get("attributes") or {}).items()
        ]
        payload = {
            "resourceSpans": [
                {
                    "resource": {
                        "attributes": [
                            {"key": "service.name", "value": {"stringValue": "magi-v3"}},
                            {"key": "deployment.environment.name", "value": {"stringValue": "self-hosted"}},
                        ]
                    },
                    "scopeSpans": [
                        {
                            "scope": {"name": str(record["scope"]), "version": "1"},
                            "spans": [
                                {
                                    "traceId": str(record["trace_id"]),
                                    "spanId": str(record["span_id"]),
                                    "parentSpanId": str(record.get("parent_span_id") or ""),
                                    "name": str(record["name"]),
                                    "kind": 2,
                                    "startTimeUnixNano": str(record["start_time_unix_nano"]),
                                    "endTimeUnixNano": str(record["end_time_unix_nano"]),
                                    "attributes": attributes,
                                    "status": {"code": 1 if record.get("status") == "ok" else 2},
                                }
                            ],
                        }
                    ],
                }
            ]
        }
        request = Request(
            self.endpoint,
            data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urlopen(request, timeout=self.timeout_sec) as response:
            if int(getattr(response, "status", 200)) >= 300:
                raise OSError("local OTLP Collector rejected span")


def _otlp_value(value: Any) -> dict[str, Any]:
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int):
        return {"intValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value}
    return {"stringValue": str(value)}


class CompositeExporter:
    def __init__(self, exporters: list[SpanExporter]) -> None:
        self.exporters = tuple(exporters)

    def export(self, record: Mapping[str, Any]) -> None:
        for exporter in self.exporters:
            try:
                exporter.export(record)
            except Exception:
                # Tracing must never break legal workflows.  The local spool
                # should be first and remains authoritative for diagnostics.
                continue


_CURRENT_CONTEXT: contextvars.ContextVar[TraceContext | None] = contextvars.ContextVar(
    "magi_trace_context", default=None
)


def current_trace_context() -> TraceContext | None:
    return _CURRENT_CONTEXT.get()


def current_trace_id() -> str:
    context = current_trace_context()
    return context.trace_id if context else ""


def receipt_trace_id() -> str:
    """Return the current trace ID or create one for a standalone receipt."""

    return current_trace_id() or new_trace_context().trace_id


def default_exporter() -> SpanExporter:
    mode = os.environ.get("MAGI_OTEL_MODE", "").strip().lower()
    production = os.environ.get("MAGI_V3_DEPLOYMENT_MODE", "").strip().lower() == "production"
    if not mode:
        mode = "jsonl" if production else "disabled"
    if mode == "disabled":
        return NoopExporter()
    runtime = Path(
        os.environ.get("MAGI_RUNTIME_DIR", "").strip()
        or Path.home() / "Library" / "Application Support" / "MAGI" / "runtime" / "MAGI_v3" / "shared" / "runtime"
    ).expanduser()
    spool = Path(os.environ.get("MAGI_TRACE_SPOOL", "").strip() or runtime / "traces" / "spans.jsonl").expanduser()
    exporters: list[SpanExporter] = [JsonlSpanExporter(spool)]
    if mode == "otlp":
        endpoint = os.environ.get("MAGI_OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "http://127.0.0.1:4318/v1/traces")
        exporters.append(OtlpHttpJsonExporter(endpoint))
    elif mode != "jsonl":
        raise TelemetryError("MAGI_OTEL_MODE must be disabled, jsonl, or otlp")
    return CompositeExporter(exporters)


@dataclass(slots=True)
class Span:
    name: str
    scope: str
    context: TraceContext
    parent_span_id: str = ""
    attributes: dict[str, str | int | float | bool] = field(default_factory=dict)
    exporter: SpanExporter = field(default_factory=NoopExporter)
    start_time_unix_nano: int = field(default_factory=time.time_ns)
    status: str = "ok"
    _token: contextvars.Token[TraceContext | None] | None = field(default=None, init=False, repr=False)
    _ended: bool = field(default=False, init=False, repr=False)

    def __enter__(self) -> "Span":
        self._token = _CURRENT_CONTEXT.set(self.context)
        return self

    @property
    def ended(self) -> bool:
        return self._ended

    def set_attribute(self, key: str, value: Any) -> None:
        self.attributes.update(_safe_attributes({key: value}))

    def record_error(self, error_type: str) -> None:
        self.status = "error"
        self.set_attribute("error.type", error_type)

    def end(self) -> None:
        if self._ended:
            return
        self._ended = True
        end_time = time.time_ns()
        record = {
            "schema": TRACE_SCHEMA,
            "scope": self.scope,
            "name": self.name,
            "trace_id": self.context.trace_id,
            "span_id": self.context.span_id,
            "parent_span_id": self.parent_span_id,
            "traceparent": self.context.traceparent,
            "start_time_unix_nano": self.start_time_unix_nano,
            "end_time_unix_nano": end_time,
            "duration_ms": round((end_time - self.start_time_unix_nano) / 1_000_000, 3),
            "status": self.status,
            "attributes": dict(sorted(self.attributes.items())),
        }
        self.exporter.export(record)
        if self._token is not None:
            _CURRENT_CONTEXT.reset(self._token)
            self._token = None

    def __exit__(self, exc_type, exc, traceback) -> bool:
        del traceback
        if exc_type is not None:
            self.record_error(_safe_name(getattr(exc_type, "__name__", "Exception"), "error.type"))
        self.end()
        return False


class Tracer:
    def __init__(self, scope: str, *, exporter: SpanExporter | None = None) -> None:
        self.scope = _safe_name(scope, "scope")
        if exporter is not None:
            self.exporter = exporter
        else:
            try:
                self.exporter = default_exporter()
            except (OSError, TelemetryError, ValueError):
                # A tracing configuration fault is surfaced by the dedicated
                # health probe; it must not prevent MAGI web routes starting.
                self.exporter = NoopExporter()

    def start_span(
        self,
        name: str,
        *,
        parent: TraceContext | None = None,
        attributes: Mapping[str, Any] | None = None,
    ) -> Span:
        inherited = parent if parent is not None else current_trace_context()
        context = child_trace_context(inherited)
        return Span(
            name=_safe_name(name, "span name"),
            scope=self.scope,
            context=context,
            parent_span_id=inherited.span_id if inherited else "",
            attributes=_safe_attributes(attributes),
            exporter=self.exporter,
        )


def receipt_sha256(receipt: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    ).hexdigest()
