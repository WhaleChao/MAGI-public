"""Legacy compatibility shim for Iron Dome.

Canonical implementation lives in `skills/iron-dome/core.py` and is exposed
via `skills.iron_dome.core`.

This module remains import-compatible for legacy callers that still reference:
`skills.bridge.iron_dome`.
"""

from __future__ import annotations

from skills.iron_dome import core as _core

# Re-export canonical Iron Dome API for backward compatibility.
for _name in dir(_core):
    if _name.startswith("__"):
        continue
    globals()[_name] = getattr(_core, _name)

# Backward-compatible alias for old constant name.
if "DANGEROUS_COMMAND_PATTERNS" not in globals():
    globals()["DANGEROUS_COMMAND_PATTERNS"] = list(globals().get("DESTRUCTIVE_PATTERNS", []))

if "PROMPT_INJECTION_PATTERNS" not in globals() and "STATIC_RULE_SETS" in globals():
    globals()["PROMPT_INJECTION_PATTERNS"] = list((globals()["STATIC_RULE_SETS"] or {}).get("PROMPT_INJECTION", []))


__all__ = [k for k in globals().keys() if not k.startswith("_")]


def _main() -> int:
    import json
    import sys

    text = " ".join(sys.argv[1:]).strip()
    if not text:
        print("Usage: python -m skills.bridge.iron_dome <text>")
        return 0

    ok, msg = is_safe(text)
    print(json.dumps({"safe": ok, "message": msg}, ensure_ascii=False))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(_main())
