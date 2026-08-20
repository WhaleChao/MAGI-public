"""Pipeline package with lazy submodule access for tests and monkeypatching."""

from __future__ import annotations

import importlib

_LAZY_SUBMODULES = {
    "attachment_pipeline",
    "chat_pipeline",
    "command_dispatch",
    "command_pipeline",
    "fuzzy_match",
    "message_pipeline",
    "message_router",
    "skill_dispatch",
    "skill_listing",
    "specialized_commands",
}


def __getattr__(name: str):
    if name in _LAZY_SUBMODULES:
        module = importlib.import_module(f"{__name__}.{name}")
        globals()[name] = module
        return module
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = sorted(_LAZY_SUBMODULES)
