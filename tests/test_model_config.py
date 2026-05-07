import importlib

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
    models = ["gemma-4-26b-a4b-it-4bit"]
    assert resolve_text_model("gemma-4", available=models) == "gemma-4-26b-a4b-it-4bit"


def test_mtp_draft_payload_is_disabled_by_default(monkeypatch):
    monkeypatch.setenv("MAGI_ENABLE_MTP_DRAFT", "0")
    import api.model_config as model_config

    reloaded = importlib.reload(model_config)

    assert reloaded.mtp_draft_payload("gemma-4-e4b-it-4bit") == {}


def test_mtp_draft_payload_resolves_e4b_and_26b(monkeypatch):
    monkeypatch.setenv("MAGI_ENABLE_MTP_DRAFT", "1")
    monkeypatch.setenv("MAGI_E4B_DRAFT_MODEL", "e4b-assistant")
    monkeypatch.setenv("MAGI_26B_DRAFT_MODEL", "26b-assistant")
    monkeypatch.setenv("MAGI_MTP_BLOCK_SIZE", "6")
    import api.model_config as model_config

    reloaded = importlib.reload(model_config)

    assert reloaded.mtp_draft_payload("gemma-4-e4b-it-4bit") == {
        "draft_model": "e4b-assistant",
        "draft_kind": "mtp",
        "draft_block_size": 6,
    }
    assert reloaded.mtp_draft_payload("gemma-4-26b-a4b-it-4bit")["draft_model"] == "26b-assistant"

    monkeypatch.setenv("MAGI_ENABLE_MTP_DRAFT", "0")
    importlib.reload(model_config)
