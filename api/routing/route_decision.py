from __future__ import annotations


def build_route_decision(
    *,
    action: str,
    matched: str,
    requires_admin: bool = False,
    handler: str = "",
    confidence: float = 1.0,
    reason: str = "",
    candidates: list[dict] | None = None,
    intent: str = "",
) -> dict:
    payload = {
        "success": True,
        "matched": matched,
        "action": action,
        "requires_admin": bool(requires_admin),
        "handler": handler,
        "confidence": float(confidence),
        "reason": reason or matched,
        "candidates": list(candidates or []),
    }
    if intent:
        payload["intent"] = intent
    return payload
