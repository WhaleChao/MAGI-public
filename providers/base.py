from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Iterable

import requests


@dataclass(slots=True)
class ProviderHealth:
    provider: str
    available: bool
    base_url: str = ""
    model: str = ""
    status_code: int | None = None
    detail: str = ""
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "available": bool(self.available),
            "base_url": self.base_url,
            "model": self.model,
            "status_code": self.status_code,
            "detail": self.detail,
            "payload": dict(self.payload),
        }

    @property
    def healthy(self) -> bool:
        return self.available


class ProviderAdapter(ABC):
    name = "base"
    default_base_url = ""
    base_url_env = ""
    api_key_env = ""
    model_env = ""
    default_model = ""
    health_path = "/health"
    health_ok_statuses = (200,)
    requires_api_key = False

    def __init__(
        self,
        base_url: str = "",
        api_key: str = "",
        model: str = "",
        session: requests.Session | None = None,
    ) -> None:
        self.base_url = self.resolve_base_url(base_url)
        self.api_key = self.resolve_api_key(api_key)
        self.model = self.resolve_model(model)
        self.session = session

    @staticmethod
    def _env(name: str) -> str:
        if not name:
            return ""
        import os

        return str(os.environ.get(name, "")).strip()

    @staticmethod
    def _normalize_base_url(value: str) -> str:
        return str(value or "").strip().rstrip("/")

    @staticmethod
    def _normalize_messages(messages: Iterable[dict[str, Any]] | str) -> list[dict[str, Any]]:
        if isinstance(messages, str):
            return [{"role": "user", "content": messages}]
        normalized: list[dict[str, Any]] = []
        for message in messages:
            if not isinstance(message, dict):
                continue
            role = str(message.get("role") or "user").strip() or "user"
            normalized.append({"role": role, "content": message.get("content")})
        return normalized

    def resolve_base_url(self, base_url: str = "") -> str:
        return self._normalize_base_url(base_url or self._env(self.base_url_env) or self.default_base_url)

    def resolve_api_key(self, api_key: str = "") -> str:
        return (api_key or self._env(self.api_key_env)).strip()

    def resolve_model(self, model: str = "") -> str:
        env_model = self._env(self.model_env)
        return (model or env_model or self.default_model).strip()

    def build_url(self, path: str) -> str:
        base = self.base_url.rstrip("/")
        suffix = str(path or "").lstrip("/")
        if not base:
            return f"/{suffix}" if suffix else ""
        return f"{base}/{suffix}" if suffix else base

    def build_headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key and self.api_key_env:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def build_health_request(self) -> dict[str, Any]:
        return {"method": "GET", "path": self.health_path, "headers": self.build_headers()}

    def health_check(
        self,
        *,
        session: requests.Session | None = None,
        timeout: float = 5.0,
        path: str = "",
    ) -> ProviderHealth:
        if self.requires_api_key and not self.api_key:
            return ProviderHealth(
                provider=self.name,
                available=False,
                base_url=self.base_url,
                model=self.model,
                detail="missing_api_key",
                payload={"health_path": path or self.health_path},
            )

        client = session or self.session or requests
        url = self.build_url(path or self.health_path)
        try:
            resp = client.get(url, headers=self.build_headers(), timeout=float(timeout))
            ok = int(resp.status_code) in set(self.health_ok_statuses)
            detail = (resp.text or "").strip()[:240] if ok else f"http_{resp.status_code}"
            return ProviderHealth(
                provider=self.name,
                available=ok,
                base_url=self.base_url,
                model=self.model,
                status_code=int(resp.status_code),
                detail=detail,
                payload={"url": url},
            )
        except Exception as exc:
            return ProviderHealth(
                provider=self.name,
                available=False,
                base_url=self.base_url,
                model=self.model,
                detail=str(exc),
                payload={"url": url},
            )

    def healthcheck(self, *, session: requests.Session | None = None, timeout: float = 5.0) -> ProviderHealth:
        return self.health_check(session=session, timeout=timeout)

    @abstractmethod
    def build_chat_payload(self, messages: Iterable[dict[str, Any]] | str, **kwargs) -> dict[str, Any]:
        raise NotImplementedError


class OpenAICompatibleProvider(ProviderAdapter):
    def build_chat_payload(self, messages: Iterable[dict[str, Any]] | str, **kwargs) -> dict[str, Any]:
        payload = {
            "model": (kwargs.pop("model", "") or self.model or self.default_model).strip(),
            "messages": self._normalize_messages(messages),
            "stream": bool(kwargs.pop("stream", False)),
        }
        if "temperature" in kwargs and kwargs["temperature"] is not None:
            payload["temperature"] = kwargs.pop("temperature")
        if "max_tokens" in kwargs and kwargs["max_tokens"] is not None:
            payload["max_tokens"] = kwargs.pop("max_tokens")
        payload.update(kwargs)
        return payload
