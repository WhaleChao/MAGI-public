import os
import sys
import tempfile

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from api.session.conversation_history import ConversationHistoryStore
from api.session.memory_policy import evaluate_memory_write
from api.session.provenance import namespace_for_source_type


def test_conversation_history_append_and_read():
    with tempfile.TemporaryDirectory() as tmp:
        store = ConversationHistoryStore(os.path.join(tmp, "history.sqlite3"))
        store.append("s1", "user", "hello")
        store.append("s1", "assistant", "world")
        rows = store.last_n("s1", 10)
        assert [r["role"] for r in rows] == ["user", "assistant"]


def test_conversation_history_last_sessions():
    with tempfile.TemporaryDirectory() as tmp:
        store = ConversationHistoryStore(os.path.join(tmp, "history.sqlite3"))
        store.append("s1", "user", "one")
        store.append("s2", "user", "two")
        sessions = store.last_sessions(2)
        assert "s1" in sessions and "s2" in sessions


def test_layer2_namespace_mapping():
    assert namespace_for_source_type("assistant_generated_utterance") == "assistant_utterances"


def test_layer3_namespace_mapping():
    assert namespace_for_source_type("verified_fact") == "verified_facts"


def test_assistant_chatlog_becomes_layer2_when_enabled(monkeypatch):
    monkeypatch.setenv("MAGI_CAPTURE_ASSISTANT_CHATLOG", "1")
    d = evaluate_memory_write(
        "這是 assistant utterance",
        source="chatlog|platform=LINE|user=u1|role=assistant",
        metadata={"source_type": "chatlog", "role": "assistant", "confidence": 0.9},
    )
    assert d.allowed
    assert d.effective_source_type == "assistant_generated_utterance"
    assert d.effective_confidence == 0.25
