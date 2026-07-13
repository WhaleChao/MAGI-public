from __future__ import annotations

from api.pipelines import message_pipeline
from api.pipelines.command_dispatch import handle_command
from api.pipelines.message_pipeline import (
    _try_agentic_route,
    _try_semantic_preflight,
    _try_tool_first_policy_route,
)
from skills.bridge.inference_gateway import InferenceGateway, classify_intent


class _BoundaryOrchestrator:
    def __init__(self):
        self.traces = []
        self.conversation_messages = []

    def _append_route_trace(self, *args, **kwargs):
        self.traces.append((args, kwargs))

    def _load_skill_interview_pending(self):
        return {}

    def _pending_key(self, user_id, platform):
        return f"{platform}:{user_id}"

    def get_active_heavy_tasks(self):
        return []

    def _brain_runtime_banner(self):
        return ""

    def _try_conversational_intent(self, message, msg_lower, user_id, role, platform):
        self.conversation_messages.append(message)
        return "CHAT_OK"

    def _handle_chat_async(self, user_id, message, platform_hint="LINE"):
        self.conversation_messages.append(message)
        return "CHAT_ASYNC_OK"


def test_explicit_inline_summary_bypasses_agentic_route(monkeypatch):
    class _PipelineOrchestrator:
        def __init__(self):
            self.history = []

        def _sanitize_incoming_message(self, message):
            return message

        def _append_history(self, user_id, role, content):
            self.history.append((role, content))

        def _quick_fixed_reply(self, message, role):
            return None

        def _handle_gibberish_report(self, user_id, message, platform):
            return None

        def _is_verified_admin_sender(self, user_id, platform):
            return False

        def _maybe_reuse_recent_attachment(self, user_id, platform, message):
            return None

        def _append_route_trace(self, *args, **kwargs):
            pass

        def _handle_memory_confirmation_if_any(self, user_id, platform, message):
            return False, None

        def _load_skill_interview_pending(self):
            return {}

        def _pending_key(self, user_id, platform):
            return f"{platform}:{user_id}"

        def _handle_skill_interview_if_any(self, user_id, platform, role, message):
            return False, None

        def _looks_like_skill_creation_request(self, message):
            return False

        def _looks_like_capability_question(self, message):
            return False

        def _run_inline_summary_command(self, message):
            return "📝 標準摘要結果（extractive_inline）:\n- 第一點很重要"

    monkeypatch.setattr(message_pipeline, "_maybe_direct_case_lookup", lambda *args, **kwargs: None)
    monkeypatch.setattr(message_pipeline, "_try_semantic_preflight", lambda *args, **kwargs: None)
    monkeypatch.setattr(message_pipeline, "_handle_docx_chat_edit_if_any", lambda *args, **kwargs: (False, None))

    def fail_agentic_route(*args, **kwargs):
        raise AssertionError("explicit inline summary should not enter agentic route")

    monkeypatch.setattr(message_pipeline, "_try_agentic_route", fail_agentic_route)

    reply = message_pipeline.process_message_inner(
        _PipelineOrchestrator(),
        "u1",
        "請直接摘要以下待摘要內容：第一點很重要。第二點也重要。",
        platform="Telegram",
    )

    assert "第一點很重要" in reply


def test_semantic_preflight_strips_heavy_before_casual_chat(tmp_path, monkeypatch):
    monkeypatch.setattr(message_pipeline, "_MAGI_ROOT", str(tmp_path))
    orch = _BoundaryOrchestrator()

    reply = _try_semantic_preflight(orch, "@heavy 我只是想跟你聊聊天", user_id="boundary_user")

    assert reply == "CHAT_OK"
    assert orch.conversation_messages == ["我只是想跟你聊聊天"]
    assert any(args[2] == "semantic_preflight" and args[3] == "casual_chat" for args, _ in orch.traces)


def test_agentic_route_strips_heavy_and_preserves_flag(tmp_path, monkeypatch):
    from skills.bridge import ensemble_inference
    from skills.bridge.ensemble_inference import ConsensusResult

    monkeypatch.setattr(message_pipeline, "_MAGI_ROOT", str(tmp_path))
    monkeypatch.setattr(ensemble_inference, "_ENSEMBLE_TOOLS_ENABLED", True)
    calls = []

    def fake_ensemble_chat_with_tools(**kwargs):
        calls.append(kwargs)
        return ConsensusResult(
            unanimous=True,
            result="AGENT_OK",
            individual_results={"tools_used": ["search_statutes"], "heavy": kwargs.get("heavy")},
            task_type="agentic",
        )

    monkeypatch.setattr(ensemble_inference, "ensemble_chat_with_tools", fake_ensemble_chat_with_tools)
    monkeypatch.setattr(ensemble_inference, "format_magi_response", lambda result: result.result)

    reply = _try_agentic_route(
        _BoundaryOrchestrator(),
        "@重型：請幫我比較民法184條與相關判決見解，整理成三點",
        user_id="boundary_user",
    )

    assert reply == "AGENT_OK"
    assert calls[0]["prompt"] == "請幫我比較民法184條與相關判決見解，整理成三點"
    assert calls[0]["heavy"] is True


def test_tool_first_route_strips_heavy_before_tool_query(monkeypatch):
    from skills.engine.tool_registry import TOOLS

    seen = {}

    def fake_query_cases(query="", **_kwargs):
        seen["query"] = query
        return f"CASE_OK:{query}"

    monkeypatch.setitem(
        TOOLS,
        "query_cases",
        {"fn": fake_query_cases},
    )

    reply = _try_tool_first_policy_route(
        _BoundaryOrchestrator(),
        "@heavy 查一下112年度訴字第1234號案件的進度",
        intent="QUERY",
        user_id="boundary_user",
    )

    assert reply.startswith("CASE_OK:")
    assert seen["query"] == "查一下112年度訴字第1234號案件的進度"


def test_command_dispatch_strips_heavy_before_explicit_draw():
    class _CommandOrchestrator:
        _cmd_registry = None
        _fuzzy_recursion_guard = True

        def __init__(self):
            self.prompts = []

        def _generate_image(self, prompt):
            self.prompts.append(prompt)
            return "DRAW_OK"

        def _should_attempt_auto_acquire(self, message, msg_lower):
            return False

        def _looks_like_capability_question(self, message):
            return False

        def _handle_chat_async(self, user_id, message, platform_hint="LINE"):
            raise AssertionError("explicit draw command should not fall through to chat")

    orch = _CommandOrchestrator()
    reply = handle_command(orch, "u1", "@heavy /draw a cat", role="user", platform="LINE")

    assert reply == "DRAW_OK"
    assert orch.prompts == ["a cat"]


def test_inference_gateway_classification_ignores_heavy_prefix():
    assert classify_intent("@heavy 翻譯這段文字") == "translate"
    assert classify_intent("@重型：請摘要這份判決") == "summary"


def test_inference_gateway_result_keeps_task_type_and_heavy_flag(monkeypatch):
    gw = InferenceGateway()

    monkeypatch.setenv("NVIDIA_NIM_ENABLE", "0")
    monkeypatch.setattr(
        gw,
        "_omlx_chat",
        lambda prompt, timeout, model="", task_type="general": {
            "success": True,
            "route": "omlx",
            "degraded": False,
            "response": f"OMLX:{prompt}",
            "task_type": task_type,
        },
    )

    result = gw.chat("@heavy 請摘要這份判決", task_type="summary", timeout=30)

    assert result["route"] == "omlx"
    assert result["task_type"] == "summary"
    assert result["heavy_opt_in"] is True
    assert result["response"] == "OMLX:請摘要這份判決"
