import os
import sys
from pathlib import Path

# Setup Path
MAGI_ROOT = Path("/Users/ai/Desktop/MAGI_v2")
sys.path.insert(0, str(MAGI_ROOT))
sys.path.insert(0, str(MAGI_ROOT / "skills" / "market-briefing"))

from committee import HedgeFundCommittee
from models.signals import CommitteeState

print("Starting Committee Test...")
c = HedgeFundCommittee()

# Mock state
state = CommitteeState(
    ticker="2330.TW",
    company_name="台積電",
    market_data={
        "closes": [1800, 1850, 1900, 1950, 2000] * 5, # 25 days
        "news": ["台積電營收亮眼", "半導體需求強勁"]
    }
)

# Run only technical analyst to save time
print("Invoking Technical Analyst...")
sig = c.technicals.run(state)
print(f"Result: {sig.action.value} / Confidence: {sig.confidence}")
print(f"Reasoning: {sig.reasoning}")
