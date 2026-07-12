import json

from api.agentic import shadow


def test_shadow_observation_publishes_only_categorical_state(monkeypatch, tmp_path):
    output = tmp_path / "agent.json"

    def write(snapshot):
        from api.agentic.telemetry import write_public_agent_status

        return write_public_agent_status(snapshot, path=output)

    monkeypatch.setattr(shadow, "write_public_agent_status", write)

    started = shadow.observe_start("請新增明天下午三點的客戶會議")
    finished = shadow.observe_finish("請新增明天下午三點的客戶會議", "請確認是否建立以下行程，請回覆「確認」。")

    assert started["status"] == "running"
    assert finished["status"] == "ready"
    assert finished["intent_category"] == "calendar"
    assert finished["waiting_confirmation"] is True
    raw = output.read_text(encoding="utf-8")
    assert "客戶" not in raw
    assert "明天下午" not in raw
    assert json.loads(raw)["verification"]["status"] == "pending"


def test_shadow_completion_reports_public_failure_without_error_text(monkeypatch, tmp_path):
    output = tmp_path / "agent.json"

    def write(snapshot):
        from api.agentic.telemetry import write_public_agent_status

        return write_public_agent_status(snapshot, path=output)

    monkeypatch.setattr(shadow, "write_public_agent_status", write)
    payload = shadow.observe_finish("執行系統健康檢查", {"error": "private stack and path"}, failed=True)

    assert payload["status"] == "blocked"
    assert payload["error_category"] == "unknown"
    assert "private stack" not in output.read_text(encoding="utf-8")
