from __future__ import annotations


def test_telegram_records_assistant_reply_with_orchestrator_history_signature(monkeypatch) -> None:
    from api.webhooks import telegram

    recorded: list[tuple[str, str]] = []

    class Orchestrator:
        def process_message(self, **_kwargs):
            return "assistant reply"

        def record_assistant_reply(self, user_id, content):
            recorded.append((user_id, content))

    monkeypatch.setattr(telegram, "_get_orchestrator", lambda: Orchestrator())
    monkeypatch.setattr(telegram, "_telegram_send_orchestrator_response", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(telegram, "_append_channel_delivery_audit", lambda *_args, **_kwargs: None)

    telegram._telegram_process_async("chat-1", "telegram-user-1", "user", "hello")

    assert recorded == [("telegram-user-1", "assistant reply")]
