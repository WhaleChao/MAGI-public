"""Integration coverage for the shared LAF submit confirmation entrypoint."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class _NoopThread:
    """Test double that blocks background submit side effects."""

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs

    def start(self):
        return None


class _ImmediateThread:
    """Test double that runs the background target synchronously."""

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs

    def start(self):
        target = self.kwargs.get("target") if self.kwargs else None
        args = self.kwargs.get("args", ()) if self.kwargs else ()
        if target is None and self.args:
            target = self.args[0]
        if target:
            target(*args)


class _MockOrch:
    """Minimal orchestrator stub for confirmation routing tests."""

    def __init__(self, tmp_path):
        self._laf_submit_pending_file = str(tmp_path / "laf_submit_pending.json")
        self._laf_progress_submit_pending_file = str(tmp_path / "laf_progress_submit_pending.json")
        self.notification_callback = None
        self.history = []

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
        return None

    def _handle_memory_confirmation_if_any(self, user_id, platform, message):
        return False, ""

    def _handle_skill_interview_if_any(self, user_id, platform, role, message):
        return False, ""

    def _looks_like_skill_creation_request(self, message):
        return False

    def _looks_like_capability_question(self, message):
        return False

    def _handle_laf_submit_confirmation_if_any(self, user_id, platform, role, message):
        from api.domains import laf_flow

        return laf_flow.handle_laf_submit_confirmation_if_any(self, user_id, platform, role, message)


def test_shared_submit_entrypoint_routes_progress_tokens(monkeypatch, tmp_path):
    from api.domains import laf_flow

    monkeypatch.setattr(laf_flow.threading, "Thread", _NoopThread)
    orch = _MockOrch(tmp_path)
    token = laf_flow.register_laf_progress_submit_pending(
        orch,
        platform="discord",
        requester_user_id="lawyer1",
        payload={"laf_case_no": "1140806-J-001", "client_name": "測試人"},
        result_data={},
    )

    handled, reply = laf_flow.handle_laf_submit_confirmation_if_any(
        orch,
        user_id="lawyer1",
        platform="discord",
        role="user",
        message=f"正確送出 {token}",
    )

    assert handled is True
    assert "進度回報" in reply
    assert token in reply
    pending = laf_flow._load_progress_pending(laf_flow._progress_pending_file(orch))
    assert pending[token]["status"] == "submitting"


def test_shared_submit_entrypoint_preserves_go_live_tokens(monkeypatch, tmp_path):
    from api.domains import laf_flow

    monkeypatch.setattr(laf_flow.threading, "Thread", _NoopThread)
    orch = _MockOrch(tmp_path)
    entry = laf_flow.register_laf_go_live_submit_pending(
        orch,
        platform="discord",
        requester_user_id="lawyer1",
        payload={"laf_case_no": "1140806-A-001", "client_name": "王小明"},
        result_data={},
    )
    token = entry["token"]

    handled, reply = laf_flow.handle_laf_submit_confirmation_if_any(
        orch,
        user_id="lawyer1",
        platform="discord",
        role="user",
        message=f"開辦正確送出 {token}",
    )

    assert handled is True
    assert "開辦回報" in reply
    assert token in reply
    pending = laf_flow.load_laf_submit_pending(orch)
    assert pending[token]["status"] == "submitting"


def test_go_live_resolver_does_not_fallback_when_message_has_other_token(tmp_path):
    from api.domains import laf_flow

    orch = _MockOrch(tmp_path)
    progress_token = laf_flow.register_laf_progress_submit_pending(
        orch,
        platform="discord",
        requester_user_id="lawyer1",
        payload={"laf_case_no": "1140806-J-003", "client_name": "測試人"},
        result_data={},
    )
    entry = laf_flow.register_laf_go_live_submit_pending(
        orch,
        platform="discord",
        requester_user_id="lawyer1",
        payload={"laf_case_no": "1140806-A-003", "client_name": "王小明"},
        result_data={},
    )

    token, resolved = laf_flow.resolve_laf_go_live_pending_token(
        orch,
        "discord",
        f"取消 {progress_token}",
    )

    assert token == ""
    assert resolved == {}
    pending = laf_flow.load_laf_submit_pending(orch)
    assert pending[entry["token"]]["status"] == "pending"


def test_shared_submit_entrypoint_does_not_cancel_go_live_for_closed_progress_token(monkeypatch, tmp_path):
    from api.domains import laf_flow

    monkeypatch.setattr(laf_flow.threading, "Thread", _NoopThread)
    orch = _MockOrch(tmp_path)
    progress_token = laf_flow.register_laf_progress_submit_pending(
        orch,
        platform="discord",
        requester_user_id="lawyer1",
        payload={"laf_case_no": "1140806-J-004", "client_name": "測試人"},
        result_data={},
    )
    progress_pending = laf_flow._load_progress_pending(laf_flow._progress_pending_file(orch))
    progress_pending[progress_token]["status"] = "cancelled"
    laf_flow._save_progress_pending(laf_flow._progress_pending_file(orch), progress_pending)
    entry = laf_flow.register_laf_go_live_submit_pending(
        orch,
        platform="discord",
        requester_user_id="lawyer1",
        payload={"laf_case_no": "1140806-A-004", "client_name": "王小明"},
        result_data={},
    )

    handled, reply = laf_flow.handle_laf_submit_confirmation_if_any(
        orch,
        user_id="lawyer1",
        platform="discord",
        role="user",
        message=f"取消 {progress_token}",
    )

    assert handled is False
    assert reply == ""
    pending = laf_flow.load_laf_submit_pending(orch)
    assert pending[entry["token"]]["status"] == "pending"


def test_message_pipeline_intercepts_progress_submit_confirmation(monkeypatch, tmp_path):
    from api.domains import laf_flow
    from api.pipelines.message_pipeline import process_message_inner

    monkeypatch.setattr(laf_flow.threading, "Thread", _NoopThread)
    orch = _MockOrch(tmp_path)
    token = laf_flow.register_laf_progress_submit_pending(
        orch,
        platform="discord",
        requester_user_id="lawyer1",
        payload={"laf_case_no": "1140806-J-002", "client_name": "測試人"},
        result_data={},
    )

    reply = process_message_inner(
        orch,
        user_id="lawyer1",
        message=f"正確送出 {token}",
        platform="discord",
        role="user",
    )

    assert "進度回報" in reply
    assert token in reply


def test_message_pipeline_intercepts_file_review_confirmation(monkeypatch, tmp_path):
    import api.pipelines.message_pipeline as message_pipeline
    from api.pipelines.message_pipeline import process_message_inner

    monkeypatch.setattr(
        message_pipeline,
        "_find_file_review_confirm_record",
        lambda message: ("150A81", {"status": "pending"}, "pending"),
    )
    monkeypatch.setattr(message_pipeline.threading, "Thread", _NoopThread)
    orch = _MockOrch(tmp_path)

    reply = process_message_inner(
        orch,
        user_id="lawyer1",
        message="確認碼150A81",
        platform="discord",
        role="user",
    )

    assert "閱卷確認碼 150A81" in reply


def test_message_pipeline_explains_expired_file_review_confirmation(monkeypatch, tmp_path):
    import api.pipelines.message_pipeline as message_pipeline
    from api.pipelines.message_pipeline import process_message_inner

    monkeypatch.setattr(
        message_pipeline,
        "_find_file_review_confirm_record",
        lambda message: ("150A81", {"status": "pending"}, "expired"),
    )
    orch = _MockOrch(tmp_path)

    reply = process_message_inner(
        orch,
        user_id="lawyer1",
        message="確認碼150A81",
        platform="discord",
        role="user",
    )

    assert "閱卷確認碼 150A81 已逾期" in reply


def test_message_pipeline_explains_closed_file_review_confirmation(monkeypatch, tmp_path):
    import api.pipelines.message_pipeline as message_pipeline
    from api.pipelines.message_pipeline import process_message_inner

    monkeypatch.setattr(
        message_pipeline,
        "_find_file_review_confirm_record",
        lambda message: ("55C5E0", {"status": "invalidated"}, "closed"),
    )
    orch = _MockOrch(tmp_path)

    reply = process_message_inner(
        orch,
        user_id="lawyer1",
        message="55C5E0",
        platform="discord",
        role="user",
    )

    assert "狀態為 invalidated" in reply


def test_file_review_confirmation_prefers_live_token_over_stale(monkeypatch, tmp_path):
    import json
    import time
    import api.pipelines.message_pipeline as message_pipeline

    root = tmp_path / "root"
    pending_dir = root / "skills" / "file-review-orchestrator"
    pending_dir.mkdir(parents=True)
    (pending_dir / ".review_submit_pending.json").write_text(
        json.dumps(
            {
                "150A81": {"status": "invalidated"},
                "55C5E0": {"status": "pending", "expires_at": time.time() + 600},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(message_pipeline, "_MAGI_ROOT", str(root))

    token, _entry, state = message_pipeline._find_file_review_confirm_record("確認碼150A81，改用 55C5E0")

    assert token == "55C5E0"
    assert state == "pending"


def test_file_review_confirmation_does_not_report_unverified_submit_as_done(monkeypatch, tmp_path):
    import json
    import api.pipelines.message_pipeline as message_pipeline
    from api.pipelines.message_pipeline import process_message_inner

    monkeypatch.setattr(
        message_pipeline,
        "_find_file_review_confirm_record",
        lambda message: ("369F53", {"status": "pending"}, "pending"),
    )
    monkeypatch.setattr(message_pipeline.threading, "Thread", _ImmediateThread)

    class _Proc:
        returncode = 0
        stdout = json.dumps({
            "success": False,
            "result": "SubmitUnverified",
            "case": "HLD 115年婚字第19號",
            "message": "未偵測到法院端已受理",
            "error": "未偵測到法院端已受理",
        }, ensure_ascii=False)
        stderr = ""

    monkeypatch.setattr(message_pipeline.subprocess, "run", lambda *args, **kwargs: _Proc())
    orch = _MockOrch(tmp_path)
    notifications = []
    orch.notification_callback = lambda uid, text, platform: notifications.append(text)

    reply = process_message_inner(
        orch,
        user_id="lawyer1",
        message="確認碼：369F53",
        platform="discord",
        role="user",
    )

    assert "閱卷確認碼 369F53" in reply
    assert notifications
    assert "閱卷確認送出失敗" in notifications[0]
    assert "送出完成" not in notifications[0]
