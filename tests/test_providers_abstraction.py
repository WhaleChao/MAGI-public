from __future__ import annotations

from dataclasses import dataclass

import pytest


@dataclass
class _FakeResponse:
    status_code: int = 200
    text: str = "ok"


class _FakeSession:
    def __init__(self, response_map: dict[str, _FakeResponse]):
        self.response_map = response_map
        self.calls: list[tuple[str, dict, float]] = []

    def get(self, url, headers=None, timeout=None):
        self.calls.append((url, headers or {}, timeout))
        return self.response_map.get(url, _FakeResponse(status_code=404, text="missing"))


def test_provider_registry_lists_all_known_adapters():
    from providers import build_provider_registry, get_provider_adapter, list_provider_names

    registry = build_provider_registry()

    assert set(registry) == {"omlx", "openai", "anthropic", "ollama", "nvidia_nim"}
    assert list_provider_names() == ["anthropic", "nvidia_nim", "ollama", "omlx", "openai"]
    assert get_provider_adapter("omlx") is not None


def test_omlx_provider_builds_openai_style_payload_and_health_url():
    from api.model_config import TEXT_PRIMARY_MODEL
    from providers.omlx import OmlxProvider

    provider = OmlxProvider()
    payload = provider.build_chat_payload("hello world", temperature=0.2, max_tokens=32)
    assert payload["model"] == TEXT_PRIMARY_MODEL
    assert payload["messages"] == [{"role": "user", "content": "hello world"}]
    assert payload["stream"] is False
    assert payload["temperature"] == 0.2
    assert payload["max_tokens"] == 32

    url = provider.build_url(provider.health_path)
    assert url.endswith("/v1/models")

    session = _FakeSession({url: _FakeResponse(status_code=200, text='{"data": []}')})
    health = provider.health_check(session=session, timeout=1)

    assert health.available is True
    assert health.status_code == 200
    assert health.payload["url"] == url


def test_openai_provider_requires_key_and_builds_payload(monkeypatch):
    from providers.openai import OpenAIProvider

    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
    provider = OpenAIProvider()
    payload = provider.build_chat_payload(
        [{"role": "system", "content": "x"}, {"role": "user", "content": "y"}],
        model="gpt-4.1-mini",
        stream=True,
        temperature=0.4,
        max_tokens=99,
    )

    assert payload["model"] == "gpt-4.1-mini"
    assert payload["stream"] is True
    assert payload["temperature"] == 0.4
    assert payload["max_tokens"] == 99
    assert payload["messages"][0]["role"] == "system"

    url = provider.build_url(provider.health_path)
    session = _FakeSession({url: _FakeResponse(status_code=200, text='{"data": []}')})
    health = provider.health_check(session=session, timeout=1)

    assert health.available is True
    assert health.detail == '{"data": []}'


def test_anthropic_provider_builds_payload_and_health_check(monkeypatch):
    from providers.anthropic import AnthropicProvider

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic-key")
    provider = AnthropicProvider()
    payload = provider.build_chat_payload(
        "請摘要這段文字",
        system="你是摘要助理",
        temperature=0.1,
        max_tokens=256,
    )

    assert payload["model"] == "claude-3-5-sonnet-latest"
    assert payload["system"] == "你是摘要助理"
    assert payload["messages"] == [{"role": "user", "content": "請摘要這段文字"}]
    assert payload["max_tokens"] == 256
    assert payload["temperature"] == 0.1

    url = provider.build_url(provider.health_path)
    session = _FakeSession({url: _FakeResponse(status_code=200, text='{"data": []}')})
    health = provider.health_check(session=session, timeout=1)

    assert health.available is True
    assert health.payload["url"] == url


def test_ollama_provider_builds_payload_and_health_check():
    from providers.ollama import OllamaProvider

    provider = OllamaProvider()
    payload = provider.build_chat_payload("hello", model="llama3.1", options={"temperature": 0.7})

    assert payload["model"] == "llama3.1"
    assert payload["messages"] == [{"role": "user", "content": "hello"}]
    assert payload["options"]["temperature"] == 0.7

    url = provider.build_url(provider.health_path)
    session = _FakeSession({url: _FakeResponse(status_code=200, text='{"models": []}')})
    health = provider.health_check(session=session, timeout=1)

    assert health.available is True
    assert health.status_code == 200
