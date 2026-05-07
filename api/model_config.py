from __future__ import annotations

import os
from typing import Iterable


DEFAULT_TEXT_MODEL = "gemma-4-e4b-it-4bit"
DEFAULT_VISION_MODEL = "gemma-4-e4b-it-4bit"
DEFAULT_OCR_MODEL = "gemma-4-e4b-it-4bit"
DEFAULT_EMBED_MODEL = "modernbert-embed-4bit"


def _clean(value: Optional[str], fallback: str = "") -> str:
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
TEXT_HEAVY_MODEL = _clean(os.environ.get("MAGI_TEXT_HEAVY_MODEL"), "gemma-4-26b-a4b-it-4bit")
TEXT_VERIFY_MODEL_PHI4 = _clean(os.environ.get("MAGI_TEXT_VERIFY_MODEL_PHI4"), "Phi-4-mini-instruct-4bit")
TEXT_VERIFY_MODEL_SMOL = _clean(os.environ.get("MAGI_TEXT_VERIFY_MODEL_SMOL"), "SmolLM3-3B-Instruct-4bit")
VISION_MODEL = _clean(os.environ.get("MAGI_OMLX_VISION_MODEL"), DEFAULT_VISION_MODEL)
OCR_MODEL = _clean(os.environ.get("MAGI_OMLX_OCR_MODEL"), DEFAULT_OCR_MODEL)
EMBED_MODEL = _clean(os.environ.get("MAGI_OMLX_EMBED_MODEL"), DEFAULT_EMBED_MODEL)
DEFAULT_MODEL_ALIAS = _clean(os.environ.get("MAGI_DEFAULT_MODEL"), TEXT_PRIMARY_MODEL)


def _env_bool(name: str, default: bool = False) -> bool:
    value = str(os.environ.get(name, "")).strip().lower()
    if not value:
        return bool(default)
    return value in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    value = str(os.environ.get(name, "")).strip()
    if not value:
        return int(default)
    try:
        return int(value)
    except ValueError:
        return int(default)


MTP_DRAFT_ENABLED = _env_bool("MAGI_ENABLE_MTP_DRAFT", False)
E4B_DRAFT_MODEL = _clean(os.environ.get("MAGI_E4B_DRAFT_MODEL"), "gemma-4-E4B-it-assistant-bf16")
TEXT_26B_DRAFT_MODEL = _clean(
    os.environ.get("MAGI_26B_DRAFT_MODEL"),
    "gemma-4-26B-A4B-it-assistant-bf16",
)
MTP_DRAFT_KIND = _clean(os.environ.get("MAGI_MTP_DRAFT_KIND"), "mtp")
MTP_BLOCK_SIZE = _env_int("MAGI_MTP_BLOCK_SIZE", 4)
HEAVY_AUTO_UPGRADE = _env_bool("MAGI_HEAVY_AUTO_UPGRADE", False)
HEAVY_MIN_CHARS = _env_int("MAGI_HEAVY_MIN_CHARS", 6000)


def resolve_draft_model(target_model: str = "") -> str:
    """Return the configured MTP assistant model for a target model."""
    target = str(target_model or TEXT_PRIMARY_MODEL).lower()
    if "26b" in target or "a4b" in target:
        return TEXT_26B_DRAFT_MODEL
    return E4B_DRAFT_MODEL


def mtp_draft_payload(target_model: str = "") -> dict[str, object]:
    """Build optional request metadata for MTP-capable local runtimes.

    The default MAGI runtime keeps this disabled because the current oMLX
    server does not advertise draft-model CLI support. Sidecars or newer
    OpenAI-compatible runtimes can opt in with MAGI_ENABLE_MTP_DRAFT=1.
    """
    if not MTP_DRAFT_ENABLED:
        return {}
    draft_model = resolve_draft_model(target_model)
    if not draft_model:
        return {}
    return {
        "draft_model": draft_model,
        "draft_kind": MTP_DRAFT_KIND,
        "draft_block_size": MTP_BLOCK_SIZE,
    }

TEXT_MODEL_ALIASES = {
    "",
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
    "gemma-4-26b-a4b-it-4bit",
}


def is_text_model_alias(name: Optional[str]) -> bool:
    return str(name or "").strip().lower() in TEXT_MODEL_ALIASES


def resolve_text_model(name: Optional[str] = None, *, available: Iterable[str] | None = None) -> str:
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
