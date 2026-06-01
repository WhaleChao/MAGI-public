from __future__ import annotations

from types import SimpleNamespace


def test_laf_closing_attachments_go_to_closing_folder():
    from skills.legal.laf import _laf_target_subfolder_for_attachment

    assert _laf_target_subfolder_for_attachment("結案酬金領款單_1131224-T-022_1150508.pdf") == "03_結案資料"
    assert _laf_target_subfolder_for_attachment("結案審查通知書_1131224-T-022_1150508.pdf") == "03_結案資料"
    assert _laf_target_subfolder_for_attachment("變動審查通知書_1131224-T-022_1150508.pdf") == "03_結案資料"


def test_laf_nightly_portal_closing_attachments_go_to_closing_folder():
    from scripts.laf_nightly_audit import _classify_portal_file

    assert _classify_portal_file("結案審查通知書_1131224-T-022_1150508.pdf") == "03_結案資料"
    assert _classify_portal_file("變動審查通知書_1131224-T-022_1150508.pdf") == "03_結案資料"


def test_laf_second_stage_attachment_goes_to_opening_folder():
    from skills.legal.laf import _laf_target_subfolder_for_attachment

    assert _laf_target_subfolder_for_attachment("附條件第二階段預付酬金領款單.pdf") == "02_開辦資料"


class _FakeDbForLafCaseCreate:
    def check_laf_case_exists(self, *args, **kwargs):
        return None

    def check_and_add_client(self, data):
        return "C000001"

    def generate_case_number(self):
        return "2026-9999"

    def execute_write(self, *args, **kwargs):
        return True

    def translate_path_to_canonical(self, path):
        return path


def _case_info(sender: str):
    return SimpleNamespace(
        message_id="msg-1",
        subject="1150527-E-024吳志炳",
        sender=sender,
        received_at="2026-06-01",
        laf_case_number="1150527-E-024",
        client_name="吳志炳",
        client_alias="",
        branch="花蓮",
        case_type="刑事",
        case_stage="一審",
        case_reason="公共危險",
        case_category="法律扶助案件",
        laf_case_type="刑事通常程序第一審",
        staff_name="",
        staff_phone="",
        staff_email="",
        has_attachment=True,
        needs_download=False,
        notification_type="派案通知",
        body="",
        attachments=[],
    )


def test_staff_email_attachments_do_not_become_portal_files(tmp_path):
    from casper_ecosystem.law_firm_orchestrators.laf_automation_v2 import OSCCaseCreator

    source = tmp_path / "1150527-E-024 吳志炳 2A.pdf"
    source.write_bytes(b"%PDF-staff")
    creator = OSCCaseCreator(_FakeDbForLafCaseCreate(), target_folder=str(tmp_path / "cases"), log_callback=lambda _msg: None)

    case_number, case_folder = creator.create_case(_case_info("承辦人 <staff@laf.org.tw>"), [str(source)])

    assert case_number == "2026-9999"
    assert (tmp_path / "cases" / "刑事" / "2026-9999-吳志炳-一審-公共危險" / "01_法扶資料" / "專員來信" / source.name).exists()
    assert not (tmp_path / "cases" / "刑事" / "2026-9999-吳志炳-一審-公共危險" / "01_法扶資料" / source.name).exists()


def test_portal_attachments_still_go_to_laf_folder(tmp_path):
    from casper_ecosystem.law_firm_orchestrators.laf_automation_v2 import OSCCaseCreator

    source = tmp_path / "法律扶助申請書_1150527-E-024_1150527.pdf"
    source.write_bytes(b"%PDF-portal")
    creator = OSCCaseCreator(_FakeDbForLafCaseCreate(), target_folder=str(tmp_path / "cases"), log_callback=lambda _msg: None)

    case_number, case_folder = creator.create_case(_case_info("laf.server@msa.hinet.net"), [str(source)])

    assert case_number == "2026-9999"
    assert (tmp_path / "cases" / "刑事" / "2026-9999-吳志炳-一審-公共危險" / "01_法扶資料" / source.name).exists()
    assert not (tmp_path / "cases" / "刑事" / "2026-9999-吳志炳-一審-公共危險" / "01_法扶資料" / "專員來信" / source.name).exists()
