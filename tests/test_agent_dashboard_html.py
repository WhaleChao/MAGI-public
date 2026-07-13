from __future__ import annotations

from pathlib import Path

from gui.magi_menubar import _agent_status_from_public_payload


ROOT = Path(__file__).resolve().parents[1]


def test_public_agent_status_allowlists_only_safe_summary_fields():
    status = _agent_status_from_public_payload(
        {
            "intent_category": "laf",
            "plan_steps": [
                {"id": "route", "state": "done"},
                {"id": "execute", "state": "running"},
                {"id": "unknown", "state": "done", "label": "private plan text"},
            ],
            "tool_category": "fetch",
            "model_id": "gemma-4-e4b-it-4bit",
            "waiting_confirmation": True,
            "retry_count": 2,
            "route_confidence": 0.82,
            "success_rate_7d": 97,
            "prompt": "private user request",
            "message": "private message body",
            "thought": "private internal reasoning",
        }
    )

    assert status["state"] == "waiting"
    assert status["intent"] == "法扶作業"
    assert status["plan"] == "選擇路由：完成；執行工具：進行中"
    assert status["tool"] == "擷取工具"
    assert status["model"] == "gemma-4-e4b-it-4bit"
    assert status["confirmation"] == "等待確認"
    assert status["retry"] == "2 次"
    assert status["route_confidence"] == "82%"
    assert status["success_rate_7d"] == "97%"
    assert "private" not in status["detail"]


def test_public_agent_status_uses_no_activity_for_missing_or_untrusted_data():
    status = _agent_status_from_public_payload(
        {
            "intent": "raw private intent",
            "plan": "raw private plan",
            "tool": "raw private tool",
            "model": "raw private model",
            "message": "private message",
        }
    )

    assert status["state"] == "idle"
    assert status["label"] == "尚無活動"
    assert status["intent"] == "尚無活動"


def test_nerv_dashboard_has_a_public_agent_status_panel_and_client_allowlist():
    html = (ROOT / "templates" / "dashboard_nerv.html").read_text(encoding="utf-8")
    normalizer = html.split("function publicAgentStatus(raw)", 1)[1].split("function renderAgentStatus(raw)", 1)[0]

    assert 'id="agent-status-grid"' in html
    for label in ("最近意圖", "計畫步驟", "工具／模型", "等待確認", "重試", "路由信心", "七日成功率"):
        assert label in html
    assert "/static/agent_status_public_latest.json" in html
    assert "不含訊息內容或內部推理" in html
    for safe_key in ("intent_category", "plan_steps", "tool_category", "model_id", "waiting_confirmation", "retry_count", "route_confidence", "success_rate_7d"):
        assert f"payload.{safe_key}" in normalizer
    for forbidden_key in ("payload.prompt", "payload.message", "payload.content", "payload.thought", "payload.reasoning"):
        assert forbidden_key not in normalizer
