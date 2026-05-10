from __future__ import annotations

from api.osc import drafts


def test_draft_context_excludes_extractive_fast_digest(monkeypatch):
    fast_digest = (
        "## 摘要類型\n"
        "抽取式快篩（主文與理由均取自裁判原文；未經 LLM 改寫）\n\n"
        "## 主文摘錄\n"
        "被告應給付原告新臺幣十萬元。\n\n"
        "## 理由摘錄\n"
        "法院認為被告應負損害賠償責任。"
    )

    monkeypatch.setattr(
        drafts,
        "_osc_collect_insights",
        lambda: [
            {
                "id": "cj-1",
                "title": "抽取式快篩裁判",
                "summary": fast_digest,
                "full_text": "法院全文內容",
                "case_reason": "侵權行為",
                "court": "臺灣高等法院",
            },
            {
                "id": "li-1",
                "title": "可引用見解",
                "summary": "法院明確指出過失與損害間須具相當因果關係。",
                "full_text": "法院明確指出過失與損害間須具相當因果關係。",
                "case_reason": "侵權行為",
                "court": "最高法院",
            },
        ],
    )

    selected = drafts._osc_resolve_draft_insights({"selected_insight_ids": ["cj-1", "li-1"]})

    assert [item["id"] for item in selected] == ["li-1"]
    assert "抽取式快篩" not in str(selected)
