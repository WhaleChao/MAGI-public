"""Offline-only MAGI V3 compatibility validation helpers.

This package deliberately contains no HTTP, database, NAS, browser, or channel
clients.  It is safe to import in unit tests and inventory/replay CI jobs.
"""

from .adapter_spec import LegacyResponse, adapt_legacy_response, assert_legacy_shape
from .fixtures import anonymize_fixture, validate_replay_fixture
from .inventory import load_and_validate_runtime_inventory
from .live_validation import plan_sha256, validate_live_plan, validate_live_report, validate_live_report_against_plan
from .side_effects import SideEffectDecision, evaluate_side_effect

__all__ = [
    "LegacyResponse",
    "SideEffectDecision",
    "adapt_legacy_response",
    "anonymize_fixture",
    "assert_legacy_shape",
    "evaluate_side_effect",
    "load_and_validate_runtime_inventory",
    "plan_sha256",
    "validate_live_plan",
    "validate_live_report",
    "validate_live_report_against_plan",
    "validate_replay_fixture",
]
