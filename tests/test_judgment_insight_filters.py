from __future__ import annotations

from api.osc.insight_filters import is_non_extractable_legal_insight


def test_prompt_echo_is_not_displayable_legal_insight():
    assert is_non_extractable_legal_insight(
        "好的，作為 MAGI 系統的 AI 助理，我將嚴格依照輸出格式擷取判決書實務見解。"
    )


def test_no_extractable_placeholder_is_not_displayable_legal_insight():
    assert is_non_extractable_legal_insight("本件屬程序性文書，無可擷取之實務見解。")


def test_prompt_instruction_tail_is_not_displayable_legal_insight():
    assert is_non_extractable_legal_insight(
        "輸出內容：嚴格依照「實務見解」、「引用裁判」、「適用法條」三個部分輸出。"
    )


def test_no_doctrinal_value_wording_is_not_displayable_legal_insight():
    assert is_non_extractable_legal_insight(
        "法院主要著重於量刑考量因素，而非創設新的法律見解；若需擷取量刑考量因素可另行處理。"
    )
