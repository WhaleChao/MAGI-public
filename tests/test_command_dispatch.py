"""Tests for Orchestrator._handle_command() dispatch logic."""

import pytest
from unittest.mock import patch, MagicMock

from api.help_text import HELP_ALIASES, build_help_text
from api.pipelines.message_router import quick_fixed_reply


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
        for alias in ["help", "指令", "說明", "功能", "menu", "指令清單", "你可以做什麼"]:
            result = orc._handle_command("user1", alias)
            assert "功能總覽" in result, f"alias '{alias}' did not return help text"

    def test_help_contains_sections(self):
        orc = _make_orchestrator()
        result = orc._handle_command("user1", "/help")
        assert "文件產生" in result
        assert "法扶作業" in result
        assert "視覺" in result

    def test_help_uses_shared_builder_for_command_and_quick_reply(self):
        orc = _make_orchestrator()
        command_help = orc._handle_command("user1", "/help", role="admin")
        quick_help = quick_fixed_reply(orc, "/help", role="admin")
        assert command_help == build_help_text("admin")
        assert quick_help == command_help

    def test_help_aliases_are_shared(self):
        assert "指令清單" in HELP_ALIASES
        assert "你可以做什麼" in HELP_ALIASES

    def test_help_contains_current_command_surface(self):
        result = build_help_text("admin")
        for snippet in [
            "實務見解",
            "研究爬蟲",
            "新增爬蟲目標",
            "閱卷聲請 ... 已遞委任",
            "閱卷聲請 ... 法扶",
            "模擬測試",
            "搜檔",
            "PDF 頁籤",
            "供應鏈掃描",
            "鐵穹規則",
            "同步技能到melchior",
            "指令清單",
        ]:
            assert snippet in result

    def test_non_admin_help_hides_admin_section(self):
        result = build_help_text("user")
        assert "技能進化與系統管理" not in result
        assert "`供應鏈掃描`" not in result
        assert "系統管理、技能進化、供應鏈掃描" in result


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
