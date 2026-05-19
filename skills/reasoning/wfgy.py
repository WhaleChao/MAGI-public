"""
Retired WFGY compatibility shim.

The previous implementation wrapped prompts with a seven-step reasoning scaffold
and explicitly asked models to output their thought process. That is unsafe for
MAGI production use because it can leak chain-of-thought scaffolding into user
answers, summaries, and distillation data.

Keep this module only so legacy imports do not crash. It must not modify prompts.
"""

from __future__ import annotations


def apply_wfgy_logic(query: str) -> str:
    """Return the original prompt unchanged; WFGY is retired."""
    return str(query or "")
