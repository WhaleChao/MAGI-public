# -*- coding: utf-8 -*-

from scripts.ops.build_ocr_training_dataset import parse_filename_fields, _support_score
from scripts.ops.build_ocr_training_dataset import FilenameFields, SourceResult


def test_parse_filename_fields_standard():
    fields = parse_filename_fields("20250707 臺灣花蓮地方法院113年度原易字第179號刑事判決（余秋菊）.pdf")

    assert fields.date == "20250707"
    assert fields.court == "臺灣花蓮地方法院"
    assert fields.case_number == "113年度原易字第179號"
    assert fields.doc_type == "刑事判決"
    assert fields.party == "余秋菊"


def test_parse_filename_fields_loose_real_world_variants():
    fields = parse_filename_fields("20241007 花蓮地院113年度原易字第179刑事庭通知書（余秋菊；113.10.18開庭）.pdf")

    assert fields.date == "20241007"
    assert fields.court == "臺灣花蓮地方法院"
    assert fields.case_number == "113年度原易字第179號"
    assert fields.doc_type == "刑事庭通知書"
    assert fields.party == "余秋菊"


def test_parse_filename_fields_without_space_after_date():
    fields = parse_filename_fields("20250214民事114年度台抗字第127號裁定（蘇建和）.pdf")

    assert fields.date == "20250214"
    assert fields.case_number == "114年度台抗字第127號"
    assert fields.doc_type == "民事裁定"
    assert fields.party == "蘇建和"


def test_support_score_rewards_ocr_evidence():
    fields = FilenameFields(
        date="20250707",
        court="臺灣花蓮地方法院",
        case_number="113年度原易字第179號",
        doc_type="刑事判決",
        party="余秋菊",
    )
    sources = [
        SourceResult(
            source="macos_vision",
            text="臺灣花蓮地方法院 113年度原易字第179號 刑事判決 被告余秋菊",
            quality=0.65,
        )
    ]

    score, support = _support_score(fields, sources)

    assert score >= 0.58
    assert "case_number_exact" in support
    assert "court_exact" in support
