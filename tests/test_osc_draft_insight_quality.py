from __future__ import annotations

from api.osc import drafts
from api.osc import judicial


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


def test_insight_page_hides_extractive_fast_digest(monkeypatch):
    fast_digest = (
        "## 摘要類型\n"
        "抽取式快篩（主文與理由均取自裁判原文；未經 LLM 改寫）\n\n"
        "## 主文摘錄\n"
        "被告應給付原告新臺幣十萬元。\n\n"
        "## 理由摘錄\n"
        "法院認為被告應負損害賠償責任。"
    )

    class FakeCursor:
        def __init__(self):
            self.calls = 0

        def execute(self, _sql):
            self.calls += 1

        def fetchall(self):
            if self.calls == 1:
                return []
            return [
                {
                    "id": 1,
                    "jid": "J-fast",
                    "court_name": "臺灣高等法院",
                    "case_number": "114年度上字第1號",
                    "case_type": "侵權行為",
                    "judgment_date": "2026-01-01",
                    "summary": fast_digest,
                    "full_text": fast_digest,
                    "source_url": "https://judgment.local/fast",
                    "crawled_at": "2026-01-02 00:00:00",
                },
                {
                    "id": 2,
                    "jid": "J-good",
                    "court_name": "最高法院",
                    "case_number": "114年度台上字第2號",
                    "case_type": "侵權行為",
                    "judgment_date": "2026-01-03",
                    "summary": "法院明確指出過失與損害間須具相當因果關係。",
                    "full_text": "法院明確指出過失與損害間須具相當因果關係。",
                    "source_url": "https://judgment.local/good",
                    "crawled_at": "2026-01-04 00:00:00",
                },
            ]

        def close(self):
            pass

    class FakeConn:
        def cursor(self, dictionary=True):
            return FakeCursor()

        def close(self):
            pass

    monkeypatch.delenv("MAGI_SHOW_FAST_INSIGHT_CANDIDATES", raising=False)
    monkeypatch.setattr(judicial, "_osc_web_connect", lambda: (FakeConn(), {}))
    monkeypatch.setattr(judicial.os.path, "exists", lambda _path: False)

    items = judicial._osc_collect_insights()

    assert [item["id"] for item in items] == ["cj-2"]
    assert "抽取式快篩" not in str(items)
