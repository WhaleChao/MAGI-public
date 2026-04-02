from __future__ import annotations

import os
from typing import Any


def _env_flag(name: str, default: str = "1") -> bool:
    raw = os.environ[name] if name in os.environ else default
    return str(raw or "").strip().lower() in {"1", "true", "yes", "on"}


class LineSDKUnavailableError(RuntimeError):
    """Raised when LINE SDK functionality is used without line-bot-sdk installed."""


class _BaseMessage:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        for key, value in kwargs.items():
            setattr(self, key, value)
        if args and not kwargs:
            if len(args) == 1:
                setattr(self, "value", args[0])
            else:
                setattr(self, "args", args)


class _UnavailableLineBotApi:
    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        self.available = False

    def __getattr__(self, _name: str):
        def _missing(*_args: Any, **_kwargs: Any) -> Any:
            raise LineSDKUnavailableError("line-bot-sdk is not installed")

        return _missing


class _UnavailableWebhookHandler:
    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        self.available = False

    def add(self, *_args: Any, **_kwargs: Any):
        def _decorator(func):
            return func

        return _decorator

    def handle(self, *_args: Any, **_kwargs: Any) -> None:
        raise LineSDKUnavailableError("line-bot-sdk is not installed")


class InvalidSignatureError(Exception):
    """Fallback InvalidSignatureError when LINE SDK is unavailable."""


class LineBotApiError(Exception):
    """Fallback LineBotApiError when LINE SDK is unavailable."""

    def __init__(self, message: str = "", status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class MessageEvent:
    pass


class TextMessage:
    pass


class ImageMessage:
    pass


class AudioMessage:
    pass


class FileMessage:
    pass


class TextSendMessage(_BaseMessage):
    def __init__(self, text: str = "", **kwargs: Any) -> None:
        super().__init__(text=text, **kwargs)


class ImageSendMessage(_BaseMessage):
    pass


LINE_SDK_AVAILABLE = False

try:
    from linebot import LineBotApi, WebhookHandler
    from linebot.exceptions import InvalidSignatureError, LineBotApiError
    from linebot.models import (
        AudioMessage,
        FileMessage,
        ImageMessage,
        ImageSendMessage,
        MessageEvent,
        TextMessage,
        TextSendMessage,
    )

    LINE_SDK_AVAILABLE = True
except ModuleNotFoundError:
    LineBotApi = _UnavailableLineBotApi
    WebhookHandler = _UnavailableWebhookHandler


def line_feature_enabled() -> bool:
    return _env_flag("MAGI_ENABLE_LINE", "1")


def build_line_clients(access_token: str, secret: str):
    token = str(access_token or "").strip()
    secret_value = str(secret or "").strip()

    if not line_feature_enabled():
        return _UnavailableLineBotApi(), _UnavailableWebhookHandler(), False, "disabled by MAGI_ENABLE_LINE"
    if not LINE_SDK_AVAILABLE:
        return _UnavailableLineBotApi(), _UnavailableWebhookHandler(), False, "line-bot-sdk is not installed"
    if not token or not secret_value:
        return _UnavailableLineBotApi(), _UnavailableWebhookHandler(), False, "credentials missing"
    return LineBotApi(token), WebhookHandler(secret_value), True, ""
