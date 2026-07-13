from __future__ import annotations

import json

import pytest

from api.agentic import build_public_agent_status, write_public_agent_status
from api.agentic.telemetry import PUBLIC_AGENT_STATUS_FILENAME


def _walk_keys(value):
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from _walk_keys(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_keys(item)


def test_default_status_is_inert_shadow_with_offline_health():
    payload = build_public_agent_status()

    assert payload == {
        "schema_version": 1,
        "status": "shadow",
        "health": {"status": "offline"},
    }


@pytest.mark.parametrize("status", ["shadow", "ready", "running", "blocked", "degraded", "completed"])
def test_supported_agent_statuses_are_preserved(status):
    payload = build_public_agent_status({"state": status})

    assert payload["status"] == status
    if status == "degraded":
        assert payload["degraded"] == {"active": True}


def test_public_schema_projects_only_bounded_operational_fields():
    payload = build_public_agent_status(
        {
            "status": "running",
            "intent": {"category": "legal"},
            "confidence": 0.82,
            "plan": {
                "status": "running",
                "current_action": "execute",
                "step_counts": {"total": 5, "running": 1, "succeeded": 2},
            },
            "tool": {"category": "search"},
            "side_effect": "reversible_write",
            "confirmation": {"required": True, "confirmed": False},
            "verification": {"ok": True},
            "health": {"ok": True},
            "degraded": {"active": False, "mode": "hold"},
            "last_success": True,
            "last_error_category": "validation_failed",
        }
    )

    assert payload == {
        "schema_version": 1,
        "status": "running",
        "intent_category": "legal",
        "confidence": 0.82,
        "route_confidence": 0.82,
        "plan_status": "running",
        "step_counts": {"total": 5, "running": 1, "succeeded": 2},
        "current_action": "execute",
        "plan_steps": [{"id": "execute", "state": "running"}],
        "tool_category": "search",
        "side_effect": "reversible_write",
        "waiting_confirmation": True,
        "verification": {"status": "passed"},
        "health": {"status": "healthy"},
        "degraded": {"active": False},
        "last_success": True,
        "error_category": "validation_failed",
    }


def test_private_fields_are_recursively_excluded_before_atomic_publish(tmp_path):
    destination = tmp_path / "static" / PUBLIC_AGENT_STATUS_FILENAME
    payload = write_public_agent_status(
        {
            "status": "running",
            "intent_category": "research",
            "current_action": "retrieve",
            "prompt": "LEAK_PROMPT",
            "message": "LEAK_MESSAGE",
            "metadata": {
                "content": "LEAK_CONTENT",
                "nested": {
                    "thought": "LEAK_THOUGHT",
                    "reasoning": "LEAK_REASONING",
                    "user_id": "LEAK_USER",
                    "case": {"client": "LEAK_CLIENT", "path": "/LEAK_PATH", "token": "LEAK_TOKEN"},
                },
            },
            "verification": {"status": "passed", "trace": "LEAK_TRACE", "details": {"message": "LEAK_DETAIL"}},
            "health": {"status": "healthy", "path": "/LEAK_HEALTH_PATH"},
            "degraded": {"active": True, "nested": {"token": "LEAK_DEGRADED_TOKEN"}},
        },
        path=destination,
    )

    written = json.loads(destination.read_text(encoding="utf-8"))
    serialized = json.dumps(written, ensure_ascii=False)
    assert written == payload
    assert payload["verification"] == {"status": "passed"}
    assert payload["health"] == {"status": "healthy"}
    assert payload["degraded"] == {"active": True}
    assert not list(destination.parent.glob(f".{destination.name}.*.tmp"))
    assert all(
        forbidden not in serialized
        for forbidden in (
            "LEAK_PROMPT",
            "LEAK_MESSAGE",
            "LEAK_CONTENT",
            "LEAK_THOUGHT",
            "LEAK_REASONING",
            "LEAK_USER",
            "LEAK_CLIENT",
            "LEAK_PATH",
            "LEAK_TOKEN",
            "LEAK_TRACE",
            "LEAK_DETAIL",
            "LEAK_HEALTH_PATH",
            "LEAK_DEGRADED_TOKEN",
        )
    )
    assert not any(
        token in key.lower()
        for key in _walk_keys(written)
        for token in ("prompt", "message", "content", "thought", "reasoning", "user", "case", "client", "path", "token")
    )
