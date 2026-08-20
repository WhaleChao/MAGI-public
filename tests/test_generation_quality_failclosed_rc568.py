"""Synthetic regressions for generation results that must not be published."""

from api.handlers.output_quality_handler import run_output_quality_gate


def test_translation_rejects_collapsed_source_paragraphs_even_when_anchors_survive() -> None:
    source = "第一段說明契約已成立，並保留原有權利。\n\n第二段說明違約後得請求損害賠償。"
    output = "第一段說明契約已成立，並保留原有權利。第二段說明違約後得請求損害賠償。"

    gate = run_output_quality_gate("translation", output, source_text=source)

    assert not gate["ok"]
    assert gate["issue"] == "translation_paragraph_structure_missing"


def test_translation_accepts_preserved_short_chinese_paragraphs() -> None:
    source = "This is the first source paragraph.\n\nThis is the second source paragraph."
    output = "這是第一段翻譯。\n\n這是第二段翻譯。"

    gate = run_output_quality_gate("translation", output, source_text=source)

    assert gate["ok"]


def test_transcript_rejects_generated_text_when_recognizer_returned_nothing() -> None:
    gate = run_output_quality_gate(
        "transcript",
        "這段看似完整的內容不能在沒有辨識原文時由潤稿器補寫。",
        source_chars=1200,
        source_text="",
        metadata={"recognizer_text_present": False, "segment_count": 0},
    )

    assert not gate["ok"]
    assert gate["issue"] == "transcript_no_recognized_content"


def test_formal_summary_requires_extractable_source_for_claim_verification() -> None:
    gate = run_output_quality_gate(
        "summary",
        "本件爭點涉及損害賠償，法院認為應依具體證據判斷。",
        metadata={"source_required": True, "source_text_present": False},
    )

    assert not gate["ok"]
    assert gate["issue"] == "summary_source_unavailable"


def test_strict_draft_export_fails_closed_without_grounding_text() -> None:
    from api.osc.saas_workbench import quality_check

    result = quality_check(
        {
            "mode": "draft",
            "strict_export": True,
            "draft_text": "民事起訴狀\n聲明事項：請求損害賠償。\n事實及理由：如附件所示。",
            "doc_type": "民事起訴狀",
        }
    )

    assert not result["pass"]
    assert "source_grounding_missing" in {issue["code"] for issue in result["issues"]}
