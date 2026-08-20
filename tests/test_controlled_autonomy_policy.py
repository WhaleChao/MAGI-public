from __future__ import annotations

from skills.engine.react_engine import ReActEngine
from skills.engine.tool_registry import TOOLS, get_compact_tools


def _engine() -> ReActEngine:
    return ReActEngine(tools=TOOLS, llm_fn=lambda _messages: "FINAL: unused")


def test_compact_agent_tools_never_expose_persistent_memory_write():
    tools = get_compact_tools("請記住我希望下週改到下午開會")

    assert "remember" not in tools
    assert TOOLS["remember"]["side_effect"] == "reversible_write"


def test_react_blocks_persistent_tool_even_if_manually_injected():
    reason = _engine()._iron_dome_check(
        "remember",
        {"content": "一筆會改變長期記憶的規則"},
    )

    assert "requires a confirmed MAGI workflow" in str(reason)


def test_react_allows_read_only_skill_and_blocks_persistent_skill_task():
    engine = _engine()

    assert engine._iron_dome_check(
        "run_skill",
        {
            "skill_name": "judicial-web-search",
            "task": "search",
            "params": "{}",
        },
    ) is None
    blocked = engine._iron_dome_check(
        "run_skill",
        {
            "skill_name": "interpreter-empirical-classifier",
            "task": "fetch_and_classify",
            "params": "{}",
        },
    )
    assert "requires a confirmed MAGI workflow" in str(blocked)


def test_react_blocks_pdf_rewrite_but_allows_filename_proposal():
    engine = _engine()

    assert engine._iron_dome_check(
        "run_skill",
        {"skill_name": "pdf-namer", "task": "propose", "params": "{}"},
    ) is None
    blocked = engine._iron_dome_check(
        "run_skill",
        {"skill_name": "pdf-bookmarker", "task": "run", "params": "{}"},
    )
    assert "requires a confirmed MAGI workflow" in str(blocked)

