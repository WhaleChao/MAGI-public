from __future__ import annotations

from .base import OpenAICompatibleProvider


class OpenAIProvider(OpenAICompatibleProvider):
    name = "openai"
    default_base_url = "https://api.openai.com/v1"
    base_url_env = "OPENAI_BASE_URL"
    api_key_env = "OPENAI_API_KEY"
    model_env = "OPENAI_MODEL"
    default_model = "gpt-4o-mini"
    health_path = "/models"
    requires_api_key = True

