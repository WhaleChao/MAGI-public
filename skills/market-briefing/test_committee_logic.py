import os
import sys
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

# Setup Path
MAGI_ROOT = Path("/Users/ai/Desktop/MAGI_v2")
sys.path.insert(0, str(MAGI_ROOT))
sys.path.insert(0, str(MAGI_ROOT / "skills" / "market-briefing"))

from committee import HedgeFundCommittee
from models.signals import CommitteeState, TradingAction

def test_mock_flow():
    print("🚀 Starting Mock Verification Flow...")
    
    # 1. Setup Committee
    committee = HedgeFundCommittee()
    
    # 2. Mock ask_llm for all agents to avoid hanging on local LLM
    # Each agent expects a JSON response with action, confidence, reasoning
    mock_responses = {
        "Technical Analyst": '{"action": "BUY", "confidence": 0.85, "reasoning": "MA黃金交叉，動能強勁。"}',
        "Fundamental Analyst": '{"action": "BUY", "confidence": 0.7, "reasoning": "營收持續成長，估值合理。"}',
        "Sentiment Analyst": '{"action": "NEUTRAL", "confidence": 0.5, "reasoning": "市場情緒觀望，無重大特發新聞。"}',
        "Risk Manager": '{"action": "BUY", "confidence": 0.9, "reasoning": "下行風險有限，支撐位穩固。"}',
        "Portfolio Manager": '{"action": "BUY", "confidence": 0.8, "reasoning": "各小組共識偏多，技術面與基本面共振，建議買進。"}'
    }

    def mocked_ask_llm(self, prompt, **kwargs):
        print(f"   [MOCK] {self.name} is reasoning...")
        return mock_responses.get(self.name, '{"action": "NEUTRAL", "confidence": 0.0, "reasoning": "No mock data."}')

    # Path to BaseAgent.ask_llm
    with patch("agents.base.BaseAgent.ask_llm", side_effect=mocked_ask_llm, autospec=True):
        
        state = CommitteeState(
            ticker="2330.TW",
            company_name="台積電",
            market_data={
                "closes": [1800, 1850, 1900, 1950, 2000] * 5,
                "news": ["Mocked News"]
            }
        )
        
        print("💡 Running Committee Deliberation...")
        final_state = committee.run_analysis("2330.TW", "台積電", state.market_data)
        
        print("\n✅ Verification SUCCESS!")
        print(f"Ticker: {final_state.ticker}")
        print(f"Final Action: {final_state.final_decision.action.value}")
        print(f"Final Confidence: {final_state.final_decision.confidence}")
        print(f"Final Report: {final_state.final_decision.reasoning}")
        
    print("\n--- Summary of Analysts ---")
    for s in final_state.signals:
        print(f"- {s.agent_name}: {s.signal.action.value} ({s.signal.confidence})")

if __name__ == "__main__":
    test_mock_flow()
