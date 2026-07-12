"""T3 tests: laf_progress_submit_pending register / resolve / confirm."""
import sys, os, time, json, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import pytest


class _MockOrch:
    """Minimal orchestrator stub for testing laf_flow progress helpers."""
    def __init__(self, tmp_path):
        self._laf_progress_submit_pending_file = str(tmp_path / "progress_pending.json")
        self.notification_callback = None


class _ImmediateThread:
    def __init__(self, target, args=(), kwargs=None, daemon=None):
        self.target = target
        self.args = args
        self.kwargs = kwargs or {}

    def start(self):
        self.target(*self.args, **self.kwargs)


# ── register_laf_progress_submit_pending ─────────────────────────────────

def test_register_returns_6hex_token(tmp_path):
    from api.domains.laf_flow import register_laf_progress_submit_pending
    orch = _MockOrch(tmp_path)
    token = register_laf_progress_submit_pending(
        orch,
        platform="discord",
        requester_user_id="user1",
        payload={"laf_case_no": "1140806-J-001", "client_name": "測試人"},
        result_data={"screenshot_path": "/tmp/test.png"},
    )
    assert isinstance(token, str)
    assert len(token) == 6
    assert token == token.upper()


def test_register_writes_to_file(tmp_path):
    from api.domains.laf_flow import register_laf_progress_submit_pending
    orch = _MockOrch(tmp_path)
    token = register_laf_progress_submit_pending(
        orch,
        platform="discord",
        requester_user_id="user1",
        payload={"laf_case_no": "1140806-J-001"},
        result_data={},
    )
    with open(orch._laf_progress_submit_pending_file, "r", encoding="utf-8") as f:
        data = json.load(f)
    assert token in data
    assert data[token]["kind"] == "laf_progress_submit"
    assert data[token]["status"] == "pending"


def test_register_has_expiry(tmp_path):
    from api.domains.laf_flow import register_laf_progress_submit_pending
    orch = _MockOrch(tmp_path)
    before = time.time()
    token = register_laf_progress_submit_pending(
        orch, platform="discord", requester_user_id="", payload={}, result_data={}
    )
    with open(orch._laf_progress_submit_pending_file, "r", encoding="utf-8") as f:
        data = json.load(f)
    assert data[token]["expires_at"] > before


# ── resolve_laf_progress_pending_token ───────────────────────────────────

def test_resolve_returns_entry_for_valid_token(tmp_path):
    from api.domains.laf_flow import register_laf_progress_submit_pending, resolve_laf_progress_pending_token
    orch = _MockOrch(tmp_path)
    token = register_laf_progress_submit_pending(
        orch, platform="discord", requester_user_id="", payload={"laf_case_no": "1140806-J-001"}, result_data={}
    )
    entry = resolve_laf_progress_pending_token(orch, token)
    assert isinstance(entry, dict)
    assert entry["token"] == token
    assert entry["status"] == "pending"


def test_resolve_returns_none_for_unknown_token(tmp_path):
    from api.domains.laf_flow import resolve_laf_progress_pending_token
    orch = _MockOrch(tmp_path)
    assert resolve_laf_progress_pending_token(orch, "AABBCC") is None


def test_resolve_returns_none_for_expired_token(tmp_path, monkeypatch):
    from api.domains import laf_flow
    from api.domains.laf_flow import register_laf_progress_submit_pending, resolve_laf_progress_pending_token
    orch = _MockOrch(tmp_path)
    monkeypatch.setenv("MAGI_LAF_PROGRESS_CONFIRM_TTL_SEC", "1")
    token = register_laf_progress_submit_pending(
        orch, platform="discord", requester_user_id="", payload={}, result_data={}
    )
    # Manually set expires_at to past
    with open(orch._laf_progress_submit_pending_file, "r", encoding="utf-8") as f:
        data = json.load(f)
    data[token]["expires_at"] = time.time() - 10
    with open(orch._laf_progress_submit_pending_file, "w", encoding="utf-8") as f:
        json.dump(data, f)
    assert resolve_laf_progress_pending_token(orch, token) is None
    with open(orch._laf_progress_submit_pending_file, "r", encoding="utf-8") as f:
        data = json.load(f)
    assert data[token]["status"] == "expired"
    assert data[token]["last_error"] == "confirmation_token_expired"


def test_stale_submitting_progress_is_recovered_as_failed(tmp_path, monkeypatch):
    from api.domains.laf_flow import (
        register_laf_progress_submit_pending,
        resolve_laf_progress_pending_token,
        _load_progress_pending,
        _progress_pending_file,
        _save_progress_pending,
    )

    monkeypatch.setenv("MAGI_LAF_PROGRESS_SUBMIT_STALE_SEC", "60")
    orch = _MockOrch(tmp_path)
    token = register_laf_progress_submit_pending(
        orch, platform="discord", requester_user_id="", payload={}, result_data={}
    )
    pf = _progress_pending_file(orch)
    pending = _load_progress_pending(pf)
    pending[token]["status"] = "submitting"
    pending[token]["heartbeat_at"] = time.time() - 7200
    assert _save_progress_pending(pf, pending) is True

    assert resolve_laf_progress_pending_token(orch, token) is None
    recovered = _load_progress_pending(pf)
    assert recovered[token]["status"] == "failed"
    assert recovered[token]["recoverable"] is True
    assert recovered[token]["last_error"] == "stale_submitting_recovered"


# ── find_pending_progress_token_for_case ─────────────────────────────────

def test_find_pending_returns_token_for_case(tmp_path):
    from api.domains.laf_flow import register_laf_progress_submit_pending, find_pending_progress_token_for_case
    orch = _MockOrch(tmp_path)
    token = register_laf_progress_submit_pending(
        orch, platform="discord", requester_user_id="",
        payload={"laf_case_no": "1140806-J-002"}, result_data={}
    )
    found_tok, found_ent = find_pending_progress_token_for_case(orch, "1140806-J-002")
    assert found_tok == token
    assert isinstance(found_ent, dict)


def test_find_pending_returns_none_for_different_case(tmp_path):
    from api.domains.laf_flow import register_laf_progress_submit_pending, find_pending_progress_token_for_case
    orch = _MockOrch(tmp_path)
    register_laf_progress_submit_pending(
        orch, platform="discord", requester_user_id="",
        payload={"laf_case_no": "1140806-J-002"}, result_data={}
    )
    found_tok, _ = find_pending_progress_token_for_case(orch, "1140806-J-999")
    assert found_tok is None


# ── handle_laf_progress_submit_confirmation_if_any ───────────────────────

def test_confirm_cancel_sets_status_cancelled(tmp_path):
    from api.domains.laf_flow import (
        register_laf_progress_submit_pending,
        handle_laf_progress_submit_confirmation_if_any,
        _load_progress_pending,
        _progress_pending_file,
    )
    orch = _MockOrch(tmp_path)
    token = register_laf_progress_submit_pending(
        orch, platform="discord", requester_user_id="",
        payload={"laf_case_no": "1140806-J-003"}, result_data={}
    )
    result = handle_laf_progress_submit_confirmation_if_any(
        orch, platform="discord", user_id="lawyer1", text=f"取消 {token}"
    )
    assert isinstance(result, dict)
    assert result.get("handled") is True
    pending = _load_progress_pending(_progress_pending_file(orch))
    assert pending[token]["status"] == "cancelled"


def test_confirm_wrong_platform_returns_warning(tmp_path):
    from api.domains.laf_flow import (
        register_laf_progress_submit_pending,
        handle_laf_progress_submit_confirmation_if_any,
    )
    orch = _MockOrch(tmp_path)
    token = register_laf_progress_submit_pending(
        orch, platform="discord", requester_user_id="",
        payload={"laf_case_no": "1140806-J-004"}, result_data={}
    )
    result = handle_laf_progress_submit_confirmation_if_any(
        orch, platform="telegram", user_id="lawyer1", text=token
    )
    assert isinstance(result, dict)
    assert result.get("handled") is True
    assert "平台" in result.get("message", "")


def test_confirm_unknown_token_returns_none(tmp_path):
    from api.domains.laf_flow import handle_laf_progress_submit_confirmation_if_any
    orch = _MockOrch(tmp_path)
    result = handle_laf_progress_submit_confirmation_if_any(
        orch, platform="discord", user_id="lawyer1", text="AABBCC"
    )
    assert result is None


def test_no_hex_token_in_text_returns_none(tmp_path):
    from api.domains.laf_flow import handle_laf_progress_submit_confirmation_if_any
    orch = _MockOrch(tmp_path)
    result = handle_laf_progress_submit_confirmation_if_any(
        orch, platform="discord", user_id="lawyer1", text="請幫我查一下案件進度"
    )
    assert result is None


def test_progress_submit_pretty_json_failure_marks_pending_failed(monkeypatch, tmp_path):
    from api.domains import laf_flow
    from api.domains.laf_flow import (
        register_laf_progress_submit_pending,
        handle_laf_progress_submit_confirmation_if_any,
        _load_progress_pending,
        _progress_pending_file,
    )

    orch = _MockOrch(tmp_path)
    token = register_laf_progress_submit_pending(
        orch,
        platform="discord",
        requester_user_id="lawyer1",
        payload={"laf_case_no": "1140806-J-005", "client_name": "測試人"},
        result_data={},
    )

    class _Proc:
        returncode = 0
        stdout = json.dumps({"success": False, "error": "未偵測到法院端已送出"}, ensure_ascii=False, indent=2)
        stderr = ""

    monkeypatch.setattr(laf_flow.threading, "Thread", _ImmediateThread)
    monkeypatch.setattr(laf_flow.subprocess, "run", lambda *args, **kwargs: _Proc())

    result = handle_laf_progress_submit_confirmation_if_any(
        orch, platform="discord", user_id="lawyer1", text=f"確認 {token}"
    )

    assert result and result["handled"] is True
    pending = _load_progress_pending(_progress_pending_file(orch))
    assert pending[token]["status"] == "failed"
    assert "未偵測到法院端已送出" in pending[token]["last_error"]
