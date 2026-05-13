from pathlib import Path
import importlib.util


ROOT = Path(__file__).resolve().parents[1]
DELIVERY_PATH = ROOT / "skills" / "market-briefing" / "delivery.py"


def _load_delivery():
    spec = importlib.util.spec_from_file_location("market_delivery_test", DELIVERY_PATH)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


def test_market_chat_summary_is_short_and_keeps_full_report_pointer():
    delivery = _load_delivery()
    stock_lines = [
        f"- 股票{i} (T{i}) ↗ 昨收 {100+i:.2f}，預估次一交易日 {101+i:.2f} (+1.23%)，信心 70%｜財報訊號：" + ("很長" * 80)
        for i in range(20)
    ]
    report = "\n".join([
        "📊 MAGI 每日股價預測・深度模式（2026-05-03 08:30）",
        "",
        "【台股】",
        *stock_lines,
        "",
        "整體偏向：多方（平均預估 +1.10% / 風險 中性）",
        "註：此為統計模型推估，非投資建議。",
        "",
        "- 近期方向命中率：62.5%",
    ])
    summary = delivery.build_market_chat_summary(
        report,
        {"success": True, "url": "https://example.test/static/exports/report.txt", "path": "/tmp/report.txt"},
    )
    assert len(summary) <= delivery.CHAT_INLINE_LIMIT
    assert "完整報告：" in summary
    assert "另有" in summary
    assert "整體偏向" in summary


def test_market_chat_summary_falls_back_to_local_path():
    delivery = _load_delivery()
    report = "📊 MAGI 每日股價預測・快速模式\n- AAPL 預估次一交易日 200.00 (+0.10%)"
    summary = delivery.build_market_chat_summary(
        report,
        {"success": True, "path": "/Users/ai/Desktop/MAGI_v2/static/exports/x.txt", "url": ""},
    )
    assert "/Users/ai/Desktop/MAGI_v2/static/exports/x.txt" in summary


def test_market_chat_summary_keeps_committee_only_stock_lines():
    delivery = _load_delivery()
    report = "\n".join([
        "📊 MAGI 每日股價預測・深度模式（2026-05-04 08:59）",
        "",
        "【台股】",
        "- 🚀 [看多] 台積電：委員會綜合評級【BUY】 (信心 80%)",
        "  委員會論點：AI demand remains strong...",
        "",
        "【美股】",
        "- （今日無美股追蹤標的）",
        "",
        "整體偏向：多方（平均預估 +2.00% / 風險 中性）",
    ])
    summary = delivery.build_market_chat_summary(report)
    assert "本次沒有成功產生個股摘要" not in summary
    assert "台積電" in summary
