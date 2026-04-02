"""
llm_direct.py — 多 Provider LLM 直連，取代 OpenClaw codex-distributed agent
=============================================================================
直接呼叫 oMLX / Claude API，不經過 OpenClaw subprocess。
- 省掉 ~3,300 tokens 固定開銷（SOUL/AGENTS/TOOLS 注入）
- 省掉 subprocess fork + session 管理
- 支援多 provider 自動 fallback

使用方式：
    from skills.bridge.llm_direct import chat, translate, summarize, classify_intent
    result = chat(prompt="你好", feature="general")
    result = chat(prompt="複雜問題", feature="react")  # 自動走 Claude
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any

logger = logging.getLogger("LLMDirect")

# ── Provider 設定 ──────────────────────────────────────

PROVIDERS: dict[str, dict[str, Any]] = {
    "omlx": {
        "base_url": os.environ.get("OMLX_BASE_URL", "http://127.0.0.1:8080/v1"),
        "api_key": os.environ.get("OMLX_API_KEY", "omlx-local"),
        "default_model": os.environ.get("MAGI_DEFAULT_MODEL", "TAIDE-12b-Chat-mlx-4bit"),
        "api_format": "openai",
        "max_context": 32768,
    },
    "claude": {
        "base_url": "https://api.anthropic.com",
        "api_key": os.environ.get("ANTHROPIC_API_KEY", ""),
        "default_model": os.environ.get("MAGI_CLAUDE_MODEL", "claude-sonnet-4-20250514"),
        "api_format": "anthropic",
        "max_context": 200000,
    },
}

# ── Feature → Provider 路由 ────────────────────────────

FEATURE_ROUTING: dict[str, dict[str, str | None]] = {
    "intent":     {"primary": "omlx", "fallback": None},        # 輕量，不值得花錢
    "translate":  {"primary": "omlx", "fallback": "claude"},     # oMLX 夠用，失敗才 fallback
    "summary":    {"primary": "omlx", "fallback": "claude"},
    "transcript": {"primary": "omlx", "fallback": None},
    "vision":     {"primary": "omlx", "fallback": "claude"},
    "general":    {"primary": "omlx", "fallback": "claude"},
    "react":      {"primary": "claude", "fallback": "omlx"},     # ReAct 需要強推理 → Claude 優先
}

DEFAULT_TIMEOUT = 90
MAX_TOKENS = 4096

# ── 精簡 System Prompts（取代 SOUL.md + AGENTS.md + TOOLS.md 的 ~3,300 tokens）──

SYSTEM_PROMPTS: dict[str, str] = {
    "intent": (
        "你是意圖分類器。根據使用者輸入，只回覆一個標籤，不要解釋：\n"
        "- QUERY：查詢、搜尋、查案件、查法條、翻譯、摘要、分析、計算等需要處理的請求\n"
        "- CMD：執行、啟動、停止、重啟、上傳、下載等系統操作指令\n"
        "- DANGER：刪除、格式化、rm -rf 等破壞性操作\n"
        "- CHAT：閒聊、問候、感謝、抱怨等不需要查詢或執行的對話\n\n"
        "範例：\n"
        "「你好」→ CHAT\n"
        "「你還活著嗎」→ CHAT\n"
        "「今天過得好嗎」→ CHAT\n"
        "「你是誰」→ CHAT\n"
        "「查案件進度」→ QUERY\n"
        "「翻譯這段」→ QUERY\n"
        "「重啟MAGI」→ CMD\n"
        "「謝謝」→ CHAT\n"
        "「幫我查法條」→ QUERY\n"
        "「摘要這份文件」→ QUERY\n\n"
        "只回覆標籤："
    ),
    "translate": "你是專業翻譯。直接輸出翻譯結果，不加任何前綴或解釋。使用繁體中文（台灣用語）。",
    "summary": (
        "你是法律文件摘要助理。輸出結構化摘要：\n"
        "裁判要旨→事實摘要→爭點→法院見解→適用法條。\n"
        "使用繁體中文（台灣用語）。"
    ),
    "transcript": "你是逐字稿校正助理。保留內容，只改善標點、斷句和說話者標記。繁體中文。",
    "vision": "描述圖片內容。繁體中文。",
    "general": "你是 CASPER，MAGI 系統的 AI 助理。簡潔、準確、使用繁體中文（台灣用語）。",
    "react": (
        "你是 CASPER，MAGI 系統的推理引擎。\n"
        "使用繁體中文（台灣用語）。\n"
        "仔細思考後再回答，必要時可分步驟推理。"
    ),
}


# ── HTTP 呼叫（用 http_pool 防 fd leak）──────────────────

def _get_session():
    """取得共用 HTTP session，fallback 到 requests。"""
    try:
        from skills.bridge.http_pool import get_session
        return get_session()
    except ImportError:
        import requests
        return requests.Session()


def _call_openai_format(
    provider_cfg: dict, messages: list[dict], **kwargs
) -> dict[str, Any]:
    """呼叫 OpenAI-compatible API（oMLX、Groq 等）。"""
    session = _get_session()
    resp = session.post(
        f"{provider_cfg['base_url']}/chat/completions",
        headers={"Authorization": f"Bearer {provider_cfg['api_key']}"},
        json={
            "model": kwargs.get("model", provider_cfg["default_model"]),
            "messages": messages,
            "temperature": kwargs.get("temperature", 0.3),
            "max_tokens": kwargs.get("max_tokens", MAX_TOKENS),
        },
        timeout=kwargs.get("timeout", DEFAULT_TIMEOUT),
    )
    resp.raise_for_status()
    data = resp.json()
    return {
        "text": data["choices"][0]["message"]["content"].strip(),
        "usage": {
            "input": data.get("usage", {}).get("prompt_tokens", 0),
            "output": data.get("usage", {}).get("completion_tokens", 0),
            "total": data.get("usage", {}).get("total_tokens", 0),
        },
    }


def _call_anthropic_format(
    provider_cfg: dict, messages: list[dict], **kwargs
) -> dict[str, Any]:
    """呼叫 Anthropic Messages API（Claude）。"""
    # Anthropic 格式：system 獨立，messages 只有 user/assistant
    system_parts: list[str] = []
    chat_messages: list[dict] = []
    for m in messages:
        if m["role"] == "system":
            system_parts.append(m["content"])
        else:
            chat_messages.append({"role": m["role"], "content": m["content"]})

    # 確保 messages 非空且以 user 開頭
    if not chat_messages:
        chat_messages.append({"role": "user", "content": "(empty)"})

    session = _get_session()
    resp = session.post(
        f"{provider_cfg['base_url']}/v1/messages",
        headers={
            "x-api-key": provider_cfg["api_key"],
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": kwargs.get("model", provider_cfg["default_model"]),
            "system": "\n".join(system_parts).strip() or "You are a helpful assistant.",
            "messages": chat_messages,
            "temperature": kwargs.get("temperature", 0.3),
            "max_tokens": kwargs.get("max_tokens", MAX_TOKENS),
        },
        timeout=kwargs.get("timeout", DEFAULT_TIMEOUT),
    )
    resp.raise_for_status()
    data = resp.json()
    text = ""
    for block in data.get("content", []):
        if block.get("type") == "text":
            text += block["text"]
    usage = data.get("usage", {})
    return {
        "text": text.strip(),
        "usage": {
            "input": usage.get("input_tokens", 0),
            "output": usage.get("output_tokens", 0),
            "total": usage.get("input_tokens", 0) + usage.get("output_tokens", 0),
        },
    }


_API_DISPATCHERS = {
    "openai": _call_openai_format,
    "anthropic": _call_anthropic_format,
}


# ── 主要 API ─────────────────────────────────────────────

def chat(
    *,
    prompt: str,
    feature: str = "general",
    temperature: float = 0.3,
    max_tokens: int = MAX_TOKENS,
    timeout: int = DEFAULT_TIMEOUT,
    model: str = "",
    provider: str = "",
    messages: list[dict] | None = None,
) -> dict[str, Any]:
    """
    直接呼叫 LLM，不經過 OpenClaw。

    Args:
        prompt: 使用者訊息（如果沒提供 messages）
        feature: 任務類型 → 自動選 provider + system prompt
        temperature: 生成溫度
        max_tokens: 最大輸出 token
        timeout: HTTP 超時秒數
        model: 強制指定模型（空 = 用 provider 預設）
        provider: 強制指定 provider（空 = 依 feature 自動路由）
        messages: 完整 messages 列表（提供時忽略 prompt/system_prompt）

    Returns:
        {"success": bool, "text": str, "provider": str, "model": str, "usage": dict, ...}
    """
    routing = FEATURE_ROUTING.get(feature, FEATURE_ROUTING["general"])
    primary = provider or routing["primary"]
    fallback = routing.get("fallback") if not provider else None

    # 組裝 messages
    if messages:
        msg_list = messages
    else:
        system_prompt = SYSTEM_PROMPTS.get(feature, SYSTEM_PROMPTS["general"])
        msg_list = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ]

    # 嘗試 primary → fallback
    last_error = ""
    for attempt_provider in [primary, fallback]:
        if not attempt_provider:
            continue
        cfg = PROVIDERS.get(attempt_provider)
        if not cfg or not cfg.get("api_key"):
            logger.debug("Provider %s: no api_key, skipping", attempt_provider)
            continue

        dispatcher = _API_DISPATCHERS.get(cfg["api_format"])
        if not dispatcher:
            continue

        use_model = model or cfg["default_model"]
        # oMLX max_num_seqs=1 → 並行請求可能 timeout。重試一次。
        max_retries = 2 if attempt_provider == "omlx" else 1
        for retry in range(max_retries):
            started = time.monotonic()
            try:
                result = dispatcher(
                    cfg,
                    msg_list,
                    model=use_model,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    timeout=timeout,
                )
                elapsed = time.monotonic() - started
                logger.info(
                    "LLM OK: provider=%s model=%s feature=%s tokens=%s elapsed=%.1fs",
                    attempt_provider, use_model, feature,
                    result.get("usage", {}).get("total", "?"), elapsed,
                )
                return {
                    "success": True,
                    "text": result["text"],
                    "feature": feature,
                    "provider": attempt_provider,
                    "model": use_model,
                    "usage": result.get("usage", {}),
                    "elapsed_sec": round(elapsed, 2),
                }
            except Exception as exc:
                elapsed = time.monotonic() - started
                last_error = f"{type(exc).__name__}: {exc}"
                if retry < max_retries - 1:
                    logger.info("LLM retry %d: provider=%s elapsed=%.1fs", retry + 1, attempt_provider, elapsed)
                    time.sleep(1)
                    continue
                logger.warning(
                    "LLM FAIL: provider=%s model=%s feature=%s error=%s elapsed=%.1fs",
                    attempt_provider, use_model, feature, last_error, elapsed,
                )
        # All retries for this provider exhausted, try next

    return {
        "success": False,
        "error": f"all_providers_failed: {last_error}",
        "feature": feature,
    }


# ── 便利函數（與 openclaw_codex_bridge 相同介面）────────

def feature_enabled(feature: str) -> bool:
    """永遠回傳 True — 直連模式不需要 policy 檢查。"""
    return True


def translate_with_codex(
    prompt: str,
    *,
    source_lang: str = "auto",
    target_lang: str = "繁體中文",
    timeout_sec: int = 90,
    **_extra,
) -> dict:
    """相容 openclaw_codex_bridge.translate_with_codex 介面。"""
    # 如果有指定目標語言，改寫 prompt
    if target_lang and target_lang != "繁體中文":
        full_prompt = f"將以下文字翻譯為{target_lang}：\n\n{prompt}"
    else:
        full_prompt = prompt
    result = chat(prompt=full_prompt, feature="translate", timeout=timeout_sec)
    return {
        "success": result["success"],
        "text": result.get("text", ""),
        "error": result.get("error"),
        "provider": result.get("provider", ""),
    }


def summarize_with_codex(
    prompt: str,
    *,
    summary_length: str = "medium",
    timeout_sec: int = 90,
    **_extra,
) -> dict:
    """相容 openclaw_codex_bridge.summarize_with_codex 介面。"""
    result = chat(prompt=prompt, feature="summary", timeout=timeout_sec)
    return {
        "success": result["success"],
        "text": result.get("text", ""),
        "error": result.get("error"),
        "provider": result.get("provider", ""),
    }


def classify_intent_with_codex(prompt: str, timeout_sec: int = 30) -> dict:
    """相容 openclaw_codex_bridge.classify_intent_with_codex 介面。"""
    result = chat(prompt=prompt, feature="intent", timeout=timeout_sec, max_tokens=16)
    intent = "CHAT"
    if result["success"]:
        label = result["text"].upper().strip()
        for valid in ("CHAT", "QUERY", "CMD", "DANGER"):
            if valid in label:
                intent = valid
                break
    return {
        "success": result["success"],
        "text": result.get("text", ""),
        "intent": intent,
        "error": result.get("error"),
        "provider": result.get("provider", ""),
    }


def polish_transcript_with_codex(prompt: str, *, timeout_sec: int = 120, **_extra) -> dict:
    """相容 openclaw_codex_bridge.polish_transcript_with_codex 介面。"""
    result = chat(prompt=prompt, feature="transcript", timeout=timeout_sec)
    return {
        "success": result["success"],
        "text": result.get("text", ""),
        "error": result.get("error"),
        "provider": result.get("provider", ""),
    }


def run_prompt(*, feature: str, prompt: str, timeout_sec: int | None = None, **kwargs) -> dict:
    """相容 openclaw_codex_bridge.run_prompt 介面。"""
    result = chat(
        prompt=prompt,
        feature=feature,
        timeout=timeout_sec or DEFAULT_TIMEOUT,
    )
    return {
        "success": result["success"],
        "text": result.get("text", ""),
        "feature": feature,
        "error": result.get("error"),
        "provider": result.get("provider", ""),
        "usage": result.get("usage", {}),
    }


def analyze_image_with_codex(
    image_path: str, *, user_prompt: str, task_type: str = "vision", timeout_sec: int | None = None
) -> dict:
    """相容 openclaw_codex_bridge.analyze_image_with_codex 介面。"""
    normalized_task = str(task_type or "vision").strip().lower() or "vision"
    _OCR_TASK_TYPES = {"ocr", "vision-ocr", "text", "read-text", "captcha", "date_extract", "stamp", "receipt"}
    if normalized_task in _OCR_TASK_TYPES:
        try:
            from skills.bridge.openclaw_codex_bridge import _local_ocr_extract
            ocr = _local_ocr_extract(image_path)
            if ocr.get("success"):
                prompt = (
                    "你是 MAGI 的 OCR 校對與抽取引擎。以下文字是以本機 tesseract 從圖片擷取出的原始 OCR 結果。\n"
                    f"- 圖片路徑：{str(image_path or '').strip()}\n"
                    f"- 任務類型：{normalized_task}\n"
                    f"- 使用者需求：{str(user_prompt or '').strip()}\n"
                    f"- OCR 語言設定：{str(ocr.get('lang') or '').strip()}\n"
                    "- 請根據原始 OCR 內容做最小必要校對，避免憑空補寫未出現的資訊。\n"
                    "- 如果使用者要求逐字輸出，請只輸出校對後文字；不要解釋。\n\n"
                    "[RAW OCR]\n"
                    f"{str(ocr.get('text') or '').strip()}"
                )
                return run_prompt(feature="vision", prompt=prompt, timeout_sec=timeout_sec)
        except ImportError:
            pass

    prompt = (
        "你在 MAGI 工作區內執行。請分析這個本機圖片檔，但不得修改原檔：\n"
        f"- 圖片路徑：{str(image_path or '').strip()}\n"
        f"- 任務類型：{normalized_task}\n"
        f"- 使用者需求：{str(user_prompt or '').strip()}\n"
        "- 僅輸出最終答案，使用繁體中文。\n"
    )
    return run_prompt(feature="vision", prompt=prompt, timeout_sec=timeout_sec)


def refine_ocr_with_codex(ocr_text: str, *, user_prompt: str, timeout_sec: int | None = None) -> dict:
    """相容 openclaw_codex_bridge.refine_ocr_with_codex 介面。"""
    prompt = (
        "你是 MAGI 的 OCR 校對與抽取引擎。以下內容是由本地 OCR 模型擷取出的原始文字。\n"
        f"- 使用者需求：{str(user_prompt or '').strip()}\n"
        "- 請在不憑空補寫的前提下，做最小必要校對。\n"
        "- 如果需求是逐字輸出，就只輸出校對後的文字；不要解釋。\n"
        "- 如果需求是抽取日期、編號或欄位，請只輸出提取結果。\n\n"
        "[RAW OCR]\n"
        f"{str(ocr_text or '').strip()}"
    )
    return run_prompt(feature="vision", prompt=prompt, timeout_sec=timeout_sec)


def load_runtime_state() -> dict:
    """相容介面 — 直連模式不需要 runtime state。"""
    return {}


def save_runtime_state(state: dict) -> None:
    """相容介面 — 直連模式不需要 runtime state。"""
    pass


def public_status_report() -> str:
    """回傳 LLM Direct 狀態。"""
    lines = ["=== LLM Direct Status ==="]
    for name, cfg in PROVIDERS.items():
        has_key = bool(cfg.get("api_key"))
        lines.append(f"  {name}: {'✓ ready' if has_key else '✗ no api_key'} ({cfg['default_model']})")
    lines.append(f"  Feature routing: {len(FEATURE_ROUTING)} features configured")
    return "\n".join(lines)


def apply_manual_command(cmd: str) -> str:
    """相容介面 — 直連模式不需要手動控制。"""
    return f"LLM Direct mode active. Command '{cmd}' ignored (no OpenClaw dependency)."
