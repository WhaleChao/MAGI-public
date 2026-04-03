from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Any, Callable, Mapping

import requests

from api.tools.base import ToolExecutor
from api.tools.contracts import ToolContext


@dataclass(slots=True)
class CallableToolExecutor:
    fn: Callable[..., Any]

    def execute(self, arguments: Mapping[str, Any], context: ToolContext | None = None) -> Any:
        kwargs = dict(arguments or {})
        if context is not None:
            sig = inspect.signature(self.fn)
            if "context" in sig.parameters and "context" not in kwargs:
                kwargs["context"] = context
            if "tool_context" in sig.parameters and "tool_context" not in kwargs:
                kwargs["tool_context"] = context
        return self.fn(**kwargs)


@dataclass(slots=True)
class HttpJsonToolExecutor:
    method: str
    url: str
    timeout_sec: int = 30
    session: requests.Session | None = None

    def execute(self, arguments: Mapping[str, Any], context: ToolContext | None = None) -> Any:
        session = self.session or requests.Session()
        payload = dict(arguments or {})
        if context is not None:
            payload.setdefault("context", {
                "user_id": context.user_id,
                "platform": context.platform,
                "correlation_id": context.correlation_id,
                "metadata": dict(context.metadata),
            })
        response = session.request(
            self.method.upper(),
            self.url,
            json=payload,
            timeout=self.timeout_sec,
        )
        response.raise_for_status()
        try:
            return response.json()
        except Exception:
            return response.text
