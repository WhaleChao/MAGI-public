from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

from api.osc import utils
from api.osc.insight_filters import (
    displayable_insight_item,
    is_official_judgment_url,
    legal_insight_row_rejection_reason,
    legal_insight_row_usable,
)


ROOT = Path(__file__).resolve().parents[1]
OFFICIAL_URL = (
    "https://judgment.judicial.gov.tw/FJUD/data.aspx?ty=JD&"
    "id=TPSM%2C115%2C%E5%8F%B0%E6%8A%97%2C299%2C20260225%2C1&ot=in"
)


def _judgment_text(case_number: str = "115年度台抗字第299號") -> str:
    return (
        "最高法院刑事裁定\n"
        f"{case_number}\n"
        "主文\n抗告駁回。\n"
        "理由\n本院認為刑事訴訟法所定抗告程序，應依裁定所載理由審查。"
        "原裁定就法律要件、證據資料與程序保障逐項說明，並無違誤。"
        "本院審酌卷內資料後，認為抗告意旨不足以動搖原裁定之法律判斷。"
        "因此依刑事訴訟法相關規定，裁定如主文。"
        "以上理由均為法院就本案法律爭點所為之判斷。" * 3
    )


def _web_row(**overrides):
    row = {
        "id": 1,
        "case_number": "115台抗299",
        "document_name": "最高法院 115 年度台抗字第 299 號刑事裁定",
        "court_reference": None,
        "court_type": None,
        "insight_type": "web_fetch_fulltext",
        "insight_text": "1) 法律爭點：抗告程序。[S1]\n2) 法院涵攝：原裁定並無違誤。[S2]",
        "case_reason": "詐欺",
        "source_file": OFFICIAL_URL,
        "raw_text": _judgment_text(),
    }
    row.update(overrides)
    return row


def _load_sync_module(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MAGI_V3_SCHEDULE_FIXTURE", "1")
    name = "sync_insights_to_vectors_rc643_test"
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "scripts" / "sync_insights_to_vectors.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _load_quarantine_module():
    name = "quarantine_unverified_legal_insights_rc643_test"
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "scripts" / "ops" / "quarantine_unverified_legal_insights.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_official_judgment_url_is_exact() -> None:
    assert is_official_judgment_url(OFFICIAL_URL)
    assert not is_official_judgment_url(OFFICIAL_URL.replace("https://", "http://"))
    assert not is_official_judgment_url(OFFICIAL_URL.replace("judgment.", "evil.judgment."))
    assert not is_official_judgment_url("https://user:pass@judgment.judicial.gov.tw/FJUD/data.aspx?id=x")
    assert not is_official_judgment_url("https://judgment.judicial.gov.tw/FJUD/default.aspx?id=x")
    assert not is_official_judgment_url("https://judgment.judicial.gov.tw/FJUD/data.aspx?ty=JD")


def test_web_fetch_requires_official_case_matching_raw_text() -> None:
    assert legal_insight_row_usable(_web_row())
    assert legal_insight_row_rejection_reason(_web_row(source_file="")) == "official_source_missing"
    assert legal_insight_row_rejection_reason(_web_row(raw_text="")) == "raw_text_missing"
    mismatch = _judgment_text("114年度侵訴字第59號")
    assert legal_insight_row_rejection_reason(_web_row(raw_text=mismatch)) == "raw_text_case_mismatch"


@pytest.mark.parametrize(
    "contamination",
    [
        "📅 **近期行程**\n\n• 03/08 00:00 - 婦女節",
        "✅ **我可以幫您管理 Obsidian 筆記！**\nobsidian ingest_source --source 案件",
        "感謝您的確認，若有任何法律檔案或用語需要修正。"
        "關於您所提有關勞資爭議的問題，我建議您先蒐集相關證據。",
    ],
)
def test_known_router_contamination_is_rejected_even_with_fabricated_provenance(
    contamination: str,
) -> None:
    row = _web_row(insight_text=contamination)
    assert legal_insight_row_rejection_reason(row) == "non_extractable_content"
    item = {
        **row,
        "source_type": "legal_insights",
        "title": row["document_name"],
        "summary": contamination,
        "full_text": row["raw_text"],
        "url": row["source_file"],
        "court": "",
    }
    assert not displayable_insight_item(item)


def test_human_reviewed_and_native_rows_have_separate_explicit_contracts() -> None:
    manual = {
        "insight_type": "manual",
        "insight_text": "人工核對：本裁定就舉證責任提出明確法律見解。",
    }
    native = {
        "insight_type": "裁定",
        "insight_text": "法院認為原裁定並無違誤。",
        "court_reference": "最高法院115年度台抗字第299號",
        "case_number": "115台抗299",
    }
    assert legal_insight_row_usable(manual)
    assert legal_insight_row_usable(native)
    assert legal_insight_row_rejection_reason({**native, "court_reference": ""}) == "court_reference_missing"
    assert legal_insight_row_rejection_reason({"insight_type": "unknown", "insight_text": "文字"}) == "insight_type_untrusted"


def test_fulltext_fallback_never_promotes_an_insight_summary(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    def fake_exec(sql, params=(), fetch="none"):
        calls.append(sql)
        if "FROM legal_insights" in sql:
            return {
                "id": 477,
                "document_name": "最高法院 115 年度台抗字第299號刑事裁定",
                "case_number": "115台抗299",
                "case_reason": "詐欺",
                "source_file": "",
                "insight_text": "📅 **近期行程**\n" + "錯誤內容" * 100,
                "raw_text": "",
            }, {}
        return None, {}

    monkeypatch.setattr(utils, "_osc_exec", fake_exec)
    result = utils._osc_lookup_fulltext_fallback(
        title="最高法院 115 年度台抗字第299號刑事裁定",
        case_number="115台抗299",
    )
    assert result == {"ok": False, "error": "fallback_not_found"}
    assert any("FROM legal_insights" in sql for sql in calls)


def test_fulltext_fallback_preserves_verified_raw_text_and_source(monkeypatch: pytest.MonkeyPatch) -> None:
    raw = _judgment_text()

    def fake_exec(sql, params=(), fetch="none"):
        if "FROM legal_insights" in sql:
            return {
                "id": 88,
                "document_name": "最高法院 115 年度台抗字第299號刑事裁定",
                "case_number": "115台抗299",
                "case_reason": "詐欺",
                "source_file": OFFICIAL_URL,
                "insight_text": "短摘要",
                "raw_text": raw,
            }, {}
        return None, {}

    monkeypatch.setattr(utils, "_osc_exec", fake_exec)
    result = utils._osc_lookup_fulltext_fallback(
        title="最高法院 115 年度台抗字第299號刑事裁定",
        case_number="115台抗299",
    )
    assert result["ok"] is True
    assert result["text"] == raw
    assert result["matched"]["url"] == OFFICIAL_URL


def test_vector_plan_excludes_unverified_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load_sync_module(monkeypatch)
    contaminated = _web_row(id=477, raw_text="", source_file="")
    valid = _web_row(id=483)
    planned = module._plan_new_insights([contaminated, valid], set())
    assert [row["id"] for row, _content in planned] == [483]


def test_quarantine_classification_and_receipt_are_content_free() -> None:
    module = _load_quarantine_module()
    rows = [
        _web_row(id=477, source_file="", raw_text="", insight_text="📅 **近期行程**"),
        _web_row(id=479, source_file="", raw_text="", insight_text="Obsidian ingest_source"),
        _web_row(id=483),
    ]
    candidates = module.classify_rows(rows)
    assert [item["id"] for item in candidates] == [477, 479]
    receipt = module.build_receipt(
        status="passed",
        apply=True,
        scanned_count=3,
        candidates=candidates,
        source_rows_updated=2,
        vectors_deleted=2,
    )
    assert receipt["candidate_count"] == 2
    assert receipt["raw_content_included"] is False
    assert receipt["pii_included"] is False
    encoded = str(receipt)
    assert "近期行程" not in encoded
    assert "Obsidian" not in encoded


def test_quarantine_receipt_is_exclusive_and_rejects_symlink_parent(tmp_path: Path) -> None:
    module = _load_quarantine_module()
    receipt = tmp_path / "receipt.json"
    handle = module._open_exclusive_receipt(receipt)
    handle.close()
    with pytest.raises(FileExistsError):
        module._open_exclusive_receipt(receipt)

    real_parent = tmp_path / "real"
    real_parent.mkdir()
    link_parent = tmp_path / "linked"
    link_parent.symlink_to(real_parent, target_is_directory=True)
    with pytest.raises(RuntimeError, match="regular directory"):
        module._open_exclusive_receipt(link_parent / "receipt.json")


def test_quarantine_requires_both_tables_to_be_transactional() -> None:
    module = _load_quarantine_module()

    class Cursor:
        def __init__(self, rows):
            self.rows = rows

        def execute(self, _sql, _params):
            return None

        def fetchall(self):
            return self.rows

    good = [
        {"table_schema": "law_firm_data", "table_name": "legal_insights", "engine": "InnoDB"},
        {"table_schema": "magi_brain", "table_name": "documents", "engine": "InnoDB"},
    ]
    module._assert_transactional_tables(Cursor(good))
    with pytest.raises(RuntimeError, match="transactional InnoDB"):
        module._assert_transactional_tables(Cursor([{**good[0], "engine": "MyISAM"}, good[1]]))
    with pytest.raises(RuntimeError, match="missing"):
        module._assert_transactional_tables(Cursor(good[:1]))
