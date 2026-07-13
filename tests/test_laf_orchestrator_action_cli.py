import importlib.util
import json
import subprocess
import sys
from pathlib import Path


def _load_action_module():
    path = Path(__file__).resolve().parents[1] / "skills" / "laf-orchestrator" / "action.py"
    spec = importlib.util.spec_from_file_location("laf_orchestrator_action", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_run_orchestrator_parses_sentinel_json(monkeypatch):
    action = _load_action_module()

    def fake_run(*args, **kwargs):
        payload = {"ok": True, "nested": {"value": 7}}
        stdout = (
            "noise before\n"
            "===MAGI_RESULT_JSON_START===\n"
            f"{json.dumps(payload, ensure_ascii=False, indent=2)}\n"
            "===MAGI_RESULT_JSON_END===\n"
            "noise after\n"
        )
        return subprocess.CompletedProcess(args[0], 0, stdout=stdout, stderr="")

    monkeypatch.setattr(action.subprocess, "run", fake_run)

    result = action._run_orchestrator(["--mode", "portal-draft"], timeout=1)

    assert result["success"] is True
    assert result["result"] == {"ok": True, "nested": {"value": 7}}


def test_run_orchestrator_pretty_json_failure_is_failure(monkeypatch):
    action = _load_action_module()

    def fake_run(*args, **kwargs):
        payload = {"success": False, "error": "portal submit failed"}
        return subprocess.CompletedProcess(args[0], 0, stdout=json.dumps(payload, ensure_ascii=False, indent=2), stderr="")

    monkeypatch.setattr(action.subprocess, "run", fake_run)

    result = action._run_orchestrator(["--mode", "portal-submit"], timeout=1)

    assert result["success"] is False
    assert result["result"]["error"] == "portal submit failed"


def test_run_orchestrator_nested_payload_failure_overrides_returncode(monkeypatch):
    action = _load_action_module()

    def fake_run(*args, **kwargs):
        payload = {"success": True, "result": {"ok": False, "error": "inner failed"}}
        return subprocess.CompletedProcess(args[0], 0, stdout=json.dumps(payload, ensure_ascii=False, indent=2), stderr="")

    monkeypatch.setattr(action.subprocess, "run", fake_run)

    result = action._run_orchestrator(["--mode", "portal-submit"], timeout=1)

    assert result["success"] is False
    assert result["result"]["result"]["error"] == "inner failed"


def test_self_test_requires_orchestrator_to_be_reachable(monkeypatch):
    action = _load_action_module()
    monkeypatch.setattr(action, "_probe_orchestrator_db", lambda *args, **kwargs: {"success": False})

    report = action.task_self_test()

    assert report["compile"]["ok"] is True
    assert report["orchestrator_reachable"] is False
    assert report["success"] is False


def test_self_test_uses_bounded_read_only_orchestrator_probe(monkeypatch):
    action = _load_action_module()
    calls = []

    def fake_probe(*args, **kwargs):
        calls.append((args, kwargs))
        return {"success": True, "result": {"db": True}}

    monkeypatch.setattr(action, "_probe_orchestrator_db", fake_probe)

    report = action.task_self_test()

    assert report["success"] is True
    assert report["orchestrator_db_probe"] == {"db": True}
    assert calls and calls[0][1]["timeout"] == 20


def test_portal_action_forwards_no_notify(monkeypatch):
    action = _load_action_module()
    captured = {}

    def fake_run_orchestrator(args_list, timeout=300, extra_env=None):
        captured["args_list"] = list(args_list)
        return {"success": True, "returncode": 0, "result": {"ok": True}}

    monkeypatch.setattr(action, "_run_orchestrator", fake_run_orchestrator)

    result = action.task_portal_action(
        "condition",
        laf_case_no="1140605-A-025",
        suppress_notify=True,
    )

    assert result["success"] is True
    assert "--no-notify" in captured["args_list"]


def test_retry_notification_redacts_target_and_raw_error(monkeypatch):
    action = _load_action_module()
    deliveries = []

    def fake_telegram(message, **kwargs):
        deliveries.append(("telegram", message, kwargs))
        return {"delivered": False, "queued": True}

    def fake_discord(message, severity, **kwargs):
        deliveries.append(("discord", message, {"severity": severity, **kwargs}))
        return True

    monkeypatch.setenv("MAGI_LAF_NOTIFY_RETRY_ON_FAILURE", "1")
    monkeypatch.setitem(
        sys.modules,
        "skills.ops.red_phone",
        type("RedPhone", (), {
            "send_telegram_push_with_status": staticmethod(fake_telegram),
            "_send_discord_bot_message": staticmethod(fake_discord),
        }),
    )
    raw_target = "王小明 1140806-J-002"
    raw_error = "invalid CSRF token for /Users/ai/cases/王小明.pdf token=super-secret"
    result = {"success": False, "error": raw_error, "stderr_tail": raw_error}

    action._notify_retrying_after_failure("closing", raw_target, result)

    assert len(deliveries) == 2
    message = deliveries[0][1]
    assert "動作類別：closing" in message
    assert "錯誤類別：portal_session_invalid" in message
    assert "追蹤碼：laf-retry-" in message
    for secret in ("王小明", "1140806-J-002", "/Users/ai", "super-secret", "invalid CSRF"):
        assert secret not in message
    assert deliveries[0][2]["queue_on_fail"] is True
    assert result["retry_trace_id"] in message


def test_preview_counts_cli_returns_nonzero_for_explicit_failure(monkeypatch):
    action = _load_action_module()
    monkeypatch.setattr(action, "task_preview_counts", lambda **_kwargs: {"success": False, "error": "db unavailable"})
    monkeypatch.setattr(action.sys, "argv", ["action.py", "--task", "preview_counts", "--client", "test"])

    assert action.main() == 1
