from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_completion_evidence_uses_latest_completed_item(tmp_path, monkeypatch):
    monkeypatch.setenv("MAGI_ROOT_DIR", str(ROOT))
    module = _load_module(ROOT / "scripts" / "weekend_resummary.py", "weekend_resummary_evidence_test")
    state = {
        "stats": {"2026-07-12": {"stopped_by": "complete"}},
        "nim_done": {
            "a": {"at": "2026-07-12T07:40:00"},
            "b": {"at": "2026-07-12T07:45:09"},
            "old": {"at": "2026-07-05T08:09:02"},
        },
    }

    assert module._completed_run_evidence(state, "2026-07-12") == datetime.fromisoformat(
        "2026-07-12T07:45:09"
    )
    state["stats"]["2026-07-12"]["stopped_by"] = "shutdown"
    assert module._completed_run_evidence(state, "2026-07-12") is None


def test_scheduler_evidence_recovers_only_matching_dispatch(tmp_path, monkeypatch):
    runtime = tmp_path / ".runtime"
    runtime.mkdir()
    state_path = runtime / "cron_state.json"
    state_path.write_text(
        json.dumps(
            {
                "job_weekend_resummary": {
                    "last_dispatch_at": "2026-07-12T06:30:17",
                    "last_success": False,
                    "last_error": "scheduler_completion_missing_after_timeout",
                    "returncode": 130,
                    "timed_out": True,
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("MAGI_USE_RUNTIME_DIR", "1")
    monkeypatch.setenv("MAGI_RUNTIME_DIR", str(runtime))
    scheduler = _load_module(ROOT / "skills" / "ops" / "cron_scheduler.py", "cron_scheduler_evidence_test")

    assert scheduler.mark_job_result_from_evidence(
        "job_weekend_resummary",
        evidence_at=datetime.fromisoformat("2026-07-12T07:45:09"),
        success=True,
        provenance="test:completed_batch",
        expected_error="scheduler_completion_missing_after_timeout",
    )
    recovered = json.loads(state_path.read_text(encoding="utf-8"))["job_weekend_resummary"]
    assert recovered["last_success"] is True
    assert recovered["returncode"] == 0
    assert recovered["timed_out"] is False
    assert recovered["last_error"] == ""

    recovered["last_dispatch_at"] = "2026-07-13T06:30:00"
    recovered["last_error"] = "scheduler_completion_missing_after_timeout"
    state_path.write_text(json.dumps({"job_weekend_resummary": recovered}), encoding="utf-8")
    assert not scheduler.mark_job_result_from_evidence(
        "job_weekend_resummary",
        evidence_at=datetime.fromisoformat("2026-07-12T07:45:09"),
        success=True,
        expected_error="scheduler_completion_missing_after_timeout",
    )
