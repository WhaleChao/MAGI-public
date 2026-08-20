from __future__ import annotations

from api.legal_research_quality import (
    EXTERNAL_CANDIDATE,
    VERIFIED_LOCAL,
    build_practice_view_card,
    citation_lock_for_items,
    enrich_and_rank_items,
    grounded_summary_evidence,
    prepare_external_legal_query,
    supporting_spans,
    validate_span_citations,
    validate_text_against_citation_lock,
)
from api.osc.tw_legal_rag import sanitize_tlr_query


REASONING = (
    "原告主張被告應負全部賠償責任，並引用多件未具體說明之裁判。\n\n"
    "按民法第184條規定，侵權行為之成立，應以行為人有故意或過失、"
    "權利受侵害及二者間具有相當因果關係為要件。\n\n"
    "本院認為原告依民事訴訟法第277條應就損害與行為間之因果關係"
    "負舉證責任；本件未提出足以證明相當因果關係之資料，故其請求不得准許。\n\n"
    "主文：原告之訴駁回。"
)


def test_external_query_privacy_gate_keeps_issue_but_removes_identifiers() -> None:
    decision = prepare_external_legal_query(
        "實務見解 當事人王小明 A123456789 0912345678 2026-0008 民法第184條因果關係"
    )
    assert decision.external_allowed is True
    assert "王小明" not in decision.safe_query
    assert "A123456789" not in decision.safe_query
    assert "0912345678" not in decision.safe_query
    assert "2026-0008" not in decision.safe_query
    assert "民法第184條" in decision.safe_query
    assert sanitize_tlr_query(
        "實務見解 當事人王小明 A123456789 民法第184條因果關係"
    ) == decision.safe_query


def test_private_narrative_without_legal_issue_fails_closed() -> None:
    decision = prepare_external_legal_query("客戶附件有薪資與帳戶資料，請幫我找相似案件")
    assert decision.external_allowed is False
    assert "private_narrative_requires_local_abstraction" in decision.reasons


def test_unlabelled_party_name_and_internal_narrative_never_leave_for_mcp() -> None:
    decision = prepare_external_legal_query(
        "王小明在本所案件中主張車禍受傷，請查民法第184條與相似裁判"
    )
    assert decision.external_allowed is False
    assert "王小明" not in decision.safe_query
    assert "主張車禍受傷" in decision.safe_query
    assert "未標籤姓名" in decision.redactions
    assert "unlabelled_person_name_requires_local_abstraction" in decision.reasons
    assert "private_narrative_requires_local_abstraction" in decision.reasons


def test_explicit_public_judge_and_court_docket_remain_searchable() -> None:
    decision = prepare_external_legal_query(
        "臺灣臺北地方法院115年度訴字第12號 法官王大明 裁判理由"
    )
    assert decision.external_allowed is True
    assert "115年度訴字第12號" in decision.safe_query
    assert "王大明" in decision.safe_query


def test_reasoning_spans_exclude_party_argument_and_map_exact_source() -> None:
    spans = supporting_spans("民法184條 因果關係 舉證責任", {"full_text": REASONING})
    assert spans
    assert all(span["source_exact"] for span in spans)
    assert any("本院認為" in span["text"] for span in spans)
    assert not any(span["text"].startswith("原告主張") for span in spans)


def test_dual_axis_ranking_and_practice_card_are_explainable() -> None:
    items = enrich_and_rank_items(
        "民法184條 因果關係 舉證責任",
        [
            {
                "title": "臺灣臺北地方法院 115年度訴字第12號",
                "court_name": "臺灣臺北地方法院",
                "full_text": REASONING,
                "verification_state": VERIFIED_LOCAL,
                "source": "court_judgments_local",
            },
            {
                "title": "最高法院 114年度台上字第99號",
                "court_name": "最高法院",
                "full_text": "本院認為契約解除之要件應依個案判斷。",
                "verification_state": EXTERNAL_CANDIDATE,
                "source": "tw_legal_rag_tlr",
            },
        ],
    )
    assert items[0]["verification_state"] == VERIFIED_LOCAL
    assert items[0]["draft_eligible"] is True
    assert items[1]["authority_score"] > items[0]["authority_score"]
    assert items[0]["similarity_score"] > items[1]["similarity_score"]
    card = build_practice_view_card("民法184條 因果關係 舉證責任", items[0])
    assert card["schema"] == "magi.practice-view-card/v2"
    assert card["rule"]
    assert card["application"]
    assert card["support_spans"]
    assert card["draft_eligible"] is True


def test_grounded_summary_has_span_citations_and_no_freeform_hallucination() -> None:
    result = grounded_summary_evidence(REASONING, query="侵權行為因果關係")
    assert result["ok"] is True
    assert "[S1]" in result["summary"]
    validation = validate_span_citations(result["summary"], result["evidence"])
    assert validation["ok"] is True
    bad = validate_span_citations(result["summary"] + "\n另依 [S99]。", result["evidence"])
    assert bad["ok"] is False
    assert bad["unknown"] == ["S99"]


def test_citation_lock_allows_only_verified_source_with_support_span() -> None:
    verified = {
        "citation_id": "J1",
        "citation_text": "臺灣臺北地方法院115年度訴字第12號",
        "verification_state": VERIFIED_LOCAL,
        "draft_eligible": True,
        "support_spans": [{"text": "本院認為因果關係未證明。", "source_exact": True}],
    }
    external = {
        "citation_id": "J2",
        "citation_text": "最高法院114年度台上字第99號",
        "verification_state": EXTERNAL_CANDIDATE,
        "draft_eligible": False,
        "support_spans": [{"text": "外部候選。", "source_exact": True}],
    }
    lock = citation_lock_for_items([verified, external])
    assert len(lock["allowed"]) == 1
    assert len(lock["rejected"]) == 1
    good = validate_text_against_citation_lock(
        "依臺灣臺北地方法院115年度訴字第12號判決意旨。",
        lock,
    )
    assert good["ok"] is True
    bad = validate_text_against_citation_lock(
        "另依最高法院114年度台上字第99號判決意旨。",
        lock,
    )
    assert bad["ok"] is False
    assert bad["violations"]


def test_citation_lock_rejects_non_exact_or_detached_support_span() -> None:
    base = {
        "citation_text": "臺灣臺北地方法院115年度訴字第12號",
        "verification_state": VERIFIED_LOCAL,
        "draft_eligible": True,
        "full_text": "本院認為因果關係未證明。",
    }
    non_exact = citation_lock_for_items(
        [{**base, "support_spans": [{"text": "本院認為因果關係未證明。", "source_exact": False}]}]
    )
    detached = citation_lock_for_items(
        [{**base, "support_spans": [{"text": "精神慰撫金一律不得超過十萬元。", "source_exact": True}]}]
    )
    assert non_exact["allowed"] == []
    assert detached["allowed"] == []
    assert non_exact["rejected"][0]["rejection_reason"] == "missing_exact_source_support"
    assert detached["rejected"][0]["rejection_reason"] == "missing_exact_source_support"


def test_draft_context_excludes_external_candidate_and_embeds_citation_lock(monkeypatch) -> None:
    from api.osc import drafts

    monkeypatch.setattr(drafts, "_osc_collect_insights", lambda: [])
    monkeypatch.setattr(drafts, "_osc_exec", lambda *_args, **_kwargs: ({}, None))
    monkeypatch.setattr(drafts, "_osc_get_setting_value", lambda _key, default="": default)
    monkeypatch.setattr(
        drafts,
        "_get_draft_prompt_template",
        lambda: (
            "案情：{case_facts}\n實務：{legal_insights}\n"
            "可引用：{citation_lock}\n範本：{reference_style}"
        ),
    )
    payload = {
        "doc_type": "民事答辯狀",
        "case_number": "115年度訴字第12號",
        "defendant": "被告",
        "case_facts": "侵權行為因果關係有爭議。",
        "selected_insights": [
            {
                "id": "verified",
                "title": "臺灣臺北地方法院115年度訴字第12號",
                "citation_text": "臺灣臺北地方法院115年度訴字第12號",
                "full_text": REASONING,
                "summary": "法院認為應證明相當因果關係。",
                "verification_state": VERIFIED_LOCAL,
                "draft_eligible": True,
                "support_spans": [
                    {
                        "text": "本院認為原告依民事訴訟法第277條應就損害與行為間之因果關係負舉證責任；本件未提出足以證明相當因果關係之資料，故其請求不得准許。",
                        "source_exact": True,
                    }
                ],
            },
            {
                "id": "external",
                "title": "最高法院114年度台上字第99號",
                "citation_text": "最高法院114年度台上字第99號",
                "full_text": "外部候選，尚未核對官方全文。",
                "summary": "外部候選。",
                "verification_state": EXTERNAL_CANDIDATE,
                "draft_eligible": False,
                "support_spans": [{"text": "外部候選。", "source_exact": True}],
            },
        ],
    }
    context = drafts._osc_build_draft_context(payload)
    assert len(context["selected_insights"]) == 2
    assert len(context["citeable_insights"]) == 1
    assert len(context["citation_lock"]["allowed"]) == 1
    assert len(context["citation_lock"]["rejected"]) == 1
    assert "臺灣臺北地方法院115年度訴字第12號" in context["prompt"]
    assert "最高法院114年度台上字第99號" not in context["prompt"]
    assert "unverified_insights_excluded:1" in context["warnings"]


def test_legal_summary_falls_back_to_source_mapped_card_without_local_model(monkeypatch) -> None:
    from api.osc import utils

    monkeypatch.setenv("MAGI_LEGAL_SUMMARY_USE_NIM", "0")
    summary = utils._osc_summarize_legal_insight(REASONING)
    assert "法律規則與法院見解" in summary
    assert "法院涵攝" in summary
    assert "[S1]" in summary
    assert "模型" not in summary


def test_nvidia_drafting_route_is_heavy_pii_scrubbed_and_has_no_silent_fallback(monkeypatch) -> None:
    from api.osc import drafts
    from skills.bridge import nim_heavy

    observed = {}

    def fake_run_nim_chat(**kwargs):
        observed.update(kwargs)
        return {
            "success": True,
            "response": "民事答辯狀\n（待律師確認）",
            "model": "nvidia/nemotron-test",
        }

    monkeypatch.setattr(nim_heavy, "run_nim_chat", fake_run_nim_chat)
    text, model = drafts._osc_generate_draft_with_nvidia("只使用白名單來源。")
    assert "民事答辯狀" in text
    assert model == "nvidia/nemotron-test"
    assert observed["heavy"] is True
    assert observed["require_pii_scrub"] is True
    assert observed["task_type"] == "legal_drafting"
    assert "[PERSON-000]" in observed["system_prompt"]
    assert "不得杜撰裁判" in observed["system_prompt"]
    assert "不得翻譯、改寫、刪除或猜測原值" in observed["system_prompt"]


def test_external_candidate_promotes_only_on_exact_official_local_fulltext(monkeypatch) -> None:
    from api.domains import judgment_flow

    class FakeDB:
        def execute(self, *_args, **_kwargs):
            return {
                "jid": "TPD,115,訴,12,20260101,1",
                "court_name": "臺灣臺北地方法院",
                "case_number": "115年度訴字第12號",
                "case_type": "侵權行為",
                "judgment_date": "2026-01-01",
                "summary": "官方摘要",
                "full_text": REASONING,
                "source_url": "https://judgment.judicial.gov.tw/example",
            }

    monkeypatch.setattr(judgment_flow, "_get_local_db_manager", lambda: FakeDB())
    result = judgment_flow._verify_external_candidates_against_local(
        "民法184條 因果關係",
        {
            "success": True,
            "items": [
                {
                    "source": "tw_legal_rag_tlr",
                    "doc_id": "TPD,115,訴,12,20260101,1",
                    "citation_text": "臺灣臺北地方法院115年度訴字第12號",
                    "summary_full": "外部節錄不得作正式引用。",
                }
            ],
        },
    )
    item = result["items"][0]
    assert item["verification_state"] == VERIFIED_LOCAL
    assert item["draft_eligible"] is True
    assert item["official_local_match"] is True
    assert item["full_text"] == REASONING
    assert item["support_spans"]


def test_legacy_tlr_cache_cannot_self_verify_as_official(monkeypatch) -> None:
    from api.domains import judgment_flow

    class FakeDB:
        def execute(self, *_args, **_kwargs):
            return {
                "jid": "TPD,115,訴,12,20260101,1",
                "court_name": "臺灣臺北地方法院",
                "case_number": "115年度訴字第12號",
                "case_type": "侵權行為",
                "judgment_date": "2026-01-01",
                "summary": "舊外部快取",
                "full_text": REASONING,
                "source_url": "https://tlr.dr-lawbot.com/document/example",
            }

    monkeypatch.setattr(judgment_flow, "_get_local_db_manager", lambda: FakeDB())
    result = judgment_flow._verify_external_candidates_against_local(
        "民法184條 因果關係",
        {
            "success": True,
            "items": [
                {
                    "source": "tw_legal_rag_tlr",
                    "doc_id": "TPD,115,訴,12,20260101,1",
                    "citation_text": "臺灣臺北地方法院115年度訴字第12號",
                    "summary_full": "外部節錄。",
                }
            ],
        },
    )
    item = result["items"][0]
    assert item["verification_state"] == EXTERNAL_CANDIDATE
    assert item["draft_eligible"] is False
    assert not item.get("official_local_match")
