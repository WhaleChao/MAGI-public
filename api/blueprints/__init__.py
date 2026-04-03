"""
MAGI API Blueprints
===================
Flask Blueprints for modular API route organization.
Gradually migrated from monolithic server.py.
"""

from api.blueprints.dashboard_pages import dashboard_pages_bp
from api.blueprints.osc_accounting import osc_accounting_bp
from api.blueprints.osc_debt import osc_debt_bp
from api.blueprints.osc_settings import osc_settings_bp

__all__ = [
    "dashboard_pages_bp",
    "osc_accounting_bp",
    "osc_debt_bp",
    "osc_settings_bp",
]
