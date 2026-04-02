# -*- coding: utf-8 -*-
"""
casper_llm_proxy.py
===================
本地 LLM 代理層：提供一個「長得像 google.generativeai.GenerativeModel」的介面，
讓既有程式（原本寫給 Gemini SDK 的）在 MAGI_ALLOW_CLOUD_MODELS=0 時仍可運作，
並且只走本機 CASPER / MELCHIOR（不使用任何雲端 API）。
"""

from __future__ import annotations

from dataclasses import dataclass

from casper_tools_client import casper_chat


@dataclass
class CasperResponse:
    text: str
    candidates: list
    prompt_feedback: str | None = None


class CasperGenerativeModel:
    """
    Minimal proxy that mimics google.generativeai.GenerativeModel enough for this codebase:
    - generate_content(prompt) -> object with .text and .candidates
    - count_tokens(text) -> int (rough estimate)
    """

    def __init__(self, timeout_sec: int = 240):
        self.timeout_sec = int(timeout_sec)

    def generate_content(self, prompt: str) -> CasperResponse:
        r = casper_chat(prompt, timeout_sec=self.timeout_sec)
        if not isinstance(r, dict) or not r.get("success"):
            return CasperResponse(
                text="",
                candidates=[],
                prompt_feedback=(r.get("error") if isinstance(r, dict) else "casper_chat failed"),
            )
        text = (r.get("response") or "").strip()
        return CasperResponse(text=text, candidates=[{"text": text}], prompt_feedback=None)

    def count_tokens(self, text: str) -> int:
        # Conservative heuristic: ~4 chars per token for CJK/English mix.
        s = text or ""
        return max(1, int(len(s) / 4))

