"""MAGI API package."""

from __future__ import annotations

import importlib

_LAZY_SUBMODULES = {
    "blueprints",
    "case_path_mapper",
    "db_helper",
    "osc",
    "pipelines",
    "poa_chat_handler",
    "server",
    "verification",
}


def __getattr__(name: str):
    if name in _LAZY_SUBMODULES:
        module = importlib.import_module(f"{__name__}.{name}")
        globals()[name] = module
        return module
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = sorted(_LAZY_SUBMODULES)
