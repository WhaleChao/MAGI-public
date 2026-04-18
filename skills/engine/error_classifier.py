"""
error_classifier.py — 結構化 API 錯誤分類器
================================================
移植自 Hermes Agent (NousResearch/hermes-agent)，
適配 MAGI 的 oMLX 本地推理 + OpenRouter 雲端推理架構。

用法：
    from skills.engine.error_classifier import classify_error
    ce = classify_error(exception_or_dict)
    if ce.retryable:
        if ce.should_compress:
            # 壓縮 context 再試
        else:
            # 直接重試（backoff）
    elif ce.should_fallback:
        # 切換到另一個模型
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

logger = logging.getLogger("error_classifier")


class FailoverReason:
    """錯誤分類枚舉。"""
    RATE_LIMIT = "rate_limit"           # 429 限流，等一下就好
    TIMEOUT = "timeout"                 # 連線/讀取逾時
    SERVER_ERROR = "server_error"       # 500/502/503
    OVERLOADED = "overloaded"           # 503 過載
    CONTEXT_OVERFLOW = "context_overflow"  # context 超過模型上限
    AUTH = "auth"                       # 401/403 認證失敗（暫時性）
    AUTH_PERMANENT = "auth_permanent"   # 認證永久失敗
    BILLING = "billing"                 # 402 額度不足
    MODEL_NOT_FOUND = "model_not_found" # 404 模型不存在
    FORMAT_ERROR = "format_error"       # 400 請求格式錯誤
    OMLX_NOT_READY = "omlx_not_ready"  # oMLX 模型未載入/啟動中
    OMLX_OOM = "omlx_oom"              # oMLX 記憶體不足
    UNKNOWN = "unknown"                 # 無法分類


@dataclass
class ClassifiedError:
    """分類後的錯誤，附帶恢復建議。"""
    reason: str = FailoverReason.UNKNOWN
    retryable: bool = True
    should_compress: bool = False
    should_fallback: bool = False
    should_rotate_credential: bool = False
    message: str = ""
    http_status: int = 0
    raw_error: str = ""

    def __str__(self) -> str:
        hints = []
        if self.retryable:
            hints.append("retry")
        if self.should_compress:
            hints.append("compress")
        if self.should_fallback:
            hints.append("fallback")
        if self.should_rotate_credential:
            hints.append("rotate")
        return "ClassifiedError(reason={}, hints=[{}], msg={})".format(
            self.reason, ",".join(hints), self.message[:80]
        )


def classify_error(
    error: Any,
    *,
    http_status: int = 0,
    response_body: str = "",
    provider: str = "",
) -> ClassifiedError:
    """
    分類一個 API 錯誤，回傳結構化的恢復建議。

    Args:
        error: Exception 物件、error dict、或 error string
        http_status: HTTP 狀態碼（如果已知）
        response_body: HTTP response body（如果已知）
        provider: "omlx" / "openrouter" / "anthropic" / ""
    """
    # 正規化輸入
    if isinstance(error, Exception):
        err_str = str(error)
        err_type = type(error).__name__
    elif isinstance(error, dict):
        err_str = str(error.get("error") or error.get("message") or error)
        http_status = http_status or int(error.get("status_code") or error.get("http_status") or 0)
        err_type = str(error.get("error_type") or "")
    else:
        err_str = str(error)
        err_type = ""

    low = err_str.lower()
    body_low = response_body.lower()
    combined = low + " " + body_low

    ce = ClassifiedError(raw_error=err_str[:500], http_status=http_status)

    # ── Pipeline Stage 1：oMLX 特有模式 ──
    if provider == "omlx" or "omlx" in low or "localhost:808" in low:
        if any(k in combined for k in ["not ready", "model not loaded", "loading model", "no model"]):
            ce.reason = FailoverReason.OMLX_NOT_READY
            ce.retryable = True
            ce.should_fallback = True
            ce.message = "oMLX 模型尚未載入，應等待或 fallback"
            return ce
        if any(k in combined for k in ["out of memory", "oom", "exceeds max-model-memory", "memory"]):
            ce.reason = FailoverReason.OMLX_OOM
            ce.retryable = False
            ce.should_fallback = True
            ce.message = "oMLX 記憶體不足"
            return ce
        if any(k in combined for k in ["connection refused", "connect timeout", "unreachable"]):
            ce.reason = FailoverReason.OMLX_NOT_READY
            ce.retryable = True
            ce.should_fallback = True
            ce.message = "oMLX 服務未啟動或連線被拒"
            return ce

    # ── Pipeline Stage 2：HTTP 狀態碼 ──
    if http_status == 429 or "429" in err_str:
        ce.reason = FailoverReason.RATE_LIMIT
        ce.retryable = True
        ce.message = "限流，稍後重試"
        return ce

    if http_status == 401 or http_status == 403:
        ce.reason = FailoverReason.AUTH
        ce.retryable = False
        ce.should_rotate_credential = True
        ce.message = "認證失敗"
        return ce

    if http_status == 402:
        if any(k in combined for k in ["try again", "resets in", "reset"]):
            ce.reason = FailoverReason.RATE_LIMIT
            ce.retryable = True
            ce.message = "暫時額度用完，等 reset"
        else:
            ce.reason = FailoverReason.BILLING
            ce.retryable = False
            ce.should_rotate_credential = True
            ce.should_fallback = True
            ce.message = "額度不足"
        return ce

    if http_status == 404:
        ce.reason = FailoverReason.MODEL_NOT_FOUND
        ce.retryable = False
        ce.should_fallback = True
        ce.message = "模型不存在"
        return ce

    if http_status == 400:
        ce.reason = FailoverReason.FORMAT_ERROR
        ce.retryable = False
        ce.message = "請求格式錯誤"
        return ce

    if http_status in (500, 502):
        ce.reason = FailoverReason.SERVER_ERROR
        ce.retryable = True
        ce.message = "伺服器錯誤"
        return ce

    if http_status in (503, 529):
        ce.reason = FailoverReason.OVERLOADED
        ce.retryable = True
        ce.should_fallback = True
        ce.message = "伺服器過載"
        return ce

    # ── Pipeline Stage 3：字串模式比對 ──
    if any(k in combined for k in [
        "context length", "context_length", "max.*token", "too many tokens",
        "maximum context", "input too long", "prompt is too long",
    ]):
        ce.reason = FailoverReason.CONTEXT_OVERFLOW
        ce.retryable = True
        ce.should_compress = True
        ce.message = "Context 超過模型上限，應壓縮後重試"
        return ce

    if any(k in combined for k in ["timeout", "timed out", "read timeout", "connect timeout"]):
        ce.reason = FailoverReason.TIMEOUT
        ce.retryable = True
        ce.message = "連線或讀取逾時"
        return ce

    if any(k in combined for k in ["connection refused", "connection reset", "broken pipe"]):
        ce.reason = FailoverReason.SERVER_ERROR
        ce.retryable = True
        ce.should_fallback = True
        ce.message = "連線失敗"
        return ce

    if any(k in combined for k in ["unauthorized", "invalid api key", "invalid_api_key"]):
        ce.reason = FailoverReason.AUTH
        ce.retryable = False
        ce.should_rotate_credential = True
        ce.message = "API Key 無效"
        return ce

    # ── Pipeline Stage 4：Fallback ──
    ce.reason = FailoverReason.UNKNOWN
    ce.retryable = True
    ce.message = "無法分類的錯誤，預設可重試"
    return ce
