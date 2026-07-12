"""Compatibility wrapper for the canonical LAF command parser.

The LAF natural-language parser used to exist in both ``api.handlers`` and
``casper_ecosystem``.  Keeping two copies caused state/status drift.  Runtime
code should import from ``api.handlers.laf_handler``; this wrapper keeps older
tests and imports working while ensuring there is only one rule implementation.
"""

from api.handlers import laf_handler as _canonical

laf_report_command_help = _canonical.laf_report_command_help
detect_laf_report_action = _canonical.detect_laf_report_action
parse_laf_report_payload = _canonical.parse_laf_report_payload
parse_laf_status_update = _canonical.parse_laf_status_update
_clean_client_name = _canonical._clean_client_name
_expand_reason_keywords = _canonical._expand_reason_keywords
_STATUS_MAP = _canonical._STATUS_MAP
_CASE_REASON_ALIASES = _canonical._CASE_REASON_ALIASES

__all__ = [
    "laf_report_command_help",
    "detect_laf_report_action",
    "parse_laf_report_payload",
    "parse_laf_status_update",
    "_clean_client_name",
    "_expand_reason_keywords",
    "_STATUS_MAP",
    "_CASE_REASON_ALIASES",
]
