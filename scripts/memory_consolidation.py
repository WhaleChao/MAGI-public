"""
MAGI Memory Consolidation — STUBBED (2026-04-20)
=================================================

This module used to consolidate OpenClaw session logs into the vector
memory store. OpenClaw Gateway has been removed from MAGI v2 (Phase 0 of
the 2026-04-20 cleanup plan), so the data source no longer exists.

Active callers:
  - scripts/nightly_council.py    (nightly 03:00 report)
  - scripts/casper_night_patrol.py (nightly patrol)

Both callers import `run_consolidation` and tolerate string return values.
This stub keeps them green (no exception) and returns a no-op message.

If/when a replacement data source is introduced (e.g. Discord / Telegram
chat log export), restore the consolidation logic here and keep the same
`run_consolidation()` signature so callers do not need to change.
"""

import logging

logger = logging.getLogger("MemoryConsolidation")

MEMORY_CATEGORIES = [
    "user_preferences",
    "task_learned",
    "important_facts",
    "context_notes",
    "decisions_made",
]


def run_consolidation(*_args, **_kwargs) -> str:
    """
    No-op stub. OpenClaw session logs no longer exist; consolidation is
    disabled. Returns a human-readable string for nightly reports.
    """
    logger.info(
        "memory_consolidation: skipped (OpenClaw session source removed 2026-04-20)"
    )
    return "記憶歸檔已停用（OpenClaw 資料源移除，等待替代資料來源）"


if __name__ == "__main__":
    print(run_consolidation())
