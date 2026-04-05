"""Centralized memory write policy.

Decides whether a piece of content is allowed to enter long-term memory
based on its source type, confidence, and provenance metadata.

Usage::

    from api.session.memory_policy import evaluate_memory_write

    decision = evaluate_memory_write(content, source, metadata)
    if decision.allowed:
        remember(content, source=source, metadata=metadata)
    else:
        logger.info("Memory write blocked: %s", decision.reason)
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any

from api.session.provenance import (
    MemoryProvenance,
    _HIGH_TRUST_TYPES,
    _LOW_TRUST_TYPES,
    parse_source_provenance,
)


@dataclass(slots=True, frozen=True)
class MemoryWriteDecision:
    allowed: bool
    reason: str
    effective_source_type: str
    effective_confidence: float
    expires_seconds: int | None = None


# --- Blocklist: content that must NEVER enter long-term memory ---

_BLOCKED_SOURCE_TYPES = frozenset({
    "timeout_fallback",
    "degraded_response",
    "synthetic_fallback",
})

_SUMMARY_DERIVED_TYPES = frozenset({
    "summary_derived",
    "generated_summary",
    "llm_summary",
    "rolling_summary",
})

# --- Thresholds (env-overridable) ---

_MIN_CONFIDENCE_FOR_PERSIST = float(
    os.environ.get("MAGI_MEMORY_MIN_CONFIDENCE", "0.40")
)

_MIN_CONTENT_LENGTH = int(
    os.environ.get("MAGI_MEMORY_MIN_CONTENT_LEN", "4")
)

_MAX_CONTENT_LENGTH = int(
    os.environ.get("MAGI_MEMORY_MAX_CONTENT_LEN", "5000")
)

# How long summary-derived context can live (seconds); 0 = block entirely
_SUMMARY_TTL_SECONDS = int(
    os.environ.get("MAGI_MEMORY_SUMMARY_TTL_SEC", "0")
)

# Assistant chatlog capture (default off)
_ALLOW_ASSISTANT_CHATLOG = os.environ.get(
    "MAGI_CAPTURE_ASSISTANT_CHATLOG", ""
).strip().lower() in {"1", "true", "yes"}


def evaluate_memory_write(
    content: str,
    source: str = "",
    metadata: dict[str, Any] | None = None,
) -> MemoryWriteDecision:
    """Evaluate whether *content* should be stored in long-term memory.

    Returns a ``MemoryWriteDecision`` with ``allowed=True/False`` and a
    human-readable ``reason``.
    """
    text = (content or "").strip()
    meta = metadata or {}

    # --- 1. Parse provenance ---
    prov = _resolve_provenance(source, meta)

    # --- 2. Content length ---
    if len(text) < _MIN_CONTENT_LENGTH:
        return _deny(prov, f"內容太短（{len(text)} < {_MIN_CONTENT_LENGTH}）")

    if len(text) > _MAX_CONTENT_LENGTH:
        return _deny(prov, f"內容超長（{len(text)} > {_MAX_CONTENT_LENGTH}）")

    # --- 3. Blocked source types ---
    if prov.source_type in _BLOCKED_SOURCE_TYPES:
        return _deny(prov, f"來源類型被禁止：{prov.source_type}")

    # --- 4. Summary-derived: block or set TTL ---
    if prov.source_type in _SUMMARY_DERIVED_TYPES:
        if _SUMMARY_TTL_SECONDS <= 0:
            return _deny(prov, "摘要衍生內容不允許進入長期記憶")
        return _allow(prov, "摘要衍生（短期）", expires_seconds=_SUMMARY_TTL_SECONDS)

    # --- 5. Assistant-generated content ---
    if prov.source_type == "assistant_generated":
        return _deny(prov, "assistant 自己產生的內容不應進入長期記憶")

    if prov.source_type == "chatlog" and prov.role == "assistant":
        if not _ALLOW_ASSISTANT_CHATLOG:
            return _deny(prov, "assistant chatlog 預設不記錄（MAGI_CAPTURE_ASSISTANT_CHATLOG=0）")
        # Even when allowed, cap confidence
        capped_conf = min(prov.confidence, 0.25)
        return _allow(
            prov,
            "assistant chatlog（信心已壓低）",
            override_confidence=capped_conf,
        )

    # --- 6. Derived-from check ---
    if prov.derived_from and prov.source_type not in _HIGH_TRUST_TYPES:
        if prov.confidence < _MIN_CONFIDENCE_FOR_PERSIST:
            return _deny(
                prov,
                f"衍生內容信心不足（{prov.confidence:.2f} < {_MIN_CONFIDENCE_FOR_PERSIST}）",
            )

    # --- 7. General confidence floor ---
    if prov.confidence < _MIN_CONFIDENCE_FOR_PERSIST and prov.source_type not in _HIGH_TRUST_TYPES:
        return _deny(
            prov,
            f"信心分數過低（{prov.confidence:.2f} < {_MIN_CONFIDENCE_FOR_PERSIST}）",
        )

    # --- 8. Sensitive content check ---
    if _looks_like_prompt_leak(text):
        return _deny(prov, "疑似 prompt leak / system instruction")

    # --- PASS ---
    return _allow(prov, "通過所有檢查")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_PROMPT_LEAK_PATTERNS = re.compile(
    r"(you are a|你是一個|system prompt|system instruction|"
    r"<\|system\|>|<\|im_start\|>system)",
    re.IGNORECASE,
)


def _looks_like_prompt_leak(text: str) -> bool:
    if len(text) > 200 and _PROMPT_LEAK_PATTERNS.search(text[:500]):
        return True
    return False


def _resolve_provenance(source: str, meta: dict[str, Any]) -> MemoryProvenance:
    """Build provenance from source string + metadata dict."""
    prov = parse_source_provenance(source)
    # Overlay explicit metadata fields if provided
    if "source_type" in meta:
        from api.session.provenance import _normalize_source_type
        prov.source_type = _normalize_source_type(meta["source_type"])
    if "verified" in meta:
        prov.verified = bool(meta["verified"])
    if "confidence" in meta:
        try:
            prov.confidence = max(0.0, min(1.0, float(meta["confidence"])))
        except (ValueError, TypeError):
            pass
    if "derived_from" in meta:
        prov.derived_from = str(meta["derived_from"])
    if "role" in meta:
        prov.role = str(meta["role"])
    return prov


def _allow(
    prov: MemoryProvenance,
    reason: str,
    *,
    expires_seconds: int | None = None,
    override_confidence: float | None = None,
) -> MemoryWriteDecision:
    conf = override_confidence if override_confidence is not None else prov.confidence
    return MemoryWriteDecision(
        allowed=True,
        reason=reason,
        effective_source_type=prov.source_type,
        effective_confidence=conf,
        expires_seconds=expires_seconds,
    )


def _deny(prov: MemoryProvenance, reason: str) -> MemoryWriteDecision:
    return MemoryWriteDecision(
        allowed=False,
        reason=reason,
        effective_source_type=prov.source_type,
        effective_confidence=prov.confidence,
    )
