"""Metadata guard regression tests."""

from api.tw_output_guard import mark_non_authoritative_context, mark_unverified_reply


def test_mark_non_authoritative_context_adds_banner():
    out = mark_non_authoritative_context(
        "這段是摘要內容",
        label="歷史摘要",
        source="模型壓縮",
    )

    assert out.startswith("【歷史摘要｜非原文｜僅供延續上下文】")
    assert "來源：模型壓縮" in out
    assert out.endswith("這段是摘要內容")


def test_mark_unverified_reply_adds_banner():
    out = mark_unverified_reply(
        "目前沒有可驗證結果，請稍後重試。",
        reason="查詢逾時（>120s）",
    )

    assert out.startswith("【未驗證回覆｜查詢逾時（>120s）】")
    assert "目前沒有可驗證結果" in out
