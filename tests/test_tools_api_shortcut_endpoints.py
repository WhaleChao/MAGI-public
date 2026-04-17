"""Tests for /shortcut/* thin wrapper endpoints (Apple Shortcut integration)."""
from __future__ import annotations

import pytest

from api.hooks import HookBus
from api.permissions import (
    PermissionEnforcer,
    PermissionMode,
    PermissionPolicy,
)


@pytest.fixture
def shortcut_client(monkeypatch, tmp_path):
    import api.tools_api as tools_api

    hook_bus = HookBus(source="test.tools_api.shortcut")
    hook_bus.add_jsonl_sink(tmp_path / "events.jsonl")
    monkeypatch.setattr(tools_api, "_TOOLS_HOOK_BUS", hook_bus)
    monkeypatch.setattr(tools_api, "_TOOLS_EVENTS_PATH", str(tmp_path / "events.jsonl"))
    monkeypatch.setattr(
        tools_api,
        "_TOOLS_PERMISSION_ENFORCER",
        PermissionEnforcer(
            policy=PermissionPolicy.from_rules([], mode=PermissionMode.PERMISSIVE)
        ),
    )
    monkeypatch.setenv("MAGI_API_KEY", "test-key")
    monkeypatch.setenv("MAGI_EXTERNAL_API_KEY", "test-key")
    import api.authz as _authz
    monkeypatch.setattr(_authz, "MAGI_API_KEY", "test-key", raising=False)
    monkeypatch.setattr(_authz, "MAGI_EXTERNAL_API_KEY", "test-key", raising=False)
    tools_api._EXTERNAL_KEY_CACHE["ts"] = 0.0
    tools_api._EXTERNAL_KEY_CACHE["value"] = ""

    return tools_api, tools_api.app.test_client()


def test_shortcut_ocr_rejects_missing_api_key(shortcut_client):
    _tools_api, client = shortcut_client
    resp = client.post("/shortcut/ocr", data=b"\xff\xd8\xff\xe0fake")
    assert resp.status_code in (401, 403)


def test_shortcut_ocr_rejects_empty_body(shortcut_client):
    _tools_api, client = shortcut_client
    resp = client.post(
        "/shortcut/ocr",
        data=b"",
        headers={"X-API-Key": "test-key", "Content-Type": "application/octet-stream"},
    )
    assert resp.status_code == 400
    assert resp.mimetype == "text/plain"
    assert "empty_body" in resp.get_data(as_text=True)


def test_shortcut_ocr_returns_plaintext_on_success(monkeypatch, shortcut_client):
    tools_api, client = shortcut_client

    class _FakeGateway:
        def vision(self, **kwargs):
            return {"success": True, "analysis": "這是一段 OCR 出來的文字"}

    monkeypatch.setattr(tools_api, "_INFERENCE_GATEWAY", _FakeGateway())

    resp = client.post(
        "/shortcut/ocr",
        data=b"\xff\xd8\xff\xe0\x00\x10JFIFfake-jpeg",
        headers={"X-API-Key": "test-key", "Content-Type": "application/octet-stream"},
    )
    assert resp.status_code == 200
    assert resp.mimetype == "text/plain"
    assert resp.get_data(as_text=True) == "這是一段 OCR 出來的文字"


def test_shortcut_pdf_text_rejects_non_pdf(shortcut_client):
    _tools_api, client = shortcut_client
    resp = client.post(
        "/shortcut/pdf_text",
        data=b"not a pdf",
        headers={"X-API-Key": "test-key"},
    )
    assert resp.status_code == 400
    assert "not_a_pdf" in resp.get_data(as_text=True)


def test_shortcut_pdf_text_returns_plaintext(monkeypatch, shortcut_client):
    tools_api, client = shortcut_client
    from skills.engine.document_reader import DocumentResult

    def _fake_read_document(*_args, **_kwargs):
        return DocumentResult(success=True, text="第一頁文字\n第二頁文字", method="markitdown")

    import skills.engine.document_reader as dr
    monkeypatch.setattr(dr, "read_document", _fake_read_document)

    resp = client.post(
        "/shortcut/pdf_text",
        data=b"%PDF-1.4\n%fake",
        headers={"X-API-Key": "test-key"},
    )
    assert resp.status_code == 200
    assert resp.mimetype == "text/plain"
    assert resp.get_data(as_text=True) == "第一頁文字\n第二頁文字"


def test_shortcut_summarize_returns_plaintext(monkeypatch, shortcut_client):
    tools_api, client = shortcut_client

    monkeypatch.setattr(tools_api, "summarize_text", lambda text: "重點一；重點二。")

    def _passthrough(fn, _wait, *args, **_kw):
        return True, fn(*args)

    monkeypatch.setattr(tools_api, "_run_with_timeout", _passthrough)

    resp = client.post(
        "/shortcut/summarize",
        data="這是一段要被摘要的很長的中文文字。".encode("utf-8"),
        headers={"X-API-Key": "test-key", "Content-Type": "text/plain; charset=utf-8"},
    )
    assert resp.status_code == 200
    assert resp.mimetype == "text/plain"
    assert resp.get_data(as_text=True) == "重點一；重點二。"


def test_shortcut_summarize_rejects_empty(shortcut_client):
    _tools_api, client = shortcut_client
    resp = client.post(
        "/shortcut/summarize",
        data=b"",
        headers={"X-API-Key": "test-key"},
    )
    assert resp.status_code == 400


def test_shortcut_transcribe_returns_plaintext(monkeypatch, shortcut_client):
    tools_api, client = shortcut_client

    import skills.bridge.tri_sage_collab as tsc
    monkeypatch.setattr(
        tsc, "transcribe_audio", lambda _path: {"success": True, "text": "今天開庭的筆錄內容。"}
    )

    def _passthrough(fn, _wait, *args, **_kw):
        return True, fn(*args)

    monkeypatch.setattr(tools_api, "_run_with_timeout", _passthrough)

    resp = client.post(
        "/shortcut/transcribe",
        data=b"FORM\x00\x00\x01\x00fake-audio",
        headers={"X-API-Key": "test-key", "Content-Type": "application/octet-stream"},
    )
    assert resp.status_code == 200
    assert resp.mimetype == "text/plain"
    assert resp.get_data(as_text=True) == "今天開庭的筆錄內容。"


def test_shortcut_transcribe_handles_failure(monkeypatch, shortcut_client):
    tools_api, client = shortcut_client

    import skills.bridge.tri_sage_collab as tsc
    monkeypatch.setattr(
        tsc, "transcribe_audio", lambda _path: {"success": False, "error": "whisper_crash"}
    )

    def _passthrough(fn, _wait, *args, **_kw):
        return True, fn(*args)

    monkeypatch.setattr(tools_api, "_run_with_timeout", _passthrough)

    resp = client.post(
        "/shortcut/transcribe",
        data=b"fake-audio-bytes",
        headers={"X-API-Key": "test-key"},
    )
    assert resp.status_code == 502
    assert "whisper_crash" in resp.get_data(as_text=True)


def test_shortcut_ocr_payload_too_large(monkeypatch, shortcut_client):
    tools_api, client = shortcut_client
    monkeypatch.setattr(tools_api, "_SHORTCUT_OCR_MAX_BYTES", 10)
    resp = client.post(
        "/shortcut/ocr",
        data=b"\xff\xd8\xff\xe0\x00\x10JFIFmuch-longer-than-10-bytes",
        headers={"X-API-Key": "test-key"},
    )
    assert resp.status_code == 400
    assert "payload_too_large" in resp.get_data(as_text=True)
