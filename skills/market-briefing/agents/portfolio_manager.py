"""
skills/market-briefing/agents/portfolio_manager.py
Portfolio Manager Agent for the Hedge Fund Committee.
"""
from __future__ import annotations
from typing import Dict, List, Any, Optional

from agents.base import BaseAgent
from models.signals import TradingSignal, TradingAction, CommitteeState

class PortfolioManager(BaseAgent):
    def __init__(self, model_name: str = ""):
        from agents.base import COMMITTEE_HEAVY_MODEL
        super().__init__(
            name="Portfolio Manager",
            role_description="Chief Investment Officer (CIO). Responsible for the final decision based on analyst input.",
            model_name=model_name or COMMITTEE_HEAVY_MODEL,
        )

    def run(self, state: CommitteeState) -> TradingSignal:
        # 1. Gather all analyst signals
        analyst_views = ""
        for s in state.signals:
            analyst_views += f"\n### {s.agent_name} ({s.agent_role})\n"
            analyst_views += f"- 建議動作: {s.signal.action.value}\n"
            analyst_views += f"- 信心度: {s.signal.confidence:.2f}\n"
            analyst_views += f"- 推理: {s.signal.reasoning}\n"

        # 2. Synthesis Prompt
        prompt = f"""
您是 {state.ticker} ({state.company_name}) 投資委員會的主席。
以下是您的分析師團隊提供的報告：

{analyst_views}

請根據以上各方的辯論與證據，做出最終決定。
如果分析師意見分歧，請權衡權重（例如：基本面適合長週期、技術面適合短週期）。

請給出最終報告：
1. 最終動作 (BUY, SELL, HOLD, NEUTRAL)
2. 綜合信心指數 (0.0 到 1.0)
3. 最終總結與操作建議 (繁體中文)，這將是給使用者的最終報告內容。

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
                reasoning=data.get("reasoning", "無法解析主席的最終總結。")
            )
        except Exception as e:
            return TradingSignal(action=TradingAction.NEUTRAL, confidence=0.0, reasoning=f"決策系統錯誤: {str(e)}")
