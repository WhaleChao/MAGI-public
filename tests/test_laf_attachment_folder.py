from __future__ import annotations


def test_laf_closing_attachments_go_to_closing_folder():
    from skills.legal.laf import _laf_target_subfolder_for_attachment

    assert _laf_target_subfolder_for_attachment("結案酬金領款單_1131224-T-022_1150508.pdf") == "03_結案資料"
    assert _laf_target_subfolder_for_attachment("結案審查通知書_1131224-T-022_1150508.pdf") == "03_結案資料"


def test_laf_second_stage_attachment_goes_to_opening_folder():
    from skills.legal.laf import _laf_target_subfolder_for_attachment

    assert _laf_target_subfolder_for_attachment("附條件第二階段預付酬金領款單.pdf") == "02_開辦資料"
