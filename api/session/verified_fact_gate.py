from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


_ALLOWED_PATHS = frozenset({"user_confirmed", "tri_sage_consensus", "file_evidence"})
_AUDIT_PATH = Path(__file__).resolve().parents[2] / ".runtime" / "verified_fact_audit.jsonl"
_REFLEXIVE_MARKERS = (
    "我上次說",
    "我之前講",
    "我剛剛說",
    "你上次說",
    "你之前說",
    "先前提過",
    "之前提過",
    "what did i say",
    "what you said",
    "earlier you said",
)


def is_reflexive_query(text: str) -> bool:
    q = str(text or "").strip().lower()
    if not q:
        return False
    return any(marker in q for marker in _REFLEXIVE_MARKERS)


def promote_to_verified(
    utterance: str,
    path: str,
    audit_reason: str,
    *,
    file_path: str = "",
    metadata: Optional[dict] = None,
) -> bool:
    if os.environ.get("MAGI_VERIFIED_FACT_GATE", "1").strip().lower() in {"0", "false", "no", "off"}:
        return False

    content = str(utterance or "").strip()
    route = str(path or "").strip()
    if not content or route not in _ALLOWED_PATHS:
        return False

    audit = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "path": route,
        "audit_reason": str(audit_reason or "").strip(),
        "file_path": str(file_path or "").strip(),
        "content": content[:1000],
        "metadata": dict(metadata or {}),
    }
    os.makedirs(_AUDIT_PATH.parent, exist_ok=True)
    with _AUDIT_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(audit, ensure_ascii=False) + "\n")

    from skills.memory.mem_bridge import remember

    src = "verified_fact|path={}|ts={}".format(route, audit["ts"])
    meta = {
        "source_type": "verified_fact",
        "verified": True,
        "confidence": 0.85,
        "audit_reason": audit["audit_reason"],
        "file_path": audit["file_path"],
        "namespace": "verified_facts",
    }
    if metadata:
        meta.update(dict(metadata))
    return bool(remember(content, source=src, metadata=meta))
