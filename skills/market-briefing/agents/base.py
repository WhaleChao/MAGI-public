"""
skills/market-briefing/agents/base.py
Base classes for the Hedge Fund Agents.

Heterogeneous committee support:
- LIGHT agents (Analysts): MAGI_COMMITTEE_LIGHT_MODEL (default: gemma-4-e4b-it-4bit)
  → Falls back to 26B automatically if E4B is not loaded in oMLX
- HEAVY agents (RiskManager, PortfolioManager): MAGI_COMMITTEE_HEAVY_MODEL (default: gemma-4-26b-a4b-it-4bit)
"""
from __future__ import annotations
import abc
import logging
import os
from typing import Dict, List, Any, Optional

from skills.bridge import melchior_client
from models.signals import TradingSignal, CommitteeState, AgentSignal

# ── Heterogeneous model configuration ─────────────────────────────────────
# Light analysts use a smaller/faster model; heavy decision-makers use 26B.
# Both fall back to TEXT_PRIMARY_MODEL if the specified model is unavailable.
_E4B_MODEL = "gemma-4-e4b-it-4bit"
_26B_MODEL = melchior_client.TEXT_PRIMARY_MODEL

COMMITTEE_LIGHT_MODEL = (
    os.environ.get("MAGI_COMMITTEE_LIGHT_MODEL") or _E4B_MODEL
).strip() or _E4B_MODEL

COMMITTEE_HEAVY_MODEL = (
    os.environ.get("MAGI_COMMITTEE_HEAVY_MODEL") or _26B_MODEL
).strip() or _26B_MODEL


class BaseAgent(abc.ABC):
    def __init__(self, name: str, role_description: str, model_name: str = ""):
        self.name = name
        self.role_description = role_description
        # model_name="" → resolved to TEXT_PRIMARY_MODEL inside melchior_client
        self.model_name = model_name.strip() if model_name else ""
        self.logger = logging.getLogger(f"Agent.{name}")

    @abc.abstractmethod
    def run(self, state: CommitteeState) -> TradingSignal:
        """Analyze market data and return a trading signal."""
        pass

    def ask_llm(self, prompt: str, system_prompt: Optional[str] = None, model: str = "") -> str:
        """Helper to call Melchior/LLM.

        Priority: explicit `model` arg > self.model_name > melchior_client.TEXT_PRIMARY_MODEL
        melchior_client.chat() already handles fallback if requested model is unavailable.
        """
        sys_p = system_prompt or (
            f"You are {self.name}, {self.role_description}. Conduct analysis in Traditional Chinese (繁體中文).\n"
            "Grounding rules: use only the market data, indicators, filings, and attributed headlines provided in the task. "
            "Do not invent prices, news, analyst ratings, events, dates, or sources. "
            "If a data field is missing or marked unavailable, say the signal is insufficient and lower confidence."
        )
        combined_prompt = f"[Instructions]\n{sys_p}\n\n[Analysis Task]\n{prompt}"

        resolved_model = model.strip() if model.strip() else (
            self.model_name or melchior_client.TEXT_PRIMARY_MODEL
        )

        res = melchior_client.chat(
            prompt=combined_prompt,
            model=resolved_model
        )
        if res and res.get("success"):
            return res.get("response", "").strip()
        return ""

    def to_agent_signal(self, signal: TradingSignal) -> AgentSignal:
        return AgentSignal(
            agent_name=self.name,
            agent_role=self.role_description,
            signal=signal
        )
