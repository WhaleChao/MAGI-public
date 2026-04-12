"""Tool router — dispatches tool-first queries to the appropriate handler.

This module bridges the gap between tool requirement classification and
actual tool execution. It provides a unified interface for:
1. Checking if a query needs a tool
2. Executing the appropriate tool
3. Structuring the tool output for LLM consumption

Usage::

    from api.tools.tool_router import route_to_tool

    result = route_to_tool(message, intent="QUERY", user_id="U123")
    if result.used_tool:
        # Tool was called; use result.structured_output for LLM context
        ...
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from api.tools.policies import ToolRequirement, classify_tool_requirement, format_tool_failure_response

logger = logging.getLogger(__name__)


@dataclass()
class ToolRouteResult:
    used_tool: bool = False
    tool_hint: str = ""
    requirement_level: str = "none"
    success: bool = False
    structured_output: str = ""
    raw_output: Any = None
    error: str = ""
    failure_response: str = ""

    def as_context(self) -> str:
        """Return tool output formatted for injection into the LLM prompt."""
        if not self.used_tool:
            return ""
        if self.success and self.structured_output:
            return f"[工具查詢結果 ({self.tool_hint})]\n{self.structured_output}"
        if self.error:
            return f"[工具查詢失敗 ({self.tool_hint})]\n{self.error}"
        return ""


def route_to_tool(
    message: str,
    *,
    intent: str = "",
    user_id: str = "",
    platform: str = "",
    has_memory_context: bool = False,
) -> ToolRouteResult:
    """Classify and optionally execute a tool for the given message.

    Currently performs classification only — actual tool execution
    is delegated to the orchestrator or tools_api layer which has
    access to the full runtime context.
    """
    req = classify_tool_requirement(
        message,
        intent=intent,
        has_memory_context=has_memory_context,
    )

    result = ToolRouteResult(
        tool_hint=req.tool_hint,
        requirement_level=req.level,
    )

    if req.level == "none":
        return result

    # Mark that a tool should be used
    result.used_tool = True

    # For "required" tools, pre-generate the failure response
    # so the caller can use it if the tool fails
    if req.level == "required":
        result.failure_response = format_tool_failure_response(req.tool_hint)

    logger.info(
        "Tool route: level=%s, hint=%s, reason=%s",
        req.level, req.tool_hint, req.reason,
    )

    return result
