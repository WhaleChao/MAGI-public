"""
skills/market-briefing/models/signals.py
Core models for the Hedge Fund Committee.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Dict, List, Optional, Any

class TradingAction(Enum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"
    NEUTRAL = "NEUTRAL"

@dataclass
class TradingSignal:
    action: TradingAction
    confidence: float  # 0.0 to 1.0
    reasoning: str
    indicators: Dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=datetime.now)

@dataclass
class AgentSignal:
    agent_name: str
    agent_role: str
    signal: TradingSignal

@dataclass
class CommitteeState:
    ticker: str
    company_name: str
    market_data: Dict[str, Any] = field(default_factory=dict)
    signals: List[AgentSignal] = field(default_factory=list)
    debate_history: List[Dict[str, str]] = field(default_factory=list)  # Role-Content pairs
    final_decision: Optional[TradingSignal] = None
    risk_assessment: Optional[str] = None
    created_at: datetime = field(default_factory=datetime.now)
