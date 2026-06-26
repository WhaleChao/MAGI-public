import importlib.util
import json
import subprocess
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
