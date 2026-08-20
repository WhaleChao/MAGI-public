from __future__ import annotations

import json

from api.domains import judgment_flow
from api.legal_workflow import detect_legal_workflow
from api.osc import drafts
from api.osc import legaltech_taiwan_law_mcp as remote
from api.pipelines import message_router


class _Response:
    def __init__(self, payload: dict):
        self._body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.headers = {"Content-Type": "application/json"}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def geturl(self):
        return remote.DEFAULT_ENDPOINT

    def read(self, _limit: int):
        return self._body


def _envelope(payload: dict) -> dict:
    return {"jsonrpc": "2.0", "id": 1, "result": {"structuredContent": payload}}


def test_remote_judgment_client_binds_jid_official_url_and_remains_candidate(monkeypatch):
    calls = []
    responses = iter(
        [
            _envelope({"input_type": "legal_question", "privacy_risk": "low"}),
            _envelope(
                {
                    "results": [
                        {
                            "case_id": "最高法院 114 年度台上字第 9 號民事判決",
                            "jid": "TPSV,114,台上,9,20260101,1",
                            "url": "https://judgment.judicial.gov.tw/FJUD/data.aspx?id=x",
                            "summary": "本院認為應就因果關係負舉證責任。",
                            "court": "最高法院",
                            "cause": "損害賠償",
                        }
                    ]
                }
            ),
            _envelope(
                {
                    "jid": "TPSV,114,台上,9,20260101,1",
                    "title": "最高法院 114 年度台上字第 9 號民事判決",
                    "official_url": "https://judgment.judicial.gov.tw/FJUD/data.aspx?id=x",
                    "content": "本院認為應就因果關係負舉證責任。",
                }
            ),
        ]
    )

    def fake_urlopen(request, **_kwargs):
        calls.append(json.loads(request.data.decode("utf-8")))
        return _Response(next(responses))

    monkeypatch.setattr(remote.urllib.request, "urlopen", fake_urlopen)
    result = remote.search_practical_judgments_via_legaltech("侵權行為 因果關係", limit=2)
    assert result["success"] is True
    assert [call["params"]["name"] for call in calls] == [
        "analyze_legal_intent",
        "search_taiwan_judgments",
        "get_taiwan_judgment",
    ]
    item = result["items"][0]
    assert item["jid"] == "TPSV,114,台上,9,20260101,1"
    assert item["source_url"].startswith("https://judgment.judicial.gov.tw/")
    assert item["verification_state"] == "external_candidate"
    assert item["draft_eligible"] is False


def test_natural_legal_question_is_normalized_to_search_terms(monkeypatch):
    calls = []
    responses = iter(
        [
            _envelope(
                {
                    "input_type": "legal_question",
                    "suggested_queries": ["侵權行為的舉證責任怎麼分配"],
                }
            ),
            _envelope(
                {
                    "results": [
                        {
                            "case_id": "最高法院 114 年度台上字第 9 號民事判決",
                            "jid": "TPSV,114,台上,9,20260101,1",
                            "url": "https://judgment.judicial.gov.tw/FJUD/data.aspx?id=x",
                        }
                    ]
                }
            ),
        ]
    )

    def fake_urlopen(request, **_kwargs):
        calls.append(json.loads(request.data.decode("utf-8")))
        return _Response(next(responses))

    monkeypatch.setattr(remote.urllib.request, "urlopen", fake_urlopen)
    result = remote.search_practical_judgments_via_legaltech(
        "侵權行為的舉證責任怎麼分配？", limit=1, fulltext_limit=0
    )
    search_args = calls[1]["params"]["arguments"]
    assert search_args["query"] == "侵權行為 舉證責任 分配"
    assert result["search_query"] == "侵權行為 舉證責任 分配"
    assert result["success"] is True


def test_judgment_client_uses_provider_court_filter_and_can_verify_ten_fulltexts(monkeypatch):
    calls = []
    results = [
        {
            "case_id": f"臺灣嘉義地方法院 115 年度嘉交簡字第 {number} 號刑事判決",
            "jid": f"CYDM,115,嘉交簡,{number},20260801,1",
            "url": f"https://judgment.judicial.gov.tw/FJUD/data.aspx?id={number}",
            "court": "臺灣嘉義地方法院",
            "cause": "公共危險",
        }
        for number in range(1, 11)
    ]
    responses = iter(
        [_envelope({"input_type": "legal_question"}), _envelope({"results": results})]
        + [
            _envelope(
                {
                    "jid": item["jid"],
                    "title": item["case_id"],
                    "official_url": item["url"],
                    "content": "官方裁判全文",
                }
            )
            for item in results
        ]
    )

    def fake_urlopen(request, **_kwargs):
        calls.append(json.loads(request.data.decode("utf-8")))
        return _Response(next(responses))

    monkeypatch.setattr(remote.urllib.request, "urlopen", fake_urlopen)
    result = remote.search_practical_judgments_via_legaltech(
        "蘇珈漪 公共危險 量刑",
        court="臺灣嘉義地方法院",
        limit=10,
        fulltext_limit=10,
    )
    search_args = calls[1]["params"]["arguments"]
    assert search_args["court"] == "臺灣嘉義地方法院"
    assert len(result["items"]) == 10
    assert all(item["full_text"] == "官方裁判全文" for item in result["items"])


def test_remote_errors_are_public_safe(monkeypatch):
    def explode(*_args, **_kwargs):
        raise RuntimeError("/private/case/path token=secret")

    monkeypatch.setattr(remote.urllib.request, "urlopen", explode)
    result = remote.call_legaltech_tool("search_taiwan_laws", query="民法")
    assert result["error"] == "remote_mcp_unavailable"
    assert "private" not in json.dumps(result)
    assert "secret" not in json.dumps(result)


def test_public_tool_catalog_is_fixed_and_result_contract_keeps_only_official_urls(monkeypatch):
    assert len(remote.legaltech_tool_catalog()) == 14
    assert {item["name"] for item in remote.legaltech_tool_catalog()} == remote._ALLOWED_TOOLS

    monkeypatch.setattr(
        remote,
        "_post_jsonrpc",
        lambda _payload: _envelope({"results": [{"url": "https://law.moj.gov.tw/LawClass/LawAll.aspx?pcode=B0000001"}, {"url": "https://example.invalid/nope"}]}),
    )
    result = remote.call_legaltech_tool("search_taiwan_laws", query="民法")
    assert result["contract_version"] == "magi.public-legal-research/v1"
    assert result["status"] == "ok"
    assert result["official_urls"] == ["https://law.moj.gov.tw/LawClass/LawAll.aspx?pcode=B0000001"]


def test_explicit_statute_request_uses_shared_official_route_before_general_research(monkeypatch):
    monkeypatch.setattr(judgment_flow, "_run_direct_taiwan_legal_mcp_lookup", lambda message: "官方法規回應")
    monkeypatch.setattr(
        judgment_flow,
        "run_practical_insight_command",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not bypass official route")),
    )
    monkeypatch.setattr(judgment_flow, "_with_legal_workflow_footer", lambda reply, *_args, **_kwargs: reply)
    assert judgment_flow.run_judgment_collector_command(object(), "查法條 民法第184條") == "官方法規回應"


def test_external_research_receives_only_redacted_query(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        judgment_flow,
        "_search_local_judgment_archive",
        lambda *_args, **_kwargs: {"success": False, "items": []},
    )

    def fake_augment(query, judgments, **kwargs):
        captured.update({"query": query, **kwargs})
        return judgments

    monkeypatch.setattr(judgment_flow, "_augment_judgments_with_external_sources", fake_augment)
    result = judgment_flow.build_legal_research_payload(
        "當事人王小明 A123456789 0912345678 2026-0008 民法第184條因果關係"
    )
    safe = captured["external_query"]
    assert captured["external_allowed"] is True
    assert "王小明" not in safe
    assert "A123456789" not in safe
    assert "0912345678" not in safe
    assert "2026-0008" not in safe
    assert "民法第184條" in safe
    assert result["privacy"]["redactions"]


def test_legaltech_candidates_are_not_dropped_by_later_optional_sources(monkeypatch):
    remote_payload = {
        "success": True,
        "items": [
            {
                "jid": "TPSV,114,台上,9,20260101,1",
                "title": "最高法院 114 年度台上字第 9 號民事判決",
                "citation_text": "最高法院 114 年度台上字第 9 號民事判決",
                "summary_preview": "已命中司法院公開裁判，引用前須核對全文。",
                "source": remote.SOURCE,
                "verification_state": "external_candidate",
                "draft_eligible": False,
            }
        ],
    }
    monkeypatch.setattr(judgment_flow, "_mcp_lookup_allowed", lambda: True)
    monkeypatch.setattr(judgment_flow, "_augment_judgments_with_mcp", lambda _q, payload, **_kw: payload)
    monkeypatch.setattr(judgment_flow, "_augment_judgments_with_tlr", lambda _q, payload, **_kw: payload)
    monkeypatch.setattr(judgment_flow, "_legaltech_mcp_lookup_allowed", lambda: True)
    monkeypatch.setattr(
        judgment_flow,
        "_augment_judgments_with_legaltech_mcp",
        lambda _q, _payload, **_kw: remote_payload,
    )
    monkeypatch.setattr(judgment_flow, "_get_local_db_manager", lambda: None)

    result = judgment_flow._augment_judgments_with_external_sources(
        "侵權行為 因果關係",
        {"success": False, "items": []},
        external_query="侵權行為 因果關係",
        external_allowed=True,
    )
    assert len(result["items"]) == 1
    assert result["items"][0]["source"] == remote.SOURCE
    assert result["items"][0]["draft_eligible"] is False


def test_draft_research_sends_only_doc_type_and_abstract_reason(monkeypatch):
    captured = {}
    monkeypatch.setattr(drafts, "_osc_collect_insights", lambda: [])
    monkeypatch.setattr(drafts, "_osc_get_setting_value", lambda *_args, **_kwargs: "")
    # Keep this privacy test independent of api.server startup and mutable
    # runtime paths so it also runs from a read-only sealed release.
    monkeypatch.setattr(drafts, "_get_draft_prompt_template", lambda: "{doc_type} {reason} {legal_insights}")

    def fake_research(query, *, limit=5):
        captured["query"] = query
        return {"success": True, "items": [], "privacy": {"external_allowed": True}}

    monkeypatch.setattr(judgment_flow, "build_legal_research_payload", fake_research)
    context = drafts._osc_build_draft_context(
        {
            "doc_type": "民事答辯狀",
            "reason": "侵權行為因果關係",
            "case_facts": "當事人王小明住在私密地址，帳號123456。",
            "plaintiff": "王小明",
            "defendant": "陳小華",
            "augment_legal_sources": True,
        }
    )
    assert captured["query"] == "侵權行為因果關係 民事答辯狀 實務見解"
    assert "王小明" not in captured["query"]
    assert "123456" not in captured["query"]
    assert context["legal_research"]["attempted"] is True


def test_legal_workflows_require_new_mcp_for_answers_and_pleadings():
    answer = detect_legal_workflow(text="民法第184條實務見解", mode="answer")
    draft = detect_legal_workflow(reason="侵權行為", doc_type="答辯狀", mode="draft")
    assert "legaltech_taiwan_law_mcp_if_privacy_safe" in answer["must_use_tools"]
    assert "legaltech_taiwan_law_mcp_if_privacy_safe" in draft["must_use_tools"]


def test_general_legal_question_uses_research_path(monkeypatch):
    assert judgment_flow._is_general_legal_question("侵權行為的舉證責任怎麼分配？")
    assert not judgment_flow._is_general_legal_question("今天天氣怎麼樣？")
    monkeypatch.setattr(
        judgment_flow,
        "run_practical_insight_command",
        lambda _orch, message, notify=False: f"researched:{message}:{notify}",
    )
    result = judgment_flow.run_judgment_collector_command(
        object(), "侵權行為的舉證責任怎麼分配？", notify=False
    )
    assert result.startswith("researched:")


def test_conversational_router_sends_general_legal_question_to_research():
    class _Orch:
        @staticmethod
        def _looks_like_capability_question(_message):
            return False

        @staticmethod
        def _run_judgment_collector_command(message, notify=False):
            return f"legal-research:{message}:{notify}"

    message = "侵權行為的舉證責任怎麼分配？"
    result = message_router.try_conversational_intent(
        _Orch(), message, message.lower(), "u1", "member", "web"
    )
    assert result == f"legal-research:{message}:False"


def test_low_quality_official_candidate_stays_visible_but_not_citeable():
    reply = judgment_flow.format_practical_insight_result(
        "侵權行為 舉證責任",
        {
            "success": True,
            "source_label": remote.SOURCE_LABEL,
            "items": [
                {
                    "title": "最高法院 114 年度台上字第 9 號民事判決",
                    "citation_text": "最高法院 114 年度台上字第 9 號民事判決",
                    "summary_preview": "已命中公開裁判，引用前須核對全文。",
                    "jid": "TPSV,114,台上,9,20260101,1",
                    "url": "https://judgment.judicial.gov.tw/FJUD/data.aspx?id=x",
                    "source": remote.SOURCE,
                    "verification_state": "external_candidate",
                    "draft_eligible": False,
                }
            ],
        },
        {"success": False, "items": []},
    )
    assert "待全文核對的官方裁判候選" in reply
    assert "TPSV,114,台上,9,20260101,1" in reply
    assert "https://judgment.judicial.gov.tw/" in reply
    assert "不得直接放入書狀" in reply
