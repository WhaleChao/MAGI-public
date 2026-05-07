"""Phase G tests — /vision and /shortcut/ocr OCR consensus opt-in.

Tests cover:
  1. /vision flag-off → no consensus import attempted
  2. /vision flag-on, task_type=ocr → consensus path taken, text/plain NOT used
  3. /vision captcha → consensus NEVER invoked (§4 red-line)
  4. /vision consensus failure → falls through to legacy gateway (no 500)
  5. /vision flag-on returns all required JSON keys plus additive ocr_* keys
  6. /shortcut/ocr flag-off → legacy path
  7. /shortcut/ocr flag-on → consensus text/plain response
  8. /shortcut/ocr consensus failure → falls through to legacy gateway
  9. /vision flag-on, task_type=vision (not ocr/text/scan) → no consensus
"""
from __future__ import annotations

import os
import pytest


# ── shared fixture ──────────────────────────────────────────────────────────

@pytest.fixture
def vision_client(monkeypatch, tmp_path):
    """Minimal Tools-API test client with vision-capable fake gateway."""
    import api.tools_api as tools_api

    monkeypatch.setenv("MAGI_API_KEY", "test-key")
    monkeypatch.setenv("MAGI_EXTERNAL_API_KEY", "test-key")
    import api.authz as _authz
    monkeypatch.setattr(_authz, "MAGI_API_KEY", "test-key", raising=False)
    monkeypatch.setattr(_authz, "MAGI_EXTERNAL_API_KEY", "test-key", raising=False)

    # ensure external key cache is pre-warmed so require_api_key passes
    tools_api._EXTERNAL_KEY_CACHE["ts"] = 0.0
    tools_api._EXTERNAL_KEY_CACHE["value"] = ""

    # fake image on disk
    img = tmp_path / "test.jpg"
    img.write_bytes(b"\xff\xd8\xff\xe0fake-jpeg")

    return tools_api, tools_api.app.test_client(), str(img)


# ── helper: build a fake OCRConsensusResult ─────────────────────────────────

def _fake_consensus_result(text="辨識文字", confidence=0.92, success=True, raise_exc=False):
    """Returns a callable suitable for monkeypatching run_consensus."""
    from skills.engine.ocr.ocr_schema import OCRConsensusResult, OCREntities

    def _run(image_path, task_type="legal", timeout_sec=None):
        if raise_exc:
            raise RuntimeError("consensus_engine_boom")
        if not success:
            return OCRConsensusResult(
                success=False, selected_text="", corrected_text="",
                confidence=0.0, writable=False, critical_conflict=False,
                warnings=[], provider_results=[], entities=None,
                error="fake_fail", duration_sec=0.1,
            )
        entities = OCREntities(
            case_numbers=["114原訴24"],
            roc_dates=[],
            courts=["臺灣花蓮地方法院"],
            parties=[],
            laf_case_numbers=[],
        )
        return OCRConsensusResult(
            success=True,
            selected_text=text,
            corrected_text=text + "（校正）",
            confidence=confidence,
            writable=True,
            critical_conflict=False,
            warnings=[],
            provider_results=[],
            entities=entities,
            error="",
            duration_sec=0.05,
        )
    return _run


# ── Test 1: /vision flag OFF → consensus never imported ─────────────────────

def test_vision_consensus_flag_off_uses_legacy(monkeypatch, vision_client):
    tools_api, client, img = vision_client
    monkeypatch.setattr(tools_api, "_VISION_OCR_CONSENSUS_ENABLE", False)

    consensus_called = []

    class _FakeGateway:
        def vision(self, **kwargs):
            return {"success": True, "analysis": "legacy_result", "route": "local", "model": "e4b"}

    monkeypatch.setattr(tools_api, "_INFERENCE_GATEWAY", _FakeGateway())

    # patch the module so any import of run_consensus would be detected
    import sys
    original = sys.modules.get("skills.engine.ocr.consensus")

    class _SentinelModule:
        def run_consensus(self, *a, **kw):
            consensus_called.append(True)
    sys.modules["skills.engine.ocr.consensus"] = _SentinelModule()

    try:
        resp = client.post(
            "/vision",
            json={"image_path": img, "task_type": "ocr"},
        )
    finally:
        if original is None:
            sys.modules.pop("skills.engine.ocr.consensus", None)
        else:
            sys.modules["skills.engine.ocr.consensus"] = original

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["success"] is True
    assert data["description"] == "legacy_result"
    assert "ocr_confidence" not in data, "additive ocr_* keys must not appear on legacy path"
    assert not consensus_called, "consensus must not be called when flag is off"


# ── Test 2: /vision flag ON + task_type=ocr → consensus path, JSON response ─

def test_vision_consensus_flag_on_ocr_task(monkeypatch, vision_client):
    tools_api, client, img = vision_client
    monkeypatch.setattr(tools_api, "_VISION_OCR_CONSENSUS_ENABLE", True)

    import skills.engine.ocr.consensus as _cons_mod
    monkeypatch.setattr(_cons_mod, "run_consensus", _fake_consensus_result("測試文字"))

    resp = client.post(
        "/vision",
        json={"image_path": img, "task_type": "ocr"},
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["success"] is True
    assert data["route"] == "ocr_consensus"
    assert "測試文字" in data["description"]
    assert data["degraded"] is False
    assert data["task_type"] == "vision"   # effective_task for non-captcha


def test_vision_nemotron_enable_routes_to_consensus(monkeypatch, vision_client):
    tools_api, client, img = vision_client
    monkeypatch.setattr(tools_api, "_VISION_OCR_CONSENSUS_ENABLE", False)
    monkeypatch.setenv("MAGI_NEMOTRON_PARSE_ENABLE", "1")

    import skills.engine.ocr.consensus as _cons_mod
    monkeypatch.setattr(_cons_mod, "run_consensus", _fake_consensus_result("Nemotron文字"))

    resp = client.post(
        "/vision",
        json={"image_path": img, "task_type": "ocr"},
    )

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["success"] is True
    assert data["route"] == "ocr_consensus"
    assert "Nemotron文字" in data["description"]


# ── Test 3: /vision captcha → consensus NEVER called (§4 red-line) ──────────

def test_vision_captcha_never_uses_consensus(monkeypatch, vision_client):
    tools_api, client, img = vision_client
    monkeypatch.setattr(tools_api, "_VISION_OCR_CONSENSUS_ENABLE", True)

    consensus_called = []

    import skills.engine.ocr.consensus as _cons_mod

    def _evil_consensus(image_path, task_type="legal", timeout_sec=None):
        consensus_called.append(True)
        # Should never reach here for captcha
        from skills.engine.ocr.ocr_schema import OCRConsensusResult
        return OCRConsensusResult.failure("should_not_be_called")

    monkeypatch.setattr(_cons_mod, "run_consensus", _evil_consensus)

    class _FakeGateway:
        def vision(self, **kwargs):
            return {"success": True, "analysis": "captcha_digits", "route": "local", "model": "e4b"}

    monkeypatch.setattr(tools_api, "_INFERENCE_GATEWAY", _FakeGateway())

    resp = client.post(
        "/vision",
        json={"image_path": img, "task_type": "captcha"},
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["task_type"] == "captcha"
    assert not consensus_called, "consensus MUST NOT be called for captcha"


# ── Test 4: /vision consensus raises → falls through to legacy (no 500) ─────

def test_vision_consensus_exception_falls_through(monkeypatch, vision_client):
    tools_api, client, img = vision_client
    monkeypatch.setattr(tools_api, "_VISION_OCR_CONSENSUS_ENABLE", True)

    import skills.engine.ocr.consensus as _cons_mod
    monkeypatch.setattr(_cons_mod, "run_consensus", _fake_consensus_result(raise_exc=True))

    class _FakeGateway:
        def vision(self, **kwargs):
            return {"success": True, "analysis": "legacy_fallback", "route": "local", "model": "e4b"}

    monkeypatch.setattr(tools_api, "_INFERENCE_GATEWAY", _FakeGateway())

    resp = client.post(
        "/vision",
        json={"image_path": img, "task_type": "ocr"},
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["success"] is True
    assert data["description"] == "legacy_fallback"
    assert "ocr_confidence" not in data


# ── Test 5: /vision consensus response has all required + additive keys ──────

def test_vision_consensus_response_schema(monkeypatch, vision_client):
    tools_api, client, img = vision_client
    monkeypatch.setattr(tools_api, "_VISION_OCR_CONSENSUS_ENABLE", True)

    import skills.engine.ocr.consensus as _cons_mod
    monkeypatch.setattr(_cons_mod, "run_consensus", _fake_consensus_result("文件內容", confidence=0.95))

    resp = client.post(
        "/vision",
        json={"image_path": img, "task_type": "text"},
    )
    assert resp.status_code == 200
    data = resp.get_json()

    # All existing fields must be present (backward-compat)
    for key in ("success", "sage", "image", "description", "route", "model",
                "degraded", "force_local", "task_type", "error"):
        assert key in data, f"required key '{key}' missing from response"

    # Additive keys must be present when consensus path is taken
    assert "ocr_confidence" in data
    assert "ocr_writable" in data
    assert "ocr_critical_conflict" in data
    assert "ocr_warnings" in data
    assert data["ocr_confidence"] == pytest.approx(0.95, abs=0.01)
    assert data["sage"] == "vision_gateway"


# ── Test 6: /shortcut/ocr flag OFF → legacy path ────────────────────────────

def test_shortcut_ocr_consensus_flag_off_uses_legacy(monkeypatch, vision_client):
    tools_api, client, img = vision_client
    monkeypatch.setattr(tools_api, "_SHORTCUT_OCR_CONSENSUS_ENABLE", False)

    class _FakeGateway:
        def vision(self, **kwargs):
            return {"success": True, "analysis": "legacy_ocr_text"}

    monkeypatch.setattr(tools_api, "_INFERENCE_GATEWAY", _FakeGateway())

    resp = client.post(
        "/shortcut/ocr",
        data=b"\xff\xd8\xff\xe0fake-jpeg",
        headers={"X-API-Key": "test-key", "Content-Type": "application/octet-stream"},
    )
    assert resp.status_code == 200
    assert resp.mimetype == "text/plain"
    assert resp.get_data(as_text=True) == "legacy_ocr_text"


# ── Test 7: /shortcut/ocr flag ON → consensus text/plain response ────────────

def test_shortcut_ocr_consensus_flag_on_returns_plaintext(monkeypatch, vision_client):
    tools_api, client, img = vision_client
    monkeypatch.setattr(tools_api, "_SHORTCUT_OCR_CONSENSUS_ENABLE", True)

    import skills.engine.ocr.consensus as _cons_mod
    monkeypatch.setattr(_cons_mod, "run_consensus", _fake_consensus_result("捷運站票根"))

    resp = client.post(
        "/shortcut/ocr",
        data=b"\xff\xd8\xff\xe0fake-jpeg",
        headers={"X-API-Key": "test-key", "Content-Type": "application/octet-stream"},
    )
    assert resp.status_code == 200
    assert resp.mimetype == "text/plain"
    text = resp.get_data(as_text=True)
    # Should contain corrected_text ("捷運站票根（校正）") from fake result
    assert "捷運站票根" in text


# ── Test 8: /shortcut/ocr consensus fails → legacy gateway used ──────────────

def test_shortcut_ocr_consensus_failure_falls_through(monkeypatch, vision_client):
    tools_api, client, img = vision_client
    monkeypatch.setattr(tools_api, "_SHORTCUT_OCR_CONSENSUS_ENABLE", True)

    import skills.engine.ocr.consensus as _cons_mod
    monkeypatch.setattr(_cons_mod, "run_consensus", _fake_consensus_result(raise_exc=True))

    class _FakeGateway:
        def vision(self, **kwargs):
            return {"success": True, "analysis": "legacy_fallback_shortcut"}

    monkeypatch.setattr(tools_api, "_INFERENCE_GATEWAY", _FakeGateway())

    resp = client.post(
        "/shortcut/ocr",
        data=b"\xff\xd8\xff\xe0fake-jpeg",
        headers={"X-API-Key": "test-key", "Content-Type": "application/octet-stream"},
    )
    assert resp.status_code == 200
    assert resp.mimetype == "text/plain"
    assert resp.get_data(as_text=True) == "legacy_fallback_shortcut"


# ── Test 9: /vision flag ON but task_type=vision (not ocr/text/scan) → no consensus ──

def test_vision_consensus_not_triggered_for_non_ocr_task(monkeypatch, vision_client):
    tools_api, client, img = vision_client
    monkeypatch.setattr(tools_api, "_VISION_OCR_CONSENSUS_ENABLE", True)

    consensus_called = []

    import skills.engine.ocr.consensus as _cons_mod

    def _track_consensus(image_path, task_type="legal", timeout_sec=None):
        consensus_called.append(True)
        from skills.engine.ocr.ocr_schema import OCRConsensusResult
        return OCRConsensusResult.failure("should_not_be_called")

    monkeypatch.setattr(_cons_mod, "run_consensus", _track_consensus)

    class _FakeGateway:
        def vision(self, **kwargs):
            return {"success": True, "analysis": "scene_description", "route": "local", "model": "e4b"}

    monkeypatch.setattr(tools_api, "_INFERENCE_GATEWAY", _FakeGateway())

    resp = client.post(
        "/vision",
        json={"image_path": img, "task_type": "vision"},
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["description"] == "scene_description"
    assert not consensus_called, "consensus must not be called for generic vision task"
