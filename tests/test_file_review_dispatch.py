from pathlib import Path
import importlib.util
import json
import re
from types import SimpleNamespace


FILE_REVIEW_ACTION = Path(__file__).resolve().parents[1] / "skills" / "file-review-orchestrator" / "action.py"


def _load_action_module(name: str):
    spec = importlib.util.spec_from_file_location(name, FILE_REVIEW_ACTION)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


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
    mod = _load_action_module("file_review_action_status_test")
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
    mod = _load_action_module("file_review_action_flow_test")

    captured = {}

    def fake_finalize(flow_id, **kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(mod, "_flow_ledger", SimpleNamespace(finalize_flow=fake_finalize))

    mod._safe_finalize_flow("flow-1", {"success": True, "result": "Ready", "message": "confirm required"})

    assert captured["status"] == "blocked"
    assert captured["ok"] is False


def test_cli_ok_returns_nonzero_for_manual_ready():
    mod = _load_action_module("file_review_action_ok_test")

    assert mod._ok({"success": True, "result": "Ready"}) == 1
    assert mod._ok({"success": False, "error": "blocked"}) == 1


def test_read_only_health_probe_requires_portal_and_skips_eventlog(monkeypatch, tmp_path):
    mod = _load_action_module("file_review_action_read_only_probe_test")
    calls = []

    class FakeManager:
        def __init__(self, *args, **kwargs):
            pass

        def probe_downloadable_from_portal(self, **_kwargs):
            return {"success": False, "error": "portal unavailable"}

        def close(self):
            calls.append("close")

    monkeypatch.setattr(mod, "_missing_runtime_deps", lambda: [])
    monkeypatch.setattr(mod, "_ensure_runtime_deps", lambda: (_ for _ in ()).throw(AssertionError("dependency bootstrap called")))
    monkeypatch.setattr(mod, "_load_config", lambda: {})
    monkeypatch.setattr(mod, "_get_credentials", lambda _cfg: {"username": "u", "password": "p", "download_folder": str(tmp_path)})
    monkeypatch.setattr(mod, "_ensure_portal_probe_imports", lambda: SimpleNamespace(FileReviewManager=FakeManager))
    monkeypatch.setattr(mod, "_get_db_manager", lambda _cfg, **_kwargs: None)
    monkeypatch.setattr(mod, "cmd_preview_emails", lambda **_kwargs: (_ for _ in ()).throw(AssertionError("Gmail fallback called")))
    monkeypatch.setattr(mod, "_eventlog", lambda *args, **kwargs: calls.append("eventlog"))
    monkeypatch.setenv("MAGI_FILE_REVIEW_PORTAL_PROBE_RETRIES", "1")
    monkeypatch.setenv("MAGI_FILE_REVIEW_PROBE_WITH_GMAIL", "0")

    result = mod.cmd_downloadable_probe(require_portal=True, read_only=True)

    assert result["success"] is False
    assert result["portal"]["success"] is False
    assert calls == ["close"]


def test_read_only_probe_missing_dependencies_has_no_side_effects(monkeypatch):
    mod = _load_action_module("file_review_action_read_only_dependencies_test")
    calls = []

    monkeypatch.setattr(mod, "_missing_runtime_deps", lambda: ["pymysql"])
    monkeypatch.setattr(mod, "_ensure_runtime_deps", lambda: (_ for _ in ()).throw(AssertionError("dependency bootstrap called")))
    monkeypatch.setattr(mod, "_load_config", lambda: (_ for _ in ()).throw(AssertionError("config loaded")))
    monkeypatch.setattr(mod, "_eventlog", lambda *args, **kwargs: calls.append("eventlog"))
    monkeypatch.setattr(mod, "_notify", lambda *args, **kwargs: calls.append("notify"))

    result = mod.cmd_downloadable_probe(read_only=True, notify=True)

    assert result["success"] is False
    assert result["error"] == "dependency_missing"
    assert result["missing_dependencies"] == ["pymysql"]
    assert calls == []


def test_read_only_preview_skips_eventlog_and_token_restore(monkeypatch, tmp_path):
    mod = _load_action_module("file_review_action_read_only_preview_test")
    calls = []

    class FakeManager:
        _last_gmail_error = "invalid_grant for /Users/example/token.pickle"

        def __init__(self, *args, **kwargs):
            pass

        def preview_recent_emails(self, **_kwargs):
            return []

        def close(self):
            calls.append("close")

    monkeypatch.setattr(mod, "_missing_runtime_deps", lambda: [])
    monkeypatch.setattr(mod, "_ensure_runtime_deps", lambda: (_ for _ in ()).throw(AssertionError("dependency bootstrap called")))
    monkeypatch.setattr(mod, "_eventlog", lambda *args, **kwargs: calls.append("eventlog"))
    monkeypatch.setattr(mod, "_restore_latest_token_backup", lambda *_args: (_ for _ in ()).throw(AssertionError("token restore called")))
    monkeypatch.setattr(mod, "_load_config", lambda: {})
    monkeypatch.setattr(mod, "_get_credentials", lambda _cfg: {"username": "u", "password": "p", "download_folder": str(tmp_path)})
    monkeypatch.setattr(mod, "_ensure_imports", lambda: SimpleNamespace(FileReviewManager=FakeManager))
    monkeypatch.setattr(mod, "_get_db_manager", lambda _cfg, **_kwargs: None)

    result = mod.cmd_preview_emails(read_only=True)

    assert result["success"] is False
    assert "invalid_grant" in result["error"]
    assert calls == ["close"]


def test_read_only_db_fallback_never_bootstraps_dependencies(monkeypatch):
    mod = _load_action_module("file_review_action_read_only_db_test")

    monkeypatch.setenv("MAGI_PREFER_LOCAL_DB", "1")
    monkeypatch.setattr(mod, "_pick_db_profiles", lambda _cfg: [])
    monkeypatch.setattr(mod, "_ensure_runtime_deps", lambda: (_ for _ in ()).throw(AssertionError("dependency bootstrap called")))

    assert mod._get_db_manager({}, read_only=True) is None


def test_read_only_preview_cli_bypasses_flow_ledger(monkeypatch):
    mod = _load_action_module("file_review_action_read_only_preview_cli_test")
    calls = []

    monkeypatch.setattr(mod, "cmd_preview_emails", lambda **kwargs: calls.append(kwargs) or {"success": True, "items": []})
    monkeypatch.setattr(mod, "_run_with_flow", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("flow ledger called")))
    monkeypatch.setattr(mod.sys, "argv", ["action.py", "--task", 'preview_emails {"read_only":true}'])

    assert mod.main() == 0
    assert calls == [{"days": 7, "read_only": True}]


def test_read_only_downloadable_probe_skips_gmail_by_default(monkeypatch, tmp_path):
    mod = _load_action_module("file_review_action_read_only_default_gmail_test")

    class FakeManager:
        def __init__(self, *args, **kwargs):
            pass

        def probe_downloadable_from_portal(self, **_kwargs):
            return {"success": True, "count": 0, "items": []}

        def close(self):
            pass

    monkeypatch.delenv("MAGI_FILE_REVIEW_PROBE_WITH_GMAIL", raising=False)
    monkeypatch.setattr(mod, "_missing_runtime_deps", lambda: [])
    monkeypatch.setattr(mod, "_load_config", lambda: {})
    monkeypatch.setattr(mod, "_get_credentials", lambda _cfg: {"username": "u", "password": "p", "download_folder": str(tmp_path)})
    monkeypatch.setattr(mod, "_ensure_portal_probe_imports", lambda: SimpleNamespace(FileReviewManager=FakeManager))
    monkeypatch.setattr(mod, "_get_db_manager", lambda _cfg, **_kwargs: None)
    monkeypatch.setattr(mod, "cmd_preview_emails", lambda **_kwargs: (_ for _ in ()).throw(AssertionError("Gmail called")))

    result = mod.cmd_downloadable_probe(require_portal=True, read_only=True)

    assert result["success"] is True
    assert result["gmail"]["success"] is False


def test_download_filter_rechecks_previously_downloaded_case_by_default(tmp_path, monkeypatch):
    mod = _load_action_module("file_review_action_incremental_filter_test")
    (tmp_path / "downloaded_registry.json").write_text(
        json.dumps(
            {
                "old_review.pdf": {
                    "yyidno": "115.上訴.000065",
                    "case_info": {"showyyidno": "115年度上訴字第65號"},
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    item = {
        "status": "downloadable",
        "case_number": "115.上訴.000065",
        "court_case_no": "115年度上訴字第65號",
        "rowid": "ROW-NEW",
    }

    monkeypatch.delenv("MAGI_ENABLE_CASE_LEVEL_DOWNLOAD_SKIP", raising=False)

    assert mod._filter_not_yet_downloaded([item], str(tmp_path)) == [item]

    monkeypatch.setenv("MAGI_ENABLE_CASE_LEVEL_DOWNLOAD_SKIP", "1")
    assert mod._filter_not_yet_downloaded([item], str(tmp_path)) == []


def test_download_filter_rechecks_clicked_rowid_by_default(tmp_path, monkeypatch):
    mod = _load_action_module("file_review_action_incremental_rowid_test")
    (tmp_path / "clicked_rowids.json").write_text(
        json.dumps({"ROW-OLD": {"first_clicked": "2026-07-01T10:00:00"}}, ensure_ascii=False),
        encoding="utf-8",
    )
    item = {
        "status": "downloadable",
        "case_number": "115.上訴.000065",
        "rowid": "ROW-OLD",
        "isdown": "Y",
        "downdt": "1150701",
    }

    monkeypatch.delenv("MAGI_ENABLE_BUTTON_LEVEL_DOWNLOAD_SKIP", raising=False)

    assert mod._filter_not_yet_downloaded([item], str(tmp_path)) == [item]

    monkeypatch.setenv("MAGI_ENABLE_BUTTON_LEVEL_DOWNLOAD_SKIP", "1")
    assert mod._filter_not_yet_downloaded([item], str(tmp_path)) == []


def test_invalid_csrf_token_is_transient_portal_failure():
    mod = _load_action_module("file_review_action_csrf_test")

    assert mod._is_transient_portal_probe_failure({"error_code": "invalid_csrf_token"})
    assert "CSRF" in mod._format_portal_probe_error({"error_code": "invalid_csrf_token"})


def test_file_review_manager_detects_invalid_csrf_text():
    from casper_ecosystem.law_firm_orchestrators.file_review_automation import FileReviewManager

    assert FileReviewManager._looks_like_invalid_csrf_text(
        "[下午5:10:41] 錯誤 forbidden: invalid CSRF token"
    )


def test_cleanup_old_downloads_removes_duplicate_quarantine_date_folders(tmp_path):
    mod = _load_action_module("file_review_action_cleanup_test")
    root = tmp_path / "閱卷下載"
    old = root / "_duplicate_downloads" / "20260601"
    fresh = root / "_duplicate_downloads" / "20260709"
    old.mkdir(parents=True)
    fresh.mkdir(parents=True)
    (old / "duplicate.pdf").write_bytes(b"%PDF old")
    (fresh / "duplicate.pdf").write_bytes(b"%PDF fresh")

    import os
    import time

    old_ts = time.time() - (16 * 86400)
    fresh_ts = time.time()
    os.utime(old, (old_ts, old_ts))
    os.utime(fresh, (fresh_ts, fresh_ts))
    mod._cleanup_old_downloads(str(root), max_days=15)

    assert not old.exists()
    assert fresh.exists()


def test_cleanup_old_downloads_removes_dated_staging_by_folder_name(tmp_path):
    mod = _load_action_module("file_review_action_cleanup_staging_test")
    root = tmp_path / "閱卷下載"
    old = root / "20000101"
    fresh = root / "29991231"
    old.mkdir(parents=True)
    fresh.mkdir(parents=True)
    (old / "stale.pdf").write_bytes(b"%PDF stale")
    (fresh / "fresh.pdf").write_bytes(b"%PDF fresh")

    summary = mod._cleanup_old_downloads(str(root), max_days=7)

    assert not old.exists()
    assert fresh.exists()
    assert summary["deleted"]
    assert summary["freed_bytes"] > 0


def test_cleanup_old_downloads_removes_pending_unarchived_after_longer_retention(tmp_path):
    mod = _load_action_module("file_review_action_cleanup_pending_test")
    root = tmp_path / "閱卷下載"
    old = root / "_待歸檔" / "20000101"
    fresh = root / "_待歸檔" / "29991231"
    old.mkdir(parents=True)
    fresh.mkdir(parents=True)
    (old / "unresolved.pdf").write_bytes(b"%PDF unresolved")
    (fresh / "new_unresolved.pdf").write_bytes(b"%PDF fresh")

    summary = mod._cleanup_old_downloads(str(root), max_days=7, pending_max_days=14)

    assert not old.exists()
    assert fresh.exists()
    assert any(row["reason"] == "pending_unarchived" for row in summary["deleted"])


def test_cleanup_old_downloads_dry_run_keeps_files(tmp_path):
    mod = _load_action_module("file_review_action_cleanup_dry_run_test")
    root = tmp_path / "閱卷下載"
    old = root / "20000101"
    old.mkdir(parents=True)
    (old / "stale.pdf").write_bytes(b"%PDF stale")

    summary = mod._cleanup_old_downloads(str(root), max_days=7, dry_run=True)

    assert old.exists()
    assert summary["deleted"] == []
    assert summary["would_delete"]
