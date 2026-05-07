"""
skills/market-briefing/agents/sentiment_analyst.py
Sentiment Analysis Agent for the Hedge Fund Committee.
"""
from __future__ import annotations
from typing import Dict, List, Any, Optional

from agents.base import BaseAgent
from models.signals import TradingSignal, TradingAction, CommitteeState

class SentimentAnalyst(BaseAgent):
    def __init__(self, model_name: str = ""):
        from agents.base import COMMITTEE_LIGHT_MODEL
        super().__init__(
            name="Sentiment Analyst",
            role_description="Expert in social listening, news analysis, and market psychology. "
                             "Focuses on headlines, analyst ratings, and social trends.",
            model_name=model_name or COMMITTEE_LIGHT_MODEL,
        )

    def run(self, state: CommitteeState) -> TradingSignal:
        # 1. Extract raw data from state (News/Sentiment)
        news = state.market_data.get("news", [])
        news_source = ((state.market_data.get("data_quality") or {}).get("news_source") or "unavailable")
        if not news:
            return TradingSignal(
                action=TradingAction.NEUTRAL,
                confidence=0.0,
                reasoning="缺乏可驗證新聞標題；不進行情緒推論。",
                indicators={"headline_count": 0, "news_source": news_source},
            )

        # 2. Format news for prompt
        news_summaries = "\n".join([f"- {item}" for item in news[:10]]) # Max 10 headlines
        
        # 3. Agentic Analysis
        prompt = f"""
請根據以下新聞標題與市場訊息分析 {state.ticker} ({state.company_name}) 的市場情緒：

{news_summaries}

資料限制：
- 只能引用上方列出的標題與來源，不得補充未提供的新聞、事件或分析師評級。
- 如果標題不足以支持明確方向，請選 NEUTRAL 或 HOLD，並降低 confidence。
- reasoning 必須點名使用了哪些標題編號；不得寫「市場普遍」這類沒有來源的泛稱。

請給出您的專業判斷，包含：
1. 具體的交易動作 (BUY, SELL, HOLD, NEUTRAL)
2. 信心指數 (0.0 到 1.0)
3. 繁體中文的推理過程，說明市場對該標題的可能反應。

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
                    "headline_count": len(news),
                    "news_source": news_source,
                    "primary_tone": "grounded_in_attributed_headlines"
                }
            )
        except Exception as e:
            self.logger.error(f"Failed to parse Sentiment Analyst response: {e}")
            return TradingSignal(
                action=TradingAction.NEUTRAL, 
                confidence=0.0, 
                reasoning=f"Agent 內部錯誤: {str(e)}"
            )
