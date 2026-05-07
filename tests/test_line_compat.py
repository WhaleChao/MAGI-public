from __future__ import annotations

import pytest


def test_line_clients_disabled_when_feature_flag_off(monkeypatch):
    from api import line_compat

    monkeypatch.setenv("MAGI_ENABLE_LINE", "0")
    line_bot_api, handler, enabled, reason = line_compat.build_line_clients("token", "secret")

    assert enabled is False
    assert "disabled" in reason
    assert hasattr(handler, "add")
    assert line_bot_api is not None


def test_line_feature_enabled_accepts_truthy_values(monkeypatch):
    from api.line_compat import line_feature_enabled

    for value in ("1", "true", "TRUE", "yes", "on"):
        monkeypatch.setenv("MAGI_ENABLE_LINE", value)
        assert line_feature_enabled() is True

    for value in ("0", "false", "FALSE", "no", "off", ""):
        monkeypatch.setenv("MAGI_ENABLE_LINE", value)
        assert line_feature_enabled() is False


def test_line_clients_enabled_use_v3_compatible_surface(monkeypatch):
    from api import line_compat

    monkeypatch.setenv("MAGI_ENABLE_LINE", "1")
    if not line_compat.LINE_SDK_AVAILABLE:
        pytest.skip("line-bot-sdk is unavailable")

    line_bot_api, handler, enabled, reason = line_compat.build_line_clients("token", "secret")

    assert enabled is True
    assert reason == ""
    assert hasattr(handler, "add")
    assert hasattr(handler, "handle")
    assert hasattr(line_bot_api, "push_message")
    assert hasattr(line_bot_api, "reply_message")
    assert hasattr(line_bot_api, "get_message_content")

    text_message = line_compat.TextSendMessage(text="hello")
    image_message = line_compat.ImageSendMessage(
        originalContentUrl="https://example.com/original.png",
        previewImageUrl="https://example.com/preview.png",
    )
    assert getattr(text_message, "text", "") == "hello"
    assert getattr(image_message, "type", "") == "image"

    if getattr(line_compat, "LINE_SDK_BACKEND", "") == "v3":
        captured = {}

        def fake_push_message(request, **kwargs):
            captured["request"] = request
            captured["kwargs"] = kwargs
            return "ok"

        def fake_reply_message(request, **kwargs):
            captured["reply_request"] = request
            captured["reply_kwargs"] = kwargs
            return "ok"

        line_bot_api._messaging_api.push_message = fake_push_message
        line_bot_api._messaging_api.reply_message = fake_reply_message
        line_bot_api._blob_api.get_message_content = lambda *_args, **_kwargs: bytearray(b"abcdef")

        assert line_bot_api.push_message("U123", [text_message, image_message]) == "ok"
        assert captured["request"].to == "U123"
        assert captured["request"].messages[0].text == "hello"
        assert captured["request"].messages[1].type == "image"

        assert line_bot_api.reply_message("reply-token", text_message) == "ok"
        reply_token = getattr(captured["reply_request"], "reply_token", None) or getattr(
            captured["reply_request"], "replyToken", None
        )
        assert reply_token == "reply-token"
        assert captured["reply_request"].messages[0].text == "hello"

        stream = line_bot_api.get_message_content("mid")
        assert b"".join(stream.iter_content(2)) == b"abcdef"
