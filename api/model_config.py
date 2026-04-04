from __future__ import annotations

import os
from typing import Iterable


DEFAULT_TEXT_MODEL = "gemma-4-e4b-it-bf16"
DEFAULT_VISION_MODEL = "GLM-OCR-bf16"
DEFAULT_EMBED_MODEL = "modernbert-embed-4bit"


def _clean(value: str | None, fallback: str = "") -> str:
    text = str(value or "").strip()
    return text or fallback


TEXT_PRIMARY_MODEL = _clean(
    os.environ.get("MAGI_TEXT_PRIMARY_MODEL")
    or os.environ.get("MAGI_MAIN_MODEL")
    or os.environ.get("CASPER_LOCAL_MODEL"),
    DEFAULT_TEXT_MODEL,
)
TEXT_REVIEW_MODEL = _clean(os.environ.get("MAGI_TW_REVIEW_MODEL"), TEXT_PRIMARY_MODEL)
GENERAL_MODEL = _clean(os.environ.get("MAGI_OMLX_GENERAL_MODEL"), TEXT_PRIMARY_MODEL)
SUMMARY_MODEL = _clean(os.environ.get("MAGI_OMLX_SUMMARY_MODEL"), TEXT_PRIMARY_MODEL)
CODE_MODEL = _clean(os.environ.get("MAGI_OMLX_CODE_MODEL"), TEXT_PRIMARY_MODEL)
VISION_MODEL = _clean(os.environ.get("MAGI_OMLX_VISION_MODEL"), DEFAULT_VISION_MODEL)
OCR_MODEL = _clean(os.environ.get("MAGI_OMLX_OCR_MODEL"), VISION_MODEL or DEFAULT_VISION_MODEL)
EMBED_MODEL = _clean(os.environ.get("MAGI_OMLX_EMBED_MODEL"), DEFAULT_EMBED_MODEL)
DEFAULT_MODEL_ALIAS = _clean(os.environ.get("MAGI_DEFAULT_MODEL"), TEXT_PRIMARY_MODEL)

TEXT_MODEL_ALIASES = {
    "",
    "taide",
    "taide-12b",
    "taide-12b-chat-mlx-4bit",
    "gemma-4",
    "gemma4",
    "gemma-4-26b",
    "gemma-4-26b-a4b",
    "gemma-4-e2b-it-local-bf16",
    "gemma-4-26b-a4b-it-4bit",
    "gemma-4-e4b-it-4bit",
    "gemma4:26b",
    "gemma4:26b-a4b-it-q4_K_M",
    "gemma-4-e4b-it-bf16",
}


def is_text_model_alias(name: str | None) -> bool:
    return str(name or "").strip().lower() in TEXT_MODEL_ALIASES


def resolve_text_model(name: str | None = None, *, available: Iterable[str] | None = None) -> str:
    requested = str(name or "").strip()
    candidate = TEXT_PRIMARY_MODEL if is_text_model_alias(requested) else requested or TEXT_PRIMARY_MODEL
    if available is None:
        return candidate
    models = [str(model).strip() for model in available if str(model).strip()]
    if not models:
        return candidate
    if candidate in models:
        return candidate
    low = candidate.lower()
    for model in models:
        model_low = model.lower()
        if low and (model_low == low or low in model_low or model_low.startswith(low)):
            return model
    for model in models:
        if "gemma-4" in model.lower():
            return model
    return models[0]


def default_local_chat_models() -> list[str]:
    return [TEXT_PRIMARY_MODEL]


def default_local_vision_models() -> list[str]:
    return [VISION_MODEL]
