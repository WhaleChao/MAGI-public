"""T3 tests: laf_progress_helper PDF selection + remark builder."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pathlib import Path
import pytest


# ── PDF priority scoring ──────────────────────────────────────────────────

def test_score_backup_wins():
    from casper_ecosystem.law_firm_orchestrators.laf_progress_helper import _score_pdf
    p_backup = Path("20260101_法院通知_存底.pdf")
    p_clean = Path("20260101_法院通知_清稿.pdf")
    assert _score_pdf(p_backup)[0] > _score_pdf(p_clean)[0]


def test_score_clean_over_vnum():
    from casper_ecosystem.law_firm_orchestrators.laf_progress_helper import _score_pdf
    p_clean = Path("20260101_書狀_清稿.pdf")
    p_v3 = Path("20260101_書狀v3.pdf")
    assert _score_pdf(p_clean)[0] > _score_pdf(p_v3)[0]


def test_score_vnum_over_date():
    from casper_ecosystem.law_firm_orchestrators.laf_progress_helper import _score_pdf
    p_v5 = Path("書狀v5.pdf")
    p_date = Path("20260101_書狀.pdf")
    s_v5 = _score_pdf(p_v5)
    s_date = _score_pdf(p_date)
    assert s_v5[0] > s_date[0]


def test_score_higher_vnum_wins():
    from casper_ecosystem.law_firm_orchestrators.laf_progress_helper import _score_pdf
    p_v10 = Path("書狀v10.pdf")
    p_v2 = Path("書狀v2.pdf")
    s10 = _score_pdf(p_v10)
    s2 = _score_pdf(p_v2)
    assert (s10[0], s10[1]) > (s2[0], s2[1])


# ── pick_latest_pdf ───────────────────────────────────────────────────────

def test_pick_court_pdf_finds_backup(tmp_path):
    from casper_ecosystem.law_firm_orchestrators.laf_progress_helper import pick_latest_pdf
    court_dir = tmp_path / "法院通知"
    court_dir.mkdir()
    (court_dir / "20260301_法院通知.pdf").touch()
    (court_dir / "20260401_法院通知_存底.pdf").touch()
    result = pick_latest_pdf(tmp_path, "court")
    assert result is not None
    assert "存底" in result.name


def test_pick_doc_pdf_finds_clean(tmp_path):
    from casper_ecosystem.law_firm_orchestrators.laf_progress_helper import pick_latest_pdf
    doc_dir = tmp_path / "我方歷次書狀"
    doc_dir.mkdir()
    (doc_dir / "20260301_答辯狀v2.pdf").touch()
    (doc_dir / "20260401_答辯狀_清稿.pdf").touch()
    result = pick_latest_pdf(tmp_path, "doc")
    assert result is not None
    assert "清稿" in result.name


def test_pick_returns_none_for_empty_folder(tmp_path):
    from casper_ecosystem.law_firm_orchestrators.laf_progress_helper import pick_latest_pdf
    assert pick_latest_pdf(tmp_path, "court") is None


def test_pick_returns_none_for_missing_folder():
    from casper_ecosystem.law_firm_orchestrators.laf_progress_helper import pick_latest_pdf
    assert pick_latest_pdf(Path("/nonexistent/path"), "doc") is None


# ── extract_date_from_pdf_name ────────────────────────────────────────────

def test_extract_date_gregorian_to_roc():
    from casper_ecosystem.law_firm_orchestrators.laf_progress_helper import extract_date_from_pdf_name
    result = extract_date_from_pdf_name(Path("20260415_書狀.pdf"))
    assert result == "115 年 4 月 15 日"


def test_extract_date_returns_none_for_no_date():
    from casper_ecosystem.law_firm_orchestrators.laf_progress_helper import extract_date_from_pdf_name
    assert extract_date_from_pdf_name(Path("書狀v3.pdf")) is None


def test_extract_date_jan_01():
    from casper_ecosystem.law_firm_orchestrators.laf_progress_helper import extract_date_from_pdf_name
    result = extract_date_from_pdf_name(Path("20260101.pdf"))
    assert result == "115 年 1 月 1 日"


# ── build_progress_remark ─────────────────────────────────────────────────

def test_build_remark_both():
    from casper_ecosystem.law_firm_orchestrators.laf_progress_helper import build_progress_remark
    court = Path("20260401_法院通知.pdf")
    doc = Path("20260415_書狀.pdf")
    r = build_progress_remark(court, doc)
    assert "收受最後一份裁定" in r
    assert "提出書狀" in r
    assert "，" in r


def test_build_remark_court_only():
    from casper_ecosystem.law_firm_orchestrators.laf_progress_helper import build_progress_remark
    court = Path("20260401_法院通知.pdf")
    r = build_progress_remark(court, None)
    assert "收受最後一份裁定" in r
    assert "提出書狀" not in r


def test_build_remark_doc_only():
    from casper_ecosystem.law_firm_orchestrators.laf_progress_helper import build_progress_remark
    doc = Path("20260415_書狀.pdf")
    r = build_progress_remark(None, doc)
    assert "提出書狀" in r
    assert "收受最後一份裁定" not in r


def test_build_remark_raises_when_both_none():
    from casper_ecosystem.law_firm_orchestrators.laf_progress_helper import build_progress_remark
    with pytest.raises(ValueError):
        build_progress_remark(None, None)


def test_build_remark_no_date_fallback():
    from casper_ecosystem.law_firm_orchestrators.laf_progress_helper import build_progress_remark
    court = Path("法院通知_存底.pdf")  # no date prefix
    r = build_progress_remark(court, None)
    assert "日期不明" in r
