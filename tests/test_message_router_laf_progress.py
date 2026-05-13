from __future__ import annotations

from api.pipelines import message_router


class _DummyOrch:
    _TOPIC_HANDLERS = {}

    def _handle_command(self, user_id, message, role=None, platform=None):
        return {
            "user_id": user_id,
            "message": message,
            "role": role,
            "platform": platform,
        }


def test_laf_progress_channel_redirects_non_progress_laf_commands():
    orch = _DummyOrch()

    out = message_router.topic_fast_path(
        orch,
        "laf_progress",
        "user-1",
        "張偉銘 結案回報",
        "user",
        "discord",
    )

    assert "法扶-結案" in out
    assert "法扶-進度回報" in out

