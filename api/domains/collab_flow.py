"""
Collaboration health status extracted from Orchestrator.

All functions accept `orch` (the Orchestrator instance) instead of `self`.
"""
from __future__ import annotations

import logging

from api.model_config import TEXT_PRIMARY_MODEL

logger = logging.getLogger("Orchestrator")


def get_collaboration_status(orch) -> str:
    """Cross-node collaboration health summary (Melchior / Balthasar / Watcher)."""
    lines = ["🤝 **協作鏈路健康度**"]

    try:
        from skills.bridge.melchior_client import check_health as melchior_health
        mh = melchior_health()
        if mh.get("online"):
            models = mh.get("models") or []
            has_main20 = any(TEXT_PRIMARY_MODEL.lower() in str(m).lower() for m in models)
            lines.append(
                f"🟢 Melchior: {mh.get('mode', 'unknown')} / v{mh.get('ollama_version', 'n/a')} / "
                f"Main20B={'yes' if has_main20 else 'no'}"
            )
        else:
            lines.append("🔴 Melchior: offline")
    except Exception as e:
        lines.append(f"🟡 Melchior: status unavailable ({e})")

    try:
        from skills.bridge.balthasar_bridge import check_health as balthasar_health
        ok, msg = balthasar_health()
        if ok:
            lines.append(f"🟢 Balthasar: {msg}")
        else:
            if "council-only" in str(msg).lower():
                lines.append("🟣 Balthasar: council-only (proxy on Casper for summarize/transcribe)")
            else:
                lines.append(f"🔴 Balthasar: {msg}")
    except Exception as e:
        lines.append(f"🟡 Balthasar: status unavailable ({e})")

    try:
        from skills.bridge.watcher_bridge import check_health as watcher_health
        ok, msg = watcher_health()
        lines.append(f"{'🟢' if ok else '🔴'} Watcher: {msg}")
    except Exception as e:
        lines.append(f"🟡 Watcher: status unavailable ({e})")

    try:
        from skills.apple.apple_intelligence import VISION_AVAILABLE
        if VISION_AVAILABLE:
            lines.append("🟢 OCR: macOS Vision (零 GPU)")
        else:
            lines.append("🟡 OCR: macOS Vision 不可用（PyObjC 未安裝）")
    except Exception:
        lines.append("🟡 OCR: macOS Vision 不可用")

    return "\n".join(lines)
