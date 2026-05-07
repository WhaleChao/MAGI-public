from __future__ import annotations

import requests
from typing import Any, Iterable

from .base import OpenAICompatibleProvider, ProviderHealth


class OllamaProvider(OpenAICompatibleProvider):
    name = "ollama"
    default_base_url = "http://127.0.0.1:11434"
    base_url_env = "OLLAMA_BASE_URL"
    api_key_env = ""
    model_env = "OLLAMA_MODEL"
    default_model = "llama3.1"
    health_path = "/api/tags"
    requires_api_key = False

    def build_chat_payload(self, messages: Iterable[dict[str, Any]] | str, **kwargs) -> dict[str, Any]:
        options = dict(kwargs.pop("options", {}) or {})
        payload = {
            "model": (kwargs.pop("model", "") or self.model or self.default_model).strip(),
            "messages": self._normalize_messages(messages),
            "stream": bool(kwargs.pop("stream", False)),
        }
        if "temperature" in kwargs and kwargs["temperature"] is not None:
            options.setdefault("temperature", kwargs.pop("temperature"))
        if "max_tokens" in kwargs and kwargs["max_tokens"] is not None:
            options.setdefault("num_predict", kwargs.pop("max_tokens"))
        if options:
            payload["options"] = options
        payload.update(kwargs)
        return payload

    def healthcheck(self, *, timeout: float = 5.0):
        try:
            resp = requests.get(self.build_url(self.health_path), headers=self.build_headers(), timeout=float(timeout))
            resp.raise_for_status()
            payload = resp.json() or {}
            models = payload.get("models") or []
            count = len(models)
            return ProviderHealth(
                provider=self.name,
                available=True,
                base_url=self.base_url,
                model=self.model,
                status_code=getattr(resp, "status_code", 200),
                detail=f"{count} models",
                payload={"models": models},
            )
        except Exception as exc:
            return ProviderHealth(
                provider=self.name,
                available=False,
                base_url=self.base_url,
                model=self.model,
                detail=str(exc),
                payload={"url": self.build_url(self.health_path)},
            )
