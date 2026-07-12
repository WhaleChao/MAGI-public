from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ACTION_PATH = ROOT / "skills" / "laf-portal-automation" / "action.py"


def _load_action():
    spec = importlib.util.spec_from_file_location("laf_portal_automation_action", ACTION_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_execute_workflow_delegates_to_laf_orchestrator_draft_mode(monkeypatch):
    action = _load_action()
    calls = []

    def fake_run(cmd, capture_output, text, timeout):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout='{"success": true}', stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = action.execute_workflow("go_live", "王小明", dry_run=True)

    assert result["ok"] is True
    cmd = calls[0]
    assert "--task" in cmd and cmd[cmd.index("--task") + 1] == "go_live"
    assert "--mode" in cmd and cmd[cmd.index("--mode") + 1] == "draft"
    assert "--client" in cmd and cmd[cmd.index("--client") + 1] == "王小明"
    assert "--dry-run" not in cmd
