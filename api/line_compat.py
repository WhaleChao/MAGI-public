from __future__ import annotations

import os
import warnings
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


class _CompatTextSendMessage(_BaseMessage):
    def __init__(self, text: str = "", **kwargs: Any) -> None:
        super().__init__(type="text", text=text, **kwargs)


class _CompatImageSendMessage(_BaseMessage):
    def __init__(
        self,
        original_content_url: str = "",
        preview_image_url: str = "",
        **kwargs: Any,
    ) -> None:
        original_url = str(
            kwargs.pop("originalContentUrl", "") or original_content_url or ""
        )
        preview_url = str(
            kwargs.pop("previewImageUrl", "") or preview_image_url or original_url
        )
        super().__init__(
            type="image",
            original_content_url=original_url,
            preview_image_url=preview_url,
            originalContentUrl=original_url,
            previewImageUrl=preview_url,
            **kwargs,
        )


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


TextSendMessage = _CompatTextSendMessage
ImageSendMessage = _CompatImageSendMessage
LineBotApi = _UnavailableLineBotApi
WebhookHandler = _UnavailableWebhookHandler
LINE_SDK_AVAILABLE = False
LINE_SDK_MODE = "unavailable"
LINE_SDK_BACKEND = LINE_SDK_MODE


def _suppress_linebot_import_warnings() -> None:
    warnings.filterwarnings(
        "ignore",
        message=r"Core Pydantic V1 functionality isn't compatible with Python 3\.14 or greater\.",
        category=UserWarning,
    )


try:
    with warnings.catch_warnings():
        _suppress_linebot_import_warnings()
        from linebot.v3.exceptions import InvalidSignatureError as _V3InvalidSignatureError
        from linebot.v3.messaging import (
            ApiClient as _V3ApiClient,
            Configuration as _V3Configuration,
            ImageMessage as _V3OutboundImageMessage,
            MessagingApi as _V3MessagingApi,
            MessagingApiBlob as _V3MessagingApiBlob,
            PushMessageRequest as _V3PushMessageRequest,
            ReplyMessageRequest as _V3ReplyMessageRequest,
            TextMessage as _V3OutboundTextMessage,
        )
        from linebot.v3.messaging.rest import ApiException as _V3ApiException
        from linebot.v3.webhook import WebhookHandler as _V3WebhookHandler
        from linebot.v3.webhooks import (
            AudioMessageContent as _V3AudioMessageContent,
            FileMessageContent as _V3FileMessageContent,
            ImageMessageContent as _V3ImageMessageContent,
            MessageEvent as _V3MessageEvent,
            TextMessageContent as _V3TextMessageContent,
        )

    class _BinaryContentStream:
        def __init__(self, payload: bytes | bytearray | None) -> None:
            self._payload = bytes(payload or b"")

        def iter_content(self, chunk_size: int = 8192):
            size = max(1, int(chunk_size))
            for start in range(0, len(self._payload), size):
                yield self._payload[start : start + size]

    class _V3LineBotApi:
        def __init__(self, access_token: str) -> None:
            self.available = True
            self._api_client = _V3ApiClient(_V3Configuration(access_token=access_token))
            self._messaging_api = _V3MessagingApi(self._api_client)
            self._blob_api = _V3MessagingApiBlob(self._api_client)

        def _normalize_message(self, message: Any):
            if isinstance(message, _CompatTextSendMessage):
                return _V3OutboundTextMessage(text=str(getattr(message, "text", "") or ""))
            if isinstance(message, _CompatImageSendMessage):
                original_url = str(
                    getattr(message, "original_content_url", "")
                    or getattr(message, "originalContentUrl", "")
                    or ""
                )
                preview_url = str(
                    getattr(message, "preview_image_url", "")
                    or getattr(message, "previewImageUrl", "")
                    or original_url
                )
                return _V3OutboundImageMessage(
                    originalContentUrl=original_url,
                    previewImageUrl=preview_url,
                )
            return message

        def _normalize_messages(self, messages: Any) -> list[Any]:
            if isinstance(messages, (list, tuple)):
                items = list(messages)
            else:
                items = [messages]
            return [self._normalize_message(item) for item in items]

        def push_message(self, user_id: str, messages: Any) -> Any:
            request = _V3PushMessageRequest(
                to=str(user_id or ""),
                messages=self._normalize_messages(messages),
            )
            try:
                return self._messaging_api.push_message(request)
            except _V3ApiException as exc:
                raise LineBotApiError(str(exc), status_code=getattr(exc, "status", None)) from exc

        def reply_message(self, reply_token: str, messages: Any) -> Any:
            request = _V3ReplyMessageRequest(
                replyToken=str(reply_token or ""),
                messages=self._normalize_messages(messages),
            )
            try:
                return self._messaging_api.reply_message(request)
            except _V3ApiException as exc:
                raise LineBotApiError(str(exc), status_code=getattr(exc, "status", None)) from exc

        def get_message_content(self, message_id: str) -> _BinaryContentStream:
            try:
                payload = self._blob_api.get_message_content(str(message_id or ""))
            except _V3ApiException as exc:
                raise LineBotApiError(str(exc), status_code=getattr(exc, "status", None)) from exc
            return _BinaryContentStream(payload)

    LineBotApi = _V3LineBotApi
    WebhookHandler = _V3WebhookHandler
    InvalidSignatureError = _V3InvalidSignatureError
    MessageEvent = _V3MessageEvent
    TextMessage = _V3TextMessageContent
    ImageMessage = _V3ImageMessageContent
    AudioMessage = _V3AudioMessageContent
    FileMessage = _V3FileMessageContent
    LINE_SDK_AVAILABLE = True
    LINE_SDK_MODE = "v3"
    LINE_SDK_BACKEND = LINE_SDK_MODE
except Exception:
    try:
        from linebot import LineBotApi as _LegacyLineBotApi, WebhookHandler as _LegacyWebhookHandler
        from linebot.exceptions import InvalidSignatureError as _LegacyInvalidSignatureError, LineBotApiError as _LegacyLineBotApiError
        from linebot.models import (
            AudioMessage as _LegacyAudioMessage,
            FileMessage as _LegacyFileMessage,
            ImageMessage as _LegacyImageMessage,
            ImageSendMessage as _LegacyImageSendMessage,
            MessageEvent as _LegacyMessageEvent,
            TextMessage as _LegacyTextMessage,
            TextSendMessage as _LegacyTextSendMessage,
        )

        LineBotApi = _LegacyLineBotApi
        WebhookHandler = _LegacyWebhookHandler
        InvalidSignatureError = _LegacyInvalidSignatureError
        LineBotApiError = _LegacyLineBotApiError
        MessageEvent = _LegacyMessageEvent
        TextMessage = _LegacyTextMessage
        ImageMessage = _LegacyImageMessage
        AudioMessage = _LegacyAudioMessage
        FileMessage = _LegacyFileMessage
        TextSendMessage = _LegacyTextSendMessage
        ImageSendMessage = _LegacyImageSendMessage
        LINE_SDK_AVAILABLE = True
        LINE_SDK_MODE = "legacy"
        LINE_SDK_BACKEND = LINE_SDK_MODE
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
