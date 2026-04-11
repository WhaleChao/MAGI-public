"""
skills/market-briefing/agents/risk_manager.py
Risk Management Agent for the Hedge Fund Committee.
"""
from __future__ import annotations
from typing import Dict, List, Any, Optional

from agents.base import BaseAgent
from models.signals import TradingSignal, TradingAction, CommitteeState

class RiskManager(BaseAgent):
    def __init__(self, model_name: str = ""):
        from agents.base import COMMITTEE_HEAVY_MODEL
        super().__init__(
            name="Risk Manager",
            role_description="Expert in risk mitigation and capital preservation. "
                             "Focuses on volatility, downside risk, and macroeconomic threats.",
            model_name=model_name or COMMITTEE_HEAVY_MODEL,
        )

    def run(self, state: CommitteeState) -> TradingSignal:
        # 1. Risk Manager takes the current signals and proposed decision (if any)
        # For simplicity, we use the same run interface but Risk Manager's reasoning 
        # is focused on "safety".
        
        vol = state.market_data.get("vol", 0.0)
        recent_news = state.market_data.get("news", [])
        
        prompt = f"""
請作為風險控管師，評估 {state.ticker} ({state.company_name}) 的當前風險：
- 當前波動率: {vol:.2f}%
- 最近新聞摘要: {recent_news[:3] if recent_news else '無資料'}

目前分析師小組 (技術/基本面/情緒) 已經給出了各自的信號。
請協助指出潛在的風險點（如：即將到來的財報、宏觀經濟數據、地緣政治、技術性超賣等）。

請給出您的專業判斷：
1. 風控動作 (BUY, SELL, HOLD, NEUTRAL) - 是否支持分析師的擴張或應轉向保守。
2. 信心指數 (0.0 到 1.0)
3. 繁體中文的風險警示推理。

格式要求：僅輸出的 JSON 字串，包含 action, confidence, reasoning 三個欄位。
        """
        
        res_text = self.ask_llm(prompt)
        
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
                reasoning=data.get("reasoning", "無法解析 LLM 推理過程。")
            )
        except Exception as e:
            return TradingSignal(action=TradingAction.NEUTRAL, confidence=0.0, reasoning=f"風控系統錯誤: {str(e)}")
