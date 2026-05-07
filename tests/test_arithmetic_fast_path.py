from __future__ import annotations

from api.pipelines.message_pipeline import (
    _extract_arithmetic_expression,
    _try_arithmetic_tool_fast_path,
)


def test_extracts_live_failed_prompt_expression():
    assert _extract_arithmetic_expression("37*19+11 等於多少？請用工具算，不要心算。") == "37*19+11"


def test_arithmetic_fast_path_uses_calculate_tool():
    reply = _try_arithmetic_tool_fast_path("37*19+11 等於多少？請用工具算，不要心算。")

    assert "37*19+11 = 714" in reply
    assert "使用工具：calculate" in reply
    assert "734" not in reply
    assert "곱하기" not in reply
    assert "要不要記" not in reply


def test_arithmetic_fast_path_handles_chinese_operator_symbols():
    reply = _try_arithmetic_tool_fast_path("請幫我算 12×3＋4 等於多少")

    assert "12*3+4 = 40" in reply


def test_arithmetic_fast_path_normalizes_multiplication_symbol():
    reply = _try_arithmetic_tool_fast_path("請幫我算 12×3+4 等於多少")

    assert "12*3+4 = 40" in reply


def test_does_not_treat_dates_as_arithmetic():
    assert _extract_arithmetic_expression("今天是 2026-04-23 嗎？") == ""
    assert _try_arithmetic_tool_fast_path("今天是 2026-04-23 嗎？") == ""
