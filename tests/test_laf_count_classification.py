from casper_ecosystem.law_firm_orchestrators.laf_orchestrator import LAFOrchestrator


def test_laf_inquiry_text_keeps_visits_out_of_meetings():
    assert LAFOrchestrator._is_laf_inquiry_text("游秀鈴律見")
    assert LAFOrchestrator._is_laf_inquiry_text("律師接見 - 游秀鈴")


def test_laf_inquiry_text_rejects_court_ruling_no_visit_phrases():
    assert not LAFOrchestrator._is_laf_inquiry_text("裁定主文：禁止接見、通信")
    assert not LAFOrchestrator._is_laf_inquiry_text("延長羈押並限制接見")


def test_video_meeting_is_not_inquiry_without_explicit_visit_wording():
    assert not LAFOrchestrator._is_laf_inquiry_text("視訊會議 - 游秀鈴", criminal_laf=True)
    assert not LAFOrchestrator._is_laf_inquiry_text("視訊會議 - 游秀鈴", criminal_laf=False)
    assert LAFOrchestrator._is_laf_inquiry_text("視訊律見 - 游秀鈴", criminal_laf=True)


def test_plain_video_meeting_keyword_is_not_a_laf_visit_signal():
    assert not LAFOrchestrator._is_laf_inquiry_text("視訊會議 - 游秀鈴")
    assert not LAFOrchestrator._is_laf_inquiry_text("三位律師視訊會議")
