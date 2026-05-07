from __future__ import annotations

import asyncio
from types import SimpleNamespace

from api import discord_channel_router as router


class _FakeGuild:
    def __init__(self, text_channels=None, categories=None):
        self.text_channels = list(text_channels or [])
        self.categories = list(categories or [])
        self.created_channels = []

    async def create_category(self, name: str):
        cat = SimpleNamespace(name=name)
        self.categories.append(cat)
        return cat

    async def create_text_channel(self, *, name: str, category, topic: str):
        ch = SimpleNamespace(name=name, id=9001, category=category, topic=topic)
        self.text_channels.append(ch)
        self.created_channels.append(ch)
        return ch


def test_progress_channel_definition_uses_new_name_and_keeps_legacy_alias():
    progress_def = next(ch for ch in router.DEFAULT_CHANNELS if ch["key"] == "laf_progress")
    assert progress_def["name"] == "法扶-進度回報"
    assert "🔄 進度回報" in progress_def.get("aliases", [])


def test_auto_setup_reuses_legacy_progress_channel_name(monkeypatch):
    legacy_channel = SimpleNamespace(name="🔄 進度回報", id=123456)
    guild = _FakeGuild(text_channels=[legacy_channel])

    saved = {}

    def _fake_save_channel_map(channel_map):
        saved.update(channel_map)
        return "/tmp/discord_channel_map.json"

    monkeypatch.setattr(router, "save_channel_map", _fake_save_channel_map)

    result = asyncio.run(router.auto_setup_channels(guild))

    assert result["laf_progress"] == "123456"
    assert not any(ch.name == "法扶-進度回報" for ch in guild.created_channels)
