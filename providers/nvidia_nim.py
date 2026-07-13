from __future__ import annotations

from .base import OpenAICompatibleProvider


class NvidiaNimProvider(OpenAICompatibleProvider):
    """NVIDIA NIM 免費算力兜底 adapter。

    - 走 build.nvidia.com 的 OpenAI-compatible endpoint
    - 僅限非中國模型（Llama / Mistral / Gemma / Nemotron-of-Llama / Phi / SmolLM）
    - 依 CLAUDE.md standing rule：禁用 DeepSeek / Qwen / Kimi / MiniMax / GLM / Yi / Baichuan
    """

    name = "nvidia_nim"
    default_base_url = "https://integrate.api.nvidia.com/v1"
    base_url_env = "NVIDIA_NIM_BASE_URL"
    api_key_env = "NVIDIA_NIM_API_KEY"
    model_env = "NVIDIA_NIM_MODEL"
    # 2026-06 live: Meta 405B is no longer reliably available on the hosted
    # NVIDIA endpoint. Use the legal-translation-gated Super 120B target
    # directly; nim_heavy still maps legacy 405B configs for compatibility.
    default_model = "nvidia/nemotron-3-super-120b-a12b"
    health_path = "/models"
    requires_api_key = True

    # 白名單 — 任何 PR 必須保證新增的 entry 不是中國模型
    ALLOWED_MODELS = frozenset({
        # Meta Llama 系（多語、128K context）
        "meta/llama-3.1-405b-instruct",      # 歷史設定相容；nim_heavy 會映射至目前主力
        "meta/llama-3.3-70b-instruct",       # 一般兜底
        "meta/llama-3.1-70b-instruct",
        "meta/llama-3.1-8b-instruct",
        # NVIDIA 基於 Llama 的 fine-tune（多語）
        "nvidia/nemotron-3-super-120b-a12b",  # 2026-06 live tested heavy primary
        "nvidia/nemotron-3-ultra-550b-a55b",  # 官方較新大型模型；目前 live 延遲高，僅手動觀察
        "nvidia/llama-3.3-nemotron-super-49b-v1",
        "nvidia/llama-3.3-nemotron-super-49b-v1.5",
        "nvidia/llama-3.1-nemotron-70b-instruct",
        "nvidia/llama-3.1-nemotron-51b-instruct",
        # Mistral（多語）
        "mistralai/mistral-large-3-675b-instruct-2512",
        "mistralai/mistral-medium-3.5-128b",
        "mistralai/mistral-large-2-instruct",
        "mistralai/mixtral-8x22b-instruct-v0.1",
        # Google Gemma（多語）
        "google/gemma-3-27b-it",
        "google/gemma-2-27b-it",
        # 微軟 Phi（多語）
        "microsoft/phi-4-multimodal-instruct",
    })

    # 顯式黑名單（進一步防守，ALLOWED 已是白名單但多一層防呆）
    BLOCKED_KEYWORDS = frozenset({
        "deepseek", "qwen", "kimi", "minimax", "yi-", "baichuan", "glm-",
        "moonshot", "internlm", "chatglm", "sensetime",
    })

    @classmethod
    def is_model_allowed(cls, model: str) -> bool:
        m = (model or "").strip().lower()
        if not m:
            return False
        for blk in cls.BLOCKED_KEYWORDS:
            if blk in m:
                return False
        return m in cls.ALLOWED_MODELS

    @classmethod
    def _require_allowed_model(cls, model: str) -> str:
        m = (model or "").strip()
        if not cls.is_model_allowed(m):
            raise ValueError(f"nvidia_nim_model_not_allowed:{m or '<empty>'}")
        return m

    def validate_model(self, model: str) -> str:
        return self._require_allowed_model(model)

    def resolve_model(self, model: str = "") -> str:
        return self._require_allowed_model(super().resolve_model(model))
