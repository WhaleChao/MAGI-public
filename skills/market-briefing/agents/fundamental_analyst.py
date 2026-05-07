"""
skills/market-briefing/agents/fundamental_analyst.py
Fundamental Analysis Agent for the Hedge Fund Committee.
"""
from __future__ import annotations
from typing import Dict, List, Any, Optional

from agents.base import BaseAgent
from models.signals import TradingSignal, TradingAction, CommitteeState

class FundamentalAnalyst(BaseAgent):
    def __init__(self, model_name: str = ""):
        from agents.base import COMMITTEE_LIGHT_MODEL
        super().__init__(
            name="Fundamental Analyst",
            role_description="Expert in value investing and fundamental analysis. "
                             "Focuses on earnings, revenue growth, profit margins, and valuation.",
            model_name=model_name or COMMITTEE_LIGHT_MODEL,
        )

    def run(self, state: CommitteeState) -> TradingSignal:
        # 1. Extract raw data from state (Fundamentals)
        fundamentals = state.market_data.get("fundamentals", {})
        if not fundamentals:
            return TradingSignal(action=TradingAction.NEUTRAL, confidence=0.0, reasoning="缺乏基本面數據（如財報或 EPS Info）。")

        # 2. Extract specific metrics (Format depends on TWSE or SEC source)
        rev_info = fundamentals.get("rev", "無資料")
        eps_info = fundamentals.get("eps", "無資料")
        pe_ratio = fundamentals.get("pe_ratio", "無資料")
        
        # 3. Agentic Analysis
        prompt = f"""
請根據以下基本面數據分析 {state.ticker} ({state.company_name})：
- 營收動態: {rev_info}
- 每股盈餘 (EPS): {eps_info}
- 本益比 (P/E): {pe_ratio}
- 其他資訊: {fundamentals.get('additional_notes', '無')}

請給出您的專業判斷，包含：
1. 具體的交易動作 (BUY, SELL, HOLD, NEUTRAL)
2. 信心指數 (0.0 到 1.0)
3. 繁體中文的推理過程，特別是獲利能力與增長潛力的分析。

格式要求：僅輸出的 JSON 字串，包含 action, confidence, reasoning 三個欄位。
        """
        
        res_text = self.ask_llm(prompt)
        
        # 4. Parse response
        try:
            import json
            import re
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
                    "revenue": rev_info,
                    "eps": eps_info,
                    "pe": pe_ratio
                }
            )
        except Exception as e:
            self.logger.error(f"Failed to parse Fundamental Analyst response: {e}")
            return TradingSignal(
                action=TradingAction.NEUTRAL, 
                confidence=0.0, 
                reasoning=f"Agent 內部錯誤: {str(e)}"
            )
