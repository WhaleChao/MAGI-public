"""
Regression tests for POA state machine guard against capability questions.
"""

import json
import pytest


class _MockOrch:
    def __init__(self):
        self.history = []
        self.history_traces = []
        self.skill_pending = {}

    def _sanitize_incoming_message(self, message):
        return message

    def _quick_fixed_reply(self, message, role):
        return ""

    def _append_history(self, user_id, role, content):
        self.history.append((str(user_id or ""), str(role or ""), str(content or "")))

    def _handle_gibberish_report(self, user_id, message, platform):
        return ""

    def _is_verified_admin_sender(self, user_id, platform):
        return True

    def remember_recent_attachment(self, **kwargs):
        return None

    def _maybe_reuse_recent_attachment(self, user_id, platform, message):
        return None

    def _append_route_trace(self, *args, **kwargs):
        self.history_traces.append((args, kwargs))

    def _handle_memory_confirmation_if_any(self, user_id, platform, message):
        return False, ""

    def _handle_skill_interview_if_any(self, user_id, platform, role, message):
        return False, ""

    def _load_skill_interview_pending(self):
        return dict(self.skill_pending)

    def _save_skill_interview_pending(self, data):
        self.skill_pending = data if isinstance(data, dict) else {}

    def _pending_key(self, user_id, platform):
        return f"{str(platform or '').strip()}::{str(user_id or '').strip()}"

    def _looks_like_skill_creation_request(self, message):
        return False

    def _looks_like_capability_question(self, message):
        return "功能" in (message or "") or "你能" in (message or "")

    def _try_conversational_intent(self, message, msg_lower, user_id, role, platform):
        if "你能" in (message or "") or "什麼事" in (message or ""):
            return "你可以讓我幫你做很多事喔，先試試看 /help 吧。"
        return None

    def _list_skills(self):
        return "MAGI 能力清單：案件、檔案、行程、分析。"

    def _handle_laf_submit_confirmation_if_any(self, user_id, platform, role, message):
        return False, ""


@pytest.fixture
def runtime_roots(tmp_path, monkeypatch):
    import api.pipelines.message_pipeline as message_pipeline

    fake_root = str(tmp_path / "MAGI_v2")
    monkeypatch.setattr(message_pipeline, "_MAGI_ROOT", fake_root)
    (tmp_path / "MAGI_v2" / ".agent").mkdir(parents=True, exist_ok=True)
    return message_pipeline


def _set_poa_state(message_pipeline, user_id: str, payload: dict):
    poa_state_file = f"{message_pipeline._MAGI_ROOT}/.agent/poa_chat_state.json"
    with open(poa_state_file, "w", encoding="utf-8") as f:
        json.dump({user_id: payload}, f, ensure_ascii=False)


def _load_poa_state(message_pipeline):
    poa_state_file = f"{message_pipeline._MAGI_ROOT}/.agent/poa_chat_state.json"
    with open(poa_state_file, "r", encoding="utf-8") as f:
        return json.load(f)


def _set_legal_attest_state(message_pipeline, user_id: str, payload: dict):
    state_file = f"{message_pipeline._MAGI_ROOT}/.agent/legal_attest_state.json"
    with open(state_file, "w", encoding="utf-8") as f:
        json.dump({user_id: payload}, f, ensure_ascii=False)


def _load_legal_attest_state(message_pipeline):
    state_file = f"{message_pipeline._MAGI_ROOT}/.agent/legal_attest_state.json"
    with open(state_file, "r", encoding="utf-8") as f:
        return json.load(f)


def test_poa_stale_state_is_bypassed_for_capability_question(runtime_roots):
    orch = _MockOrch()
    _set_poa_state(runtime_roots, "user-1", {"step": "ask_amount", "doc_type": "poa"})

    reply = runtime_roots.process_message_inner(
        orch,
        user_id="user-1",
        message="請問你能做什麼事？",
        platform="web",
        role="user",
    )

    assert "案件與檔案" in reply
    state = _load_poa_state(runtime_roots)
    assert "user-1" not in state


def test_skill_interview_stale_state_is_bypassed_for_capability_question(runtime_roots):
    orch = _MockOrch()
    orch.skill_pending = {
        "web::user-1": {
            "kind": "skill_interview",
            "step": 2,
            "draft": {"purpose": "建立測試技能"},
        }
    }

    reply = runtime_roots.process_message_inner(
        orch,
        user_id="user-1",
        message="請問你能做到什麼事",
        platform="web",
        role="user",
    )

    assert "案件與檔案" in reply
    assert "web::user-1" not in orch.skill_pending


def test_legal_attest_stale_state_is_bypassed_for_capability_question(runtime_roots):
    orch = _MockOrch()
    _set_legal_attest_state(runtime_roots, "user-1", {"step": "ask_content"})

    reply = runtime_roots.process_message_inner(
        orch,
        user_id="user-1",
        message="請問你能做到什麼事",
        platform="web",
        role="user",
    )

    assert "案件與檔案" in reply
    state = _load_legal_attest_state(runtime_roots)
    assert "user-1" not in state


def test_new_task_boundary_is_detected_without_treating_plain_answers_as_tasks(runtime_roots):
    assert runtime_roots._looks_like_new_task_boundary("今天行程")
    assert runtime_roots._looks_like_new_task_boundary("請幫我查案件 2025-0134")
    assert runtime_roots._looks_like_new_task_boundary("查案件 2025-0134")
    assert not runtime_roots._looks_like_new_task_boundary("臺灣新北地方法院")
    assert not runtime_roots._looks_like_new_task_boundary("50000")


def test_capability_detector_accepts_web_meta_question_without_punctuation():
    from api.pipelines.skill_dispatch import looks_like_capability_question
    from api.pipelines.message_router import try_conversational_intent

    assert looks_like_capability_question("請問你能做到什麼事")
    assert try_conversational_intent(
        _MockOrch(),
        "請問你能做到什麼事",
        "請問你能做到什麼事",
        "user-1",
        "user",
        "WEB",
    )
    assert "案件與檔案" in try_conversational_intent(
        _MockOrch(),
        "請問你能做到什麼事",
        "請問你能做到什麼事",
        "user-1",
        "user",
        "WEB",
    )


def test_poa_trigger_keeps_flow_when_not_capability(runtime_roots, monkeypatch):
    orch = _MockOrch()
    _set_poa_state(runtime_roots, "user-1", {"step": "ask_amount", "doc_type": "poa"})

    def fake_handle_chat(user_id, message):
        return f"POA_FLOW:{message}"

    monkeypatch.setattr("api.poa_chat_handler.handle_chat", fake_handle_chat)

    reply = runtime_roots.process_message_inner(
        orch,
        user_id="user-1",
        message="幫我做委任狀",
        platform="web",
        role="user",
    )

    assert reply.startswith("POA_FLOW:")


def test_poa_stale_state_is_cleared_before_new_task_dispatch(runtime_roots, monkeypatch):
    orch = _MockOrch()
    _set_poa_state(runtime_roots, "user-1", {"step": "ask_amount", "doc_type": "receipt"})

    def fake_case_dispatch(message, user_id=None, platform=None):
        return "CASE_RESULT"

    monkeypatch.setattr("api.pipelines.skill_dispatch.dispatch_case_management", fake_case_dispatch)

    reply = runtime_roots.process_message_inner(
        orch,
        user_id="user-1",
        message="查案件 2025-0134",
        platform="web",
        role="user",
    )

    assert reply == "CASE_RESULT"
    state = _load_poa_state(runtime_roots)
    assert "user-1" not in state
