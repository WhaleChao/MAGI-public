from __future__ import annotations

import threading

from flask import Flask, g


class _FakeOrchestrator:
    def __init__(self):
        self.user_history = {}
        self._history_summaries = {}
        self._history_summaries_lock = threading.Lock()
        self.notification_callback = None

    def get_active_heavy_tasks(self):
        return []

    def _brain_runtime_banner(self):
        return "MAGI"

    def _call_with_timeout(self, fn, *_args, **_kwargs):
        return fn()


def test_chat_pipeline_passes_heavy_flag_from_request_context(monkeypatch):
    """General chat must keep @heavy alive after message_pipeline strips the prefix."""
    from api.pipelines.chat_pipeline import handle_chat_async
    from skills.bridge import grounded_ai

    captured = {}

    def _fake_chat_casper(message, conversation_history="", heavy=False):
        captured["message"] = message
        captured["heavy"] = heavy
        captured["history"] = conversation_history
        return "heavy ok"

    monkeypatch.setenv("MAGI_CHAT_ASYNC", "0")
    monkeypatch.setattr(grounded_ai, "chat_casper", _fake_chat_casper)

    app = Flask(__name__)
    with app.app_context():
        g.heavy_opt_in = True
        reply = handle_chat_async(_FakeOrchestrator(), "u1", "請深度分析民法第184條", platform_hint="WEB")

    assert reply == "MAGI\nheavy ok"
    assert captured["message"] == "請深度分析民法第184條"
    assert captured["heavy"] is True


def test_chat_pipeline_defaults_to_non_heavy_without_flag(monkeypatch):
    from api.pipelines.chat_pipeline import handle_chat_async
    from skills.bridge import grounded_ai

    captured = {}

    def _fake_chat_casper(message, conversation_history="", heavy=False):
        captured["heavy"] = heavy
        return "normal ok"

    monkeypatch.setenv("MAGI_CHAT_ASYNC", "0")
    monkeypatch.setattr(grounded_ai, "chat_casper", _fake_chat_casper)

    app = Flask(__name__)
    with app.app_context():
        reply = handle_chat_async(_FakeOrchestrator(), "u1", "一般問題", platform_hint="WEB")

    assert reply == "MAGI\nnormal ok"
    assert captured["heavy"] is False
