from pathlib import Path
import importlib.util
from types import SimpleNamespace
import time


ROOT = Path(__file__).resolve().parents[1]


def _load_worker(name: str):
    path = ROOT / "skills" / "ops" / "file_review_auto_worker.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_file_review_auto_worker_checks_but_does_not_download_by_default():
    src = (ROOT / "skills" / "ops" / "file_review_auto_worker.py").read_text(encoding="utf-8")
    assert 'MAGI_FILE_REVIEW_AUTO_RUN_ON_START", "1"' in src
    assert 'MAGI_FILE_REVIEW_AUTO_INTERVAL_SEC", "900"' in src
    assert 'MAGI_FILE_REVIEW_AUTO_DOWNLOAD", "0"' in src
    assert 'MAGI_FILE_REVIEW_AUTO_PAYMENT_SLIPS", "1"' in src
    assert 'MAGI_FILE_REVIEW_AUTO_PAYMENT_TIMEOUT_SEC", "300"' in src
    assert 'MAGI_FILE_REVIEW_PROBE_WITH_GMAIL", "1"' in src
    assert 'MAGI_ENABLE_BACKGROUND_FILE_REVIEW_CHECK", "1"' in src
    assert "supplemental_payment_slip_scan_failed" in src


def test_file_review_downloadable_probe_uses_mode_specific_gmail_default():
    src = (ROOT / "skills" / "file-review-orchestrator" / "action.py").read_text(encoding="utf-8")
    automation_src = (ROOT / "casper_ecosystem" / "law_firm_orchestrators" / "file_review_automation.py").read_text(encoding="utf-8")
    assert 'gmail_default = "0" if read_only else "1"' in src
    assert 'MAGI_FILE_REVIEW_PROBE_WITH_GMAIL", gmail_default' in src
    assert 'MAGI_ENABLE_CASE_LEVEL_DOWNLOAD_SKIP", "0"' in src
    assert 'MAGI_ENABLE_BUTTON_LEVEL_DOWNLOAD_SKIP", "0"' in src
    assert 'MAGI_ENABLE_CASE_LEVEL_DOWNLOAD_SKIP", "0"' in automation_src
    assert 'MAGI_ENABLE_BUTTON_LEVEL_DOWNLOAD_SKIP", "0"' in automation_src


def test_file_review_worker_tail_normalizes_timeout_bytes():
    mod = _load_worker("file_review_auto_worker_test")
    assert mod._tail(b"  abc\xe4\xb8\xad  ") == "abc中"
    assert mod._task_ok_or_nonfatal({"ok": False, "nonfatal": True}) is True


def test_file_review_worker_pretty_json_failure_overrides_returncode(monkeypatch):
    mod = _load_worker("file_review_auto_worker_pretty_json_test")

    monkeypatch.setattr(
        mod.safe_process,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout='{\n  "success": false,\n  "error": "manual blocked"\n}\n',
            stderr="",
            timed_out=False,
        ),
    )

    result = mod._run_task("download", 30, {})

    assert result["ok"] is False
    assert result["parsed"]["error"] == "manual blocked"


def test_file_review_worker_fails_closed_for_empty_or_non_boolean_json(monkeypatch):
    mod = _load_worker("file_review_auto_worker_contract_test")
    responses = iter(("", '{"success":"true"}'))

    monkeypatch.setattr(
        mod.safe_process,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout=next(responses),
            stderr="",
            timed_out=False,
        ),
    )

    empty = mod._run_task("check_emails", 30, {})
    non_boolean = mod._run_task("check_emails", 30, {})

    assert empty["ok"] is False
    assert empty["contract_error"] == "missing_json_object_contract"
    assert non_boolean["ok"] is False
    assert non_boolean["contract_error"] == "non_boolean_success_or_ok_contract"


def test_file_review_stale_reaper_requires_worker_ownership_evidence(monkeypatch):
    mod = _load_worker("file_review_auto_worker_reaper_test")
    process = {
        "pid": 4321,
        "ppid": 1,
        "age_sec": 1800,
        "cmd": "skills/file-review-orchestrator/action.py --task download",
    }
    kill_calls = []

    monkeypatch.setattr(mod, "_list_download_processes", lambda: [process])
    monkeypatch.setattr(mod, "_load_download_ownership", lambda: {})
    monkeypatch.setattr(mod.os, "killpg", lambda *args: kill_calls.append(args))

    assert mod._reap_stale_download_processes(1200) == []
    assert kill_calls == []

    ownership = {
        "run_id": "run-123",
        "pid": 4321,
        "worker_pid": 999,
        "task": "download",
        "started_at": time.time() - 1800,
    }
    monkeypatch.setattr(mod, "_load_download_ownership", lambda: ownership)
    monkeypatch.setattr(mod.os, "getpgid", lambda _pid: 4321)
    monkeypatch.setattr(mod, "_is_pid_alive", lambda _pid: False)

    reaped = mod._reap_stale_download_processes(1200)

    assert [item["pid"] for item in reaped] == [4321]
    assert kill_calls == [(4321, 15)]
