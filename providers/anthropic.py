from __future__ import annotations

from typing import Any, Iterable

from .base import ProviderAdapter


class AnthropicProvider(ProviderAdapter):
    name = "anthropic"
    default_base_url = "https://api.anthropic.com"
    base_url_env = "ANTHROPIC_BASE_URL"
    api_key_env = "ANTHROPIC_API_KEY"
    model_env = "ANTHROPIC_MODEL"
    default_model = "claude-3-5-sonnet-latest"
    health_path = "/v1/models"
    health_ok_statuses = (200,)
    requires_api_key = True

    def build_headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["x-api-key"] = self.api_key
        headers["anthropic-version"] = "2023-06-01"
        return headers

    def build_chat_payload(self, messages: Iterable[dict[str, Any]] | str, **kwargs) -> dict[str, Any]:
        normalized = self._normalize_messages(messages)
        system = kwargs.pop("system", "")
        payload = {
            "model": (kwargs.pop("model", "") or self.model or self.default_model).strip(),
            "messages": normalized,
            "max_tokens": int(kwargs.pop("max_tokens", 1024)),
        }
        if system:
            payload["system"] = system
        if "temperature" in kwargs and kwargs["temperature"] is not None:
            payload["temperature"] = kwargs.pop("temperature")
        payload.update(kwargs)
        return payload

