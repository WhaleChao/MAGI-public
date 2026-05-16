import importlib.util
import json
import subprocess
import sys
from pathlib import Path


ROOT = Path("/Users/ai/Desktop/MAGI_v2")
ACTION_PATH = ROOT / "skills" / "interpreter-empirical-classifier" / "action.py"


def _load_action():
    spec = importlib.util.spec_from_file_location("interpreter_empirical_classifier_action", ACTION_PATH)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_interpreter_empirical_classifier_self_test():
    action = _load_action()
    result = action.self_test()
    assert result["success"] is True
    assert result["outputs"]["csv"].endswith(".csv")


def test_interpreter_empirical_classifier_cli_outputs_json():
    proc = subprocess.run(
        [sys.executable, str(ACTION_PATH), "--task", "self_test"],
        cwd=str(ROOT),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["success"] is True


def test_interpreter_tool_definition_is_exposed():
    data = json.loads((ROOT / "skills" / "definitions.json").read_text(encoding="utf-8"))
    tool = next((t for t in data["tools"] if t.get("name") == "run_interpreter_empirical_classifier"), None)
    assert tool is not None
    assert tool["endpoint"] == "/skills/run"
    skill = tool["parameters"]["properties"]["skill"]
    assert skill["default"] == "interpreter-empirical-classifier"
    assert skill["enum"] == ["interpreter-empirical-classifier"]


def test_interpreter_skill_is_in_react_run_skill_allowlist():
    from skills.engine.tool_registry import _ALLOWED_SKILLS

    assert "interpreter-empirical-classifier" in _ALLOWED_SKILLS
