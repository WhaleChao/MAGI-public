from __future__ import annotations

from api.domains import judgment_flow


def test_extract_judgment_payload_supports_practical_insight_prefix():
    payload, err = judgment_flow.extract_judgment_collect_payload("實務見解 預售屋遲延交屋")
    assert err == ""
    assert payload == {"case_reason": "預售屋遲延交屋"}


def test_practical_insight_result_combines_statutes_and_judgments():
    text = judgment_flow.format_practical_insight_result(
        "預售屋遲延交屋",
        {
            "success": True,
            "items": [
                {
                    "title": "最高法院 114 台上 3753",
                    "summary_preview": "法院指出，交屋遲延仍應依契約與損害證明審酌。",
                    "url": "https://judgment.example/1",
                }
            ],
        },
        {
            "ok": True,
            "items": [
                {
                    "source": "statute|law=民法|article=第184條",
                    "content": "因故意或過失，不法侵害他人權利者，負損害賠償責任。",
                }
            ],
        },
    )
    assert "適用法規" in text
    assert "民法 第184條" in text
    assert "最高法院 114 台上 3753" in text


def test_run_judgment_collector_routes_practical_insight(monkeypatch):
    monkeypatch.setattr(judgment_flow, "run_practical_insight_command", lambda orch, message, notify=False: "PRACTICAL")
    assert judgment_flow.run_judgment_collector_command(object(), "實務見解 侵權行為", notify=False) == "PRACTICAL"


def test_legal_research_request_includes_regulation_and_constitutional_queries():
    assert judgment_flow._is_legal_research_request("查法條 民法第184條")
    assert judgment_flow._is_legal_research_request("查釋字 748")
    assert judgment_flow._is_legal_research_request("查裁判 預售屋遲延交屋")


def test_direct_regulation_query_uses_mcp(monkeypatch):
    monkeypatch.setattr(judgment_flow, "taiwan_legal_mcp_enabled", lambda: True)
    monkeypatch.setattr(judgment_flow, "taiwan_legal_mcp_available", lambda: True)
    monkeypatch.setattr(
        judgment_flow,
        "call_taiwan_legal_tool",
        lambda tool, **kwargs: {
            "success": True,
            "law": {"name": kwargs["law_name"]},
            "articles": [{"article_no": kwargs["article_no"], "content": "因故意或過失，不法侵害他人權利者，負損害賠償責任。"}],
            "source_url": "https://law.moj.gov.tw/example",
        },
    )

    text = judgment_flow.run_judgment_collector_command(None, "查法條 民法第184條", notify=False)
    assert "台灣法律資料庫 MCP" in text
    assert "民法" in text
    assert "第 184 條" in text


def test_practical_insight_falls_back_to_local_archive(monkeypatch):
    def _fake_run_skill_json(skill_script, task, timeout_sec):
        if "judgment-collector" in skill_script:
            return {"success": False, "error": "http_500"}
        return {
            "ok": True,
            "items": [
                {
                    "source": "statute|law=民法|article=第184條",
                    "content": "因故意或過失，不法侵害他人權利者，負損害賠償責任。",
                }
            ],
        }

    monkeypatch.setattr(judgment_flow, "_run_skill_json", _fake_run_skill_json)
    monkeypatch.setattr(
        judgment_flow,
        "_search_local_judgment_archive",
        lambda query, limit=3: {
            "success": True,
            "source_label": "本地判決庫 fallback",
            "items": [
                {
                    "title": "臺灣高等法院 侵權行為損害賠償",
                    "summary_preview": "本地 archive 摘要。",
                    "url": "https://judgment.local/1",
                }
            ],
        },
    )

    text = judgment_flow.run_practical_insight_command(None, "實務見解 侵權行為", notify=False)
    assert "本地判決庫 fallback" in text
    assert "臺灣高等法院 侵權行為損害賠償" in text


def test_practical_insight_augments_with_taiwan_legal_mcp(monkeypatch):
    def _fake_run_skill_json(skill_script, task, timeout_sec):
        if "judgment-collector" in skill_script:
            return {
                "success": True,
                "source_label": "本地實務見解庫",
                "items": [
                    {
                        "title": "本地見解",
                        "summary_preview": "本地摘要。",
                        "url": "https://judgment.local/1",
                    }
                ],
            }
        return {"ok": True, "items": []}

    monkeypatch.setattr(judgment_flow, "_run_skill_json", _fake_run_skill_json)
    monkeypatch.setattr(judgment_flow, "taiwan_legal_mcp_enabled", lambda: True)
    monkeypatch.setattr(judgment_flow, "taiwan_legal_mcp_available", lambda: True)
    monkeypatch.setattr(
        judgment_flow,
        "search_practical_judgments_via_mcp",
        lambda query, case_type="", limit=3, fulltext_limit=1: {
            "success": True,
            "source_label": "台灣法律資料庫 MCP（司法院公開資料）",
            "items": [
                {
                    "title": "MCP 司法院見解",
                    "summary_preview": "MCP 摘要。",
                    "url": "https://judgment.judicial.gov.tw/example",
                }
            ],
        },
    )

    text = judgment_flow.run_practical_insight_command(None, "實務見解 遲延交屋", notify=False)
    assert "本地見解" in text
    assert "MCP 司法院見解" in text
    assert "台灣法律資料庫 MCP" in text


def test_judgment_search_success_also_augments_with_mcp(monkeypatch):
    def _fake_run_skill_json(skill_script, task, timeout_sec):
        return {
            "success": True,
            "source_label": "原判決搜尋",
            "items": [
                {
                    "title": "原搜尋結果",
                    "summary_preview": "原搜尋摘要。",
                    "url": "https://judgment.local/original",
                }
            ],
        }

    monkeypatch.setattr(judgment_flow, "_run_skill_json", _fake_run_skill_json)
    monkeypatch.setattr(judgment_flow, "taiwan_legal_mcp_enabled", lambda: True)
    monkeypatch.setattr(judgment_flow, "taiwan_legal_mcp_available", lambda: True)
    monkeypatch.setattr(
        judgment_flow,
        "search_practical_judgments_via_mcp",
        lambda query, case_type="", limit=3, fulltext_limit=1: {
            "success": True,
            "source_label": "台灣法律資料庫 MCP（司法院公開資料）",
            "items": [
                {
                    "title": "MCP 補強結果",
                    "summary_preview": "MCP 補強摘要。",
                    "url": "https://judgment.judicial.gov.tw/example",
                }
            ],
        },
    )

    text = judgment_flow.run_judgment_collector_command(None, "查判決 遲延交屋", notify=False)
    assert "原搜尋結果" in text
    assert "MCP 補強結果" in text
    assert "台灣法律資料庫 MCP" in text


def test_format_practical_insight_prefers_non_degraded_items():
    text = judgment_flow.format_practical_insight_result(
        "侵權行為",
        {
            "success": True,
            "source_label": "本地判決庫 fallback",
            "items": [
                {
                    "title": "正常摘要案例",
                    "summary_preview": "法院認為應依過失與因果關係判斷損害賠償責任。",
                    "url": "https://judgment.local/good",
                    "is_degraded": False,
                }
            ],
        },
        {
            "ok": True,
            "items": [
                {
                    "source": "statute|law=民法|article=第184條",
                    "content": "因故意或過失，不法侵害他人權利者，負損害賠償責任。",
                }
            ],
        },
    )

    assert "正常摘要案例" in text
    assert "系統降級回覆" not in text


def test_practical_insight_labels_extractive_fast_digest():
    fast_digest = (
        "## 摘要類型\n"
        "抽取式快篩（主文與理由均取自裁判原文；未經 LLM 改寫）\n\n"
        "## 主文摘錄\n"
        "被告應給付原告新臺幣十萬元。\n\n"
        "## 理由摘錄\n"
        "法院認為被告應負損害賠償責任。"
    )
    text = judgment_flow.format_practical_insight_result(
        "侵權行為",
        {
            "success": True,
            "source_label": "本地實務見解庫",
            "items": [
                {
                    "title": "臺灣高等法院 114年度上字第1號",
                    "summary_preview": fast_digest,
                }
            ],
        },
        {"ok": True, "items": []},
    )

    assert "抽取式快篩，僅供定位原文" in text
    assert "引用或生成書狀前請核對裁判全文" in text
