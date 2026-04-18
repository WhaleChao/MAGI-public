import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from api.session.memory_policy import evaluate_memory_write
from api.session.verified_fact_gate import is_reflexive_query
from skills.memory.mem_bridge import _source_trust_weight


def test_reflexive_query_detection():
    assert is_reflexive_query("你還記得我上次說過什麼嗎")


def test_non_reflexive_query_detection():
    assert not is_reflexive_query("法扶補助上限是多少")


def test_assistant_utterance_stays_low_trust_for_normal_query():
    weight = _source_trust_weight(
        "assistant_generated_utterance|role=assistant|namespace=assistant_utterances",
        query="法扶補助上限是多少",
    )
    assert weight < 0.2


def test_assistant_utterance_can_help_only_for_reflexive_query():
    weight = _source_trust_weight(
        "assistant_generated_utterance|role=assistant|namespace=assistant_utterances",
        query="你上次說過什麼",
    )
    assert weight > 0.3


def test_assistant_generated_never_promotes_by_policy():
    d = evaluate_memory_write(
        "法扶補助每月上限 15 萬元",
        source="assistant_generated|mode=chat",
        metadata={"source_type": "assistant_generated", "confidence": 0.95},
    )
    assert not d.allowed
