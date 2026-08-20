from __future__ import annotations

import importlib.util
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _load():
    name = "autopilot_child_binding_test"
    spec = importlib.util.spec_from_file_location(
        name,
        ROOT / "skills" / "magi-autopilot" / "action.py",
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_child_binding_accepts_current_release_script(monkeypatch):
    module = _load()
    monkeypatch.setattr(module, "MAGI_ROOT_DIR", str(ROOT))
    monkeypatch.setattr(module, "VENV_PY", "/tmp/runtime/bin/python")
    monkeypatch.setenv("MAGI_SKILL_PYTHON", "/tmp/runtime/bin/python")
    path = ROOT / "skills" / "judgment-collector" / "action.py"
    assert module._bound_skill_subprocess_error(str(path)) == ""


def test_child_binding_rejects_old_release_path(monkeypatch, tmp_path):
    module = _load()
    monkeypatch.setattr(module, "MAGI_ROOT_DIR", str(ROOT))
    monkeypatch.setattr(module, "VENV_PY", "/tmp/runtime/bin/python")
    monkeypatch.setenv("MAGI_SKILL_PYTHON", "/tmp/runtime/bin/python")
    old = tmp_path / "v3-old" / "skills" / "judgment-collector" / "action.py"
    old.parent.mkdir(parents=True)
    old.write_text("", encoding="utf-8")
    error = module._bound_skill_subprocess_error(str(old))
    assert error.startswith("child_script_outside_active_release:")


def test_autopilot_inline_children_embed_the_resolved_root() -> None:
    source = (ROOT / "skills" / "magi-autopilot" / "action.py").read_text(
        encoding="utf-8"
    )
    assert '"sys.path.insert(0,str(_MAGI_ROOT));"' not in source
    assert '"sys.path.insert(0, str(_MAGI_ROOT))"' not in source


def test_daily_reflection_reads_v3_history_and_empty_input_is_safe_wait(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("MAGI_RUNTIME_DIR", str(tmp_path / "runtime"))
    module_path = ROOT / "skills" / "ops" / "daily_reflection.py"
    spec = importlib.util.spec_from_file_location("daily_reflection_v3_test", module_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    db_path = tmp_path / "runtime" / "conversation_history.sqlite3"
    db_path.parent.mkdir(parents=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE conversation_history ("
            "id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT, ts TEXT)"
        )
        conn.execute(
            "INSERT INTO conversation_history VALUES (1, ?, ?, ?, ?)",
            ("office-session", "user", "請整理今日工作", datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()

    assert "請整理今日工作" in module.parse_v3_conversation_history(hours=1)
    monkeypatch.setattr(module, "parse_v3_conversation_history", lambda **_kwargs: "")
    result = module.run_reflection()
    assert result["success"] is True
    assert result["skipped"] is True
    assert result["reason"] == "no_recent_v3_conversation_logs"


def test_todo_parser_skips_absurd_relative_duration_without_crashing() -> None:
    module_path = ROOT / "skills" / "osc-orchestrator" / "osc_headless" / "todos.py"
    spec = importlib.util.spec_from_file_location("todo_duration_bound_test", module_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    todos = module.extract_todos_from_filename(
        "20260801_應於999999999日內補正.pdf",
        "/tmp/20260801_應於999999999日內補正.pdf",
    )
    assert todos == []
