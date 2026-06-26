from pathlib import Path
import importlib.util
import re
from types import SimpleNamespace


FILE_REVIEW_ACTION = Path(__file__).resolve().parents[1] / "skills" / "file-review-orchestrator" / "action.py"


def test_download_sync_dispatches_to_sync_handler():
    """`download_sync` must dispatch to dedicated sync handler, not generic handler."""
    src = FILE_REVIEW_ACTION.read_text(encoding="utf-8")

    assert 'def cmd_download_sync(' in src
    assert '"download_sync"' in src

    m = re.search(
        r'if task\.startswith\("download_sync"\):\n(.*?)\n    if task == "download"',
        src,
        re.S,
    )
    assert m is not None, "download_sync dispatch block is missing"

    block = m.group(1)
    assert '"download_sync"' in block
    assert 'cmd_download_sync' in block
    assert 'cmd_download(case_number=' not in block


def test_download_status_preserves_failed_job_success_false(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("file_review_action_status_test", FILE_REVIEW_ACTION)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "BG_JOB_DIR", str(tmp_path))

    mod._write_download_job(
        "job_failed",
        {
            "status": "failed",
            "running": False,
            "success": False,
            "error": "download failed",
        },
    )

    result = mod.cmd_download_status("job_failed")

    assert result["success"] is False
    assert result["status"] == "failed"


def test_flow_result_ready_is_blocked_not_ok(monkeypatch):
    spec = importlib.util.spec_from_file_location("file_review_action_flow_test", FILE_REVIEW_ACTION)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    captured = {}

    def fake_finalize(flow_id, **kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(mod, "_flow_ledger", SimpleNamespace(finalize_flow=fake_finalize))

    mod._safe_finalize_flow("flow-1", {"success": True, "result": "Ready", "message": "confirm required"})

    assert captured["status"] == "blocked"
    assert captured["ok"] is False


def test_cli_ok_returns_nonzero_for_manual_ready():
    spec = importlib.util.spec_from_file_location("file_review_action_ok_test", FILE_REVIEW_ACTION)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    assert mod._ok({"success": True, "result": "Ready"}) == 1
    assert mod._ok({"success": False, "error": "blocked"}) == 1
