from __future__ import annotations

import json

from api.deep_task_control import enqueue_local_deep_admission
from api.durable_notifications import claim_for_user
from scripts.ops.local_deep_queue_worker import drain_once


class _Controller:
    def run(self, work):
        return work()


def test_cross_process_result_is_recipient_bound_case_normalized_and_once(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("MAGI_RUNTIME_DIR", str(tmp_path))
    enqueue_local_deep_admission(
        task_type="legal_analysis",
        prompt="private",
        secure_task_ref="vault:1",
        user_id="user-1",
        platform="WEB",
    )
    result = drain_once(
        retrieve=lambda _: "private",
        execute=lambda *_: "可交付的分析",
        controller_factory=_Controller,
    )
    assert result["state"] == "succeeded"
    assert claim_for_user(user_id="user-2", platform="web") == []
    first = claim_for_user(user_id="user-1", platform="web")
    assert first[0]["text"] == "本機深度工作已完成：可交付的分析"
    assert claim_for_user(user_id="user-1", platform="WEB") == []


def test_corrupt_existing_outbox_fails_closed_without_erasing_it(tmp_path, monkeypatch):
    monkeypatch.setenv("MAGI_RUNTIME_DIR", str(tmp_path))
    outbox = tmp_path / "durable_user_outbox.json"
    outbox.write_text("{broken", encoding="utf-8")

    try:
        claim_for_user(user_id="user-1", platform="web")
    except json.JSONDecodeError:
        pass
    else:
        raise AssertionError("corrupt durable outbox must fail closed")

    assert outbox.read_text(encoding="utf-8") == "{broken"
