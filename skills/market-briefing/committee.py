"""
skills/market-briefing/committee.py
Orchestrator for the Hedge Fund Multi-Agent Committee.

Heterogeneous architecture:
  Analysts (light tier)  → MAGI_COMMITTEE_LIGHT_MODEL (gemma-4-e4b-it-4bit when available, else 26B fallback)
  Managers (heavy tier)  → MAGI_COMMITTEE_HEAVY_MODEL (gemma-4-26b-a4b-it-4bit)

To enable E4B:
  1. Download model: huggingface-cli download mlx-community/gemma-4-e4b-it-4bit
  2. Start a second oMLX instance at port 8082 with the E4B model
  3. Set env: MAGI_COMMITTEE_LIGHT_MODEL=gemma-4-e4b-it-4bit
     (already the default; melchior_client falls back to 26B if E4B not in oMLX models list)
"""
from __future__ import annotations
import logging
from typing import Dict, List, Any, Optional

from models.signals import CommitteeState, AgentSignal
from agents.technical_analyst import TechnicalAnalyst
from agents.fundamental_analyst import FundamentalAnalyst
from agents.sentiment_analyst import SentimentAnalyst
from agents.risk_manager import RiskManager
from agents.portfolio_manager import PortfolioManager


class HedgeFundCommittee:
    def __init__(
        self,
        light_model: str = "",
        heavy_model: str = "",
    ):
        """
        Args:
            light_model: Override model for analyst agents (defaults to MAGI_COMMITTEE_LIGHT_MODEL).
            heavy_model: Override model for risk/portfolio managers (defaults to MAGI_COMMITTEE_HEAVY_MODEL).
        """
        self.logger = logging.getLogger("HedgeFundCommittee")
        self.technicals = TechnicalAnalyst(model_name=light_model)
        self.fundamentals = FundamentalAnalyst(model_name=light_model)
        self.sentiment = SentimentAnalyst(model_name=light_model)
        self.risk_manager = RiskManager(model_name=heavy_model)
        self.portfolio_manager = PortfolioManager(model_name=heavy_model)

        # Log the active model configuration
        self.logger.info(
            "Committee init — analysts=%s managers=%s",
            self.technicals.model_name,
            self.risk_manager.model_name,
        )

    def run_analysis(self, ticker: str, company_name: str, market_data: Dict[str, Any]) -> CommitteeState:
        """Execute the full multi-agent committee workflow."""
        state = CommitteeState(
            ticker=ticker,
            company_name=company_name,
            market_data=market_data
        )

        self.logger.info(f"Starting committee deliberation for {ticker}")

        # 1. Run Analysts (Sequential to avoid local LLM locking/contention)
        analysts = [self.technicals, self.fundamentals, self.sentiment]
        for agent in analysts:
            try:
                self.logger.info(f"Invoking {agent.name}...")
                signal = agent.run(state)
                state.signals.append(agent.to_agent_signal(signal))
            except Exception as e:
                self.logger.error(f"Error in {agent.name}: {e}")

        # 2. Run Risk Manager
        try:
            self.logger.info("Invoking Risk Manager...")
            risk_signal = self.risk_manager.run(state)
            state.signals.append(self.risk_manager.to_agent_signal(risk_signal))
            state.risk_assessment = risk_signal.reasoning
        except Exception as e:
            self.logger.error(f"Error in Risk Manager: {e}")

        # 3. Final decision by Portfolio Manager
        try:
            self.logger.info("Invoking Portfolio Manager for final decision...")
            final_signal = self.portfolio_manager.run(state)
            state.final_decision = final_signal
        except Exception as e:
            self.logger.error(f"Error in Portfolio Manager: {e}")

        return state
