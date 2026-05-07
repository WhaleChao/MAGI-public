from __future__ import annotations

from api.session import SessionContextBuilder, SessionStore


def test_session_context_keeps_raw_history_summaries_and_pending_state_separate():
    store = SessionStore()
    store.append_message("s-1", "user", "Hello", metadata={"channel": "LINE"})
    store.append_message("s-1", "assistant", "Hi there")
    store.add_summary("s-1", "Discussed next steps", authoritative=False, metadata={"kind": "rollup"})
    store.update_pending_state("s-1", case_id="CASE-7", draft_review="pending")

    builder = SessionContextBuilder(store)
    context = builder.build("s-1", system_prompt="You are Casper")

    assert context.session_id == "s-1"
    assert [message.content for message in context.raw_history] == ["Hello", "Hi there"]
    assert len(context.summaries) == 1
    assert context.summaries[0].authoritative is False
    assert context.pending_state["case_id"] == "CASE-7"
    assert context.pending_state["draft_review"] == "pending"
    assert context.assembled_messages[0]["role"] == "system"
    assert "Derived summary (non-authoritative)" in context.assembled_messages[1]["content"]
    assert "Discussed next steps" in context.rendered_text
    assert "Hello" in context.rendered_text


def test_session_context_history_and_summary_limits_apply_only_to_assembled_messages():
    store = SessionStore()
    store.append_message("s-2", "user", "First")
    store.append_message("s-2", "user", "Second")
    store.add_summary("s-2", "Earlier summary")
    store.add_summary("s-2", "Latest summary")

    builder = SessionContextBuilder(store)
    context = builder.build("s-2", history_limit=1, summary_limit=1)

    assert [message.content for message in context.raw_history] == ["First", "Second"]
    assert [summary.text for summary in context.summaries] == ["Earlier summary", "Latest summary"]
    assembled_contents = [message["content"] for message in context.assembled_messages]
    assert any("Latest summary" in content for content in assembled_contents)
    assert all("First" not in message["content"] for message in context.assembled_messages if message.get("origin") != "summary")
