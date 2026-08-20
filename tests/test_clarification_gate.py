from __future__ import annotations


class _Orchestrator:
    def __init__(self):
        self.traces = []
        self.history = []

    def _append_route_trace(self, *args, **kwargs):
        self.traces.append((args, kwargs))

    def _append_history(self, user_id, role, content):
        self.history.append((role, content))

    def _sanitize_incoming_message(self, message):
        return message

    def has_recent_attachment_followup(self, user_id, platform, message):
        return False


def test_case_count_with_ambiguous_scope_requires_clarification():
    from api.routing.clarification import detect_clarification_need

    decision = detect_clarification_need(
        "請告訴我目前紀錄在案的法扶刑事案件數量，這個事務所的，不是其他資料"
    )

    assert decision.needed is True
    assert decision.key == "case_count_scope"
    assert "全部歷年紀錄" in decision.question
    assert "尚未最終結案" in decision.question


def test_explicit_case_count_scope_does_not_ask_again():
    from api.routing.clarification import detect_clarification_need

    assert not detect_clarification_need("本所全部歷年法扶刑事案件共有幾件").needed
    assert not detect_clarification_need("本所目前未結案的法扶刑事案件有幾件").needed


def test_common_target_and_time_ambiguities_share_the_gate():
    from api.routing.clarification import detect_clarification_need

    assert detect_clarification_need("幫我下載這份判決").key == "file_target"
    assert not detect_clarification_need("幫我下載這份判決", has_attachment=True).needed
    assert detect_clarification_need("告訴我有哪些行程").key == "schedule_range"
    assert not detect_clarification_need("告訴我本週有哪些行程").needed
    assert detect_clarification_need("分析這個文件").key == "file_target"


def test_explicit_targets_and_time_ranges_do_not_over_ask():
    from api.routing.clarification import detect_clarification_need

    assert not detect_clarification_need("幫我下載2026-0062的判決.pdf").needed
    assert not detect_clarification_need("分析2026-0062案件").needed
    assert not detect_clarification_need("告訴我今天有哪些行程").needed


def test_recent_attachment_context_avoids_redundant_clarification(monkeypatch):
    from api.pipelines import message_pipeline

    class _RecentAttachmentOrchestrator(_Orchestrator):
        def has_recent_attachment_followup(self, user_id, platform, message):
            return True

        def _quick_fixed_reply(self, message, role):
            return "RECENT_ATTACHMENT_CONTINUED"

    monkeypatch.setattr(message_pipeline, "_try_semantic_preflight", lambda *_args, **_kwargs: "")
    reply = message_pipeline.process_message_inner(
        _RecentAttachmentOrchestrator(),
        "u1",
        "幫我分析這個文件",
        platform="WEB",
    )

    assert reply == "RECENT_ATTACHMENT_CONTINUED"


def test_clarification_answer_resumes_original_request():
    from api.routing.clarification import (
        detect_clarification_need,
        remember_clarification,
        resolve_pending_clarification,
    )

    orch = _Orchestrator()
    original = "本所法扶刑事案件有幾件"
    remember_clarification(
        orch,
        "u1",
        "WEB",
        original,
        detect_clarification_need(original),
    )

    resolved = resolve_pending_clarification(orch, "u1", "WEB", "兩者都列")

    assert resolved.resolved is True
    assert "全部歷年紀錄" in resolved.message
    assert "區分尚未最終結案與已最終結案" in resolved.message


def test_target_clarification_does_not_treat_acknowledgement_as_a_target():
    from api.routing.clarification import (
        detect_clarification_need,
        remember_clarification,
        resolve_pending_clarification,
    )

    orch = _Orchestrator()
    original = "幫我下載這份判決"
    decision = detect_clarification_need(original)
    remember_clarification(orch, "u1", "WEB", original, decision)

    for answer in ("好", "可以", "嗯", "照辦"):
        resolved = resolve_pending_clarification(orch, "u1", "WEB", answer)
        assert resolved.pending is True
        assert resolved.message == decision.question


def test_ambiguous_mutations_require_a_target_before_routing():
    from api.routing.clarification import detect_clarification_need

    for message, expected_key in (
        ("刪除這個案件", "mutation_target"),
        ("幫我把這份資料上傳", "mutation_target"),
        ("把那個檔案搬到歸檔", "file_target"),
        ("修改這個案件", "mutation_target"),
    ):
        decision = detect_clarification_need(message)
        assert decision.needed is True
        assert decision.key == expected_key


def test_new_task_clears_unrelated_pending_clarification():
    from api.routing.clarification import (
        detect_clarification_need,
        remember_clarification,
        resolve_pending_clarification,
    )

    orch = _Orchestrator()
    original = "幫我下載這份判決"
    remember_clarification(
        orch,
        "u1",
        "WEB",
        original,
        detect_clarification_need(original),
    )

    resolved = resolve_pending_clarification(orch, "u1", "WEB", "查今天行程")

    assert resolved.resolved is False
    assert resolved.pending is False
    assert resolved.message == "查今天行程"


def test_pipeline_clarification_preempts_database_and_model(monkeypatch):
    from api.pipelines import message_pipeline

    monkeypatch.setattr(
        message_pipeline,
        "_maybe_direct_case_statistics",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("database route must not run")),
    )
    monkeypatch.setattr(
        message_pipeline,
        "_try_semantic_preflight",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("model route must not run")),
    )
    orch = _Orchestrator()

    reply = message_pipeline.process_message_inner(
        orch,
        "u1",
        "請告訴我目前紀錄在案的法扶刑事案件數量",
        platform="WEB",
    )

    assert "全部歷年紀錄" in reply
    assert "尚未最終結案" in reply
    assert any(args[2] == "clarification_gate" for args, _kwargs in orch.traces)


def test_pipeline_followup_resumes_original_database_query(monkeypatch):
    from api.pipelines import message_pipeline
    from skills.engine import tool_registry

    seen = {}

    def fake_query(query="", **_kwargs):
        seen["query"] = query
        return "本所法扶刑事案件共 63 件，未結案 27 件、已結案 36 件。"

    monkeypatch.setattr(tool_registry, "_query_cases", fake_query)
    orch = _Orchestrator()
    first = message_pipeline.process_message_inner(
        orch,
        "u1",
        "本所法扶刑事案件有幾件",
        platform="WEB",
    )
    second = message_pipeline.process_message_inner(
        orch,
        "u1",
        "兩者都列",
        platform="WEB",
    )

    assert "全部歷年紀錄" in first
    assert "共 63 件" in second
    assert "區分尚未最終結案與已最終結案" in seen["query"]


def test_recent_case_pronoun_resolves_only_from_user_authored_reference() -> None:
    from api.routing.clarification import resolve_recent_case_reference
    from api.session.store import SessionStore

    orch = _Orchestrator()
    orch._session_store = SessionStore()
    orch._session_store.remember_recent("u1", kind="case", item_id="2026-0062", label="2026-0062")

    resolved = resolve_recent_case_reference(orch, "u1", "WEB", "查剛才那件的進度")

    assert resolved.resolved is True
    assert "2026-0062" in resolved.message


def test_multiple_recent_cases_ask_and_numeric_choice_restores_identifier() -> None:
    from api.routing.clarification import resolve_pending_clarification, resolve_recent_case_reference
    from api.session.store import SessionStore

    orch = _Orchestrator()
    orch._session_store = SessionStore()
    orch._session_store.remember_recent("u1", kind="case", item_id="2026-0062", label="2026-0062")
    orch._session_store.remember_recent("u1", kind="case", item_id="2026-0077", label="2026-0077")

    pending = resolve_recent_case_reference(orch, "u1", "WEB", "查剛才那件的進度")
    chosen = resolve_pending_clarification(orch, "u1", "WEB", "1")

    assert pending.pending is True
    assert "2026-0077" in pending.message and "2026-0062" in pending.message
    assert chosen.resolved is True
    assert "2026-0077" in chosen.message


def test_unresolved_recent_case_accepts_party_name_as_safe_answer() -> None:
    from api.routing.clarification import resolve_pending_clarification, resolve_recent_case_reference
    from api.session.store import SessionStore

    orch = _Orchestrator()
    orch._session_store = SessionStore()
    pending = resolve_recent_case_reference(orch, "u1", "WEB", "查剛才那件的進度")
    chosen = resolve_pending_clarification(orch, "u1", "WEB", "吳倆茹")

    assert pending.pending is True
    assert chosen.resolved is True
    assert "吳倆茹" in chosen.message
