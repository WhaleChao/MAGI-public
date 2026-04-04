from api.model_config import (
    DEFAULT_VISION_MODEL,
    TEXT_PRIMARY_MODEL,
    default_local_chat_models,
    default_local_vision_models,
    resolve_text_model,
)


def test_default_local_chat_models_use_primary_text_model():
    assert default_local_chat_models() == [TEXT_PRIMARY_MODEL]


def test_default_local_vision_models_use_default_vision_model():
    assert default_local_vision_models() == [DEFAULT_VISION_MODEL]


def test_resolve_text_model_maps_legacy_alias_to_primary():
    models = ["gemma-4-26b-a4b-it-4bit", "GLM-OCR-bf16"]
    assert resolve_text_model("taide-12b", available=models) == "gemma-4-26b-a4b-it-4bit"
