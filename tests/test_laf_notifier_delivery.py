from __future__ import annotations


def test_laf_notifier_does_not_direct_discord_when_red_phone_mirrors(monkeypatch, tmp_path):
    from casper_ecosystem.law_firm_orchestrators.line_notifier import LAFNotifier
    from skills.ops import red_phone

    monkeypatch.setattr(
        red_phone,
        "send_telegram_push_with_status",
        lambda *args, **kwargs: {
            "telegram": True,
            "delivered": True,
            "queued": False,
            "topic_key": kwargs.get("topic_key") or "laf_dispatch",
        },
    )

    notifier = LAFNotifier(env_path=str(tmp_path / ".env"), config_path=str(tmp_path / "config.json"))
    discord_calls = []
    monkeypatch.setattr(notifier, "_push_discord", lambda *args, **kwargs: discord_calls.append((args, kwargs)) or True)

    assert notifier.notify_admin("📥 法扶審核結果通知已觸發官網附件下載", topic_key="laf_dispatch") is True
    assert discord_calls == []


def test_laf_notifier_direct_discord_fallback_when_red_phone_only_queues(monkeypatch, tmp_path):
    from casper_ecosystem.law_firm_orchestrators.line_notifier import LAFNotifier
    from skills.ops import red_phone

    monkeypatch.setattr(
        red_phone,
        "send_telegram_push_with_status",
        lambda *args, **kwargs: {
            "telegram": False,
            "delivered": False,
            "queued": True,
            "topic_key": kwargs.get("topic_key") or "laf_dispatch",
        },
    )

    notifier = LAFNotifier(env_path=str(tmp_path / ".env"), config_path=str(tmp_path / "config.json"))
    discord_calls = []
    monkeypatch.setattr(notifier, "_push_discord", lambda *args, **kwargs: discord_calls.append((args, kwargs)) or True)

    assert notifier.notify_admin("📥 法扶審核結果通知已觸發官網附件下載", topic_key="laf_dispatch") is True
    assert len(discord_calls) == 1
