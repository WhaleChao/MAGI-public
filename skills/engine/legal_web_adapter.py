# -*- coding: utf-8 -*-
"""
MAGI legal web adapter
======================

Shared engine-selection shim for legal interactive web flows.

Current policy:
- Legacy portal automation defaults to Selenium/WebDriver.
- MAGI's v2 legal portals (法扶、閱卷、筆錄) default to Playwright Chromium
  because those modules use the shared Playwright wrapper with Selenium fallback.
- When Scrapling is requested through feature flags, we record the intent and
  keep a deterministic fallback reason so modules can dual-track safely.
"""

from __future__ import annotations

import hashlib
import os
import shutil
from typing import Dict, Iterable, Tuple
from urllib.parse import urlsplit


_DEFAULT_ENGINE_BY_COMPONENT = {
    "file_review_portal": "playwright",
    "laf_portal_v2": "playwright",
    "judicial_sso_v2": "playwright",
    "judicial_transcript_v2": "playwright",
}

_SECURITY_PROFILE_BY_COMPONENT = {
    "laf_portal_v2": {
        "profile_id": "laf-portal",
        "allowed_navigation_hosts": ("lawyer.laf.org.tw",),
        "secret_names": ("LAF_USERNAME", "LAF_PASSWORD"),
    },
    "file_review_portal": {
        "profile_id": "file-review-portal",
        "allowed_navigation_hosts": (
            "portal.ezlawyer.com.tw",
            "eefile.judicial.gov.tw",
        ),
        "secret_names": ("MAGI_JUDICIAL_USERNAME", "MAGI_JUDICIAL_PASSWORD"),
    },
    "judicial_sso_v2": {
        "profile_id": "judicial-sso",
        "allowed_navigation_hosts": (
            "portal.ezlawyer.com.tw",
            "www.ezlawyer.com.tw",
            "eefile.judicial.gov.tw",
        ),
        "secret_names": ("MAGI_JUDICIAL_USERNAME", "MAGI_JUDICIAL_PASSWORD"),
    },
    "judicial_transcript_v2": {
        "profile_id": "judicial-transcript",
        "allowed_navigation_hosts": ("www.ezlawyer.com.tw",),
        "secret_names": (
            "MAGI_JUDICIAL_RECORD_USERNAME",
            "MAGI_JUDICIAL_RECORD_PASSWORD",
        ),
    },
}


def _truthy(value: str) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _normalize_engine(value: str) -> str:
    raw = (value or "").strip().lower()
    if raw in {"scrapling", "dynamicfetcher", "stealthyfetcher"}:
        return "scrapling"
    if raw in {"playwright", "pw", "chromium"}:
        return "playwright"
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
        requested = _DEFAULT_ENGINE_BY_COMPONENT.get(component, "selenium")

    security = _SECURITY_PROFILE_BY_COMPONENT.get(component, {})
    base = {
        "browser_profile_id": str(security.get("profile_id") or component),
        "allowed_navigation_hosts": ",".join(security.get("allowed_navigation_hosts") or ()),
        "allowed_secret_names": ",".join(security.get("secret_names") or ()),
        "browser_sandbox_bypass": "0",
    }

    if interactive_required and requested == "scrapling":
        return {
            **base,
            "component": component,
            "requested_engine": requested,
            "selected_engine": "selenium",
            "interactive_required": "1",
            "fallback_reason": "interactive_flow_requires_browser_automation",
        }

    return {
        **base,
        "component": component,
        "requested_engine": requested,
        "selected_engine": requested,
        "interactive_required": "1" if interactive_required else "0",
        "fallback_reason": "",
    }


def legal_web_allowed_hosts(
    profile: Dict[str, str], *, extra_urls: Iterable[str] = ()
) -> Tuple[str, ...]:
    """Resolve the top-level navigation allowlist for a legal browser profile.

    Localhost is accepted only when a caller explicitly supplies a local mock
    URL. Subresources are intentionally not filtered here because the official
    portals use third-party static assets; cookies remain origin-scoped.
    """
    hosts = {
        value.strip().lower().rstrip(".")
        for value in str(profile.get("allowed_navigation_hosts") or "").split(",")
        if value.strip()
    }
    for value in extra_urls:
        parsed = urlsplit(str(value or ""))
        host = (parsed.hostname or "").lower().rstrip(".")
        if host in {"127.0.0.1", "localhost", "::1"}:
            hosts.add(host)
    return tuple(sorted(hosts))


def preinstalled_selenium_driver_kwargs(browser: str = "chrome") -> Dict[str, object]:
    """Bind Selenium to a preinstalled driver in a sealed release.

    Development keeps Selenium Manager convenience. A production service may
    not download an executable after cutover.
    """
    if not (os.environ.get("MAGI_V3_RELEASE_MANIFEST") or "").strip():
        return {}
    normalized = str(browser or "chrome").strip().lower()
    if normalized == "edge":
        declared = (os.environ.get("MAGI_EDGEDRIVER_PATH") or "").strip()
        executable = declared or shutil.which("msedgedriver") or ""
        if not executable or not os.path.isfile(executable):
            raise RuntimeError("sealed release requires preinstalled MAGI_EDGEDRIVER_PATH")
        from selenium.webdriver.edge.service import Service

        return {"service": Service(executable)}
    declared = (os.environ.get("MAGI_CHROMEDRIVER_PATH") or "").strip()
    executable = declared or shutil.which("chromedriver") or ""
    if not executable or not os.path.isfile(executable):
        raise RuntimeError("sealed release requires preinstalled MAGI_CHROMEDRIVER_PATH")
    expected_sha256 = (os.environ.get("MAGI_CHROMEDRIVER_SHA256") or "").strip()
    if len(expected_sha256) != 64 or any(
        character not in "0123456789abcdef" for character in expected_sha256
    ):
        raise RuntimeError(
            "sealed release requires hash-bound MAGI_CHROMEDRIVER_SHA256"
        )
    digest = hashlib.sha256()
    with open(executable, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    if digest.hexdigest() != expected_sha256:
        raise RuntimeError("sealed release ChromeDriver SHA-256 mismatch")
    from selenium.webdriver.chrome.service import Service

    return {"service": Service(executable)}


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
