from __future__ import annotations

from datetime import datetime, timedelta, timezone

from api.session.context_builder import SessionContextBuilder
from api.session.models import RecentReference, SessionKey
from api.session.references import resolve_reference
from api.session.store import SessionStore


UTC = timezone.utc


def test_platform_neutral_session_key_has_stable_serialization():
    key = SessionKey(platform="discord", conversation_id="dm-88", actor_id="user-7")

    restored = SessionKey.deserialize(key.serialize())

    assert restored == key
    assert restored.to_dict()["platform"] == "discord"


def test_cross_platform_identity_sharing_is_explicit_and_local():
    store = SessionStore()
    discord = SessionKey(platform="discord", conversation_id="dm-1", actor_id="dc-user")
    telegram = SessionKey(platform="telegram", conversation_id="chat-9", actor_id="tg-user")
    store.bind_identity("person-42", platform="discord", actor_id="dc-user")
    store.bind_identity("person-42", platform="telegram", actor_id="tg-user")

    store.remember_recent(discord, kind="case", item_id="CASE-1", label="陳案", share_identity=True)

    assert store.list_recent(telegram) == []
    assert [item.item_id for item in store.list_recent(telegram, share_identity=True)] == ["CASE-1"]


def test_context_builder_can_explicitly_read_an_identity_shared_session():
    store = SessionStore()
    discord = SessionKey(platform="discord", conversation_id="dm-1", actor_id="dc-user")
    telegram = SessionKey(platform="telegram", conversation_id="chat-9", actor_id="tg-user")
    store.bind_identity("person-42", platform="discord", actor_id="dc-user")
    store.bind_identity("person-42", platform="telegram", actor_id="tg-user")
    shared_session_id = store.session_id_for(discord, share_identity=True)
    store.append_message(shared_session_id, "user", "跨頻道延續")

    context = SessionContextBuilder(store).build(telegram, share_identity=True)

    assert context.session_id == shared_session_id
    assert context.raw_history[0].content == "跨頻道延續"


def test_recent_references_expire_and_context_only_exposes_live_items():
    store = SessionStore()
    started = datetime(2026, 7, 12, 8, 0, tzinfo=UTC)
    store.remember_recent("s-1", kind="attachment", item_id="A-1", ttl_seconds=10, now=started)

    assert store.list_recent("s-1", now=started + timedelta(seconds=9))[0].item_id == "A-1"
    assert store.list_recent("s-1", now=started + timedelta(seconds=10)) == []
    assert store.purge_expired_references(now=started + timedelta(seconds=10)) == 1
    assert SessionContextBuilder(store).build("s-1").recent_references == {}


def test_store_serialization_round_trips_recent_references_and_identity_binding():
    store = SessionStore()
    key = SessionKey(platform="discord", conversation_id="dm-8", actor_id="dc-user")
    store.bind_identity("person-42", platform="discord", actor_id="dc-user")
    store.append_message("legacy", "user", "保留舊 API")
    store.remember_recent(key, kind="draft", item_id="D-2", payload={"title": "答辯狀"}, share_identity=True)

    restored = SessionStore.from_json(store.to_json())

    assert restored.list_messages("legacy")[0].content == "保留舊 API"
    assert restored.get_identity_binding(platform="discord", actor_id="dc-user").identity_id == "person-42"
    assert restored.list_recent(key, share_identity=True)[0].payload == {"title": "答辯狀"}


def test_recent_reference_supports_every_required_kind():
    store = SessionStore()
    for kind in ("case", "person", "attachment", "schedule", "draft", "plan"):
        store.remember_recent("s", kind=kind, item_id=f"{kind}-1")

    assert set(store.recent_by_kind("s")) == {"case", "person", "attachment", "schedule", "draft", "plan"}


def test_latest_case_reference_resolves_when_recent_order_is_clear():
    now = datetime(2026, 7, 12, 9, 0, tzinfo=UTC)
    references = [
        RecentReference("case", "CASE-NEW", "新案", created_at=now),
        RecentReference("case", "CASE-OLD", "舊案", created_at=now - timedelta(minutes=3)),
    ]

    result = resolve_reference("剛才那件請整理", references, now=now)

    assert result.status == "resolved"
    assert result.selected.reference.item_id == "CASE-NEW"
    assert result.proposed_update == {"case_id": "CASE-NEW"}


def test_same_case_uses_explicit_active_case_when_multiple_candidates_exist():
    now = datetime(2026, 7, 12, 9, 0, tzinfo=UTC)
    references = [
        RecentReference("case", "CASE-1", created_at=now),
        RecentReference("case", "CASE-2", created_at=now - timedelta(seconds=10)),
    ]

    result = resolve_reference("同一案件補上附件", references, active_case_id="CASE-2", now=now)

    assert result.status == "resolved"
    assert result.selected.reference.item_id == "CASE-2"
    assert result.proposed_update == {"case_id": "CASE-2"}


def test_ambiguous_case_and_time_return_candidates_without_writable_proposal():
    now = datetime(2026, 7, 12, 9, 0, tzinfo=UTC)
    case_references = [
        RecentReference("case", "CASE-1", created_at=now),
        RecentReference("case", "CASE-2", created_at=now - timedelta(seconds=10)),
    ]
    before = [item.to_dict() for item in case_references]

    case_result = resolve_reference("同一案件", case_references, now=now)
    time_result = resolve_reference(
        "改到四點",
        [RecentReference("schedule", "EVENT-1", created_at=now)],
        now=now,
    )

    assert case_result.status == "ambiguous"
    assert case_result.selected is None
    assert case_result.proposed_update is None
    assert time_result.status == "ambiguous"
    assert time_result.selected is None
    assert time_result.proposed_update is None
    assert time_result.time_candidates == ("04:00", "16:00")
    assert [item.to_dict() for item in case_references] == before


def test_multiple_schedule_targets_stay_ambiguous_even_when_one_is_older():
    now = datetime(2026, 7, 12, 9, 0, tzinfo=UTC)
    result = resolve_reference(
        "改到下午四點",
        [
            RecentReference("schedule", "EVENT-NEW", created_at=now),
            RecentReference("schedule", "EVENT-OLD", created_at=now - timedelta(hours=1)),
        ],
        now=now,
    )

    assert result.status == "ambiguous"
    assert result.selected is None
    assert result.proposed_update is None


def test_explicit_afternoon_schedule_change_is_a_safe_proposal_not_a_write():
    now = datetime(2026, 7, 12, 9, 0, tzinfo=UTC)
    schedule = RecentReference("schedule", "EVENT-1", "調解", created_at=now)

    result = resolve_reference("改到下午四點", [schedule], now=now)

    assert result.status == "resolved"
    assert result.proposed_update == {"schedule_id": "EVENT-1", "start_time": "16:00"}
