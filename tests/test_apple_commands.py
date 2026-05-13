# -*- coding: utf-8 -*-
"""Tests for api/commands/apple_commands.py — Apple command registry integration."""

from unittest.mock import patch, MagicMock

import pytest

from api.command_registry import CommandRegistry, CommandContext


def _make_ctx(message: str, user_id: str = "test_user") -> CommandContext:
    return CommandContext(
        user_id=user_id,
        message=message,
        msg_lower=message.lower(),
        role="user",
        platform="LINE",
        orchestrator=MagicMock(),
    )


class TestAppleCommandsRegistration:
    def test_registers_commands(self):
        from api.commands.apple_commands import register_apple_commands
        registry = CommandRegistry()
        register_apple_commands(registry)
        assert len(registry._commands) == 3

    def test_trial_command_dispatches(self):
        from api.commands.apple_commands import register_apple_commands
        registry = CommandRegistry()
        register_apple_commands(registry)

        ctx = _make_ctx("!開庭 113勞訴19 2026-05-01 09:30")
        with patch("skills.apple.eventkit_bridge.create_trial_events", return_value=["開庭事件"]):
            result = registry.dispatch(ctx)
        assert result is not None
        assert "開庭" in result

    def test_trial_command_bad_format(self):
        from api.commands.apple_commands import register_apple_commands
        registry = CommandRegistry()
        register_apple_commands(registry)

        ctx = _make_ctx("!開庭")
        result = registry.dispatch(ctx)
        assert result is not None
        assert "格式" in result

    def test_spotlight_search_dispatches(self):
        from api.commands.apple_commands import register_apple_commands
        registry = CommandRegistry()
        register_apple_commands(registry)

        ctx = _make_ctx("搜檔 113勞訴19")
        with patch("skills.ops.spotlight_search.spotlight_search_case", return_value=[
            {"name": "test.pdf", "path": "/tmp/test.pdf", "size": 12345, "modified": ""}
        ]), patch("skills.ops.spotlight_search.is_exact_query", return_value=True), \
             patch("os.path.isdir", return_value=False):
            result = registry.dispatch(ctx)
        assert result is not None
        assert "test.pdf" in result

    def test_spotlight_search_no_results(self):
        from api.commands.apple_commands import register_apple_commands
        registry = CommandRegistry()
        register_apple_commands(registry)

        ctx = _make_ctx("搜檔 不存在的東西")
        with patch("skills.ops.spotlight_search.spotlight_search", return_value=[]), \
             patch("skills.ops.spotlight_search.is_exact_query", return_value=False), \
             patch("os.path.isdir", return_value=False):
            result = registry.dispatch(ctx)
        assert result is not None
        assert "未找到" in result

    def test_notify_test_dispatches(self):
        from api.commands.apple_commands import register_apple_commands
        registry = CommandRegistry()
        register_apple_commands(registry)

        ctx = _make_ctx("通知測試")
        with patch("skills.ops.macos_notify.send_notification", return_value=True), \
             patch("skills.ops.macos_notify.HAS_TERMINAL_NOTIFIER", False):
            result = registry.dispatch(ctx)
        assert result is not None
        assert "通知" in result

    def test_unrelated_message_not_dispatched(self):
        from api.commands.apple_commands import register_apple_commands
        registry = CommandRegistry()
        register_apple_commands(registry)

        ctx = _make_ctx("幫我查一下最新的民法修正案")
        result = registry.dispatch(ctx)
        assert result is None  # Not handled by Apple commands
