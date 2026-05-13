"""Metadata guard regression tests."""

from api.tw_output_guard import mark_non_authoritative_context, mark_unverified_reply, normalize_output_text


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


def test_normalize_output_text_blocks_memory_badge_leak():
    broken = "根據您的 [使用者陳述]，您覺得綠茶滿好喝的。關於您的問題，身為 CAS"

    out = normalize_output_text(broken)

    assert "[使用者陳述]" not in out
    assert "身為 CAS" not in out
    assert "內部判斷文字" in out
