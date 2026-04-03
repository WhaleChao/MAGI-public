from __future__ import annotations

from unittest.mock import MagicMock

from providers import AnthropicProvider, OllamaProvider, OmlxProvider, OpenAIProvider


def test_omlx_provider_builds_payload_and_healthcheck(monkeypatch):
    import providers.omlx as omux_mod

    provider = OmlxProvider(base_url="http://127.0.0.1:8080", model="taide-12b")
    payload = provider.build_chat_payload("hello")

    assert payload["model"] == "taide-12b"
    assert payload["messages"][0]["content"] == "hello"

    resp = MagicMock()
    resp.status_code = 200
    resp.text = "ok"
    resp.raise_for_status.return_value = None
    resp.json.return_value = {"data": [{"id": "m1"}, {"id": "m2"}]}
    provider.session = MagicMock(get=MagicMock(return_value=resp))

    health = provider.health_check(path="/models")
    assert health.available is True
    assert health.provider == "omlx"
    assert health.payload["url"].endswith("/models")


def test_ollama_provider_builds_payload_and_healthcheck(monkeypatch):
    import providers.ollama as ollama_mod

    provider = OllamaProvider(base_url="http://127.0.0.1:11434", model="llama3")
    payload = provider.build_chat_payload("hello")

    assert payload["model"] == "llama3"
    assert payload["messages"][0]["content"] == "hello"

    resp = MagicMock()
    resp.status_code = 200
    resp.text = "ok"
    resp.raise_for_status.return_value = None
    resp.json.return_value = {"models": [{"name": "llama3"}, {"name": "qwen"}]}
    provider.session = MagicMock(get=MagicMock(return_value=resp))

    health = provider.health_check(path="/api/tags")
    assert health.available is True
    assert health.provider == "ollama"
    assert health.payload["url"].endswith("/api/tags")


def test_openai_and_anthropic_payload_shapes():
    openai = OpenAIProvider(model="gpt-4.1-mini")
    anthropic = AnthropicProvider(model="claude-3-5-sonnet-latest")

    openai_payload = openai.build_chat_payload("hello")
    anthropic_payload = anthropic.build_chat_payload("hello")

    assert openai_payload["messages"][0]["content"] == "hello"
    assert anthropic_payload["messages"][0]["content"] == "hello"
    assert openai_payload["model"] == "gpt-4.1-mini"
    assert anthropic_payload["model"] == "claude-3-5-sonnet-latest"
