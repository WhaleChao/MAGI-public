# -*- coding: utf-8 -*-

from scripts.ops.build_ocr_training_dataset import (
    parse_filename_fields,
    _safe_training_relative_path,
    _support_score,
    _training_messages,
)
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


def test_parse_filename_fields_payment_slip_party_from_case_marker():
    fields = parse_filename_fields("20250218 113年度原易字第179號《余秋菊案》花蓮地院規費繳款單（線上聲請閱卷）.pdf")

    assert fields.court == "臺灣花蓮地方法院"
    assert fields.case_number == "113年度原易字第179號"
    assert fields.doc_type == "規費繳款單"
    assert fields.party == "余秋菊"


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


def test_training_output_path_normalizes_legacy_judgment_folder_and_redacts_party(tmp_path):
    root = tmp_path / "cases"
    pdf = (
        root
        / "一般案件"
        / "民事"
        / "2025-0001-王大明-一審-損害賠償"
        / "10_判決書"
        / "20250214 臺灣花蓮地方法院114年度訴字第127號民事判決（王大明）.pdf"
    )
    pdf.parent.mkdir(parents=True)
    pdf.write_bytes(b"%PDF")
    fields = parse_filename_fields(pdf.name)

    rel = _safe_training_relative_path(pdf, [root], fields)

    assert not rel.startswith("/")
    assert "10_判決書或終局裁定及處分" in rel
    assert "王大明" not in rel
    assert "[PARTY]" in rel or "[REDACTED]" in rel


def test_training_messages_redact_party_name():
    fields = FilenameFields(
        date="20250214",
        court="臺灣花蓮地方法院",
        case_number="114年度訴字第127號",
        doc_type="民事判決",
        party="王大明",
    )
    sources = [SourceResult(source="native_text", text="被告王大明到庭", quality=0.8)]

    messages = _training_messages(fields, sources, "20250214 民事判決（王大明）.pdf")
    blob = "\n".join(m["content"] for m in messages)

    assert "王大明" not in blob
    assert "[PARTY]" in blob
