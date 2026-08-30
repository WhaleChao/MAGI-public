from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

from .paths import ROUTE_METHOD_REVIEW_PATH
from .schema import ContractValidationError, load_json
from .side_effects import SIDE_EFFECT_CLASSES


ROUTE_METHOD_REVIEW_SUPPLEMENT_PATH = Path(__file__).with_name(
    "route-method-review-supplement.json"
)


@dataclass(frozen=True, order=True)
class RouteMethodKey:
    service: str
    rule: str
    method: str
    endpoint: str


@dataclass(frozen=True)
class RouteMethodReview:
    key: RouteMethodKey
    side_effect_class: str
    reviewed: bool
    reviewed_by: str
    rationale: str

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self.key), **{key: value for key, value in asdict(self).items() if key != "key"}}


def load_route_method_reviews(
    path: str | Path = ROUTE_METHOD_REVIEW_PATH,
    *,
    supplement_path: str | Path | None = None,
    expected_inventory_fingerprint: str | None = None,
) -> dict[RouteMethodKey, RouteMethodReview]:
    primary_path = Path(path).expanduser().resolve()
    payloads = [load_json(primary_path)]
    resolved_supplement: Path | None = None
    if supplement_path is not None:
        resolved_supplement = Path(supplement_path).expanduser().resolve()
    elif primary_path == Path(ROUTE_METHOD_REVIEW_PATH).resolve():
        resolved_supplement = ROUTE_METHOD_REVIEW_SUPPLEMENT_PATH.resolve()
    if resolved_supplement is not None:
        payloads.append(load_json(resolved_supplement))
    reviews: dict[RouteMethodKey, RouteMethodReview] = {}
    for payload in payloads:
        _merge_route_method_review_payload(
            payload,
            reviews,
            expected_inventory_fingerprint=expected_inventory_fingerprint,
        )
    return reviews


def _merge_route_method_review_payload(
    payload: Any,
    reviews: dict[RouteMethodKey, RouteMethodReview],
    *,
    expected_inventory_fingerprint: str | None,
) -> None:
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ContractValidationError("route-method review manifest schema_version must be 1")
    if payload.get("review_policy") != "explicit_route_method_only":
        raise ContractValidationError("route-method review manifest must use explicit_route_method_only")
    if expected_inventory_fingerprint and payload.get("inventory_fingerprint") != expected_inventory_fingerprint:
        raise ContractValidationError("route-method review manifest is not pinned to the current inventory fingerprint")
    rows = payload.get("reviews")
    if not isinstance(rows, list):
        raise ContractValidationError("route-method review manifest reviews must be an array")

    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ContractValidationError(f"route-method review {index} must be an object")
        key = RouteMethodKey(
            service=str(row.get("service") or ""),
            rule=str(row.get("rule") or ""),
            method=str(row.get("method") or "").upper(),
            endpoint=str(row.get("endpoint") or ""),
        )
        if key.service not in {"5002", "5003"} or not key.rule.startswith("/"):
            raise ContractValidationError(f"route-method review {index} has invalid service/rule")
        if key.method not in {"GET", "POST", "PUT", "PATCH", "DELETE"} or not key.endpoint:
            raise ContractValidationError(f"route-method review {index} has invalid method/endpoint")
        effect = str(row.get("side_effect_class") or "")
        reviewed = row.get("reviewed") is True
        reviewed_by = str(row.get("reviewed_by") or "").strip()
        rationale = str(row.get("rationale") or "").strip()
        if effect not in SIDE_EFFECT_CLASSES:
            raise ContractValidationError(f"route-method review {index} has unknown side-effect class")
        if reviewed and (not reviewed_by or not rationale):
            raise ContractValidationError(f"route-method review {index} lacks reviewer/rationale")
        if key in reviews:
            raise ContractValidationError(f"duplicate route-method review: {key}")
        reviews[key] = RouteMethodReview(key, effect, reviewed, reviewed_by, rationale)


def validate_reviews_against_inventory(
    reviews: dict[RouteMethodKey, RouteMethodReview],
    routes: Iterable[Any],
) -> None:
    inventory_keys = {
        RouteMethodKey(route.service, route.rule, method, route.endpoint)
        for route in routes
        for method in route.methods
    }
    unknown = sorted(set(reviews) - inventory_keys)
    if unknown:
        raise ContractValidationError(f"review manifest contains routes absent from pinned inventory: {unknown[:3]}")


def require_reviewed_route_method(
    *,
    service: str,
    rule: str,
    method: str,
    endpoint: str,
    reviews: dict[RouteMethodKey, RouteMethodReview] | None = None,
) -> RouteMethodReview:
    review_map = reviews if reviews is not None else load_route_method_reviews()
    key = RouteMethodKey(str(service), str(rule), str(method).upper(), str(endpoint))
    review = review_map.get(key)
    if review is None or not review.reviewed:
        raise ContractValidationError(
            f"route-method has no explicit completed review: {key.service} {key.method} {key.rule} -> {key.endpoint}"
        )
    return review
