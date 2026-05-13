from __future__ import annotations

import importlib


def test_omlx_chat_resolves_stale_env_model_to_loaded_model(monkeypatch):
    monkeypatch.setenv("MAGI_DEFAULT_MODEL", "gemma-4-26b-a4b-it-4bit")
    import skills.bridge.llm_direct as direct

    direct = importlib.reload(direct)
    seen: dict[str, str] = {}

    class FakeResponse:
        status_code = 200
        text = "{}"

        def __init__(self, payload):
            self._payload = payload

        def json(self):
            return self._payload

        def raise_for_status(self):
            return None

    class FakeSession:
        def get(self, url, timeout=0):
            return FakeResponse({"data": [{"id": "gemma-4-e4b-it-4bit"}]})

        def post(self, url, headers=None, json=None, timeout=0):
            seen["model"] = json["model"]
            return FakeResponse(
                {
                    "choices": [{"message": {"content": "好"}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                }
            )

    monkeypatch.setattr(direct, "_get_session", lambda: FakeSession())

    result = direct.chat(prompt="ping", feature="general", provider="omlx", max_tokens=4, timeout=3)

    assert result["success"] is True
    assert result["model"] == "gemma-4-e4b-it-4bit"
    assert seen["model"] == "gemma-4-e4b-it-4bit"
