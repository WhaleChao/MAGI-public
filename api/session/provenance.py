from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, Union


_LOW_TRUST_TYPES = {
    "assistant_generated",
    "summary_derived",
    "generated_summary",
    "llm_summary",
    "chatlog",
}

_HIGH_TRUST_TYPES = {
    "manual",
    "user_rule",
    "user_profile",
    "user_confirmed",
    "verified",
    "official",
    "statute",
    "judicial_api",
    "case_statutes",
    "legal_crawler_judgment",
    "legal_crawler_news",
}


def _to_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return bool(default)
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on", "y"}


def _to_confidence(value: Any, default: float = 0.0) -> float:
    try:
        conf = float(value)
    except Exception:
        return max(0.0, min(1.0, float(default)))
    return max(0.0, min(1.0, conf))


def _normalize_source_type(source_type: str) -> str:
    raw = str(source_type or "").strip().lower()
    if not raw:
        return "unknown"
    if raw.startswith("user_profile_"):
        return "user_profile"
    if raw.startswith("user_chat_"):
        return "user_chat"
    return raw


@dataclass()
class MemoryProvenance:
    raw_source: str
    source_type: str = "unknown"
    verified: bool = False
    confidence: float = 0.0
    derived_from: str = ""
    role: str = ""
    source_id: str = ""
    metadata: dict[str, str] = field(default_factory=dict)

    @property
    def trust_label(self) -> str:
        if self.verified:
            return "已驗證"
        if self.source_type in {"chatlog", "user_chat"} and self.role == "user":
            return "原始對話"
        if self.source_type in _LOW_TRUST_TYPES or self.derived_from:
            return "衍生線索"
        return "未驗證"

    def as_dict(self) -> dict[str, Any]:
        return {
            "raw_source": self.raw_source,
            "source_type": self.source_type,
            "verified": self.verified,
            "confidence": self.confidence,
            "derived_from": self.derived_from,
            "role": self.role,
            "source_id": self.source_id,
            "metadata": dict(self.metadata),
            "trust_label": self.trust_label,
        }


def default_confidence_for_source(source_type: str, *, verified: bool = False, role: str = "") -> float:
    normalized = _normalize_source_type(source_type)
    if verified or normalized in _HIGH_TRUST_TYPES:
        return 0.98
    if normalized in {"chatlog", "user_chat"}:
        return 0.82 if role == "user" else 0.18
    if normalized in _LOW_TRUST_TYPES:
        return 0.12
    if normalized.startswith("user_"):
        return 0.88
    if "crawler" in normalized or "research" in normalized or "web" in normalized or "news" in normalized:
        return 0.72
    return 0.55


def parse_source_provenance(source: str) -> MemoryProvenance:
    raw_source = str(source or "").strip()
    if not raw_source:
        return MemoryProvenance(raw_source="", source_type="unknown", confidence=0.0)

    tokens = [token for token in raw_source.split("|") if token]
    base = tokens[0] if tokens else "unknown"
    metadata: dict[str, str] = {}
    extras: list[str] = []
    for token in tokens[1:]:
        if "=" in token:
            key, value = token.split("=", 1)
            metadata[key.strip().lower()] = value.strip()
        else:
            extras.append(token.strip())

    source_type = _normalize_source_type(
        metadata.get("source_type")
        or metadata.get("kind")
        or base
    )
    role = metadata.get("role", "")
    if not role:
        if source_type == "user_chat":
            role = "user"
        elif source_type == "chatlog" and metadata.get("user"):
            role = "user"
        elif source_type == "chatlog" and metadata.get("assistant"):
            role = "assistant"
    derived_from = metadata.get("derived_from") or metadata.get("derived") or metadata.get("drv") or ""
    source_id = metadata.get("source_id") or metadata.get("id") or metadata.get("key") or ""
    explicit_verified = metadata.get("verified")
    if explicit_verified is None:
        verified = source_type in _HIGH_TRUST_TYPES
    else:
        verified = _to_bool(explicit_verified)
    confidence = _to_confidence(
        metadata.get("confidence", metadata.get("conf")),
        default=default_confidence_for_source(source_type, verified=verified, role=role),
    )
    if derived_from and source_type not in _HIGH_TRUST_TYPES:
        confidence = min(confidence, 0.35)
    if source_type in {"assistant_generated", "summary_derived", "generated_summary", "llm_summary"}:
        verified = False
        confidence = min(confidence, 0.20)
    if source_type == "chatlog" and role == "assistant":
        verified = False
        confidence = min(confidence, 0.18)
    if source_type in {"chatlog", "user_chat"} and role == "user" and explicit_verified is None:
        verified = False
        confidence = max(confidence, 0.82)
    if extras:
        metadata["extras"] = ",".join(extras)
    return MemoryProvenance(
        raw_source=raw_source,
        source_type=source_type,
        verified=verified,
        confidence=confidence,
        derived_from=derived_from,
        role=role,
        source_id=source_id,
        metadata=metadata,
    )


def build_source_signature(
    source: str,
    *,
    source_type: str = "",
    verified: Optional[bool] = None,
    confidence: Optional[float] = None,
    derived_from: str = "",
    role: str = "",
    source_id: str = "",
    metadata: Optional[dict[str, Any]] = None,
) -> str:
    raw_source = str(source or "").strip()
    parts = [token for token in raw_source.split("|") if token]
    base = parts[0] if parts else (source_type or "unknown")
    kv: dict[str, str] = {}
    extras: list[str] = []
    for token in parts[1:]:
        if "=" in token:
            key, value = token.split("=", 1)
            kv[key.strip().lower()] = value.strip()
        else:
            extras.append(token.strip())

    if source_type:
        base = source_type
    if verified is not None:
        kv["verified"] = "1" if verified else "0"
    if confidence is not None:
        kv["conf"] = f"{_to_confidence(confidence):.2f}"
    if derived_from:
        kv["derived_from"] = str(derived_from).strip()
    if role:
        kv["role"] = str(role).strip()
    if source_id:
        kv["source_id"] = str(source_id).strip()
    for key, value in (metadata or {}).items():
        if value is None or str(key).strip().lower() in {"verified", "conf", "confidence"}:
            continue
        kv[str(key).strip().lower()] = str(value).strip()

    ordered = [base]
    ordered.extend(token for token in extras if token)
    for key in sorted(kv):
        value = kv[key]
        if value:
            ordered.append(f"{key}={value}")
    return "|".join(ordered)[:250]


def render_provenance_badge(source: str) -> str:
    prov = parse_source_provenance(source)
    return f"{prov.trust_label}｜信心 {prov.confidence:.2f}"
