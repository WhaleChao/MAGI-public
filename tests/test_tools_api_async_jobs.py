"""Tests for async /skills/run + GET /jobs/<job_id> poll endpoint.

Coverage:
  - sync path unchanged (backward compat)
  - async=true → 202 + job_id + poll_url
  - poll: 400 for invalid job_id format
  - poll: 404 for unknown job_id
  - poll: 202 for queued job
  - poll: 202 for running job
  - poll: 200 + result for done job
  - poll: 400 + error for failed job
  - poll: 400 + error for abandoned job
  - background worker completes job in DB
  - background worker records failure in DB
  - _trim_skill_result_for_storage stays under 4000 chars
  - missing skill/task still 400 in async mode
  - worker no-ops when claim returns False (already claimed)
"""
from __future__ import annotations

import json
import threading
import time
from typing import Any, Dict
from unittest.mock import MagicMock, patch

import pytest


# ── helpers ──────────────────────────────────────────────────────────────────

def _ok_result(skill: str = "test-skill", task: str = "help") -> dict:
    return {"success": True, "skill": skill, "output": f"done:{task}", "stderr": ""}


def _fail_result() -> dict:
    return {"success": False, "error": "something_broke", "output": "", "stderr": ""}


# ── fixture ───────────────────────────────────────────────────────────────────

_TEST_API_KEY = "test-api-key-asyncjobs-12345"
_AUTH_HEADER = {"X-API-Key": _TEST_API_KEY}


@pytest.fixture()
def ctx(monkeypatch, tmp_path):
    """Flask test client with:
    - job queue DB redirected to tmp_path
    - tool access always allowed
    - run_skill_action stubbed (overridable per test)
    - API key auth satisfied via _TEST_API_KEY
    """
    import skills.memory.job_queue as jq
    import api.tools_api as tools_api
    import api.authz as authz

    # Redirect SQLite DB so tests never touch the real .agent/jobs/
    db_dir = str(tmp_path / "jobs")
    db_path = str(tmp_path / "jobs" / "job_queue.db")
    monkeypatch.setattr(jq, "_DB_DIR", db_dir)
    monkeypatch.setattr(jq, "_DB_PATH", db_path)

    # Satisfy API key auth: patch the module-level constant read by _check_api_key
    monkeypatch.setattr(authz, "MAGI_API_KEY", _TEST_API_KEY)

    # Allow all tool access
    monkeypatch.setattr(tools_api, "_check_tool_access", lambda *a, **k: (True, "allow"))
    monkeypatch.setattr(tools_api, "_start_tool_event", lambda *a, **k: time.time())
    monkeypatch.setattr(tools_api, "_finish_tool_event", lambda *a, **k: None)
    monkeypatch.setattr(tools_api, "_tool_preview", lambda r: {})
    monkeypatch.setattr(tools_api, "_tool_denied_response", lambda *a, **k: ({"error": "denied"}, 403))
    monkeypatch.setattr(tools_api, "_tool_exception_response", lambda *a, **k: ({"error": "exc"}, 500))
    monkeypatch.setattr(tools_api, "_resolve_skill_action_path", lambda s: f"/tmp/{s}/action.py")

    # Default stub for run_skill_action (sync + background worker both hit this)
    monkeypatch.setattr(
        "skills.evolution.skill_genesis.run_skill_action",
        lambda skill, task, **kw: _ok_result(skill, task),
    )

    raw_client = tools_api.app.test_client()

    # Thin wrapper that always injects the auth header
    class _AuthClient:
        def post(self, path, **kw):
            kw.setdefault("headers", {}).update(_AUTH_HEADER)
            return raw_client.post(path, **kw)

        def get(self, path, **kw):
            kw.setdefault("headers", {}).update(_AUTH_HEADER)
            return raw_client.get(path, **kw)

        # Expose raw client for tests that need no-auth checks
        @property
        def raw(self):
            return raw_client

    yield {"client": _AuthClient(), "jq": jq, "tools_api": tools_api, "tmp_path": tmp_path}


# ── 1. Sync path unchanged ────────────────────────────────────────────────────

def test_sync_skill_run_succeeds(ctx):
    resp = ctx["client"].post(
        "/skills/run",
        json={"skill": "translator", "task": "help"},
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["success"] is True
    assert "job_id" not in data


def test_sync_skill_run_missing_task_returns_400(ctx):
    resp = ctx["client"].post("/skills/run", json={"skill": "translator"})
    assert resp.status_code == 400
    assert "task" in resp.get_json().get("error", "").lower()


def test_sync_skill_run_missing_skill_returns_400(ctx):
    resp = ctx["client"].post("/skills/run", json={"task": "help"})
    assert resp.status_code == 400
    assert "skill" in resp.get_json().get("error", "").lower()


# ── 2. Async submit ───────────────────────────────────────────────────────────

def test_async_returns_202_with_job_id(ctx):
    resp = ctx["client"].post(
        "/skills/run",
        json={"skill": "translator", "task": "help", "async": True},
    )
    assert resp.status_code == 202
    data = resp.get_json()
    assert data["success"] is True
    assert data["queued"] is True
    assert "job_id" in data
    assert data["poll_url"] == f"/jobs/{data['job_id']}"


def test_async_missing_task_still_400(ctx):
    resp = ctx["client"].post("/skills/run", json={"skill": "translator", "async": True})
    assert resp.status_code == 400


def test_async_missing_skill_still_400(ctx):
    resp = ctx["client"].post("/skills/run", json={"task": "help", "async": True})
    assert resp.status_code == 400


# ── 3. Poll endpoint – format / not-found ────────────────────────────────────

def test_poll_invalid_job_id_format_returns_400(ctx):
    # Flask routing normalises path-traversal sequences before they reach the handler,
    # so only test cases that do reach our regex guard.
    for bad in ("ab", "has spaces here!", "A" * 81):
        resp = ctx["client"].get(f"/jobs/{bad}")
        assert resp.status_code == 400, f"expected 400 for {bad!r}"
        assert resp.get_json()["error"] == "invalid_job_id"


def test_poll_unknown_job_id_returns_404(ctx):
    resp = ctx["client"].get("/jobs/20260418_120000_abcdef")
    assert resp.status_code == 404
    data = resp.get_json()
    assert data["error"] == "job_not_found"


# ── 4. Poll endpoint – queued / running ──────────────────────────────────────

def test_poll_queued_job_returns_202(ctx):
    jq = ctx["jq"]
    job_id = jq.enqueue(job_type="skill_run", platform="api", user_text="translator:help")
    resp = ctx["client"].get(f"/jobs/{job_id}")
    assert resp.status_code == 202
    data = resp.get_json()
    assert data["status"] == "queued"
    assert data["job_id"] == job_id


def test_poll_running_job_returns_202(ctx):
    jq = ctx["jq"]
    job_id = jq.enqueue(job_type="skill_run", platform="api", user_text="translator:help")
    jq.claim(job_id)
    resp = ctx["client"].get(f"/jobs/{job_id}")
    assert resp.status_code == 202
    data = resp.get_json()
    assert data["status"] == "running"


# ── 5. Poll endpoint – done ───────────────────────────────────────────────────

def test_poll_done_job_returns_200_with_result(ctx):
    jq = ctx["jq"]
    job_id = jq.enqueue(job_type="skill_run", platform="api", user_text="translator:help")
    jq.claim(job_id)
    result = _ok_result()
    jq.complete(job_id, json.dumps(result))

    resp = ctx["client"].get(f"/jobs/{job_id}")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["status"] == "done"
    assert data["success"] is True
    assert data["result"]["output"] == result["output"]


# ── 6. Poll endpoint – failed / abandoned ────────────────────────────────────

def test_poll_failed_job_returns_400_with_error(ctx):
    jq = ctx["jq"]
    job_id = jq.enqueue(job_type="skill_run", platform="api", user_text="translator:help")
    jq.claim(job_id)
    jq.fail(job_id, "subprocess_crashed")

    resp = ctx["client"].get(f"/jobs/{job_id}")
    assert resp.status_code == 400
    data = resp.get_json()
    assert data["status"] == "failed"
    assert data["success"] is False
    assert "subprocess_crashed" in data["error"]


def test_poll_abandoned_job_returns_400(ctx):
    jq = ctx["jq"]
    job_id = jq.enqueue(job_type="skill_run", platform="api", user_text="translator:help")
    jq.claim(job_id)
    jq.abandon(job_id, "max_attempts_exceeded")

    resp = ctx["client"].get(f"/jobs/{job_id}")
    assert resp.status_code == 400
    data = resp.get_json()
    assert data["status"] == "abandoned"
    assert data["success"] is False


# ── 7. Background worker ──────────────────────────────────────────────────────

def test_background_worker_completes_job(ctx, monkeypatch):
    """Full integration: async submit → worker runs → poll returns done."""
    done_event = threading.Event()
    original_complete = ctx["jq"].complete

    def _complete_and_signal(job_id, result=""):
        original_complete(job_id, result)
        done_event.set()

    monkeypatch.setattr(ctx["jq"], "complete", _complete_and_signal)

    # Patch the import inside the background worker too
    import skills.memory.job_queue as jq_mod
    monkeypatch.setattr(jq_mod, "complete", _complete_and_signal)

    resp = ctx["client"].post(
        "/skills/run",
        json={"skill": "translator", "task": "help", "async": True},
    )
    assert resp.status_code == 202
    job_id = resp.get_json()["job_id"]

    assert done_event.wait(timeout=10), "background worker did not complete within 10s"

    poll = ctx["client"].get(f"/jobs/{job_id}")
    assert poll.status_code == 200
    data = poll.get_json()
    assert data["status"] == "done"
    assert data["success"] is True


def test_background_worker_records_failure(ctx, monkeypatch):
    """Worker records failure when run_skill_action raises."""
    import skills.evolution.skill_genesis as sg
    monkeypatch.setattr(sg, "run_skill_action", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("kaboom")))

    done_event = threading.Event()
    import skills.memory.job_queue as jq_mod
    original_fail = jq_mod.fail

    def _fail_and_signal(job_id, error=""):
        original_fail(job_id, error)
        done_event.set()

    monkeypatch.setattr(jq_mod, "fail", _fail_and_signal)

    resp = ctx["client"].post(
        "/skills/run",
        json={"skill": "bad-skill", "task": "crash", "async": True},
    )
    assert resp.status_code == 202
    job_id = resp.get_json()["job_id"]

    assert done_event.wait(timeout=10), "background worker did not fail within 10s"

    poll = ctx["client"].get(f"/jobs/{job_id}")
    assert poll.status_code == 400
    data = poll.get_json()
    assert data["status"] == "failed"
    assert "kaboom" in data["error"]


def test_background_worker_noop_when_already_claimed(ctx, monkeypatch):
    """Worker silently exits when claim() returns False (race condition guard)."""
    import skills.memory.job_queue as jq_mod
    import api.tools_api as tools_api

    call_log: list = []
    monkeypatch.setattr(jq_mod, "claim", lambda job_id: False)
    monkeypatch.setattr(jq_mod, "complete", lambda *a, **k: call_log.append("complete"))
    monkeypatch.setattr(jq_mod, "fail", lambda *a, **k: call_log.append("fail"))

    tools_api._run_skill_job_background(
        "fake_job_id", "translator", "help", 30, True, True, True, ""
    )
    assert call_log == [], "worker should not call complete/fail when claim returns False"


# ── 8. Result trimming ────────────────────────────────────────────────────────

def test_trim_skill_result_stays_under_db_cap():
    import api.tools_api as tools_api

    big_result = {
        "success": True,
        "skill": "translator",
        "output": "X" * 50_000,
        "stderr": "E" * 10_000,
        "trace": [{"cmd": "python3 action.py", "stdout": "Y" * 5000}],
    }
    encoded = tools_api._trim_skill_result_for_storage(big_result)
    assert len(encoded) <= 4000, f"encoded length {len(encoded)} exceeds 4000"
    parsed = json.loads(encoded)
    assert parsed["success"] is True
    assert parsed.get("_truncated") is True


def test_trim_small_result_unchanged():
    import api.tools_api as tools_api

    small = {"success": True, "output": "ok", "skill": "s"}
    encoded = tools_api._trim_skill_result_for_storage(small)
    assert json.loads(encoded) == small
