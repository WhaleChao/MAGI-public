"""Privacy-safe lawyer contact values for generated legal documents."""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from api.osc.case_defaults import default_case_lawyer, is_demo_lawyer


CONTACT_PLACEHOLDERS = (
    "LAWYER_NAME",
    "LAWYER_ADDRESS",
    "LAWYER_PHONE",
    "LAWYER_MOBILE",
)

_PAYLOAD_KEYS = {
    "LAWYER_NAME": (
        "LAWYER_NAME",
        "lawyer_name",
        "default_lawyer",
        "lawyer",
        "受任律師",
    ),
    "LAWYER_ADDRESS": (
        "LAWYER_ADDRESS",
        "lawyer_address",
        "law_firm_address",
        "law_firm_address_line",
        "office_address",
    ),
    "LAWYER_PHONE": (
        "LAWYER_PHONE",
        "lawyer_phone",
        "law_firm_phone",
        "office_phone",
    ),
    "LAWYER_MOBILE": (
        "LAWYER_MOBILE",
        "lawyer_mobile",
        "law_firm_mobile",
        "office_mobile",
    ),
}
_ENV_KEYS = {
    # The debt lawyer name is resolved through ``default_case_lawyer`` below so
    # OSC and generated documents share the same settings/environment order.
    "LAWYER_NAME": ("MAGI_LAF_DEFAULT_LAWYER_NAME",),
    "LAWYER_ADDRESS": (
        "MAGI_PUBLIC_LAWYER_ADDRESS",
        "MAGI_LAW_FIRM_ADDRESS",
    ),
    "LAWYER_PHONE": (
        "MAGI_PUBLIC_LAWYER_PHONE",
        "MAGI_LAW_FIRM_PHONE",
    ),
    "LAWYER_MOBILE": (
        "MAGI_PUBLIC_LAWYER_MOBILE",
        "MAGI_LAW_FIRM_MOBILE",
    ),
}
_PROFILE_ATTRS = {
    "LAWYER_NAME": "lawyer_name",
    "LAWYER_ADDRESS": "address_line",
    "LAWYER_PHONE": "phone",
    "LAWYER_MOBILE": "mobile",
}
_MISSING_LABELS = {
    "LAWYER_NAME": "請填律師姓名",
    "LAWYER_ADDRESS": "請填律師地址",
    "LAWYER_PHONE": "請填律師電話",
    "LAWYER_MOBILE": "請填律師手機",
}
_PUBLIC_SEED_VALUES = {
    "",
    "受任律師",
    "待確認",
    "事務所名稱",
    "事務所地址",
    "事務所電話",
    "範例法律事務所",
    "範例事務所地址",
}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _usable(field: str, value: Any) -> str:
    text = _text(value)
    if text in _PUBLIC_SEED_VALUES or text.startswith("範例"):
        return ""
    if field == "LAWYER_NAME" and is_demo_lawyer(text):
        return ""
    return text


def _first(field: str, mapping: Mapping[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = _usable(field, mapping.get(key))
        if value:
            return value
    return ""


def _requested_fields(requested_fields: Iterable[str] | None) -> tuple[str, ...]:
    if requested_fields is None:
        return CONTACT_PLACEHOLDERS
    fields = tuple(dict.fromkeys(str(field or "").strip() for field in requested_fields))
    invalid = [field for field in fields if field not in CONTACT_PLACEHOLDERS]
    if invalid:
        raise ValueError(f"unknown lawyer contact fields: {', '.join(invalid)}")
    return fields


def resolve_lawyer_contact(
    payload: Mapping[str, Any] | None = None,
    *,
    requested_fields: Iterable[str] | None = None,
    settings_getter: Callable[[str, str], Any] | None = None,
) -> dict[str, str]:
    """Resolve document placeholders without embedding private template defaults.

    Precedence is explicit payload, the shared OSC consumer-debt lawyer
    settings/environment contract, contact environment, then the existing
    law-firm profile. Any absent or public synthetic seed becomes an
    unmistakable human-fill marker instead of personal data. ``requested_fields``
    prevents callers such as the application generator from loading unused
    profile fields.
    """

    data = payload or {}
    fields = _requested_fields(requested_fields)
    resolved = {
        field: _first(field, data, _PAYLOAD_KEYS[field])
        for field in fields
    }
    if "LAWYER_NAME" in resolved and not resolved["LAWYER_NAME"]:
        resolved["LAWYER_NAME"] = _usable(
            "LAWYER_NAME",
            default_case_lawyer(
                case_type="消費者債務清理",
                settings_getter=settings_getter,
                env=os.environ,
            ),
        )
    for field in fields:
        if not resolved[field]:
            resolved[field] = _first(field, os.environ, _ENV_KEYS[field])

    if any(not resolved[field] for field in fields):
        try:
            from api.laf_branch_profiles import get_law_firm_profile

            profile = get_law_firm_profile()
        except Exception:
            profile = None
        if profile is not None:
            for field in fields:
                if not resolved[field]:
                    resolved[field] = _usable(
                        field,
                        getattr(profile, _PROFILE_ATTRS[field], "")
                    )
    return {
        field: resolved[field] or _MISSING_LABELS[field]
        for field in fields
    }


__all__ = ["CONTACT_PLACEHOLDERS", "resolve_lawyer_contact"]
