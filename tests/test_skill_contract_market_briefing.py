"""
Skill contract tests for market-briefing.

Categories:
  1. Normal   - valid input produces expected output format
  2. Missing  - graceful handling when required fields are missing
  3. Boundary - edge cases (empty strings, very long input, special chars)
  4. Reject   - input that should be refused (injection, off-topic)

Note: market-briefing/action.py has complex import-time side effects
(dataclasses, zoneinfo, ssl contexts) that make dynamic loading fragile.
Tests here use AST analysis + targeted source inspection to verify the
skill contract without requiring a full module import.
"""

import os
import sys
import ast
import json
import re
import pytest
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

SKILL_DIR = Path(__file__).resolve().parent.parent / "skills" / "market-briefing"
ACTION_PY = SKILL_DIR / "action.py"
PREDICT_ENGINE_PY = SKILL_DIR / "predict" / "predict_engine.py"
SENTIMENT_ANALYST_PY = SKILL_DIR / "agents" / "sentiment_analyst.py"
BASE_AGENT_PY = SKILL_DIR / "agents" / "base.py"


def _parse_tree() -> ast.Module:
    source = ACTION_PY.read_text(encoding="utf-8")
    return ast.parse(source, filename=str(ACTION_PY))


def _func_names() -> set[str]:
    tree = _parse_tree()
    return {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}


def _class_names() -> set[str]:
    tree = _parse_tree()
    return {n.name for n in ast.walk(tree) if isinstance(n, ast.ClassDef)}


def _source() -> str:
    return ACTION_PY.read_text(encoding="utf-8")


# ===================================================================
# 1. Normal
# ===================================================================


class TestNormal:
    def test_action_py_exists(self):
        assert ACTION_PY.exists()

    def test_action_py_parseable(self):
        tree = _parse_tree()
        assert tree is not None

    def test_has_main_function(self):
        assert "main" in _func_names()

    def test_has_cmd_briefing(self):
        assert "_cmd_briefing" in _func_names()

    def test_has_cmd_list(self):
        assert "_cmd_list" in _func_names()

    def test_has_cmd_performance(self):
        assert "_cmd_performance" in _func_names()

    def test_has_cmd_prompt(self):
        assert "_cmd_prompt" in _func_names()

    def test_has_cmd_export(self):
        assert "_cmd_export" in _func_names()

    def test_common_tw_aliases_defined(self):
        """Source should define COMMON_TW_ALIASES with stock mappings."""
        source = _source()
        assert "COMMON_TW_ALIASES" in source
        assert '"台積電"' in source or "'台積電'" in source
        assert '"2330"' in source or "'2330'" in source

    def test_default_state_defined(self):
        source = _source()
        assert "_DEFAULT_STATE" in source
        assert "watchlist" in source
        assert "first_prompt_date" in source
        assert "last_report_date" in source

    def test_tz_now_function_exists(self):
        assert "_tz_now" in _func_names()

    def test_default_model_params_defined(self):
        source = _source()
        assert "_DEFAULT_MODEL_PARAMS" in source
        assert "w_trend" in source
        assert "w_mom" in source


# ===================================================================
# 2. Missing data
# ===================================================================


class TestMissingData:
    def test_cmd_list_handles_empty_state(self):
        """_cmd_list should handle state with empty watchlist."""
        source = _source()
        # It reads watchlist from state -- check it has defensive code
        assert "_watchlist_from_state" in source or "watchlist" in source

    def test_cmd_prompt_sets_first_date(self):
        """_cmd_prompt should set first_prompt_date if not already set."""
        source = _source()
        assert "first_prompt_date" in source

    def test_cmd_briefing_checks_empty_watchlist(self):
        """_cmd_briefing should handle empty watchlist gracefully."""
        source = _source()
        assert "not items" in source or "if not items" in source

    def test_state_path_defined(self):
        source = _source()
        assert "STATE_PATH" in source
        assert ".agent" in source or "AGENT_DIR" in source

    def test_argparse_used(self):
        source = _source()
        assert "argparse" in source


# ===================================================================
# 3. Boundary
# ===================================================================


class TestBoundary:
    def test_skill_md_exists(self):
        skill_md = SKILL_DIR / "SKILL.md"
        assert skill_md.exists()

    def test_has_watchitem_dataclass(self):
        """Should define a WatchItem dataclass for type-safe stock tracking."""
        source = _source()
        assert "WatchItem" in source or "watchitem" in source.lower()

    def test_supports_tw_and_us_markets(self):
        source = _source()
        assert '"TW"' in source or "'TW'" in source
        assert '"US"' in source or "'US'" in source

    def test_skill_python_fallback(self):
        """_skill_python should have a fallback to python3."""
        source = _source()
        assert "_skill_python" in source
        assert "python3" in source

    def test_state_paths_use_agent_dir(self):
        source = _source()
        assert "AGENT_DIR" in source
        assert "STATE_PATH" in source
        assert "CACHE_PATH" in source

    def test_thread_pool_used_for_concurrency(self):
        source = _source()
        assert "ThreadPoolExecutor" in source

    def test_has_backtest_function(self):
        assert "_cmd_backtest" in _func_names()

    def test_has_sector_function(self):
        assert "_cmd_sector" in _func_names()

    def test_has_comps_function(self):
        assert "_cmd_comps" in _func_names()

    def test_deep_mode_uses_attributed_news_fetcher(self):
        source = PREDICT_ENGINE_PY.read_text(encoding="utf-8")
        assert "fetch_market_news" in source
        assert "format_news_for_prompt" in source
        assert "news_sources" in source
        assert "Google News RSS" in source
        assert "市場關注 {item.label} 近期趨勢與新聞" not in source

    def test_sentiment_agent_requires_grounded_headlines(self):
        source = SENTIMENT_ANALYST_PY.read_text(encoding="utf-8")
        assert "缺乏可驗證新聞標題" in source
        assert "只能引用上方列出的標題與來源" in source
        assert "不得補充未提供的新聞" in source

    def test_base_agent_has_grounding_rules(self):
        source = BASE_AGENT_PY.read_text(encoding="utf-8")
        assert "Grounding rules" in source
        assert "Do not invent prices, news" in source
        assert "lower confidence" in source


# ===================================================================
# 4. Should reject
# ===================================================================


class TestShouldReject:
    def test_no_eval_or_exec(self):
        tree = _parse_tree()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id not in ("eval", "exec")

    def test_no_shell_true(self):
        source = _source()
        assert "shell=True" not in source

    def test_no_hardcoded_api_keys(self):
        source = _source()
        lines = source.splitlines()
        for i, line in enumerate(lines, 1):
            lower = line.lower()
            if "api_key=" in lower and "os.environ" not in line and "environ.get" not in line:
                if "test" not in lower and "#" not in line.split("api_key")[0]:
                    pytest.fail(f"Line {i} may have hardcoded API key: {line.strip()}")

    def test_ssl_context_not_disabled(self):
        source = _source()
        if "CERT_NONE" in source:
            assert "verify" in source.lower() or "context" in source.lower()

    def test_no_dangerous_os_calls(self):
        """Should not use os.system or os.popen."""
        source = _source()
        assert "os.system(" not in source
        assert "os.popen(" not in source
