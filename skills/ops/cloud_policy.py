"""
Cloud Model Policy (雲端模型策略)
=================================
Central switch to disable any cloud LLM usage (Gemini/OpenAI/Anthropic/etc.).

This is separate from MAGI_ALLOW_INTERNET:
- MAGI_ALLOW_INTERNET controls generic outbound HTTP (web search, GitHub fetch, etc.)
- MAGI_ALLOW_CLOUD_MODELS controls calling hosted model APIs (LLM-as-a-service)
"""

from __future__ import annotations

import os


def cloud_models_allowed() -> bool:
    """
    Returns True if cloud model APIs are allowed.
    Default: disabled.
    """
    v = os.environ.get("MAGI_ALLOW_CLOUD_MODELS", "0").strip().lower()
    return v in {"1", "true", "yes", "on"}


def require_cloud_models_allowed(feature: str = "cloud model") -> None:
    """
    Raise a RuntimeError when cloud models are disabled.
    """
    if not cloud_models_allowed():
        raise RuntimeError(f"Cloud models disabled (MAGI_ALLOW_CLOUD_MODELS=0): blocked {feature}.")

