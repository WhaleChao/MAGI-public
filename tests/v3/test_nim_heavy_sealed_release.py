from __future__ import annotations

from skills.bridge import nim_heavy


def test_nim_heavy_imports_without_legacy_providers_package() -> None:
    assert nim_heavy._model_allowed("nvidia/nemotron-3-super-120b-a12b")
    assert nim_heavy._model_allowed("meta/llama-3.3-70b-instruct")
    assert not nim_heavy._model_allowed("deepseek/deepseek-v3")
    assert not nim_heavy._model_allowed("unknown/provider-model")
