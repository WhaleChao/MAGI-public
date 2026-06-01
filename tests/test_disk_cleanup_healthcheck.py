# -*- coding: utf-8 -*-
"""Tests for Layer 4 磁碟自動清理健檢."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

# 在 import script 前先把 runtime dir 隔離到 tmp，避免 polluting production
# （fixture 會在每個測試案例再覆寫一次，這裡只防 import 時的 side-effect）
os.environ.setdefault("MAGI_USE_RUNTIME_DIR", "1")

from scripts.ops import disk_cleanup_healthcheck as dc  # noqa: E402


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime"
    home = tmp_path / "home"
    magi = tmp_path / "magi"
    runtime.mkdir()
    home.mkdir()
    magi.mkdir()
    (home / ".omlx").mkdir()
    (magi / ".agent").mkdir()
    monkeypatch.setenv("MAGI_RUNTIME_DIR", str(runtime))
    monkeypatch.setenv("MAGI_USE_RUNTIME_DIR", "1")
    monkeypatch.setenv("HOME", str(home))
    # point the module-level MAGI_ROOT to sandbox
    monkeypatch.setattr(dc, "MAGI_ROOT", magi, raising=True)
    return {"runtime": runtime, "home": home, "magi": magi, "tmp": tmp_path}


# ---------- cleanup_metrics --------------------------------------------

def _write_jsonl(path: Path, lines: int, line_bytes: int = 200) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for i in range(lines):
            rec = {"i": i, "pad": "x" * line_bytes}
            f.write(json.dumps(rec) + "\n")


def test_metrics_rotate_triggers_when_over_threshold(sandbox, monkeypatch):
    metrics_dir = sandbox["runtime"] / "metrics"
    metrics_dir.mkdir(parents=True, exist_ok=True)
    huge = metrics_dir / "nvidia_nim_usage.jsonl"
    # 10MB 預設；寫 ~12MB
    monkeypatch.setenv("MAGI_DISK_METRICS_ROTATE_BYTES", str(1024 * 1024))  # 1MB to speed up
    # re-read env at module layer
    monkeypatch.setattr(dc, "METRICS_ROTATE_BYTES", 1024 * 1024, raising=True)
    monkeypatch.setattr(dc, "METRICS_KEEP_TAIL", 50, raising=True)
    _write_jsonl(huge, 20_000, line_bytes=200)
    before = huge.stat().st_size
    assert before > 1024 * 1024
    actions = dc.cleanup_metrics(dry_run=False)
    after = huge.stat().st_size
    assert after < before
    assert any(a["action"] == "rotate" and a.get("kept_lines") == 50 for a in actions)
    # 確認真的只留 tail
    with open(huge) as f:
        kept = f.readlines()
    assert len(kept) == 50
    assert json.loads(kept[-1])["i"] == 19_999


def test_metrics_dry_run_does_not_modify_file(sandbox, monkeypatch):
    metrics_dir = sandbox["runtime"] / "metrics"
    metrics_dir.mkdir(parents=True, exist_ok=True)
    f = metrics_dir / "external_chat_metrics.jsonl"
    monkeypatch.setattr(dc, "METRICS_ROTATE_BYTES", 1024, raising=True)
    _write_jsonl(f, 200, line_bytes=50)
    before = f.stat().st_size
    actions = dc.cleanup_metrics(dry_run=True)
    after = f.stat().st_size
    assert before == after
    assert actions
    assert all(a.get("dry_run") is True for a in actions)


def test_metrics_under_threshold_is_noop(sandbox, monkeypatch):
    metrics_dir = sandbox["runtime"] / "metrics"
    metrics_dir.mkdir(parents=True, exist_ok=True)
    f = metrics_dir / "small.jsonl"
    monkeypatch.setattr(dc, "METRICS_ROTATE_BYTES", 10 * 1024 * 1024, raising=True)
    _write_jsonl(f, 10, line_bytes=20)
    actions = dc.cleanup_metrics(dry_run=False)
    assert actions == []


def test_metrics_handles_nested_ocr_jsonl_dir(sandbox, monkeypatch):
    """Phase C OCR 的 bug: runtime_dir.metrics('ocr') 會被當 dir 用，裡頭有 *.jsonl。"""
    metrics_dir = sandbox["runtime"] / "metrics"
    nested = metrics_dir / "ocr.jsonl"
    nested.mkdir(parents=True, exist_ok=True)
    inner = nested / "pdf_ocr_consensus.jsonl"
    monkeypatch.setattr(dc, "METRICS_ROTATE_BYTES", 1024, raising=True)
    monkeypatch.setattr(dc, "METRICS_KEEP_TAIL", 5, raising=True)
    _write_jsonl(inner, 500, line_bytes=50)
    actions = dc.cleanup_metrics(dry_run=False)
    assert any(Path(a["path"]) == inner for a in actions)
    with open(inner) as f:
        assert len(f.readlines()) == 5


def test_metrics_protected_names_not_rotated(sandbox, monkeypatch):
    metrics_dir = sandbox["runtime"] / "metrics"
    metrics_dir.mkdir(parents=True, exist_ok=True)
    # 雖然 cron_state 不該住在 metrics/ 下，但作為防禦測試
    f = metrics_dir / "cron_state.jsonl"
    monkeypatch.setattr(dc, "METRICS_ROTATE_BYTES", 1024, raising=True)
    _write_jsonl(f, 2000, line_bytes=100)
    before = f.stat().st_size
    dc.cleanup_metrics(dry_run=False)
    after = f.stat().st_size
    assert before == after  # 受保護不變


# ---------- cleanup_omlx_cache -----------------------------------------

def _touch_with_atime(path: Path, age_seconds: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * 100)
    now = time.time()
    os.utime(path, (now - age_seconds, now - age_seconds))


def test_omlx_cache_removes_stale_files(sandbox, monkeypatch):
    cache = sandbox["home"] / ".omlx" / "cache-e4b"
    stale = cache / "model_blob_old"
    fresh = cache / "model_blob_new"
    _touch_with_atime(stale, 8 * 86400)   # 8 days old
    _touch_with_atime(fresh, 1 * 86400)   # 1 day old
    actions = dc.cleanup_omlx_cache(dry_run=False)
    assert not stale.exists()
    assert fresh.exists()
    info = next(a for a in actions if a["cache"].endswith("cache-e4b"))
    assert info["deleted_files"] == 1


def test_omlx_cache_dry_run_no_delete(sandbox, monkeypatch):
    cache = sandbox["home"] / ".omlx" / "cache-phi4"
    stale = cache / "old"
    _touch_with_atime(stale, 10 * 86400)
    dc.cleanup_omlx_cache(dry_run=True)
    assert stale.exists()


def test_omlx_cache_apply_respects_safety_cap(sandbox, monkeypatch):
    cache = sandbox["home"] / ".omlx" / "cache-e4b"
    stale = cache / "old"
    _touch_with_atime(stale, 10 * 86400)
    monkeypatch.setattr(dc, "OMLX_CACHE_MAX_DELETE_BYTES", 1, raising=True)
    actions = dc.cleanup_omlx_cache(dry_run=False)
    assert stale.exists()
    info = next(a for a in actions if a["cache"].endswith("cache-e4b"))
    assert info["skipped"] is True
    assert info["deleted_files"] == 0


def test_omlx_cache_hard_cap_prunes_recent_but_settled_cache(sandbox, monkeypatch):
    cache = sandbox["home"] / ".omlx" / "cache-26b"
    files = [cache / f"blob-{i}.safetensors" for i in range(4)]
    for idx, path in enumerate(files):
        _touch_with_atime(path, (idx + 1) * 86400)
    monkeypatch.setattr(dc, "_disk_free_gb", lambda path=dc.MAGI_ROOT: 100.0)
    monkeypatch.setattr(dc, "OMLX_CACHE_KEEP_DAYS", 99, raising=True)
    monkeypatch.setattr(dc, "OMLX_CACHE_CAP_GB", 250 / (1024 ** 3), raising=True)
    monkeypatch.setattr(dc, "OMLX_CACHE_MAX_DELETE_BYTES", 10_000, raising=True)

    actions = dc.cleanup_omlx_cache(dry_run=False)

    remaining = [p for p in files if p.exists()]
    assert len(remaining) <= 2
    info = next(a for a in actions if a["cache"].endswith("cache-26b"))
    assert info["candidate_files"] >= 2
    assert info["deleted_files"] >= 2


def test_omlx_cache_hard_cap_keeps_newly_written_files(sandbox, monkeypatch):
    cache = sandbox["home"] / ".omlx" / "cache-26b"
    fresh = cache / "fresh.safetensors"
    fresh.parent.mkdir(parents=True, exist_ok=True)
    fresh.write_bytes(b"x" * 1000)
    monkeypatch.setattr(dc, "_disk_free_gb", lambda path=dc.MAGI_ROOT: 100.0)
    monkeypatch.setattr(dc, "OMLX_CACHE_KEEP_DAYS", 99, raising=True)
    monkeypatch.setattr(dc, "OMLX_CACHE_CAP_GB", 1 / (1024 ** 3), raising=True)
    monkeypatch.setattr(dc, "OMLX_CACHE_RECENT_GRACE_MINUTES", 60, raising=True)

    actions = dc.cleanup_omlx_cache(dry_run=False)

    assert fresh.exists()
    info = next(a for a in actions if a["cache"].endswith("cache-26b"))
    assert info["deleted_files"] == 0


def test_rejected_distill_cleanup_removes_unlinked_failed_merged_model(sandbox, monkeypatch):
    distill = sandbox["home"] / ".omlx" / "training" / "gemma-distill"
    merged = distill / "merged" / "Gemma-gemma-distill-v004"
    merged.mkdir(parents=True)
    (merged / "config.json").write_text("{}", encoding="utf-8")
    (merged / "model.safetensors").write_bytes(b"x" * 1024)
    (distill / "pending_deploy.json").write_text(
        json.dumps({
            "version": "gemma-distill-v004",
            "status": "rejected",
            "deploy_allowed": False,
            "merged_path": str(merged),
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(dc, "DISTILL_REJECTED_MIN_AGE_HOURS", 0, raising=True)
    monkeypatch.setattr(dc, "DISTILL_REJECTED_LOW_WATER_GB", 70, raising=True)
    monkeypatch.setattr(dc, "_disk_free_gb", lambda path=dc.MAGI_ROOT: 20.0)

    actions = dc.cleanup_rejected_distill_models(dry_run=False)

    assert not merged.exists()
    assert actions[0]["deleted_dirs"] == 1


def test_rejected_distill_cleanup_preserves_deployed_symlink_target(sandbox, monkeypatch):
    distill = sandbox["home"] / ".omlx" / "training" / "gemma-distill"
    merged = distill / "merged" / "Gemma-gemma-distill-v004"
    merged.mkdir(parents=True)
    (merged / "config.json").write_text("{}", encoding="utf-8")
    (merged / "model.safetensors").write_bytes(b"x" * 1024)
    link_root = sandbox["home"] / ".omlx" / "models-text"
    link_root.mkdir(parents=True)
    (link_root / "gemma-distill-v004").symlink_to(merged)
    (distill / "pending_deploy.json").write_text(
        json.dumps({"version": "gemma-distill-v004", "status": "rejected", "deploy_allowed": False}),
        encoding="utf-8",
    )
    monkeypatch.setattr(dc, "DISTILL_REJECTED_MIN_AGE_HOURS", 0, raising=True)
    monkeypatch.setattr(dc, "DISTILL_REJECTED_LOW_WATER_GB", 70, raising=True)
    monkeypatch.setattr(dc, "_disk_free_gb", lambda path=dc.MAGI_ROOT: 20.0)

    actions = dc.cleanup_rejected_distill_models(dry_run=False)

    assert merged.exists()
    assert actions[0]["deleted_dirs"] == 0


# ---------- Synology Drive empty case shells ----------------------------

def test_cleanup_empty_synology_case_shells_removes_only_empty_case_roots(sandbox, monkeypatch):
    root = sandbox["tmp"] / "SynologyDrive-homes" / "01_案件"
    empty_case = root / "一般案件" / "民事" / "2026-0099-測試-一審-給付"
    real_case = root / "一般案件" / "民事" / "2026-0100-保留-一審-給付"
    non_case = root / "一般案件" / "民事" / "範本"
    (empty_case / "02_我方歷次書狀").mkdir(parents=True)
    (empty_case / "02_我方歷次書狀" / ".gitkeep").write_text("keep", encoding="utf-8")
    (real_case / "02_我方歷次書狀").mkdir(parents=True)
    (real_case / "02_我方歷次書狀" / "書狀.pdf").write_bytes(b"pdf")
    non_case.mkdir(parents=True)
    old = time.time() - 24 * 3600
    for p in (empty_case, real_case, non_case):
        os.utime(p, (old, old))

    monkeypatch.setenv("MAGI_DISK_SYNOLOGY_EMPTY_CASE_ROOTS", str(root))
    monkeypatch.setattr(dc, "SYNOLOGY_EMPTY_CASE_SHELL_MIN_AGE_HOURS", 0, raising=True)

    actions = dc.cleanup_empty_synology_case_shells(dry_run=False)

    assert not empty_case.exists()
    assert real_case.exists()
    assert non_case.exists()
    assert actions[0]["deleted_dirs"] == 1


def test_cleanup_empty_synology_case_shells_dry_run_preserves(sandbox, monkeypatch):
    root = sandbox["tmp"] / "SynologyDrive-homes" / "01_案件"
    empty_case = root / "一般案件" / "民事" / "2026-0099-測試-一審-給付"
    empty_case.mkdir(parents=True)
    monkeypatch.setenv("MAGI_DISK_SYNOLOGY_EMPTY_CASE_ROOTS", str(root))
    monkeypatch.setattr(dc, "SYNOLOGY_EMPTY_CASE_SHELL_MIN_AGE_HOURS", 0, raising=True)

    actions = dc.cleanup_empty_synology_case_shells(dry_run=True)

    assert empty_case.exists()
    assert actions[0]["candidate_dirs"] == 1


def test_cleanup_empty_synology_case_shells_removes_closed_shells_without_age_delay(sandbox, monkeypatch):
    root = sandbox["tmp"] / "SynologyDrive-homes" / "01_案件"
    closed_case = root / "一般案件" / "民事" / "2026-0099-測試-一審-給付"
    recent_open_case = root / "一般案件" / "民事" / "2026-0100-保留-一審-給付"
    closed_case.mkdir(parents=True)
    recent_open_case.mkdir(parents=True)

    monkeypatch.setenv("MAGI_DISK_SYNOLOGY_EMPTY_CASE_ROOTS", str(root))
    monkeypatch.setattr(dc, "SYNOLOGY_EMPTY_CASE_SHELL_MIN_AGE_HOURS", 24, raising=True)
    monkeypatch.setattr(dc, "_closed_case_numbers_for_shell_cleanup", lambda: {"2026-0099"})

    actions = dc.cleanup_empty_synology_case_shells(dry_run=False)

    assert not closed_case.exists()
    assert recent_open_case.exists()
    assert actions[0]["deleted_dirs"] == 1


def test_cleanup_empty_synology_case_shells_ignores_legacy_delete_guard(sandbox, monkeypatch):
    root = sandbox["tmp"] / "SynologyDrive-homes" / "01_案件"
    empty_case = root / "一般案件" / "民事" / "2026-0099-測試-一審-給付"
    (empty_case / "02_我方歷次書狀").mkdir(parents=True)
    (empty_case / "02_我方歷次書狀" / ".gitkeep").write_text("keep", encoding="utf-8")
    monkeypatch.setenv("MAGI_DISK_SYNOLOGY_EMPTY_CASE_ROOTS", str(root))
    monkeypatch.setenv("MAGI_NO_DELETE", "1")
    monkeypatch.setenv("MAGI_DB_NO_DELETE", "1")
    monkeypatch.setattr(dc, "SYNOLOGY_EMPTY_CASE_SHELL_MIN_AGE_HOURS", 0, raising=True)
    orig_unlink = os.unlink

    def guarded_unlink(path, *args, **kwargs):
        if os.environ.get("MAGI_NO_DELETE") == "1" and "01_案件" in str(path):
            raise PermissionError(f"legacy guard blocked {path}")
        return orig_unlink(path, *args, **kwargs)

    monkeypatch.setattr(dc.os, "unlink", guarded_unlink)

    actions = dc.cleanup_empty_synology_case_shells(dry_run=False)

    assert not empty_case.exists()
    assert actions[0]["deleted_dirs"] == 1
    assert os.environ.get("MAGI_NO_DELETE") == "1"
    assert os.environ.get("MAGI_DB_NO_DELETE") == "1"


def test_remove_tree_robust_tolerates_vanishing_metadata_file(tmp_path, monkeypatch):
    root = tmp_path / "01_案件" / "2026-0099-測試"
    root.mkdir(parents=True)
    ghost = root / "._.DS_Store"
    ghost.write_bytes(b"")
    orig_unlink = os.unlink

    def flaky_unlink(path, *args, **kwargs):
        if str(path).endswith("._.DS_Store"):
            orig_unlink(path, *args, **kwargs)
            raise FileNotFoundError(path)
        return orig_unlink(path, *args, **kwargs)

    monkeypatch.setattr(dc.os, "unlink", flaky_unlink)

    dc._remove_tree_robust(root)

    assert not root.exists()


def test_synology_empty_case_roots_default_to_smb_and_skip_file_provider(monkeypatch, tmp_path):
    monkeypatch.delenv("MAGI_DISK_SYNOLOGY_EMPTY_CASE_ROOTS", raising=False)
    monkeypatch.setattr(dc, "SYNOLOGY_EMPTY_CASE_SHELL_INCLUDE_LOCAL", False, raising=True)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("MAGI_NAS_HOME_USER", "lumi63181107")

    roots = [str(p) for p in dc._synology_drive_active_roots()]

    assert "/Volumes/homes/lumi63181107/01_案件" in roots
    assert str(tmp_path / ".magi_mounts/homes/lumi63181107/01_案件") in roots
    assert str(tmp_path / "Library/CloudStorage/SynologyDrive-homes/01_案件") not in roots


def test_synology_empty_case_roots_can_include_local_views(monkeypatch, tmp_path):
    monkeypatch.delenv("MAGI_DISK_SYNOLOGY_EMPTY_CASE_ROOTS", raising=False)
    monkeypatch.setattr(dc, "SYNOLOGY_EMPTY_CASE_SHELL_INCLUDE_LOCAL", True, raising=True)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("MAGI_NAS_HOME_USER", "lumi63181107")

    roots = [str(p) for p in dc._synology_drive_active_roots()]

    assert str(tmp_path / "Library/CloudStorage/SynologyDrive-homes/01_案件") in roots
    assert str(tmp_path / "SynologyDrive/homes/01_案件") in roots
    assert str(tmp_path / "SynologyDrive/01_案件") in roots
    assert "/Volumes/homes/lumi63181107/01_案件" in roots


# ---------- cleanup_tmp ------------------------------------------------

def test_tmp_cleanup_removes_old_magi_files(sandbox, monkeypatch, tmp_path):
    fake_tmp = tmp_path / "tmp"
    fake_tmp.mkdir()
    old = fake_tmp / "magi_debug.png"
    recent = fake_tmp / "magi_current.png"
    unrelated = fake_tmp / "other.png"
    for p in (old, recent, unrelated):
        p.write_bytes(b"x")
    now = time.time()
    os.utime(old, (now - 72 * 3600, now - 72 * 3600))
    os.utime(recent, (now - 1 * 3600, now - 1 * 3600))
    os.utime(unrelated, (now - 100 * 3600, now - 100 * 3600))
    # 讓 cleanup_tmp 改看 fake_tmp
    monkeypatch.setattr(dc, "Path", dc.Path)  # keep identity

    def _fake_iterdir(self):
        if str(self) == "/tmp":
            return iter(fake_tmp.iterdir())
        return iter([])

    # patch /tmp 路徑：用更直接的方法
    real_iterdir = dc.Path.iterdir

    def guarded(self):
        if str(self) == "/tmp":
            return (fake_tmp / p.name for p in fake_tmp.iterdir())
        return real_iterdir(self)

    monkeypatch.setattr(dc.Path, "iterdir", guarded, raising=False)
    # 最終 action
    actions = dc.cleanup_tmp(dry_run=False)
    assert not old.exists()
    assert recent.exists()
    assert unrelated.exists()   # 未以 magi_/omlx_ 開頭，不碰
    info = actions[0]
    assert info["deleted_count"] == 1


def test_tmp_cleanup_skips_protected_state_files(sandbox, monkeypatch, tmp_path):
    fake_tmp = tmp_path / "tmp2"
    fake_tmp.mkdir()
    protected = fake_tmp / "omlx_switch_alert.txt"
    protected.write_bytes(b"alert!")
    now = time.time()
    os.utime(protected, (now - 96 * 3600, now - 96 * 3600))
    real_iterdir = dc.Path.iterdir

    def guarded(self):
        if str(self) == "/tmp":
            return (fake_tmp / p.name for p in fake_tmp.iterdir())
        return real_iterdir(self)

    monkeypatch.setattr(dc.Path, "iterdir", guarded, raising=False)
    dc.cleanup_tmp(dry_run=False)
    assert protected.exists()  # 受保護


def test_tmp_cleanup_preserves_json_files(sandbox, monkeypatch, tmp_path):
    fake_tmp = tmp_path / "tmp_json"
    fake_tmp.mkdir()
    old_json = fake_tmp / "magi_standalone_state.json"
    old_log = fake_tmp / "magi_debug.log"
    for p in (old_json, old_log):
        p.write_bytes(b"x")
    now = time.time()
    os.utime(old_json, (now - 96 * 3600, now - 96 * 3600))
    os.utime(old_log, (now - 96 * 3600, now - 96 * 3600))
    real_iterdir = dc.Path.iterdir

    def guarded(self):
        if str(self) == "/tmp":
            return (fake_tmp / p.name for p in fake_tmp.iterdir())
        return real_iterdir(self)

    monkeypatch.setattr(dc.Path, "iterdir", guarded, raising=False)
    dc.cleanup_tmp(dry_run=False)
    assert old_json.exists()
    assert not old_log.exists()


# ---------- main pipeline ----------------------------------------------

def test_main_dry_run_writes_summary(sandbox, monkeypatch):
    monkeypatch.setenv("MAGI_DISK_CLEANUP_DRY_RUN", "1")
    rc = dc.main()
    assert rc == 0
    summary = sandbox["runtime"] / "metrics" / "disk_cleanup_summary.jsonl"
    assert summary.exists()
    lines = summary.read_text().splitlines()
    assert lines
    parsed = json.loads(lines[-1])
    assert parsed["dry_run"] is True
    assert "metrics" in parsed and "omlx_cache" in parsed
    assert "compressed_artifacts" in parsed
    assert "generated_staging" in parsed
    assert "nas_recycle" in parsed
    assert "nas_recycle_heavy" in parsed


# ---------- compression ------------------------------------------------

def test_runtime_compression_gzips_old_logs_and_skips_json(sandbox, monkeypatch):
    magi = sandbox["magi"]
    logs = magi / "logs"
    old_log = logs / "old.log"
    old_json = logs / "standalone_state.json"
    logs.mkdir(parents=True, exist_ok=True)
    old_log.write_bytes(b"x" * 200)
    old_json.write_bytes(b"{}" * 200)
    ts = time.time() - 5 * 86400
    os.utime(old_log, (ts, ts))
    os.utime(old_json, (ts, ts))
    monkeypatch.setattr(dc, "MAGI_ROOT", magi, raising=True)
    monkeypatch.setattr(dc, "_disk_free_gb", lambda path=dc.MAGI_ROOT: 100.0)
    monkeypatch.setattr(dc, "RUNTIME_COMPRESS_MIN_BYTES", 10, raising=True)
    monkeypatch.setattr(dc, "RUNTIME_COMPRESS_MAX_AGE_DAYS", 1, raising=True)

    actions = dc.compress_runtime_artifacts(dry_run=False)

    assert actions
    assert not old_log.exists()
    assert (logs / "old.log.gz").exists()
    assert old_json.exists()


# ---------- generated staging -----------------------------------------

def test_generated_staging_cleanup_removes_old_exports_but_preserves_json(sandbox, monkeypatch):
    magi = sandbox["magi"]
    exports = magi / "exports"
    old_docx = exports / "summary.docx"
    fresh_pdf = exports / "fresh.pdf"
    state_json = exports / "paperclip_state.json"
    exports.mkdir(parents=True, exist_ok=True)
    for path in (old_docx, fresh_pdf, state_json):
        path.write_bytes(b"x" * 20)
    now = time.time()
    os.utime(old_docx, (now - 5 * 86400, now - 5 * 86400))
    os.utime(fresh_pdf, (now - 1 * 3600, now - 1 * 3600))
    os.utime(state_json, (now - 5 * 86400, now - 5 * 86400))
    monkeypatch.setattr(dc, "MAGI_ROOT", magi, raising=True)
    monkeypatch.setattr(dc, "EXPORT_OUTPUT_MAX_AGE_DAYS", 3, raising=True)

    actions = dc.cleanup_generated_staging(dry_run=False)

    assert not old_docx.exists()
    assert fresh_pdf.exists()
    assert state_json.exists()
    exports_info = next(a for a in actions if a["label"] == "exports")
    assert exports_info["deleted_files"] == 1


def test_generated_staging_cleanup_includes_module_download_staging(sandbox, monkeypatch):
    magi = sandbox["magi"]
    review = magi / "閱卷下載"
    old_pdf = review / "duplicate_payment.pdf"
    old_pdf.parent.mkdir(parents=True, exist_ok=True)
    old_pdf.write_bytes(b"x" * 20)
    ts = time.time() - 20 * 86400
    os.utime(old_pdf, (ts, ts))
    monkeypatch.setattr(dc, "MAGI_ROOT", magi, raising=True)
    monkeypatch.setattr(dc, "MODULE_STAGING_MAX_AGE_DAYS", 14, raising=True)

    actions = dc.cleanup_generated_staging(dry_run=False)

    assert not old_pdf.exists()
    review_info = next(a for a in actions if a["label"] == "file_review_staging")
    assert review_info["deleted_files"] == 1


def test_main_enforce_mode_flag_read(sandbox, monkeypatch):
    monkeypatch.setenv("MAGI_DISK_CLEANUP_DRY_RUN", "0")
    assert dc._is_dry_run() is False
    monkeypatch.setenv("MAGI_DISK_CLEANUP_DRY_RUN", "1")
    assert dc._is_dry_run() is True


def test_main_apply_arg_overrides_env_dry_run(sandbox, monkeypatch):
    monkeypatch.setenv("MAGI_DISK_CLEANUP_DRY_RUN", "1")
    calls = []
    monkeypatch.setattr(dc, "cleanup_metrics", lambda dry_run: calls.append(dry_run) or [])
    monkeypatch.setattr(dc, "cleanup_omlx_cache", lambda dry_run: [])
    monkeypatch.setattr(dc, "cleanup_rejected_distill_models", lambda dry_run: [])
    monkeypatch.setattr(dc, "cleanup_tmp", lambda dry_run: [{"candidate_count": 0}])
    monkeypatch.setattr(dc, "cleanup_db_backups", lambda dry_run: [])
    monkeypatch.setattr(dc, "cleanup_build_artifacts", lambda dry_run: [])
    monkeypatch.setattr(dc, "cleanup_stale_git_tmp_packs", lambda dry_run: [])
    monkeypatch.setattr(dc, "cleanup_nas_recycle", lambda dry_run: [])
    monkeypatch.setattr(dc, "cleanup_nas_recycle_heavy", lambda dry_run: [])
    monkeypatch.setattr(dc, "report_agent_logs", lambda dry_run: [])
    assert dc.main(["--apply"]) == 0
    assert calls == [False]


def test_db_backup_cleanup_keeps_latest_per_kind(sandbox, monkeypatch):
    backup_dir = sandbox["magi"] / "_db_backups" / "law_firm_data"
    backup_dir.mkdir(parents=True)
    now = time.time()
    files = []
    for i in range(5):
        f = backup_dir / f"law_firm_data_local_20260511_12000{i}.sql.gz"
        f.write_bytes(b"x" * (i + 1))
        Path(str(f) + ".meta.json").write_text("{}", encoding="utf-8")
        os.utime(f, (now + i, now + i))
        files.append(f)
    remote = backup_dir / "law_firm_data_remote_20260511_120000.sql.gz"
    remote.write_bytes(b"remote")
    monkeypatch.setattr(dc, "DB_BACKUP_KEEP_LATEST", 2, raising=True)

    actions = dc.cleanup_db_backups(dry_run=False)

    remaining = sorted(p.name for p in backup_dir.glob("*.sql.gz"))
    assert remaining == [
        "law_firm_data_local_20260511_120003.sql.gz",
        "law_firm_data_local_20260511_120004.sql.gz",
        "law_firm_data_remote_20260511_120000.sql.gz",
    ]
    assert not Path(str(files[0]) + ".meta.json").exists()
    local = next(a for a in actions if a["label"] == "local")
    assert local["deleted_files"] == 3


def test_build_artifact_cleanup_removes_when_disk_low(sandbox, monkeypatch):
    artifact = sandbox["magi"] / "dist" / "Paperclip.app"
    artifact.mkdir(parents=True)
    (artifact / "binary").write_bytes(b"x" * 100)
    monkeypatch.setattr(dc, "BUILD_ARTIFACT_CLEANUP_ENABLE", True, raising=True)
    monkeypatch.setattr(dc, "BUILD_ARTIFACT_LOW_WATER_GB", 20, raising=True)
    monkeypatch.setattr(dc, "_disk_free_gb", lambda _path: 5.0)

    actions = dc.cleanup_build_artifacts(dry_run=False)

    assert not artifact.exists()
    assert any(a["deleted"] is True and a["low_water"] is True for a in actions)


def test_build_artifact_cleanup_preserves_standalone_json(sandbox, monkeypatch):
    artifact = sandbox["magi"] / "dist" / "Paperclip.app"
    data = artifact / "Contents" / "Resources" / "holidays_config.json"
    data.parent.mkdir(parents=True, exist_ok=True)
    data.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(dc, "BUILD_ARTIFACT_CLEANUP_ENABLE", True, raising=True)
    monkeypatch.setattr(dc, "BUILD_ARTIFACT_LOW_WATER_GB", 20, raising=True)
    monkeypatch.setattr(dc, "_disk_free_gb", lambda _path: 5.0)

    actions = dc.cleanup_build_artifacts(dry_run=False)

    assert artifact.exists()
    assert any(
        a.get("skipped") is True and a.get("reason") == "contains_preserved_standalone_content"
        for a in actions
    )


def test_git_tmp_pack_cleanup_removes_stale_temp_packs(sandbox, monkeypatch):
    pack_dir = sandbox["magi"] / ".git" / "objects" / "pack"
    pack_dir.mkdir(parents=True)
    old = pack_dir / "tmp_pack_old"
    fresh = pack_dir / "tmp_pack_fresh"
    keep = pack_dir / "pack-real.pack"
    for p in (old, fresh, keep):
        p.write_bytes(b"x" * 10)
    now = time.time()
    os.utime(old, (now - 48 * 3600, now - 48 * 3600))
    os.utime(fresh, (now, now))
    os.utime(keep, (now - 48 * 3600, now - 48 * 3600))
    monkeypatch.setattr(dc, "GIT_TMP_PACK_CLEANUP_ENABLE", True, raising=True)
    monkeypatch.setattr(dc, "GIT_TMP_PACK_MAX_AGE_HOURS", 24, raising=True)
    monkeypatch.setattr(dc, "_git_tmp_pack_roots", lambda: [sandbox["magi"]])
    monkeypatch.setattr(dc, "_git_process_running", lambda: False)

    actions = dc.cleanup_stale_git_tmp_packs(dry_run=False)

    assert not old.exists()
    assert fresh.exists()
    assert keep.exists()
    assert actions[0]["deleted_files"] == 1


# ---------- NAS recycle cleanup ----------------------------------------

def test_nas_recycle_cleanup_requires_explicit_enable(sandbox, monkeypatch):
    monkeypatch.setattr(dc, "NAS_RECYCLE_CLEANUP_ENABLE", False, raising=True)

    actions = dc.cleanup_nas_recycle(dry_run=False)

    assert actions == [{"enabled": False, "reason": "MAGI_DISK_NAS_RECYCLE_ENABLE=0"}]


def test_nas_recycle_cleanup_removes_only_old_recycle_items(sandbox, monkeypatch):
    recycle = sandbox["tmp"] / "#recycle"
    old = recycle / "old.pdf"
    fresh = recycle / "fresh.pdf"
    recycle.mkdir(parents=True, exist_ok=True)
    old.write_bytes(b"old")
    fresh.write_bytes(b"fresh")
    now = time.time()
    os.utime(old, (now - 20 * 86400, now - 20 * 86400))
    os.utime(fresh, (now - 1 * 86400, now - 1 * 86400))
    monkeypatch.setattr(dc, "NAS_RECYCLE_CLEANUP_ENABLE", True, raising=True)
    monkeypatch.setattr(dc, "NAS_RECYCLE_ALLOW_NON_VOLUME", True, raising=True)
    monkeypatch.setattr(dc, "NAS_RECYCLE_MAX_AGE_DAYS", 14, raising=True)
    monkeypatch.setattr(dc, "NAS_RECYCLE_MAX_DELETE_ITEMS", 50, raising=True)
    monkeypatch.setenv("MAGI_DISK_NAS_RECYCLE_ROOTS", str(recycle))

    actions = dc.cleanup_nas_recycle(dry_run=False)

    assert not old.exists()
    assert fresh.exists()
    assert actions[0]["candidate_items"] == 1
    assert actions[0]["deleted_items"] == 1


def test_nas_recycle_cleanup_respects_delete_cap(sandbox, monkeypatch):
    recycle = sandbox["tmp"] / "#recycle"
    recycle.mkdir(parents=True, exist_ok=True)
    now = time.time()
    files = []
    for idx in range(3):
        p = recycle / f"old-{idx}.pdf"
        p.write_bytes(b"x")
        os.utime(p, (now - 20 * 86400 - idx, now - 20 * 86400 - idx))
        files.append(p)
    monkeypatch.setattr(dc, "NAS_RECYCLE_CLEANUP_ENABLE", True, raising=True)
    monkeypatch.setattr(dc, "NAS_RECYCLE_ALLOW_NON_VOLUME", True, raising=True)
    monkeypatch.setattr(dc, "NAS_RECYCLE_MAX_AGE_DAYS", 14, raising=True)
    monkeypatch.setattr(dc, "NAS_RECYCLE_MAX_DELETE_ITEMS", 1, raising=True)
    monkeypatch.setenv("MAGI_DISK_NAS_RECYCLE_ROOTS", str(recycle))

    actions = dc.cleanup_nas_recycle(dry_run=False)

    assert sum(not p.exists() for p in files) == 1
    assert actions[0]["candidate_items"] == 3
    assert actions[0]["deleted_items"] == 1
    assert actions[0]["stopped_reason"] == "max_delete_items_reached"


def test_nas_recycle_cleanup_reports_heavy_backup_without_deleting(sandbox, monkeypatch):
    recycle = sandbox["tmp"] / "#recycle"
    backup = recycle / "Backup"
    old_file = backup / "old.bin"
    old_file.parent.mkdir(parents=True, exist_ok=True)
    old_file.write_bytes(b"x")
    ts = time.time() - 30 * 86400
    os.utime(backup, (ts, ts))
    os.utime(old_file, (ts, ts))
    monkeypatch.setattr(dc, "NAS_RECYCLE_CLEANUP_ENABLE", True, raising=True)
    monkeypatch.setattr(dc, "NAS_RECYCLE_ALLOW_NON_VOLUME", True, raising=True)
    monkeypatch.setattr(dc, "NAS_RECYCLE_MAX_AGE_DAYS", 14, raising=True)
    monkeypatch.setattr(dc, "NAS_RECYCLE_MAX_DELETE_ITEMS", 50, raising=True)
    monkeypatch.setenv("MAGI_DISK_NAS_RECYCLE_ROOTS", str(recycle))

    actions = dc.cleanup_nas_recycle(dry_run=False)

    assert backup.exists()
    assert actions[0]["candidate_items"] == 0
    assert actions[0]["skipped_heavy_items"] == 1
    assert str(backup) in actions[0]["skipped_heavy_paths"]


def test_nas_recycle_heavy_requires_explicit_enable(sandbox, monkeypatch):
    monkeypatch.setattr(dc, "NAS_RECYCLE_HEAVY_CLEANUP_ENABLE", False, raising=True)

    actions = dc.cleanup_nas_recycle_heavy(dry_run=False)

    assert actions == [{"enabled": False, "reason": "MAGI_DISK_NAS_RECYCLE_HEAVY_ENABLE=0"}]


def test_nas_recycle_heavy_deletes_files_incrementally(sandbox, monkeypatch):
    recycle = sandbox["tmp"] / "#recycle"
    backup = recycle / "Backup"
    files = [backup / f"old-{idx}.bin" for idx in range(3)]
    for path in files:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * 10)
    ts = time.time() - 30 * 86400
    for path in [backup, *files]:
        os.utime(path, (ts, ts))
    monkeypatch.setattr(dc, "NAS_RECYCLE_HEAVY_CLEANUP_ENABLE", True, raising=True)
    monkeypatch.setattr(dc, "NAS_RECYCLE_ALLOW_NON_VOLUME", True, raising=True)
    monkeypatch.setattr(dc, "NAS_RECYCLE_MAX_AGE_DAYS", 14, raising=True)
    monkeypatch.setattr(dc, "NAS_RECYCLE_HEAVY_MAX_FILES", 2, raising=True)
    monkeypatch.setattr(dc, "NAS_RECYCLE_HEAVY_MAX_RUNTIME_SEC", 60, raising=True)
    monkeypatch.setattr(dc, "NAS_RECYCLE_HEAVY_MAX_DELETE_BYTES", 10_000, raising=True)
    monkeypatch.setenv("MAGI_DISK_NAS_RECYCLE_ROOTS", str(recycle))

    actions = dc.cleanup_nas_recycle_heavy(dry_run=False)
    info = actions[0]

    assert info["candidate_items"] == 1
    assert info["processed_items"] == 1
    assert info["deleted_files"] == 2
    assert info["deleted_bytes"] == 20
    assert info["stopped_reason"] == "max_files_reached"
    assert sum(not path.exists() for path in files) == 2
    assert backup.exists()


def test_nas_recycle_heavy_dry_run_does_not_descend_or_delete(sandbox, monkeypatch):
    recycle = sandbox["tmp"] / "#recycle"
    steam = recycle / "SteamLibrary"
    old = steam / "steamapps" / "big.dat"
    old.parent.mkdir(parents=True, exist_ok=True)
    old.write_bytes(b"x")
    ts = time.time() - 30 * 86400
    os.utime(steam, (ts, ts))
    os.utime(old, (ts, ts))
    monkeypatch.setattr(dc, "NAS_RECYCLE_HEAVY_CLEANUP_ENABLE", True, raising=True)
    monkeypatch.setattr(dc, "NAS_RECYCLE_ALLOW_NON_VOLUME", True, raising=True)
    monkeypatch.setattr(dc, "NAS_RECYCLE_MAX_AGE_DAYS", 14, raising=True)
    monkeypatch.setenv("MAGI_DISK_NAS_RECYCLE_ROOTS", str(recycle))

    actions = dc.cleanup_nas_recycle_heavy(dry_run=True)

    assert old.exists()
    assert actions[0]["candidate_items"] == 1
    assert actions[0]["processed_items"] == 1
    assert actions[0]["items"][0]["would_process"] is True
