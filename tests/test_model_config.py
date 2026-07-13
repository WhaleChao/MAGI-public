import importlib

from api.model_config import (
    DEFAULT_VISION_MODEL,
    TEXT_PRIMARY_MODEL,
    default_local_chat_models,
    default_local_vision_models,
    is_disallowed_model,
    resolve_text_model,
)


def test_default_local_chat_models_use_primary_text_model():
    assert default_local_chat_models() == [TEXT_PRIMARY_MODEL]


def test_default_local_vision_models_use_default_vision_model():
    assert default_local_vision_models() == [DEFAULT_VISION_MODEL]


def test_resolve_text_model_maps_legacy_alias_to_primary():
    models = ["gemma-4-26b-a4b-it-4bit"]
    assert resolve_text_model("gemma-4", available=models) == "gemma-4-26b-a4b-it-4bit"


def test_china_models_are_blocked_from_resolution(monkeypatch):
    assert is_disallowed_model("Qwen2.5-Coder-14B-Instruct-4bit")
    assert is_disallowed_model("deepseek-r1:14b")
    assert is_disallowed_model("GLM-4.7:latest")
    models = ["Qwen2.5-Coder-14B-Instruct-4bit", "gemma-4-e4b-it-4bit"]
    assert resolve_text_model("Qwen2.5-Coder-14B-Instruct-4bit", available=models) == "gemma-4-e4b-it-4bit"


def test_env_china_primary_model_falls_back(monkeypatch):
    monkeypatch.delenv("MAGI_TEXT_PRIMARY_MODEL", raising=False)
    monkeypatch.delenv("CASPER_LOCAL_MODEL", raising=False)
    monkeypatch.setenv("MAGI_MAIN_MODEL", "qwen2.5-coder:7b")
    import api.model_config as model_config

    reloaded = importlib.reload(model_config)

    assert reloaded.TEXT_PRIMARY_MODEL == reloaded.DEFAULT_TEXT_MODEL
    monkeypatch.delenv("MAGI_MAIN_MODEL", raising=False)
    importlib.reload(model_config)


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
