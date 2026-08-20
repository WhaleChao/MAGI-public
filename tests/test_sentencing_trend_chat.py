from __future__ import annotations

from api.domains import judgment_flow
from api.pipelines.message_router import try_conversational_intent
from api.sentencing_trends import (
    format_roc_date,
    format_sentencing_trend_chat_result,
    parse_sentencing_trend_chat_query,
)


def _result(**overrides):
    result = {
        "ok": True,
        "filters": {
            "court": "臺灣花蓮地方法院",
            "judge": "王小明",
            "offense": "詐欺",
            "date_from": "2023-01-01",
            "date_to": "2026-12-31",
        },
        "candidate_count": 4,
        "eligible_count": 2,
        "local_eligible_count": 1,
        "mcp_verified_count": 1,
        "excluded_count": 2,
        "statistics": {
            "declared_terms": {
                "count": 2,
                "median_months": 8.0,
                "q1_months": 6.0,
                "q3_months": 10.0,
                "min_months": 4.0,
                "max_months": 12.0,
            },
            "execution_terms": {
                "count": 1,
                "median_months": 10.0,
                "q1_months": 10.0,
                "q3_months": 10.0,
                "min_months": 10.0,
                "max_months": 10.0,
            },
        },
        "items": [
            {
                "statistics_eligible": True,
                "court": "臺灣花蓮地方法院",
                "case_number": "115年度訴字第1號",
                "judgment_date": "2026-06-30",
                "judges": [{"role": "法官", "name": "王小明"}],
                "sentences": [{"text": "處有期徒刑捌月", "months": 8.0}],
                "appendix_sentences": [],
                "execution_sentence": {"text": "應執行有期徒刑拾月", "months": 10.0},
                "source_url": "https://judgment.judicial.gov.tw/FJUD/data.aspx?id=official",
            }
        ],
        "mcp": {"status": "ok", "items": [{"title": "候選"}], "verified_count": 1},
    }
    result.update(overrides)
    return result


def test_chat_parser_extracts_full_filters_and_roc_year_range():
    filters, clarification = parse_sentencing_trend_chat_query(
        "請查臺灣花蓮地方法院王小明法官詐欺案件，民國112年至115年的量刑趨勢"
    )
    assert clarification == ""
    assert filters == {
        "court": "臺灣花蓮地方法院",
        "judge": "王小明",
        "offense": "詐欺",
        "date_from": "2023-01-01",
        "date_to": "2026-12-31",
    }


def test_chat_parser_normalises_court_alias_and_keeps_long_offence():
    filters, clarification = parse_sentencing_trend_chat_query(
        "花蓮地院蘇珈漪法官違反毒品危害防制條例量刑趨勢"
    )
    assert clarification == ""
    assert filters["court"] == "臺灣花蓮地方法院"
    assert filters["judge"] == "蘇珈漪"
    assert filters["offense"] == "違反毒品危害防制條例"


def test_chat_parser_supports_simple_offence_and_clarifies_missing_filters():
    filters, clarification = parse_sentencing_trend_chat_query("判決趨勢 詐欺")
    assert clarification == ""
    assert filters["offense"] == "詐欺"

    filters, clarification = parse_sentencing_trend_chat_query("量刑趨勢")
    assert not any(filters.values())
    assert "至少提供法院、法官或案由" in clarification


def test_chat_parser_rejects_reversed_period_without_guessing():
    filters, clarification = parse_sentencing_trend_chat_query(
        "花蓮地院詐欺案，民國115年至112年量刑趨勢"
    )
    assert filters == {}
    assert "起始日期晚於結束日期" in clarification


def test_chat_formatter_reports_provenance_statistics_and_official_source():
    text = format_sentencing_trend_chat_result(_result())
    assert "可核對樣本：2 筆（本機 1；MCP 官方全文核實 1）" in text
    assert "個別宣告刑（含完整附表）：2 筆" in text
    assert "最後定應執行刑：1 筆" in text
    assert "https://judgment.judicial.gov.tw/" in text
    assert "MCP 只負責擴充候選" in text
    assert "期間：民國112年1月1日 至 民國115年12月31日" in text
    assert "民國115年6月30日" in text
    assert "2026-06-30" not in text


def test_display_date_uses_full_taiwanese_roc_format_without_changing_invalid_values():
    assert format_roc_date("2026-08-17") == "民國115年8月17日"
    assert format_roc_date("2026-08-17T09:30:00") == "民國115年8月17日"
    assert format_roc_date("115年08月17日") == "民國115年8月17日"
    assert format_roc_date("not-a-date") == "not-a-date"


def test_chat_formatter_never_promotes_unverified_mcp_candidate():
    text = format_sentencing_trend_chat_result(
        _result(
            eligible_count=0,
            local_eligible_count=0,
            mcp_verified_count=0,
            items=[],
            statistics={
                "declared_terms": {"count": 0},
                "execution_terms": {"count": 0},
            },
        )
    )
    assert "不會用未核實候選推算量刑" in text
    assert "找到 1 筆候選" in text


def test_orchestrator_chat_uses_shared_search_core_and_mcp_gate(monkeypatch):
    import api.sentencing_trends as sentencing

    captured = {}

    def fake_search(**kwargs):
        captured.update(kwargs)
        return _result()

    def fake_mcp(*_args, **_kwargs):
        return {"success": True, "items": []}

    monkeypatch.setattr(sentencing, "search_sentencing_trends", fake_search)
    monkeypatch.setattr(sentencing, "search_public_judgment_candidates", fake_mcp)
    text = judgment_flow.run_judgment_trend_command(
        object(),
        "請查臺灣花蓮地方法院王小明法官詐欺案件，民國112年至115年的量刑趨勢",
    )
    assert captured["court"] == "臺灣花蓮地方法院"
    assert captured["judge"] == "王小明"
    assert captured["offense"] == "詐欺"
    assert captured["include_mcp"] is True
    assert captured["mcp_search"] is fake_mcp
    assert "法官量刑與判決趨勢" in text


def test_orchestrator_chat_does_not_expose_internal_failure(monkeypatch):
    import api.sentencing_trends as sentencing

    monkeypatch.setattr(
        sentencing,
        "search_sentencing_trends",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("/private/db trace-secret")),
    )
    text = judgment_flow.run_judgment_trend_command(object(), "判決趨勢 詐欺")
    assert "/private/db" not in text
    assert "trace-secret" not in text
    assert "未核實資料" in text


class _RouterOrchestrator:
    def _looks_like_capability_question(self, _message):
        return False

    def _run_judgment_trend_command(self, message):
        return f"TREND:{message}"


def test_bare_sentencing_trend_request_routes_without_polite_prefix():
    message = "花蓮地院蘇珈漪法官公共危險量刑趨勢"
    result = try_conversational_intent(
        _RouterOrchestrator(), message, message.lower(), "u1", "user", "WEB"
    )
    assert result == f"TREND:{message}"
