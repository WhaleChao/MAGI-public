"""NVIDIA NIM Provider Adapter 測試

覆蓋：
1. 白名單正反測試（Meta/Mistral/Gemma 通過；DeepSeek/Qwen/MiniMax 等中國模型攔截）
2. Adapter 註冊進 provider registry
3. 預設模型為 Llama-3.1-405B
"""
from __future__ import annotations

import pytest

from providers import build_provider_registry
from providers.nvidia_nim import NvidiaNimProvider


class TestNvidiaNimAllowList:
    def test_llama_405b_allowed(self):
        assert NvidiaNimProvider.is_model_allowed("meta/llama-3.1-405b-instruct") is True

    def test_llama_70b_allowed(self):
        assert NvidiaNimProvider.is_model_allowed("meta/llama-3.3-70b-instruct") is True

    def test_mistral_large_allowed(self):
        assert NvidiaNimProvider.is_model_allowed("mistralai/mistral-large-2-instruct") is True

    def test_gemma_allowed(self):
        assert NvidiaNimProvider.is_model_allowed("google/gemma-3-27b-it") is True

    def test_nvidia_nemotron_llama_allowed(self):
        assert NvidiaNimProvider.is_model_allowed("nvidia/llama-3.1-nemotron-70b-instruct") is True

    @pytest.mark.parametrize("banned", [
        "deepseek/deepseek-r1",
        "deepseek-ai/deepseek-v3",
        "qwen/qwen-2.5-72b-instruct",
        "alibaba/qwen-coder-32b",
        "minimaxai/minimax-m2.7",
        "kimi/kimi-k2.5",
        "moonshotai/kimi-latest",
        "thudm/glm-4-9b",
        "zhipu/glm-5-air",
        "01-ai/yi-large",
        "baichuan-inc/baichuan-13b",
        "internlm/internlm-7b",
        "sensetime/sensechat",
    ])
    def test_chinese_models_blocked(self, banned):
        assert NvidiaNimProvider.is_model_allowed(banned) is False, \
            f"中國模型 {banned} 未被攔截 — 違反 CLAUDE.md standing rule"

    def test_unknown_model_blocked(self):
        assert NvidiaNimProvider.is_model_allowed("some/random-model") is False

    def test_empty_model_blocked(self):
        assert NvidiaNimProvider.is_model_allowed("") is False
        assert NvidiaNimProvider.is_model_allowed(None) is False


class TestNvidiaNimRegistration:
    def test_registered_in_registry(self):
        registry = build_provider_registry()
        assert "nvidia_nim" in registry
        assert isinstance(registry["nvidia_nim"], NvidiaNimProvider)

    def test_default_model_is_llama_405b(self):
        adapter = NvidiaNimProvider()
        assert adapter.default_model == "meta/llama-3.1-405b-instruct"

    def test_base_url_default(self):
        adapter = NvidiaNimProvider()
        assert "integrate.api.nvidia.com" in adapter.default_base_url
