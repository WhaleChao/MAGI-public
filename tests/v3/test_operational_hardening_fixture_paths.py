from __future__ import annotations

from pathlib import Path
import os

from api.platforms.safe_process import parse_cron_command
from scripts.ops import audit_operational_hardening as hardening
from scripts.v3_validation import schedule_nonstorage_fixture_matrix as matrix


def test_audit_fixture_quotes_release_paths_with_spaces(monkeypatch, tmp_path):
    release = tmp_path / "Application Support" / "MAGI" / "release"
    monkeypatch.setattr(matrix, "ROOT", release)

    fixture = matrix._audit_input(1)
    command = fixture["cron_jobs"][0]["command"]
    argv = parse_cron_command(command)

    assert argv == [
        str(release / "venv/bin/python3"),
        str(release / "scripts/magi_doctor.py"),
        "--json",
    ]


def test_hardening_fixture_provider_quotes_laf_scanner_path(monkeypatch, tmp_path):
    release = tmp_path / "Application Support" / "MAGI" / "release"
    monkeypatch.setattr(hardening, "ROOT", release)
    provider = hardening._FixtureExternalProvider([])

    job = provider.cron_jobs()[0]
    argv = parse_cron_command(job["command"])

    assert argv == [
        str(release / "venv/bin/python3"),
        str(release / "scripts/ops/laf_gmail_dispatch_scan.py"),
        "--apply",
        "--json-out",
        "fixture.json",
    ]


def test_current_critical_paths_have_no_pass_only_exception_handlers():
    """A red hardening audit must not be caused by an invisible critical error."""
    report = hardening.audit_silent_exception_handlers()

    assert report["critical_count"] == 0


def _write_degraded_profile(tmp_path: Path, profile: str, model_dir: str, stamp: str) -> None:
    home = tmp_path / "home"
    omlx = home / ".omlx"
    (omlx / "models-text" / model_dir).mkdir(parents=True)
    (omlx / "active_profile").write_text(profile, encoding="utf-8")
    (omlx / stamp).write_text("", encoding="utf-8")


def test_hardening_accepts_fresh_bounded_night_e4b_fallback(monkeypatch, tmp_path):
    _write_degraded_profile(
        tmp_path, "night-e4b-degraded", "gemma-4-e4b-it-4bit", "night_fallback_stamp"
    )
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    monkeypatch.setattr(hardening, "expected_omlx_profile_now", lambda _now: ("night", "26b"))
    monkeypatch.setattr(
        hardening,
        "_current_omlx_models",
        lambda port: ["gemma-4-e4b-it-4bit"] if port == 8080 else [],
    )

    report = hardening.audit_omlx_profile()

    assert report["ok"] is True
    assert report["degraded"] is True
    assert report["fallback_keyword"] == "e4b"
    assert report["fallback_retry_seconds"] == 21600
    assert report["sidecars_ok"] is True
    assert "will retry" in report["remediation"]


def test_hardening_rejects_stale_or_topology_mismatched_fallback(monkeypatch, tmp_path):
    _write_degraded_profile(
        tmp_path, "night-e4b-degraded", "gemma-4-e4b-it-4bit", "night_fallback_stamp"
    )
    home = tmp_path / "home"
    stamp = home / ".omlx" / "night_fallback_stamp"
    stale = stamp.stat().st_mtime - 21601
    os.utime(stamp, (stale, stale))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(hardening, "expected_omlx_profile_now", lambda _now: ("night", "26b"))
    monkeypatch.setattr(
        hardening,
        "_current_omlx_models",
        lambda port: ["gemma-4-e4b-it-4bit"] if port in {8080, 8082} else [],
    )

    report = hardening.audit_omlx_profile()

    assert report["ok"] is False
    assert report["degraded"] is False
    assert report["sidecars_ok"] is False
