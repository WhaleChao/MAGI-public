from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from skills.engine import playwright_wrapper as browser
from skills.engine.legal_web_adapter import (
    legal_web_allowed_hosts,
    preinstalled_selenium_driver_kwargs,
    resolve_legal_web_engine,
)


ROOT = Path(__file__).resolve().parents[1]


def test_production_browser_sources_never_disable_chromium_sandbox() -> None:
    offenders = []
    for base in (ROOT / "skills", ROOT / "casper_ecosystem"):
        for path in base.rglob("*.py"):
            if "manual_assets" in path.parts:
                continue
            if "--no-sandbox" in path.read_text(encoding="utf-8", errors="replace"):
                offenders.append(str(path.relative_to(ROOT)))
    assert offenders == []


def test_legal_portals_have_dedicated_profiles_and_navigation_allowlists() -> None:
    laf = resolve_legal_web_engine("laf_portal_v2")
    review = resolve_legal_web_engine("file_review_portal")
    transcript = resolve_legal_web_engine("judicial_transcript_v2")

    assert laf["browser_profile_id"] == "laf-portal"
    assert review["browser_profile_id"] == "file-review-portal"
    assert transcript["browser_profile_id"] == "judicial-transcript"
    assert laf["browser_sandbox_bypass"] == "0"
    assert legal_web_allowed_hosts(laf) == ("lawyer.laf.org.tw",)
    assert "eefile.judicial.gov.tw" in legal_web_allowed_hosts(review)
    assert legal_web_allowed_hosts(
        laf, extra_urls=("http://127.0.0.1:18080/lafcsp/",)
    ) == ("127.0.0.1", "lawyer.laf.org.tw")


def test_playwright_wrapper_rejects_undeclared_top_level_host() -> None:
    wrapped = browser.PlaywrightDriverWrapper.__new__(browser.PlaywrightDriverWrapper)
    wrapped._allowed_navigation_hosts = frozenset({"lawyer.laf.org.tw"})

    wrapped._assert_navigation_allowed("https://lawyer.laf.org.tw/lafcsp/")
    wrapped._assert_navigation_allowed("about:blank")
    with pytest.raises(browser.NavigationPolicyError):
        wrapped._assert_navigation_allowed("https://example.invalid/credential-capture")


def test_sealed_release_blocks_playwright_runtime_install(monkeypatch) -> None:
    monkeypatch.setenv("MAGI_V3_RELEASE_MANIFEST", "/sealed/release/release-manifest.json")

    def forbidden_run(*args, **kwargs):
        raise AssertionError("subprocess install must not run in a sealed release")

    monkeypatch.setattr(browser.subprocess, "run", forbidden_run)
    result = browser._run_playwright_chromium_install()
    assert result["ok"] is False
    assert result["blocked"] is True
    assert result["reason"] == "runtime_install_forbidden_in_sealed_release"


def test_browser_health_does_not_auto_install_by_default(monkeypatch) -> None:
    monkeypatch.delenv("MAGI_PLAYWRIGHT_AUTO_INSTALL", raising=False)
    monkeypatch.delenv("MAGI_V3_RELEASE_MANIFEST", raising=False)
    monkeypatch.setattr(
        browser,
        "playwright_chromium_health",
        lambda **kwargs: {"ok": False, "reason": "chromium_not_installed"},
    )
    monkeypatch.setattr(
        browser,
        "_run_playwright_chromium_install",
        lambda: (_ for _ in ()).throw(AssertionError("implicit runtime install")),
    )

    result = browser.ensure_playwright_chromium()
    assert result["ok"] is False
    assert result["install_attempted"] is False


def test_sealed_selenium_driver_requires_exact_preinstalled_hash(
    tmp_path: Path, monkeypatch
) -> None:
    from selenium.webdriver.chrome import service as chrome_service

    class InertService:
        def __init__(self, path: str) -> None:
            self.path = path

    monkeypatch.setattr(chrome_service, "Service", InertService)
    driver = tmp_path / "chromedriver"
    driver.write_bytes(b"sealed-driver-fixture\n")
    driver.chmod(0o755)
    digest = hashlib.sha256(driver.read_bytes()).hexdigest()
    monkeypatch.setenv("MAGI_V3_RELEASE_MANIFEST", "/sealed/release/release-manifest.json")
    monkeypatch.setenv("MAGI_CHROMEDRIVER_PATH", str(driver))
    monkeypatch.setenv("MAGI_CHROMEDRIVER_SHA256", digest)

    service = preinstalled_selenium_driver_kwargs()["service"]
    assert service.path == str(driver)

    monkeypatch.setenv("MAGI_CHROMEDRIVER_SHA256", "0" * 64)
    with pytest.raises(RuntimeError, match="SHA-256 mismatch"):
        preinstalled_selenium_driver_kwargs()
