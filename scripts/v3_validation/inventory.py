from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .paths import CAPABILITY_MANIFEST_PATH, ROUTE_METHOD_REVIEW_PATH, RUNTIME_ROUTES_PATH
from .route_reviews import (
    RouteMethodKey,
    load_route_method_reviews,
    validate_reviews_against_inventory,
)
from .schema import load_json


EXPECTED_COUNTS = {"5002": 280, "5003": 67, "total": 347}
EXPECTED_FINGERPRINT = "fb615907acc4aae3e16ed254366362046bbf5747e29d0c27c2d27b5e33afaf27"


@dataclass(frozen=True, order=True)
class RouteRecord:
    service: str
    rule: str
    methods: tuple[str, ...]
    endpoint: str

    @classmethod
    def from_mapping(cls, service: str, row: dict[str, Any]) -> "RouteRecord":
        if not isinstance(row, dict):
            raise ValueError("runtime route row must be an object")
        declared_service = str(row.get("service") or service)
        if service not in {"5002", "5003"} or declared_service != service:
            raise ValueError(f"runtime route has invalid/conflicting service: {service!r} / {declared_service!r}")
        methods = tuple(sorted({str(item).upper() for item in row.get("methods", ()) if str(item).strip()}))
        if not methods or not set(methods) <= {"GET", "POST", "PUT", "PATCH", "DELETE"}:
            raise ValueError(f"runtime route has invalid methods: {row.get('methods')!r}")
        rule = str(row.get("rule") or "")
        endpoint = str(row.get("endpoint") or "")
        if not rule.startswith("/") or not endpoint:
            raise ValueError("runtime route must have an absolute rule and non-empty endpoint")
        return cls(
            service=declared_service,
            rule=rule,
            methods=methods,
            endpoint=endpoint,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "service": self.service,
            "rule": self.rule,
            "methods": list(self.methods),
            "endpoint": self.endpoint,
        }

    @property
    def signature(self) -> str:
        return f"{self.service} {'|'.join(self.methods)} {self.rule} -> {self.endpoint}"


def normalize_inventory(payload: dict[str, Any]) -> tuple[RouteRecord, ...]:
    rows: list[RouteRecord] = []
    for service, entries in (payload.get("services") or {}).items():
        if not isinstance(entries, list):
            raise ValueError(f"service {service!r} routes must be a list")
        rows.extend(RouteRecord.from_mapping(str(service), row) for row in entries)
    return tuple(sorted(rows))


def inventory_fingerprint(routes: Iterable[RouteRecord]) -> str:
    normalized = [route.as_dict() for route in sorted(routes)]
    body = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(body).hexdigest()


def _capability_ids(path: str | Path = CAPABILITY_MANIFEST_PATH) -> set[str]:
    payload = load_json(path)
    return {str(row.get("id")) for row in payload.get("capabilities", ()) if row.get("id")}


def classify_capability(route: RouteRecord) -> str:
    """Conservative V2 route-to-V3 capability coverage classification.

    The pinned inventory fingerprint makes additions fail before these ordered
    rules can silently absorb a newly introduced route.
    """

    rule = route.rule.lower()
    endpoint = route.endpoint.lower()
    text = f"{rule} {endpoint}"

    if rule in {"/callback", "/line/webhook", "/telegram/webhook"}:
        return "channels"
    if any(token in text for token in ("transcrib", "audio", "translate", "/collab/music")):
        return "audio_transcription_translation"
    if any(token in text for token in ("/pdf", "document", "draft", "quotation", "forms/", "stamp")):
        return "documents_ocr_pdf"
    if "memory" in text or rule in {"/remember", "/recall"}:
        return "memory_knowledge_search"
    if any(token in text for token in ("judgment", "legal/<", "/legal", "research/judgment")):
        return "judgments_legal_research"
    if any(token in text for token in ("/laf", "legal-aid", "debt-required")):
        return "laf_legal_aid"
    if any(token in text for token in ("accounting", "/debt", "/gcal", "calendar")):
        return "osc_accounting_debt_calendar"
    if rule.startswith("/skills") or rule.startswith("/api/skills") or "/skill-" in rule:
        return "skills_lifecycle"
    if any(token in text for token in ("/api/nerv", "codex-distributed", "golem", "/collab/chat", "/vision")):
        return "agents_models_routing"
    if any(token in text for token in ("drive", "nas", "obsidian")):
        return "drive_nas_obsidian_sync"
    health_tokens = (
        "health",
        "readyz",
        "livez",
        "self-repair",
        "system-test",
        "process-monitor",
        "iron_dome",
        "iron-dome",
        "audit_log",
    )
    if any(token in text for token in health_tokens):
        return "operations_health_security"
    if rule.startswith("/api/osc") or rule in {"/osc", "/osc/debt"}:
        return "osc_case_management"
    if route.service == "5003" or rule.startswith(("/s/", "/toolsapi/", "/static/exports", "/exports/")):
        return "public_share_external_api"
    return "admin_dashboard_menubar"


def build_coverage(
    routes: Iterable[RouteRecord],
    reviews: dict[RouteMethodKey, Any],
) -> tuple[dict[str, Any], ...]:
    """Describe explicit review coverage without inferring implementation completion."""

    def describe(route: RouteRecord) -> dict[str, Any]:
        reviewed_methods: list[dict[str, str]] = []
        unreviewed_methods: list[str] = []
        for method in route.methods:
            key = RouteMethodKey(route.service, route.rule, method, route.endpoint)
            review = reviews.get(key)
            if review is None or not review.reviewed:
                unreviewed_methods.append(method)
                continue
            reviewed_methods.append(
                {
                    "method": method,
                    "side_effect_class": review.side_effect_class,
                    "reviewed_by": review.reviewed_by,
                }
            )
        return {
            **route.as_dict(),
            "suggested_capability": classify_capability(route),
            "reviewed_methods": reviewed_methods,
            "unreviewed_methods": unreviewed_methods,
            "implementation_covered": not unreviewed_methods,
        }

    return tuple(describe(route) for route in sorted(routes))


def validate_inventory(
    payload: dict[str, Any],
    *,
    expected_counts: dict[str, int] = EXPECTED_COUNTS,
    expected_fingerprint: str = EXPECTED_FINGERPRINT,
    capability_manifest_path: str | Path = CAPABILITY_MANIFEST_PATH,
    route_review_path: str | Path = ROUTE_METHOD_REVIEW_PATH,
    route_review_supplement_path: str | Path | None = None,
) -> dict[str, Any]:
    if payload.get("schema_version") != 1:
        raise ValueError("runtime route inventory schema_version must be 1")
    routes = normalize_inventory(payload)
    signatures = [route.signature for route in routes]
    if len(signatures) != len(set(signatures)):
        duplicates = sorted({item for item in signatures if signatures.count(item) > 1})
        raise ValueError(f"duplicate runtime route signatures: {duplicates[:5]}")
    actual_counts = {
        "5002": sum(route.service == "5002" for route in routes),
        "5003": sum(route.service == "5003" for route in routes),
        "total": len(routes),
    }
    if actual_counts != expected_counts:
        raise ValueError(f"runtime route counts changed: expected {expected_counts}, got {actual_counts}")
    declared = payload.get("counts") or {}
    if {key: declared.get(key) for key in expected_counts} != expected_counts:
        raise ValueError(f"declared runtime counts do not match pinned baseline: {declared}")
    fingerprint = inventory_fingerprint(routes)
    if fingerprint != expected_fingerprint:
        raise ValueError(f"runtime route fingerprint changed: expected {expected_fingerprint}, got {fingerprint}")
    valid_capabilities = _capability_ids(capability_manifest_path)
    reviews = load_route_method_reviews(
        route_review_path,
        supplement_path=route_review_supplement_path,
        expected_inventory_fingerprint=expected_fingerprint,
    )
    validate_reviews_against_inventory(reviews, routes)
    coverage = build_coverage(routes, reviews)
    invalid = [row for row in coverage if row["suggested_capability"] not in valid_capabilities]
    if invalid:
        raise ValueError(f"routes mapped to unknown capabilities: {invalid[:3]}")
    if len(coverage) != expected_counts["total"]:
        raise ValueError("route inventory normalization is incomplete")
    route_method_total = sum(len(route.methods) for route in routes)
    reviewed_route_methods = sum(len(row["reviewed_methods"]) for row in coverage)
    fully_reviewed_routes = sum(bool(row["implementation_covered"]) for row in coverage)
    review_summary = {
        "route_inventory_total": len(routes),
        "route_method_total": route_method_total,
        "reviewed_route_methods": reviewed_route_methods,
        "unreviewed_route_methods": route_method_total - reviewed_route_methods,
        "fully_reviewed_routes": fully_reviewed_routes,
        "unreviewed_routes": len(routes) - fully_reviewed_routes,
        "implementation_coverage_complete": reviewed_route_methods == route_method_total,
    }
    return {
        "ok": review_summary["implementation_coverage_complete"],
        "inventory_valid": True,
        "implementation_coverage_complete": review_summary["implementation_coverage_complete"],
        "counts": actual_counts,
        "fingerprint": fingerprint,
        "coverage": coverage,
        "review_summary": review_summary,
    }


def load_and_validate_runtime_inventory(path: str | Path = RUNTIME_ROUTES_PATH) -> dict[str, Any]:
    return validate_inventory(load_json(path))
