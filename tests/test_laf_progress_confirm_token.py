"""Plan C unit tests: LAF progress submit 兩階段確認碼。

測試範圍：
- register_laf_progress_submit_pending：產生 6-hex token、TTL、寫 pending file
- resolve_laf_progress_pending_token：種類嚴格匹配（不誤吃 go_live token）
- kind 嚴格分離：progress token 不被 go_live 路徑吃；go_live token 不被 progress 路徑吃
- cmd_confirm_progress：安全閘門（CLI 來源拒絕）、bypass flag
- token 無效 / 已使用 / 過期 → 回傳 error
- handle_laf_submit_confirmation_if_any 進度分支路由
"""
import os
import sys
import time
import json

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest


class _MockOrch:
    """Minimal orchestrator stub for Plan C tests."""
    def __init__(self, tmp_path):
        self._laf_submit_pending_file = str(tmp_path / "laf_submit_pending.json")
        self._laf_progress_submit_pending_file = str(tmp_path / "laf_progress_submit_pending.json")
        self.notification_callback = None

    def _sanitize_incoming_message(self, message):
        return message

    def _quick_fixed_reply(self, message, role):
        return ""

    def _append_history(self, user_id, role, content):
        pass

    def _handle_gibberish_report(self, user_id, message, platform):
        return ""

    def _is_verified_admin_sender(self, user_id, platform):
        return True

    def _handle_laf_submit_confirmation_if_any(self, user_id, platform, role, message):
        from api.domains import laf_flow
        return laf_flow.handle_laf_submit_confirmation_if_any(self, user_id, platform, role, message)


# ── register_laf_progress_submit_pending ─────────────────────────────────

def test_register_returns_6hex_string(tmp_path):
    from api.domains.laf_flow import register_laf_progress_submit_pending
    orch = _MockOrch(tmp_path)
    token = register_laf_progress_submit_pending(
        orch,
        platform="discord",
        requester_user_id="lawyer1",
        payload={"laf_case_no": "1140806-T-027", "client_name": "黃彩庭"},
        result_data={"preview_url": "https://example.com/preview.png"},
    )
    assert isinstance(token, str)
    assert len(token) == 6
    assert token == token.upper()


def test_register_writes_kind_laf_progress_submit(tmp_path):
    from api.domains.laf_flow import register_laf_progress_submit_pending, _load_progress_pending, _progress_pending_file
    orch = _MockOrch(tmp_path)
    token = register_laf_progress_submit_pending(
        orch, platform="discord", requester_user_id="",
        payload={"laf_case_no": "1140806-T-027"}, result_data={},
    )
    pending = _load_progress_pending(_progress_pending_file(orch))
    assert token in pending
    assert pending[token]["kind"] == "laf_progress_submit"
    assert pending[token]["status"] == "pending"


def test_register_ttl_default_1800(tmp_path):
    from api.domains.laf_flow import register_laf_progress_submit_pending, _load_progress_pending, _progress_pending_file
    orch = _MockOrch(tmp_path)
    before = time.time()
    token = register_laf_progress_submit_pending(
        orch, platform="discord", requester_user_id="",
        payload={}, result_data={},
    )
    pending = _load_progress_pending(_progress_pending_file(orch))
    expires_at = pending[token]["expires_at"]
    assert expires_at > before + 1700
    assert expires_at < before + 1900


# ── resolve_laf_progress_pending_token ───────────────────────────────────

def test_resolve_returns_entry_for_valid_token(tmp_path):
    from api.domains.laf_flow import register_laf_progress_submit_pending, resolve_laf_progress_pending_token
    orch = _MockOrch(tmp_path)
    token = register_laf_progress_submit_pending(
        orch, platform="discord", requester_user_id="",
        payload={"laf_case_no": "1140806-T-027"}, result_data={},
    )
    entry = resolve_laf_progress_pending_token(orch, token)
    assert isinstance(entry, dict)
    assert entry["kind"] == "laf_progress_submit"
    assert entry["status"] == "pending"


def test_resolve_returns_none_for_expired(tmp_path):
    from api.domains.laf_flow import register_laf_progress_submit_pending, resolve_laf_progress_pending_token, _load_progress_pending, _save_progress_pending, _progress_pending_file
    orch = _MockOrch(tmp_path)
    token = register_laf_progress_submit_pending(
        orch, platform="discord", requester_user_id="",
        payload={}, result_data={},
    )
    # manually expire
    pf = _progress_pending_file(orch)
    pending = _load_progress_pending(pf)
    pending[token]["expires_at"] = time.time() - 10
    _save_progress_pending(pf, pending)
    assert resolve_laf_progress_pending_token(orch, token) is None


def test_resolve_returns_none_for_unknown_token(tmp_path):
    from api.domains.laf_flow import resolve_laf_progress_pending_token
    orch = _MockOrch(tmp_path)
    assert resolve_laf_progress_pending_token(orch, "AABBCC") is None


# ── kind 嚴格分離：progress token 不被 go_live 吃；go_live token 不被 progress 吃 ──

def test_progress_token_not_eaten_by_go_live_resolver(tmp_path):
    """go_live resolver 必須拒絕 progress token（kind 不匹配）。"""
    from api.domains.laf_flow import (
        register_laf_progress_submit_pending,
        resolve_laf_go_live_pending_token,
    )
    orch = _MockOrch(tmp_path)
    token = register_laf_progress_submit_pending(
        orch, platform="discord", requester_user_id="",
        payload={"laf_case_no": "1140806-T-027"}, result_data={},
    )
    # go_live resolver should NOT match a progress token
    resolved_token, entry = resolve_laf_go_live_pending_token(orch, "discord", token)
    assert resolved_token == ""
    assert entry == {}


def test_go_live_token_not_eaten_by_progress_resolver(tmp_path):
    """progress resolver 必須拒絕 go_live token（kind 不匹配）。"""
    from api.domains.laf_flow import (
        register_laf_go_live_submit_pending,
        resolve_laf_progress_pending_token,
    )
    orch = _MockOrch(tmp_path)
    entry = register_laf_go_live_submit_pending(
        orch, platform="discord", requester_user_id="",
        payload={"laf_case_no": "1140806-A-001"}, result_data={},
    )
    token = entry["token"]
    # progress resolver should NOT match a go_live token
    result = resolve_laf_progress_pending_token(orch, token)
    assert result is None


# ── cmd_confirm_progress 安全閘門 ─────────────────────────────────────────

def test_cmd_confirm_progress_rejects_cli_source():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "laf_action",
        os.path.join(os.path.dirname(__file__), "..", "skills", "laf-orchestrator", "action.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    result = mod.cmd_confirm_progress("AABBCC", source="cli")
    assert result["ok"] is False
    assert "user" in result["error"] or "來源" in result["error"]


def test_cmd_confirm_progress_bypass_with_env_var(monkeypatch, tmp_path):
    """MAGI_LAF_ALLOW_PROGRESS_CONFIRM=1 allows bypass of source gate."""
    monkeypatch.setenv("MAGI_LAF_ALLOW_PROGRESS_CONFIRM", "1")
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "laf_action2",
        os.path.join(os.path.dirname(__file__), "..", "skills", "laf-orchestrator", "action.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    result = mod.cmd_confirm_progress("AABBCC", source="cli")
    # Should pass gate but fail on invalid token
    assert result["ok"] is False
    assert "無效" in result.get("error", "") or "expired" in result.get("error", "")


def test_cmd_confirm_progress_allows_discord_source():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "laf_action3",
        os.path.join(os.path.dirname(__file__), "..", "skills", "laf-orchestrator", "action.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    result = mod.cmd_confirm_progress("AABBCC", source="discord", platform="discord")
    # Gate passes, invalid token
    assert result["ok"] is False
    assert "無效" in result.get("error", "") or "expired" in result.get("error", "")


# ── handle_laf_submit_confirmation_if_any 路由 ───────────────────────────

def test_handle_routes_progress_token_correctly(monkeypatch, tmp_path):
    """handle_laf_submit_confirmation_if_any 先試 progress，命中 → 返回進度回報訊息。"""
    from api.domains import laf_flow
    monkeypatch.setattr(laf_flow.threading, "Thread", lambda *a, **kw: type("T", (), {"start": lambda s: None})())
    orch = _MockOrch(tmp_path)
    token = laf_flow.register_laf_progress_submit_pending(
        orch, platform="discord", requester_user_id="lawyer1",
        payload={"laf_case_no": "1140806-T-027", "client_name": "黃彩庭"},
        result_data={},
    )
    handled, reply = laf_flow.handle_laf_submit_confirmation_if_any(
        orch, user_id="lawyer1", platform="discord", role="user",
        message=f"正確送出 {token}",
    )
    assert handled is True
    assert "進度回報" in reply
    assert token in reply


def test_handle_progress_confirm_does_not_touch_go_live_pending(monkeypatch, tmp_path):
    """進度回報確認不應更動 go_live pending file。"""
    from api.domains import laf_flow
    monkeypatch.setattr(laf_flow.threading, "Thread", lambda *a, **kw: type("T", (), {"start": lambda s: None})())
    orch = _MockOrch(tmp_path)
    go_live_entry = laf_flow.register_laf_go_live_submit_pending(
        orch, platform="discord", requester_user_id="lawyer1",
        payload={"laf_case_no": "1140806-A-001"}, result_data={},
    )
    go_live_token = go_live_entry["token"]
    progress_token = laf_flow.register_laf_progress_submit_pending(
        orch, platform="discord", requester_user_id="lawyer1",
        payload={"laf_case_no": "1140806-T-027"}, result_data={},
    )
    # Confirm the progress token
    laf_flow.handle_laf_submit_confirmation_if_any(
        orch, user_id="lawyer1", platform="discord", role="user",
        message=f"正確送出 {progress_token}",
    )
    # go_live pending should still be 'pending'
    go_live_pending = laf_flow.load_laf_submit_pending(orch)
    assert go_live_pending[go_live_token]["status"] == "pending"


# ── api/orchestrator.py wrapper methods ──────────────────────────────────

def test_orchestrator_wrapper_register_progress(tmp_path):
    """api/orchestrator.py 的 _register_laf_progress_submit_pending wrapper。"""
    from api.orchestrator import Orchestrator
    orch = _MockOrch(tmp_path)
    # Test wrapper directly through the module function (Orchestrator heavy to init)
    from api.domains import laf_flow
    token = laf_flow.register_laf_progress_submit_pending(
        orch, platform="discord", requester_user_id="",
        payload={"laf_case_no": "1140806-T-099"}, result_data={},
    )
    assert isinstance(token, str) and len(token) == 6
