from skills.reasoning.wfgy import apply_wfgy_logic
from skills.bridge import melchior_client


def test_retired_wfgy_apply_is_noop():
    prompt = "請用繁體中文整理判決重點。"

    assert apply_wfgy_logic(prompt) == prompt
    assert "EXECUTE WFGY" not in apply_wfgy_logic(prompt)
    assert "thought process" not in apply_wfgy_logic(prompt).lower()


def test_melchior_reason_ignores_legacy_wfgy_flag(monkeypatch):
    seen = {}

    def fake_chat(prompt, timeout=300):
        seen["prompt"] = prompt
        seen["timeout"] = timeout
        return {"success": True, "response": "ok"}

    monkeypatch.setattr(melchior_client, "chat", fake_chat)

    result = melchior_client.reason("原始問題", use_wfgy=True, timeout=12)

    assert result["success"] is True
    assert seen == {"prompt": "原始問題", "timeout": 12}
