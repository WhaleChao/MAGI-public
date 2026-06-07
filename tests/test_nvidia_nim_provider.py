"""NVIDIA NIM Provider Adapter 測試

覆蓋：
1. 白名單正反測試（Meta/Mistral/Gemma 通過；DeepSeek/Qwen/MiniMax 等中國模型攔截）
2. Adapter 註冊進 provider registry
3. 預設模型為 live 驗證通過的 Super 120B；歷史 405B 設定仍會被 nim_heavy 映射
"""
from __future__ import annotations

import pytest

from providers import build_provider_registry
from providers.nvidia_nim import NvidiaNimProvider


class TestNvidiaNimAllowList:
    def test_llama_405b_target_allowed(self):
        assert NvidiaNimProvider.is_model_allowed("meta/llama-3.1-405b-instruct") is True

    def test_llama_70b_allowed(self):
        assert NvidiaNimProvider.is_model_allowed("meta/llama-3.3-70b-instruct") is True

    def test_mistral_large_allowed(self):
        assert NvidiaNimProvider.is_model_allowed("mistralai/mistral-large-2-instruct") is True

    def test_gemma_allowed(self):
        assert NvidiaNimProvider.is_model_allowed("google/gemma-3-27b-it") is True

    def test_nvidia_nemotron_llama_allowed(self):
        assert NvidiaNimProvider.is_model_allowed("nvidia/llama-3.1-nemotron-70b-instruct") is True

    def test_nvidia_large_fallback_allowed(self):
        assert NvidiaNimProvider.is_model_allowed("nvidia/nemotron-3-super-120b-a12b") is True

    def test_new_nvidia_candidates_allowed_for_manual_observation(self):
        assert NvidiaNimProvider.is_model_allowed("nvidia/nemotron-3-ultra-550b-a55b") is True
        assert NvidiaNimProvider.is_model_allowed("nvidia/llama-3.3-nemotron-super-49b-v1") is True

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

    def test_default_model_is_super_120b_target(self):
        adapter = NvidiaNimProvider()
        assert adapter.default_model == "nvidia/nemotron-3-super-120b-a12b"

    def test_base_url_default(self):
        adapter = NvidiaNimProvider()
        assert "integrate.api.nvidia.com" in adapter.default_base_url
