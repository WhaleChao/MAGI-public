"""Compatibility shim for legacy 26B tier routing imports.

The current deployment routes heavy requests to the local OpenAI-compatible
endpoint on the main inference port. Older code still imports `tier_router`
for readiness checks and constants.
"""

from __future__ import annotations

import json
import os
from urllib import request as _urlrequest

OLLAMA_BASE = (
    os.environ.get("MAGI_COUNCIL_OLLAMA_BASE")
    or os.environ.get("OMLX_URL")
    or "http://127.0.0.1:8080"
).rstrip("/")
OLLAMA_MODEL = os.environ.get("MAGI_COUNCIL_OLLAMA_MODEL", "gemma-4-26b-a4b-it-4bit").strip()
OLLAMA_KEEP_ALIVE = os.environ.get("MAGI_COUNCIL_OLLAMA_KEEP_ALIVE", "30m").strip()


def ensure_26b_ready(progress_fn=None) -> bool:
    """Best-effort compatibility probe for legacy heavy-tier routing."""
    if callable(progress_fn):
        try:
            progress_fn("檢查 26B 推理端點狀態...")
        except Exception:
            pass
    try:
        with _urlrequest.urlopen(f"{OLLAMA_BASE}/v1/models", timeout=5) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="ignore") or "{}")
        data = payload.get("data") or []
        if not data:
            return False
        model_ids = {str(item.get("id") or "").strip() for item in data if isinstance(item, dict)}
        return (not OLLAMA_MODEL) or (OLLAMA_MODEL in model_ids) or bool(model_ids)
    except Exception:
        return False

