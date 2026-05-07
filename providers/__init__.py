from __future__ import annotations

from typing import Any

from .anthropic import AnthropicProvider
from .base import OpenAICompatibleProvider, ProviderAdapter, ProviderHealth
from .nvidia_nim import NvidiaNimProvider
from .ollama import OllamaProvider
from .omlx import OmlxProvider
from .openai import OpenAIProvider
from .mlx_mtp import MlxMtpProvider


def build_provider_registry(*, session=None, config: dict[str, dict[str, Any]] | None = None) -> dict[str, ProviderAdapter]:
    config = config or {}
    adapters: list[ProviderAdapter] = [
        OmlxProvider(session=session, **config.get("omlx", {})),
        OpenAIProvider(session=session, **config.get("openai", {})),
        AnthropicProvider(session=session, **config.get("anthropic", {})),
        OllamaProvider(session=session, **config.get("ollama", {})),
        NvidiaNimProvider(session=session, **config.get("nvidia_nim", {})),
        MlxMtpProvider(session=session, **config.get("mlx_mtp", {})),
    ]
    return {adapter.name: adapter for adapter in adapters}


def get_provider_adapter(name: str, *, session=None, config: dict[str, dict[str, Any]] | None = None) -> ProviderAdapter | None:
    return build_provider_registry(session=session, config=config).get(str(name or "").strip().lower())


def list_provider_names() -> list[str]:
    return sorted(build_provider_registry().keys())


__all__ = [
    "AnthropicProvider",
    "NvidiaNimProvider",
    "MlxMtpProvider",
    "OllamaProvider",
    "OmlxProvider",
    "OpenAICompatibleProvider",
    "OpenAIProvider",
    "ProviderAdapter",
    "ProviderHealth",
    "build_provider_registry",
    "get_provider_adapter",
    "list_provider_names",
]
