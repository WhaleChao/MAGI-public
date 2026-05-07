from __future__ import annotations

import requests

from .base import OpenAICompatibleProvider, ProviderHealth
from api.model_config import E4B_DRAFT_MODEL, MTP_BLOCK_SIZE, MTP_DRAFT_KIND, TEXT_PRIMARY_MODEL


class MlxMtpProvider(OpenAICompatibleProvider):
    name = "mlx_mtp"
    try:
        from api.routing.service_registry import get_service_url as _get_svc_url
        default_base_url = _get_svc_url("mlx_mtp_inference") + "/v1"
    except Exception:
        default_base_url = "http://127.0.0.1:8090/v1"
    base_url_env = "MLX_MTP_BASE_URL"
    api_key_env = "MLX_MTP_API_KEY"
    model_env = "MLX_MTP_MODEL"
    default_model = TEXT_PRIMARY_MODEL
    health_path = "/models"
    requires_api_key = False

    def build_chat_payload(self, messages, **kwargs) -> dict:
        payload = super().build_chat_payload(messages, **kwargs)
        payload.setdefault("draft_model", E4B_DRAFT_MODEL)
        payload.setdefault("draft_kind", MTP_DRAFT_KIND)
        payload.setdefault("draft_block_size", MTP_BLOCK_SIZE)
        return payload

    def healthcheck(self, *, timeout: float = 5.0):
        try:
            resp = requests.get(self.build_url(self.health_path), headers=self.build_headers(), timeout=float(timeout))
            resp.raise_for_status()
            payload = resp.json() or {}
            models = payload.get("data") or []
            return ProviderHealth(
                provider=self.name,
                available=True,
                base_url=self.base_url,
                model=self.model,
                status_code=getattr(resp, "status_code", 200),
                detail=f"{len(models)} models",
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
