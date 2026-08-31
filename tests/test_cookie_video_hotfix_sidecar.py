from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "scripts/ops/cookie_video_hotfix_sidecar.py"


def _module():
    spec = importlib.util.spec_from_file_location("cookie_video_hotfix_sidecar", MODULE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_sidecar_registers_only_public_hotfix_surfaces() -> None:
    module = _module()
    app = module.create_app()
    rules = {rule.rule for rule in app.url_map.iter_rules()}

    assert {
        "/cookie-cutter", "/api/cookie-cutter/prepare",
        "/api/cookie-cutter/generate", "/api/cookie-cutter/health",
        "/video-studio", "/api/video-studio/interpret",
        "/api/video-studio/render", "/api/video-studio/render-assets",
        "/api/video-studio/health", "/api/cookie-video-hotfix/health",
        "/exam-tutor", "/api/exam-tutor/trends",
    }.issubset(rules)
    assert not any(
        token in rule
        for rule in rules
        for token in ("drive", "laf", "transcript", "cron", "scheduler")
    )
    payload = app.test_client().get("/api/cookie-video-hotfix/health").get_json()
    assert payload == {
        "ok": True,
        "schema": "magi.cookie-video-hotfix-health/v1",
        "source_sha256": module.source_attestation()["source_sha256"],
        "file_count": 13,
        "cookie_contract": "magi.cookie-cutter-model/v2",
        "video_contract": "magi.video-autopilot-storyboard/v4",
        "host": "loopback",
        "scheduler_touched": False,
        "drive_touched": False,
        "portal_touched": False,
        "pii_included": False,
    }


def test_sidecar_bound_sources_are_regular_and_hash_complete() -> None:
    module = _module()
    attestation = module.source_attestation()
    assert attestation["schema"] == "magi.cookie-video-hotfix-source/v1"
    assert attestation["file_count"] == len(module.BOUND_FILES) == 13
    assert set(attestation["file_sha256"]) == set(module.BOUND_FILES)
    assert all(len(value) == 64 for value in attestation["file_sha256"].values())
    assert attestation["pii_included"] is False


def test_exam_tutor_hotfix_changes_only_the_example_alias() -> None:
    page = _module().create_app().test_client().get("/exam-tutor")
    assert page.status_code == 200
    assert page.headers["Cache-Control"] == "no-store"
    html = page.get_data(as_text=True)
    assert html.count("例如：Lumi") == 2
    assert "小林" not in html
