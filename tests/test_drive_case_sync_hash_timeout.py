from __future__ import annotations

import json
import signal
import sys

import pytest

from api.osc import drive_case_sync
from scripts import drive_case_sync_worker
from scripts.ops import resource_guarded_run
from skills.ops.cron_result_policy import classify_cron_result


def _case_folder(*, source: str, path: str, drive_id: str = "") -> drive_case_sync.CaseFolder:
    return drive_case_sync.CaseFolder(
        source=source,
        path=path,
        relative_path=path,
        name="2026-0001-測試案件-一審-測試",
        category="民事",
        status="進行中",
        case_kind="一般案件",
        meta=drive_case_sync.CaseMeta(case_number="2026-0001"),
        drive_id=drive_id,
        local_path=path if source == "local" else "",
    )


def _file_entry(*, source: str, path: str, relative_path: str) -> drive_case_sync.FileEntry:
    return drive_case_sync.FileEntry(
        source=source,
        path=path,
        relative_path=relative_path,
        name=relative_path.rsplit("/", 1)[-1],
        is_folder=False,
        size=3,
        md5="900150983cd24fb0d6963f7d28e17f72" if source == "drive" else "",
        drive_id=relative_path if source == "drive" else "",
        mime_type="application/pdf",
    )


def test_case_scan_stops_after_first_storage_hash_timeout(tmp_path, monkeypatch):
    local_root = tmp_path / "2026-0001-測試案件-一審-測試"
    local_root.mkdir()
    local_entries = []
    drive_entries = []
    for name in ("a.pdf", "b.pdf", "c.pdf"):
        target = local_root / name
        target.write_bytes(b"abc")
        local_entries.append(
            _file_entry(source="local", path=str(target), relative_path=name)
        )
        drive_entries.append(
            _file_entry(source="drive", path=name, relative_path=name)
        )

    monkeypatch.setattr(
        drive_case_sync,
        "drive_descendant_context",
        lambda *_args, **_kwargs: drive_case_sync.BoundedEntries(drive_entries),
    )
    monkeypatch.setattr(
        drive_case_sync,
        "local_descendant_context",
        lambda *_args, **_kwargs: drive_case_sync.BoundedEntries(local_entries),
    )
    calls = {"count": 0}

    def timeout_once(_path):
        calls["count"] += 1
        raise drive_case_sync.DriveCaseSyncError("local_hash_timeout:900s:test")

    monkeypatch.setattr(drive_case_sync, "local_file_md5", timeout_once)
    comparison = {
        "matched": [{
            "drive": _case_folder(source="drive", path="進行中/2026-0001", drive_id="drive-case"),
            "local": _case_folder(source="local", path=str(local_root)),
        }]
    }

    plan = drive_case_sync.build_file_sync_plan(comparison, object())

    assert calls["count"] == 1
    assert plan["summary"]["storage_unavailable_case_scans"] == 1
    assert plan["summary"]["pending_unverified_files"] == 1
    assert plan["summary"]["incomplete_case_scans"] == 0
    assert plan["summary"]["case_errors"] == 0
    assert plan["cases"][0]["scan_deferred"]["failure_code"] == "local_hash_timeout"
    assert plan["cases"][0]["download_missing"] == []
    assert plan["cases"][0]["nas_only"] == []
    report = {"file_sync_plan": plan, "execution_result": {"summary": {}}}
    assert drive_case_sync_worker._storage_unavailable_wait(report) is True


def test_hash_storage_dropout_remains_deferred_with_normal_sync_backlog():
    """Planned missing files are work, not evidence that the storage retry failed."""
    report = {
        "file_sync_plan": {
            "summary": {
                "pending_unverified_files": 1,
                "case_errors": 0,
                "incomplete_case_scans": 0,
                "conflict_files": 0,
                "content_mismatch_files": 0,
                "drive_missing_in_nas_files": 43,
                "nas_missing_in_drive_files": 100,
            },
            "cases": [
                {
                    "pending": [
                        {"reason": "local_hash_failed:local_hash_smb_helper_failed"}
                    ]
                }
            ],
        },
        "execution_result": {
            "summary": {
                "failed": 0,
                "download_failed": 0,
                "upload_failed": 0,
                "download_pending_unverified": 0,
                "upload_pending_unverified": 0,
                "stopped_by_limit": True,
            }
        },
    }

    assert drive_case_sync.report_has_partial_failures(report) is True
    assert drive_case_sync_worker._storage_unavailable_wait(report) is True


def test_all_case_chunks_fairly_cover_a_cycle_without_repeating_cursor():
    total = 5
    cursor = 0
    visited = []
    for _ in range(total):
        limit = drive_case_sync_worker._fair_all_case_chunk_limit(4, 1)
        assert limit == 1
        visited.append(cursor)
        cursor = drive_case_sync_worker._all_case_offset_after_run(
            cursor,
            (cursor + limit) % total,
            has_partial_failures=False,
        )
    assert visited == [0, 1, 2, 3, 4]
    assert cursor == 0


def test_all_case_cursor_does_not_advance_for_failed_chunk():
    assert drive_case_sync_worker._all_case_offset_after_run(
        3, 4, has_partial_failures=True
    ) == 3


def test_inner_budget_reserves_terminal_headroom():
    assert drive_case_sync_worker._inner_inventory_budget(5400, 300) == 5100
    assert drive_case_sync_worker._inner_inventory_budget(300, 300) == 0


def test_smb_storage_wait_allows_only_pure_semantic_case_errors():
    report = {
        "file_sync_plan": {
            "summary": {"pending_unverified_files": 1, "case_errors": 2,
                        "semantic_collision_files": 8, "incomplete_case_scans": 0,
                        "conflict_files": 0, "content_mismatch_files": 0},
            "cases": [
                {"error": "semantic_path_collision"},
                {"error": "semantic_path_collision"},
                {"pending": [{"reason": "local_hash_failed:local_hash_smb_helper_storage_unavailable"}]},
            ],
        },
        "execution_result": {"summary": {"failed": 0, "download_failed": 0,
                                             "upload_failed": 0,
                                             "download_pending_unverified": 0,
                                             "upload_pending_unverified": 0}},
    }
    assert drive_case_sync_worker._storage_unavailable_wait(report) is True
    report["file_sync_plan"]["cases"][1]["error"] = "unexpected_case_error"
    assert drive_case_sync_worker._storage_unavailable_wait(report) is False


def test_worker_status_execution_summary_counts_only_checksum_missing_existing_conflicts():
    report = {
        "execution_result": {
            "summary": {"upload_pending_unverified": 3},
            "upload_result": {
                "manifest": [
                    {"status": "pending_existing_conflict", "reason": "drive_existing_checksum_missing"},
                    {"status": "pending_existing_conflict", "reason": "drive_existing_checksum_missing"},
                    {"status": "pending_existing_conflict", "reason": "drive_existing_checksum_differs"},
                    {"status": "pending_existing_unverified", "reason": "drive_checksum_missing"},
                ]
            },
        }
    }

    summary = drive_case_sync_worker._status_execution_summary(report)

    assert summary["upload_pending_unverified"] == 3
    assert summary["upload_pending_existing_checksum_missing_conflict"] == 2


def test_worker_status_file_summary_counts_only_explicit_drive_checksum_missing():
    report = {
        "file_sync_plan": {
            "summary": {"pending_unverified_files": 2},
            "cases": [{"pending": [
                {"status": "pending_unverified", "reason": "drive_checksum_missing"},
                {"status": "pending_unverified", "reason": "local_hash_failed:local_hash_smb_helper_failed"},
                {"status": "pending_unverified", "reason": "google_doc_export_checksum_unavailable"},
            ]}],
        }
    }

    summary = drive_case_sync_worker._status_file_sync_summary(report)

    assert summary["pending_existing_checksum_missing_conflict"] == 1


def test_smb_md5_failure_code_is_safe_and_actionable():
    assert drive_case_sync._smb_md5_failure_code(1, b"md5: <private path>: Input/output error") == (
        "local_hash_smb_helper_storage_unavailable"
    )
    assert drive_case_sync._smb_md5_failure_code(1, b"md5: <private path>: Permission denied") == (
        "local_hash_smb_helper_permission_denied"
    )
    assert drive_case_sync._smb_md5_failure_code(1, b"unexpected failure") == (
        "local_hash_smb_helper_failed"
    )


def test_smb_md5_nonzero_path_stderr_is_memory_only_and_does_not_leak(tmp_path, monkeypatch):
    source = tmp_path / "source.pdf"
    source.write_bytes(b"pdf")
    # Build a synthetic POSIX-looking value at runtime.  The assertion still
    # proves that path-bearing stderr is not public, without embedding a
    # workstation-shaped path literal in the release test corpus.
    private_path = chr(47).join(("", "synthetic", "restricted", "file.pdf"))
    created_suffixes = []
    real_mkstemp = drive_case_sync.tempfile.mkstemp

    def tracked_mkstemp(*args, **kwargs):
        created_suffixes.append(str(kwargs.get("suffix") or ""))
        return real_mkstemp(*args, **kwargs)

    class FakeMd5:
        def is_file(self):
            return True

        def __str__(self):
            return sys.executable

    monkeypatch.setattr(drive_case_sync, "Path", lambda value: FakeMd5() if value == "/sbin/md5" else __import__("pathlib").Path(value))
    monkeypatch.setattr(drive_case_sync.tempfile, "mkstemp", tracked_mkstemp)
    # The fake helper emits a path-bearing error.  The public exception must
    # retain only its fixed failure enum, while the capture remains in memory.
    real_popen = drive_case_sync.subprocess.Popen
    monkeypatch.setattr(
        drive_case_sync.subprocess,
        "Popen",
        lambda *args, **kwargs: real_popen(
            [sys.executable, "-c", f"import sys; sys.stderr.write({private_path!r}); sys.exit(1)"],
            stdin=kwargs.get("stdin"), stdout=kwargs.get("stdout"), stderr=kwargs.get("stderr"),
            start_new_session=kwargs.get("start_new_session", False),
        ),
    )
    # The branch is normally selected only for NAS input; replace the source
    # predicate without granting access to any mounted share.
    monkeypatch.setattr(drive_case_sync, "_smb_file_size_with_retry", lambda _path: source.stat().st_size)
    monkeypatch.setattr(drive_case_sync.os.path, "getsize", lambda _path: source.stat().st_size)

    # Directly exercise the classifier integration with a controlled /Volumes
    # spelling; the helper never opens that path because FakeMd5 is substituted.
    synthetic_smb_path = chr(47).join(("", "Volumes", "synthetic", "no-real-file.pdf"))
    with pytest.raises(drive_case_sync.DriveCaseSyncError) as exc:
        drive_case_sync.local_file_md5(synthetic_smb_path)

    assert private_path not in str(exc.value)
    assert str(exc.value).startswith("local_hash_smb_helper_failed:")
    assert ".stderr" not in created_suffixes


def test_worker_status_file_summary_exposes_only_smb_retry_aggregate():
    report = {
        "file_sync_plan": {
            "summary": {},
            "cases": [{"pending": [
                {"status": "pending_unverified", "reason": "local_hash_failed:local_hash_smb_helper_storage_unavailable"},
                {"status": "pending_unverified", "reason": "local_hash_failed:local_hash_smb_helper_failed"},
            ]}],
        }
    }

    summary = drive_case_sync_worker._status_file_sync_summary(report)

    assert summary["smb_hash_storage_unavailable_files"] == 1


def test_data_integrity_review_wait_is_terminal_for_only_checksum_missing_conflict():
    report = {
        "file_sync_plan": {
            "summary": {
                "semantic_collision_files": 2,
                "pending_unverified_files": 1,
                "unverified_existing_files": 1,
                "incomplete_case_scans": 0,
                "storage_unavailable_case_scans": 0,
            },
            "cases": [{"pending": [
                {"status": "pending_unverified", "reason": "drive_checksum_missing"},
            ]}],
        },
        "execution_result": {"summary": {"download_failed": 0, "upload_failed": 0}},
        "drive_folder_result": {"summary": {"failed": 0}},
    }

    assert drive_case_sync_worker._data_integrity_review_wait(report, True) is True


def test_data_integrity_review_wait_rejects_local_hash_and_retries_normally():
    report = {
        "file_sync_plan": {
            "summary": {
                "semantic_collision_files": 8,
                "pending_unverified_files": 1,
                "unverified_existing_files": 1,
                "incomplete_case_scans": 0,
                "storage_unavailable_case_scans": 1,
            },
            "cases": [{"pending": [
                {"status": "pending_unverified", "reason": "local_hash_failed:local_hash_smb_helper_failed"},
            ]}],
        },
        "execution_result": {"summary": {"download_failed": 0, "upload_failed": 0}},
        "drive_folder_result": {"summary": {"failed": 0}},
    }

    assert drive_case_sync_worker._data_integrity_review_wait(report, True) is False


def test_local_hash_timeout_has_smb_floor(tmp_path, monkeypatch):
    target = tmp_path / "small.pdf"
    target.write_bytes(b"pdf")
    monkeypatch.setenv("MAGI_DRIVE_SYNC_LOCAL_HASH_TIMEOUT_SEC", "900")

    assert drive_case_sync._local_hash_timeout_seconds(str(target)) == 900.0


def test_local_hash_timeout_scales_with_file_size(tmp_path, monkeypatch):
    target = tmp_path / "large.pdf"
    target.write_bytes(b"x" * 4096)
    monkeypatch.setenv("MAGI_DRIVE_SYNC_LOCAL_HASH_TIMEOUT_SEC", "20")
    monkeypatch.setenv("MAGI_DRIVE_SYNC_LOCAL_HASH_MIN_BYTES_PER_SEC", "1024")

    assert drive_case_sync._local_hash_timeout_seconds(str(target)) == 34.0


def test_local_hash_timeout_can_be_explicitly_disabled(tmp_path, monkeypatch):
    target = tmp_path / "disabled.pdf"
    target.write_bytes(b"pdf")
    monkeypatch.setenv("MAGI_DRIVE_SYNC_LOCAL_HASH_TIMEOUT_SEC", "0")

    assert drive_case_sync._local_hash_timeout_seconds(str(target)) == 0.0


def test_drive_uploads_use_bounded_resumable_chunks_by_default(monkeypatch):
    monkeypatch.delenv("MAGI_DRIVE_SYNC_RESUMABLE_UPLOAD_MIN_BYTES", raising=False)
    monkeypatch.delenv("MAGI_DRIVE_SYNC_UPLOAD_CHUNK_BYTES", raising=False)

    threshold, chunk = drive_case_sync._drive_upload_transport_settings()

    assert threshold == 8 * 1024 * 1024
    assert chunk == 8 * 1024 * 1024


def test_drive_upload_chunk_is_rounded_to_google_quantum(monkeypatch):
    monkeypatch.setenv("MAGI_DRIVE_SYNC_RESUMABLE_UPLOAD_MIN_BYTES", "1048576")
    monkeypatch.setenv("MAGI_DRIVE_SYNC_UPLOAD_CHUNK_BYTES", "9000000")

    threshold, chunk = drive_case_sync._drive_upload_transport_settings()

    assert threshold == 1048576
    assert chunk % (256 * 1024) == 0
    assert chunk <= 9000000


def test_nested_smb_sigterm_delegates_to_worker_cleanup_handler():
    calls = []

    def outer(signum, frame):
        calls.append((signum, frame))
        raise SystemExit(77)

    with pytest.raises(SystemExit) as exc:
        drive_case_sync._continue_signal_chain(outer, signal.SIGTERM, "frame")

    assert exc.value.code == 77
    assert calls == [(signal.SIGTERM, "frame")]


def test_nested_smb_sigterm_still_exits_without_outer_handler():
    with pytest.raises(SystemExit) as exc:
        drive_case_sync._continue_signal_chain(signal.SIG_DFL, signal.SIGTERM, None)

    assert exc.value.code == 128 + signal.SIGTERM


def _run_drive_worker_lock_contender(tmp_path, monkeypatch, active_worker_kind: str):
    """Run only the lock-contender exit path; no Drive/NAS operation occurs."""
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    monkeypatch.setattr(drive_case_sync_worker, "runtime_dir", lambda: runtime)
    monkeypatch.setattr(
        drive_case_sync_worker,
        "acquire_worker_lock",
        lambda requested_kind: {
            "acquired": False,
            "status": "already_running",
            "active_pid": 4321,
            "active_worker_kind": active_worker_kind,
            "lock_path": "lock-aggregate-only",
        },
    )
    monkeypatch.setattr(
        drive_case_sync_worker,
        "save_worker_status",
        lambda *_args, **_kwargs: pytest.fail("contender must not replace owner status"),
    )
    return drive_case_sync_worker.main(["--direct-all-cases"]), runtime


def test_all_files_same_kind_owner_is_safe_noop_success(tmp_path, monkeypatch):
    result, runtime = _run_drive_worker_lock_contender(tmp_path, monkeypatch, "all_files")

    assert result == 0
    receipt = json.loads(
        (runtime / "drive_case_sync_worker_skip_all_files_latest.json").read_text(encoding="utf-8")
    )
    assert receipt["ok"] is True
    assert receipt["status"] == "already_running"
    assert receipt["retryable"] is False
    assert receipt["active_worker_kind"] == "all_files"


def test_all_files_priority_owner_is_deferred_and_retryable(tmp_path, monkeypatch):
    result, runtime = _run_drive_worker_lock_contender(tmp_path, monkeypatch, "priority")

    assert result == 75
    receipt = json.loads(
        (runtime / "drive_case_sync_worker_skip_all_files_latest.json").read_text(encoding="utf-8")
    )
    assert receipt["ok"] is False
    assert receipt["status"] == "deferred_owner_kind_conflict"
    assert receipt["deferred"] is True
    assert receipt["retryable"] is True
    assert receipt["action_required"] is False
    assert receipt["active_worker_kind"] == "priority"


def test_unclassified_lock_owner_never_clears_all_files_retry(tmp_path, monkeypatch):
    result, runtime = _run_drive_worker_lock_contender(tmp_path, monkeypatch, "")

    assert result == 75
    receipt = json.loads(
        (runtime / "drive_case_sync_worker_skip_all_files_latest.json").read_text(encoding="utf-8")
    )
    assert receipt["ok"] is False
    assert receipt["reason"] == "owner_worker_kind_unclassified"
    assert receipt["active_worker_kind"] == "unclassified"


def test_drive_controlled_interruption_is_retryable(tmp_path, monkeypatch):
    drive_dir = tmp_path / "drive_sync"
    drive_dir.mkdir()
    (drive_dir / "drive_case_sync_worker_status_latest.json").write_text(
        json.dumps(
            {
                "status_by_kind": {
                    "all_files": {
                        "ok": False,
                        "status": "interrupted",
                        "action_required": False,
                        "pid": 321,
                        "worker_kind": "all_files",
                        "finished_at": "2026-08-06T06:48:00+08:00",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(resource_guarded_run.runtime_dir, "root", lambda: tmp_path)

    safe, event = resource_guarded_run._drive_sync_status_retryable_interruption(
        "job_drive_case_sync_all_files",
        child_pid=321,
    )

    assert safe is True
    assert event["drive_retryable_interruption"] is True


def test_drive_interruption_requires_matching_nonblocking_receipt(tmp_path, monkeypatch):
    drive_dir = tmp_path / "drive_sync"
    drive_dir.mkdir()
    (drive_dir / "drive_case_sync_worker_status_latest.json").write_text(
        json.dumps(
            {
                "status_by_kind": {
                    "all_files": {
                        "ok": False,
                        "status": "interrupted",
                        "action_required": True,
                        "pid": 999,
                        "worker_kind": "all_files",
                        "finished_at": "2026-08-06T06:48:00+08:00",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(resource_guarded_run.runtime_dir, "root", lambda: tmp_path)

    safe, _event = resource_guarded_run._drive_sync_status_retryable_interruption(
        "job_drive_case_sync_all_files",
        child_pid=321,
    )

    assert safe is False


def test_drive_retryable_interruption_emits_strict_deferred_contract():
    payload = resource_guarded_run._retryable_interruption_deferred_payload(
        "job_drive_case_sync_all_files",
        {"drive_status": "interrupted"},
    )

    classified = classify_cron_result(
        75,
        json.dumps(payload, ensure_ascii=False),
        "",
    )

    assert payload["deferred"] is True
    assert payload["partial"] is False
    assert payload["action_required"] is False
    assert classified.status == "deferred"
    assert classified.success is False


def test_drive_storage_wait_does_not_become_guard_failure(tmp_path, monkeypatch):
    drive_dir = tmp_path / "drive_sync"
    drive_dir.mkdir()
    (drive_dir / "drive_case_sync_worker_status_latest.json").write_text(
        json.dumps({
            "status_by_kind": {
                "all_files": {
                    "ok": False,
                    "success": False,
                    "status": "deferred",
                    "deferred": True,
                    "partial": False,
                    "action_required": False,
                    "reason": "storage_unavailable",
                    "pid": 321,
                    "finished_at": "2026-08-06T16:17:23+08:00",
                }
            }
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(resource_guarded_run.runtime_dir, "root", lambda: tmp_path)

    returncode, event = resource_guarded_run._drive_sync_status_failure_returncode(
        "job_drive_case_sync_all_files",
        child_pid=321,
    )

    assert returncode is None
    assert event["drive_status_contract_deferred"] is True
    assert event["drive_action_required"] is False


@pytest.mark.parametrize(
    ("partial", "action_required"),
    [(True, False), (False, True)],
)
def test_drive_invalid_deferred_contract_remains_failed(
    tmp_path, monkeypatch, partial, action_required
):
    drive_dir = tmp_path / "drive_sync"
    drive_dir.mkdir()
    (drive_dir / "drive_case_sync_worker_status_latest.json").write_text(
        json.dumps({
            "status_by_kind": {
                "all_files": {
                    "ok": False,
                    "status": "deferred",
                    "deferred": True,
                    "partial": partial,
                    "action_required": action_required,
                    "pid": 654,
                    "finished_at": "2026-08-06T16:17:23+08:00",
                }
            }
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(resource_guarded_run.runtime_dir, "root", lambda: tmp_path)

    returncode, event = resource_guarded_run._drive_sync_status_failure_returncode(
        "job_drive_case_sync_all_files",
        child_pid=654,
    )

    assert returncode == 2
    assert event["drive_status_contract_failed"] is True
