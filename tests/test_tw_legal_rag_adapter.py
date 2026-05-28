from __future__ import annotations

from api.domains import judgment_flow
from api.osc import tw_legal_rag


def test_sanitize_tlr_query_removes_private_identifiers():
    sanitized = tw_legal_rag.sanitize_tlr_query(
        "@MAGI 實務見解 2025-0121 王小明 A123456789 0912345678 foo@example.com 114年度台上字第3753號"
    )

    assert "2025-0121" not in sanitized
    assert "A123456789" not in sanitized
    assert "0912345678" not in sanitized
    assert "foo@example.com" not in sanitized
    assert "114年度台上字第3753號" in sanitized


def test_search_practical_judgments_via_tlr_returns_bundle(monkeypatch):
    monkeypatch.setenv("MAGI_TWLEGALRAG_ENABLE", "1")

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return None

        def search_and_read(self, query, *, search_type, max_results, read_top):
            return [
                tw_legal_rag.TLRJudgment(
                    rank=1,
                    doc_id="TPS,114,台上,3753,20250101,1",
                    citation_text="最高法院 114年度台上字第3753號",
                    court_name="最高法院",
                    jdate="2025-01-01",
                    snippet="法院就通譯品質進行審查。",
                    citation_url="https://dr-lawbot.com/fullview/TPS,114,台上,3753,20250101,1",
                    citation_markdown="[最高法院 114年度台上字第3753號](https://example.test)",
                    result_token="token",
                    fulltext="法院認為通譯程序足以保障被告訴訟權。",
                )
            ]

    monkeypatch.setattr(tw_legal_rag, "TLRClient", FakeClient)

    result = tw_legal_rag.search_practical_judgments_via_tlr("通譯 最高法院", limit=1, fulltext_limit=1)

    assert result["success"] is True
    assert result["items"][0]["title"] == "最高法院 114年度台上字第3753號"
    assert result["bundle"]["allowed_citations"] == ["J1"]
    assert result["bundle"]["judgments"][0]["fulltext_excerpt"].startswith("法院認為")


def test_citation_check_against_tlr_bundle_detects_out_of_bundle():
    bundle = {
        "judgments": [
            {
                "citation_text": "最高法院 114年度台上字第3753號",
                "doc_id": "TPS,114,台上,3753,20250101,1",
            }
        ]
    }

    ok = tw_legal_rag.citation_check_against_tlr_bundle("最高法院 114年度台上字第3753號", bundle)
    bad = tw_legal_rag.citation_check_against_tlr_bundle("最高法院 999年度台上字第999999號", bundle)

    assert ok["overall"] == "pass"
    assert bad["overall"] == "fail"


def test_tlr_disabled_is_soft_failure(monkeypatch):
    monkeypatch.setenv("MAGI_TWLEGALRAG_ENABLE", "0")

    result = tw_legal_rag.search_practical_judgments_via_tlr("通譯")

    assert result["success"] is False
    assert result["error"] == "tw_legal_rag_disabled"


def test_tlr_hits_cache_to_local_court_judgments(monkeypatch):
    monkeypatch.setenv("MAGI_TWLEGALRAG_CACHE_HITS", "1")
    calls = []

    class FakeDb:
        def execute(self, query, params=None, fetch=None):
            calls.append((query, params, fetch))
            return 1

    monkeypatch.setattr(judgment_flow, "_get_local_db_manager", lambda: FakeDb())
    result = {
        "items": [
            {
                "jid": "TPS,114,台上,3753,20250101,1",
                "citation_text": "最高法院 114年度台上字第3753號",
                "summary_full": "法院認為通譯程序足以保障被告訴訟權。",
                "url": "https://example.test/judgment",
                "court_name": "最高法院",
                "judgment_date": "2025-01-01",
                "case_category": "刑事",
            }
        ]
    }

    cached = judgment_flow._cache_tlr_judgments_to_local(result)

    assert cached == 1
    assert any("CREATE TABLE IF NOT EXISTS court_judgments" in call[0] for call in calls)
    insert_call = [call for call in calls if "INSERT INTO court_judgments" in call[0]][0]
    assert insert_call[1][0] == "TPS,114,台上,3753,20250101,1"
    assert insert_call[1][4] == "2025-01-01"


def test_tlr_fast_digest_is_not_cached(monkeypatch):
    monkeypatch.setenv("MAGI_TWLEGALRAG_CACHE_HITS", "1")
    calls = []

    class FakeDb:
        def execute(self, query, params=None, fetch=None):
            calls.append((query, params, fetch))
            return 1

    monkeypatch.setattr(judgment_flow, "_get_local_db_manager", lambda: FakeDb())
    result = {
        "items": [
            {
                "jid": "TPS,114,台上,1,20250101,1",
                "citation_text": "最高法院 114年度台上字第1號",
                "summary_preview": "短片段",
                "url": "https://dr-lawbot.com/fullview/example",
                "is_fast_digest": True,
            }
        ]
    }

    cached = judgment_flow._cache_tlr_judgments_to_local(result)

    assert cached == 0
    assert not any("INSERT INTO court_judgments" in call[0] for call in calls)
