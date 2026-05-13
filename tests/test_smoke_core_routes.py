# -*- coding: utf-8 -*-
from __future__ import annotations

from scripts.ops.smoke_core_routes import Case, _classify_case_output


def test_classify_pass_with_alternative_phrase():
    case = Case("translate_guide", "你會翻譯嗎？", ("我可以幫您翻譯", "翻譯結果"))
    status = _classify_case_output(case, "🌐 翻譯結果（google_gtx_primary）: 你會翻譯嗎？")
    assert status == "PASS"


def test_classify_warn_for_missing_dependency():
    case = Case("judgment_guide", "你會查判決嗎？", "我可以幫您查判決", warn_substring=("missing API key",))
    status = _classify_case_output(case, "❌ 判決搜尋失敗：unauthorized: missing API key")
    assert status == "WARN"
