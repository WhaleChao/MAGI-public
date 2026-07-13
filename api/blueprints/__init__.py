"""
MAGI API Blueprints
===================
Flask Blueprints for modular API route organization.
Gradually migrated from monolithic server.py.
"""

from __future__ import annotations

import importlib

from api.blueprints.dashboard_pages import dashboard_pages_bp
from api.blueprints.admin_runtime import create_admin_runtime_blueprint
from api.blueprints.osc_accounting import osc_accounting_bp
from api.blueprints.osc_debt import osc_debt_bp
from api.blueprints.osc_settings import osc_settings_bp
from api.blueprints.web_runtime import create_web_runtime_blueprint

_LAZY_SUBMODULES = {
    "admin_runtime",
    "dashboard_pages",
    "osc_accounting",
    "osc_cases",
    "osc_debt",
    "osc_files",
    "osc_gcal",
    "osc_pdf",
    "osc_settings",
    "web_runtime",
}


def __getattr__(name: str):
    if name in _LAZY_SUBMODULES:
        module = importlib.import_module(f"{__name__}.{name}")
        globals()[name] = module
        return module
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "dashboard_pages_bp",
    "create_admin_runtime_blueprint",
    "osc_accounting_bp",
    "osc_debt_bp",
    "osc_settings_bp",
    "create_web_runtime_blueprint",
    *_LAZY_SUBMODULES,
]
