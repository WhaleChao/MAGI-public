from __future__ import annotations

import importlib
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

    assert set(registry) == {"omlx", "openai", "anthropic", "ollama", "nvidia_nim", "mlx_mtp"}
    assert list_provider_names() == ["anthropic", "mlx_mtp", "nvidia_nim", "ollama", "omlx", "openai"]
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


def test_omlx_provider_adds_draft_fields_only_when_enabled(monkeypatch):
    monkeypatch.setenv("MAGI_ENABLE_MTP_DRAFT", "1")
    monkeypatch.setenv("MAGI_E4B_DRAFT_MODEL", "e4b-draft")

    import api.model_config as model_config
    import providers.omlx as omlx_mod

    importlib.reload(model_config)
    reloaded_omlx = importlib.reload(omlx_mod)

    provider = reloaded_omlx.OmlxProvider(model="gemma-4-e4b-it-4bit")
    payload = provider.build_chat_payload("hello")

    assert payload["draft_model"] == "e4b-draft"
    assert payload["draft_kind"] == "mtp"
    assert "draft_block_size" in payload

    monkeypatch.setenv("MAGI_ENABLE_MTP_DRAFT", "0")
    importlib.reload(model_config)
    importlib.reload(omlx_mod)


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


def test_mlx_mtp_provider_builds_draft_payload():
    from providers.mlx_mtp import MlxMtpProvider

    provider = MlxMtpProvider(base_url="http://127.0.0.1:8090/v1", model="gemma-4-e4b-it-4bit")
    payload = provider.build_chat_payload("hello", max_tokens=16)

    assert payload["model"] == "gemma-4-e4b-it-4bit"
    assert payload["draft_model"]
    assert payload["draft_kind"] == "mtp"
    assert payload["draft_block_size"] >= 1
