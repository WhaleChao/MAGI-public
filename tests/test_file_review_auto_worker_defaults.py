from pathlib import Path
import importlib.util


ROOT = Path(__file__).resolve().parents[1]


def test_file_review_auto_worker_checks_but_does_not_download_by_default():
    src = (ROOT / "skills" / "ops" / "file_review_auto_worker.py").read_text(encoding="utf-8")
    assert 'MAGI_FILE_REVIEW_AUTO_RUN_ON_START", "1"' in src
    assert 'MAGI_FILE_REVIEW_AUTO_DOWNLOAD", "0"' in src
    assert 'MAGI_FILE_REVIEW_AUTO_PAYMENT_SLIPS", "1"' in src
    assert 'MAGI_FILE_REVIEW_AUTO_PAYMENT_TIMEOUT_SEC", "300"' in src
    assert 'MAGI_FILE_REVIEW_PROBE_WITH_GMAIL", "1"' in src
    assert 'MAGI_ENABLE_BACKGROUND_FILE_REVIEW_CHECK", "1"' in src
    assert "supplemental_payment_slip_scan_failed" in src


def test_file_review_downloadable_probe_cross_checks_gmail_by_default():
    src = (ROOT / "skills" / "file-review-orchestrator" / "action.py").read_text(encoding="utf-8")
    assert 'MAGI_FILE_REVIEW_PROBE_WITH_GMAIL", "1"' in src


def test_file_review_worker_tail_normalizes_timeout_bytes():
    path = ROOT / "skills" / "ops" / "file_review_auto_worker.py"
    spec = importlib.util.spec_from_file_location("file_review_auto_worker_test", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod._tail(b"  abc\xe4\xb8\xad  ") == "abc中"
    assert mod._task_ok_or_nonfatal({"ok": False, "nonfatal": True}) is True


def test_file_review_worker_pretty_json_failure_overrides_returncode(monkeypatch):
    path = ROOT / "skills" / "ops" / "file_review_auto_worker.py"
    spec = importlib.util.spec_from_file_location("file_review_auto_worker_pretty_json_test", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    class _Proc:
        returncode = 0
        stdout = '{\n  "success": false,\n  "error": "manual blocked"\n}\n'
        stderr = ""

    monkeypatch.setattr(mod.subprocess, "run", lambda *args, **kwargs: _Proc())

    result = mod._run_task("download", 30, {})

    assert result["ok"] is False
    assert result["parsed"]["error"] == "manual blocked"
