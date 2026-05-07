"""
skills/market-briefing/agents/technical_analyst.py
Technical Analysis Agent for the Hedge Fund Committee.
"""
from __future__ import annotations
from typing import Dict, List, Any
import statistics

from agents.base import BaseAgent
from models.signals import TradingSignal, TradingAction, CommitteeState

class TechnicalAnalyst(BaseAgent):
    def __init__(self, model_name: str = ""):
        from agents.base import COMMITTEE_LIGHT_MODEL
        super().__init__(
            name="Technical Analyst",
            role_description="Expert in technical analysis, trend following, and momentum strategies. "
                             "Focuses on price action, moving averages, and volatility.",
            model_name=model_name or COMMITTEE_LIGHT_MODEL,
        )

    def run(self, state: CommitteeState) -> TradingSignal:
        # 1. Extract raw data from state (assumes market_data is populated by a gatherer)
        closes = state.market_data.get("closes", [])
        if len(closes) < 20:
            return TradingSignal(action=TradingAction.NEUTRAL, confidence=0.0, reasoning="歷史數據不足（需至少 20 根 K 線）。")

        # 2. Calculate Indicators (Logic moved from legacy action.py)
        last = float(closes[-1])
        ema5 = self._ema(closes, 5)
        ema20 = self._ema(closes, 20)
        
        trend = (ema5 - ema20) / ema20 * 100.0 if ema20 else 0.0
        mom5 = (last - closes[-6]) / closes[-6] * 100.0 if len(closes) >= 6 else 0.0
        
        returns = [ (closes[i] - closes[i-1])/closes[i-1] for i in range(1, len(closes)) ]
        vol = statistics.pstdev(returns) * 100.0 if returns else 0.0

        # 3. Agentic Analysis
        prompt = f"""
請根據以下技術指標分析 {state.ticker} ({state.company_name})：
- 目前價格: {last:.2f}
- 趨勢指標 (EMA5 vs EMA20): {trend:.2f}%
- 動能指標 (5日動能): {mom5:.2f}%
- 波動率 (STD): {vol:.2f}%

請給出您的專業判斷，包含：
1. 具體的交易動作 (BUY, SELL, HOLD, NEUTRAL)
2. 信心指數 (0.0 到 1.0)
3. 繁體中文的推理過程，說明觀察到的價格型態。

格式要求：僅輸出的 JSON 字串，包含 action, confidence, reasoning 三個欄位。
        """
        
        res_text = self.ask_llm(prompt)
        
        # 4. Parse response
        try:
            import json
            import re
            # Extract JSON if LLM added markdown
            match = re.search(r'\{.*\}', res_text, re.DOTALL)
            data = json.loads(match.group(0)) if match else {}
            
            action_map = {
                "BUY": TradingAction.BUY,
                "SELL": TradingAction.SELL,
                "HOLD": TradingAction.HOLD,
                "NEUTRAL": TradingAction.NEUTRAL
            }
            
            return TradingSignal(
                action=action_map.get(data.get("action", "NEUTRAL").upper(), TradingAction.NEUTRAL),
                confidence=float(data.get("confidence", 0.5)),
                reasoning=data.get("reasoning", "無法解析 LLM 推理過程。"),
                indicators={
                    "trend": round(trend, 4),
                    "mom5": round(mom5, 4),
                    "vol": round(vol, 4)
                }
            )
        except Exception as e:
            self.logger.error(f"Failed to parse Technical Analyst response: {e}")
            return TradingSignal(
                action=TradingAction.NEUTRAL, 
                confidence=0.0, 
                reasoning=f"Agent 內部錯誤: {str(e)}"
            )

    def _ema(self, values: List[float], span: int) -> float:
        if not values: return 0.0
        alpha = 2.0 / (span + 1.0)
        ema = values[0]
        for v in values[1:]:
            ema = v * alpha + ema * (1.0 - alpha)
        return ema
