"""Tests for Orchestrator._handle_command() dispatch logic."""

import pytest
from unittest.mock import patch, MagicMock


def _make_orchestrator():
    """Create a minimal Orchestrator instance without full __init__."""
    with patch("api.orchestrator.ThreadPoolExecutor"), \
         patch("api.orchestrator.switch_brain_mode"), \
         patch("api.orchestrator.get_brain_status"):
        from api.orchestrator import Orchestrator
        orc = object.__new__(Orchestrator)
        orc._history = {}
        orc._profile_facts = {}
        orc._callbacks = []
        orc._bg_task_pool = MagicMock()
        orc._route_traces = {}
        return orc


# ── Help command ─────────────────────────────────────────────


class TestHandleCommandHelp:
    def test_help_returns_help_text(self):
        orc = _make_orchestrator()
        result = orc._handle_command("user1", "/help")
        assert "MAGI" in result
        assert "功能總覽" in result

    def test_help_aliases(self):
        orc = _make_orchestrator()
        for alias in ["help", "指令", "說明", "功能", "menu"]:
            result = orc._handle_command("user1", alias)
            assert "功能總覽" in result, f"alias '{alias}' did not return help text"

    def test_help_contains_sections(self):
        orc = _make_orchestrator()
        result = orc._handle_command("user1", "/help")
        assert "文件產生" in result
        assert "法扶作業" in result
        assert "視覺" in result


# ── Draw command ─────────────────────────────────────────────


class TestHandleCommandDraw:
    def test_draw_command_calls_generate_image(self):
        orc = _make_orchestrator()
        orc._generate_image = MagicMock(return_value="img_result")
        result = orc._handle_command("user1", "/draw a cute cat")
        assert orc._generate_image.called

    def test_draw_empty_prompt_asks_for_description(self):
        orc = _make_orchestrator()
        result = orc._handle_command("user1", "/draw")
        assert "描述" in result or "請" in result
