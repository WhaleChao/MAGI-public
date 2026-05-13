# -*- coding: utf-8 -*-
"""
MAGI legal web adapter
======================

Shared engine-selection shim for legal interactive web flows.

Current policy:
- Default stays on Selenium/WebDriver for interactive portal automation.
- When Scrapling is requested through feature flags, we record the intent and
  keep a deterministic fallback reason so modules can dual-track safely.
"""

from __future__ import annotations

import os
from typing import Dict


def _truthy(value: str) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _normalize_engine(value: str) -> str:
    raw = (value or "").strip().lower()
    if raw in {"scrapling", "dynamicfetcher", "stealthyfetcher"}:
        return "scrapling"
    if raw in {"selenium", "webdriver", "chrome", "edge"}:
        return "selenium"
    return ""


def _env_name(prefix: str, component: str) -> str:
    token = "".join(ch if ch.isalnum() else "_" for ch in (component or "").upper())
    while "__" in token:
        token = token.replace("__", "_")
    return f"{prefix}_{token}"


def resolve_legal_web_engine(component: str, *, interactive_required: bool = True) -> Dict[str, str]:
    requested = (
        _normalize_engine(os.environ.get(_env_name("MAGI_WEB_ENGINE", component), ""))
        or _normalize_engine(os.environ.get("MAGI_LEGAL_WEB_ENGINE", ""))
    )
    if not requested and _truthy(os.environ.get("MAGI_USE_SCRAPLING", "")):
        requested = "scrapling"
    if not requested:
        requested = "selenium"

    if interactive_required and requested == "scrapling":
        return {
            "component": component,
            "requested_engine": requested,
            "selected_engine": "selenium",
            "interactive_required": "1",
            "fallback_reason": "interactive_flow_requires_browser_automation",
        }

    return {
        "component": component,
        "requested_engine": requested,
        "selected_engine": requested,
        "interactive_required": "1" if interactive_required else "0",
        "fallback_reason": "",
    }


def format_legal_web_engine_log(profile: Dict[str, str]) -> str:
    component = profile.get("component", "unknown")
    selected = profile.get("selected_engine", "selenium")
    requested = profile.get("requested_engine", selected)
    if requested != selected:
        return (
            f"[engine] {component}: requested={requested}, selected={selected}, "
            f"reason={profile.get('fallback_reason', '') or 'fallback'}"
        )
    return f"[engine] {component}: selected={selected}"
