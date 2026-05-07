from __future__ import annotations

import requests

from .base import OpenAICompatibleProvider, ProviderHealth
from api.model_config import MTP_DRAFT_ENABLED, TEXT_PRIMARY_MODEL, mtp_draft_payload


class OmlxProvider(OpenAICompatibleProvider):
    name = "omlx"
    try:
        from api.routing.service_registry import get_service_url as _get_svc_url
        default_base_url = _get_svc_url("omlx_inference") + "/v1"
    except Exception:
        default_base_url = "http://127.0.0.1:8080/v1"
    base_url_env = "OMLX_BASE_URL"
    api_key_env = "OMLX_API_KEY"
    model_env = "OMLX_MODEL"
    default_model = TEXT_PRIMARY_MODEL
    health_path = "/models"
    requires_api_key = False

    def build_headers(self) -> dict[str, str]:
        headers = super().build_headers()
        headers["Authorization"] = f"Bearer {self.api_key or 'omlx-local'}"
        return headers

    def build_chat_payload(self, messages, **kwargs) -> dict:
        payload = super().build_chat_payload(messages, **kwargs)
        payload.update(mtp_draft_payload(str(payload.get("model") or self.model)))
        return payload

    def healthcheck(self, *, timeout: float = 5.0):
        try:
            resp = requests.get(self.build_url(self.health_path), headers=self.build_headers(), timeout=float(timeout))
            resp.raise_for_status()
            payload = resp.json() or {}
            models = payload.get("data") or []
            count = len(models)
            return ProviderHealth(
                provider=self.name,
                available=True,
                base_url=self.base_url,
                model=self.model,
                status_code=getattr(resp, "status_code", 200),
                detail=f"{count} models",
                payload={
                    "models": models,
                    "mtp_draft_enabled": MTP_DRAFT_ENABLED,
                    "draft_payload": mtp_draft_payload(self.model),
                },
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
