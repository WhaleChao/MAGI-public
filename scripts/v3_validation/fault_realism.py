#!/usr/bin/env python3
"""Offline SIGKILL commit-window evidence with explicit realism limits.

The probe only kills child processes that it creates inside a caller-provided
sandbox.  It never opens a listener, imports a MAGI service, or touches the
live MAGI application-support directory.  The result is useful partial fault
evidence; it is deliberately not represented as physical disk-full or
power-loss evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import plistlib
import re
import selectors
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence


SCRIPT_PATH = Path(__file__).resolve()
REPO_ROOT = SCRIPT_PATH.parents[2]
LIVE_ROOT = (Path.home() / "Library" / "Application Support" / "MAGI").resolve()
SCHEMA_VERSION = 1
BLOCKER_CODE = "FAULT_CAMPAIGN_REALISM_INCOMPLETE"
WORKLOAD = "fault_injection_realism_audit"
PAYLOAD_ROWS_PER_JOB = 32
PAYLOAD_BYTES = 4096
DEFAULT_KILL_DELAYS_US = (0, 25, 100, 500, 2_000, 10_000)
DEFAULT_TIME_OFFSET_KILL_DELAYS_US = (0, 50, 250, 1_000, 5_000, 20_000)
TRANSACTION_STAGE_MARKERS = (
    "READY",
    "BEGIN",
    "JOB_INSERT",
    *(f"PAYLOAD_{index:02d}" for index in range(PAYLOAD_ROWS_PER_JOB)),
    "COMMIT_STARTED",
    "COMMIT_ACK",
)
HDIUTIL = Path("/usr/bin/hdiutil")
DISKUTIL = Path("/usr/sbin/diskutil")
CLANG = Path("/usr/bin/clang")
_OWNED_DISK_RE = re.compile(r"^/dev/disk[0-9]+$")

_SQLITE_VFS_FSYNC_PROBE_SOURCE = r'''
#include <sqlite3.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

typedef struct FaultFile FaultFile;
struct FaultFile {
    sqlite3_file base;
    sqlite3_file *real;
    int flags;
};

static sqlite3_vfs *parent_vfs = NULL;
static sqlite3_vfs fault_vfs;
static int armed = 0;
static int injected = 0;
static int injected_flags = 0;
static int sync_calls_after_arm = 0;

static const sqlite3_io_methods fault_io;

static FaultFile *fault_file(sqlite3_file *file) { return (FaultFile *)file; }
static const sqlite3_io_methods *real_io(sqlite3_file *file) {
    return fault_file(file)->real->pMethods;
}
static int ffClose(sqlite3_file *f) { return real_io(f)->xClose(fault_file(f)->real); }
static int ffRead(sqlite3_file *f, void *b, int n, sqlite3_int64 o) {
    return real_io(f)->xRead(fault_file(f)->real, b, n, o);
}
static int ffWrite(sqlite3_file *f, const void *b, int n, sqlite3_int64 o) {
    return real_io(f)->xWrite(fault_file(f)->real, b, n, o);
}
static int ffTruncate(sqlite3_file *f, sqlite3_int64 n) {
    return real_io(f)->xTruncate(fault_file(f)->real, n);
}
static int ffSync(sqlite3_file *f, int flags) {
    if (armed) {
        sync_calls_after_arm += 1;
        if (!injected) {
            injected = 1;
            injected_flags = fault_file(f)->flags;
            return SQLITE_IOERR_FSYNC;
        }
    }
    return real_io(f)->xSync(fault_file(f)->real, flags);
}
static int ffFileSize(sqlite3_file *f, sqlite3_int64 *n) {
    return real_io(f)->xFileSize(fault_file(f)->real, n);
}
static int ffLock(sqlite3_file *f, int n) { return real_io(f)->xLock(fault_file(f)->real, n); }
static int ffUnlock(sqlite3_file *f, int n) { return real_io(f)->xUnlock(fault_file(f)->real, n); }
static int ffCheckReservedLock(sqlite3_file *f, int *r) {
    return real_io(f)->xCheckReservedLock(fault_file(f)->real, r);
}
static int ffFileControl(sqlite3_file *f, int op, void *arg) {
    return real_io(f)->xFileControl(fault_file(f)->real, op, arg);
}
static int ffSectorSize(sqlite3_file *f) { return real_io(f)->xSectorSize(fault_file(f)->real); }
static int ffDeviceCharacteristics(sqlite3_file *f) {
    return real_io(f)->xDeviceCharacteristics(fault_file(f)->real);
}
static int ffShmMap(sqlite3_file *f, int page, int size, int extend, void volatile **out) {
    const sqlite3_io_methods *m = real_io(f);
    return m->iVersion >= 2 && m->xShmMap
        ? m->xShmMap(fault_file(f)->real, page, size, extend, out) : SQLITE_IOERR;
}
static int ffShmLock(sqlite3_file *f, int offset, int n, int flags) {
    const sqlite3_io_methods *m = real_io(f);
    return m->iVersion >= 2 && m->xShmLock
        ? m->xShmLock(fault_file(f)->real, offset, n, flags) : SQLITE_IOERR;
}
static void ffShmBarrier(sqlite3_file *f) {
    const sqlite3_io_methods *m = real_io(f);
    if (m->iVersion >= 2 && m->xShmBarrier) m->xShmBarrier(fault_file(f)->real);
}
static int ffShmUnmap(sqlite3_file *f, int delete_flag) {
    const sqlite3_io_methods *m = real_io(f);
    return m->iVersion >= 2 && m->xShmUnmap
        ? m->xShmUnmap(fault_file(f)->real, delete_flag) : SQLITE_IOERR;
}
static int ffFetch(sqlite3_file *f, sqlite3_int64 off, int n, void **out) {
    const sqlite3_io_methods *m = real_io(f);
    if (m->iVersion >= 3 && m->xFetch) return m->xFetch(fault_file(f)->real, off, n, out);
    *out = NULL;
    return SQLITE_OK;
}
static int ffUnfetch(sqlite3_file *f, sqlite3_int64 off, void *p) {
    const sqlite3_io_methods *m = real_io(f);
    return m->iVersion >= 3 && m->xUnfetch
        ? m->xUnfetch(fault_file(f)->real, off, p) : SQLITE_OK;
}

static const sqlite3_io_methods fault_io = {
    3, ffClose, ffRead, ffWrite, ffTruncate, ffSync, ffFileSize, ffLock,
    ffUnlock, ffCheckReservedLock, ffFileControl, ffSectorSize,
    ffDeviceCharacteristics, ffShmMap, ffShmLock, ffShmBarrier, ffShmUnmap,
    ffFetch, ffUnfetch
};

static int fvOpen(sqlite3_vfs *v, const char *name, sqlite3_file *file, int flags, int *out_flags) {
    FaultFile *wrapped = (FaultFile *)file;
    memset(file, 0, (size_t)v->szOsFile);
    wrapped->real = (sqlite3_file *)((unsigned char *)file + sizeof(FaultFile));
    wrapped->flags = flags;
    int rc = parent_vfs->xOpen(parent_vfs, name, wrapped->real, flags, out_flags);
    if (rc == SQLITE_OK) wrapped->base.pMethods = &fault_io;
    return rc;
}
static int fvDelete(sqlite3_vfs *v, const char *z, int sync_dir) {
    (void)v; return parent_vfs->xDelete(parent_vfs, z, sync_dir);
}
static int fvAccess(sqlite3_vfs *v, const char *z, int flags, int *out) {
    (void)v; return parent_vfs->xAccess(parent_vfs, z, flags, out);
}
static int fvFullPathname(sqlite3_vfs *v, const char *z, int n, char *out) {
    (void)v; return parent_vfs->xFullPathname(parent_vfs, z, n, out);
}
static void *fvDlOpen(sqlite3_vfs *v, const char *z) { (void)v; return parent_vfs->xDlOpen(parent_vfs, z); }
static void fvDlError(sqlite3_vfs *v, int n, char *out) { (void)v; parent_vfs->xDlError(parent_vfs, n, out); }
static void (*fvDlSym(sqlite3_vfs *v, void *p, const char *z))(void) {
    (void)v; return parent_vfs->xDlSym(parent_vfs, p, z);
}
static void fvDlClose(sqlite3_vfs *v, void *p) { (void)v; parent_vfs->xDlClose(parent_vfs, p); }
static int fvRandomness(sqlite3_vfs *v, int n, char *out) {
    (void)v; return parent_vfs->xRandomness(parent_vfs, n, out);
}
static int fvSleep(sqlite3_vfs *v, int n) { (void)v; return parent_vfs->xSleep(parent_vfs, n); }
static int fvCurrentTime(sqlite3_vfs *v, double *out) {
    (void)v; return parent_vfs->xCurrentTime(parent_vfs, out);
}
static int fvGetLastError(sqlite3_vfs *v, int n, char *out) {
    (void)v; return parent_vfs->xGetLastError(parent_vfs, n, out);
}
static int fvCurrentTimeInt64(sqlite3_vfs *v, sqlite3_int64 *out) {
    (void)v;
    return parent_vfs->iVersion >= 2 && parent_vfs->xCurrentTimeInt64
        ? parent_vfs->xCurrentTimeInt64(parent_vfs, out) : SQLITE_ERROR;
}
static int fvSetSystemCall(sqlite3_vfs *v, const char *z, sqlite3_syscall_ptr p) {
    (void)v;
    return parent_vfs->iVersion >= 3 && parent_vfs->xSetSystemCall
        ? parent_vfs->xSetSystemCall(parent_vfs, z, p) : SQLITE_NOTFOUND;
}
static sqlite3_syscall_ptr fvGetSystemCall(sqlite3_vfs *v, const char *z) {
    (void)v;
    return parent_vfs->iVersion >= 3 && parent_vfs->xGetSystemCall
        ? parent_vfs->xGetSystemCall(parent_vfs, z) : NULL;
}
static const char *fvNextSystemCall(sqlite3_vfs *v, const char *z) {
    (void)v;
    return parent_vfs->iVersion >= 3 && parent_vfs->xNextSystemCall
        ? parent_vfs->xNextSystemCall(parent_vfs, z) : NULL;
}

static int exec_sql(sqlite3 *db, const char *sql) {
    char *error = NULL;
    int rc = sqlite3_exec(db, sql, NULL, NULL, &error);
    sqlite3_free(error);
    return rc;
}
static int scalar_int(sqlite3 *db, const char *sql, int *value) {
    sqlite3_stmt *stmt = NULL;
    int rc = sqlite3_prepare_v2(db, sql, -1, &stmt, NULL);
    if (rc == SQLITE_OK && sqlite3_step(stmt) == SQLITE_ROW) *value = sqlite3_column_int(stmt, 0);
    else if (rc == SQLITE_OK) rc = SQLITE_ERROR;
    sqlite3_finalize(stmt);
    return rc;
}
static int integrity_ok(sqlite3 *db) {
    sqlite3_stmt *stmt = NULL;
    int ok = 0;
    if (sqlite3_prepare_v2(db, "PRAGMA integrity_check", -1, &stmt, NULL) == SQLITE_OK
        && sqlite3_step(stmt) == SQLITE_ROW) {
        const unsigned char *text = sqlite3_column_text(stmt, 0);
        ok = text && strcmp((const char *)text, "ok") == 0;
    }
    sqlite3_finalize(stmt);
    return ok;
}

int main(int argc, char **argv) {
    if (argc != 2) return 64;
    parent_vfs = sqlite3_vfs_find(NULL);
    if (!parent_vfs) return 65;
    memset(&fault_vfs, 0, sizeof(fault_vfs));
    fault_vfs.iVersion = parent_vfs->iVersion;
    fault_vfs.szOsFile = (int)(sizeof(FaultFile) + (size_t)parent_vfs->szOsFile);
    fault_vfs.mxPathname = parent_vfs->mxPathname;
    fault_vfs.zName = "magi_fault_vfs";
    fault_vfs.xOpen = fvOpen; fault_vfs.xDelete = fvDelete; fault_vfs.xAccess = fvAccess;
    fault_vfs.xFullPathname = fvFullPathname; fault_vfs.xDlOpen = fvDlOpen;
    fault_vfs.xDlError = fvDlError; fault_vfs.xDlSym = fvDlSym; fault_vfs.xDlClose = fvDlClose;
    fault_vfs.xRandomness = fvRandomness; fault_vfs.xSleep = fvSleep;
    fault_vfs.xCurrentTime = fvCurrentTime; fault_vfs.xGetLastError = fvGetLastError;
    fault_vfs.xCurrentTimeInt64 = fvCurrentTimeInt64; fault_vfs.xSetSystemCall = fvSetSystemCall;
    fault_vfs.xGetSystemCall = fvGetSystemCall; fault_vfs.xNextSystemCall = fvNextSystemCall;
    if (sqlite3_vfs_register(&fault_vfs, 0) != SQLITE_OK) return 66;

    sqlite3 *db = NULL;
    int rc = sqlite3_open_v2(argv[1], &db, SQLITE_OPEN_READWRITE | SQLITE_OPEN_CREATE, fault_vfs.zName);
    if (rc != SQLITE_OK) return 67;
    sqlite3_extended_result_codes(db, 1);
    if (exec_sql(db, "PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL;"
                     "CREATE TABLE jobs(id TEXT PRIMARY KEY);"
                     "INSERT INTO jobs VALUES('baseline');") != SQLITE_OK) return 68;
    armed = 1;
    int commit_rc = exec_sql(db, "BEGIN IMMEDIATE; INSERT INTO jobs VALUES('must_rollback'); COMMIT;");
    int extended_rc = sqlite3_extended_errcode(db);
    armed = 0;
    exec_sql(db, "ROLLBACK;");
    sqlite3_close(db);

    db = NULL;
    if (sqlite3_open_v2(argv[1], &db, SQLITE_OPEN_READWRITE, fault_vfs.zName) != SQLITE_OK) return 69;
    int baseline = -1, partial = -1, final = -1;
    int ok = integrity_ok(db);
    scalar_int(db, "SELECT COUNT(*) FROM jobs WHERE id='baseline'", &baseline);
    scalar_int(db, "SELECT COUNT(*) FROM jobs WHERE id='must_rollback'", &partial);
    int recovery_rc = exec_sql(db, "INSERT INTO jobs VALUES('recovered');");
    scalar_int(db, "SELECT COUNT(*) FROM jobs", &final);
    sqlite3_close(db);
    printf("{\"commit_rc\":%d,\"extended_rc\":%d,\"expected_extended_rc\":%d,"
           "\"sync_calls_after_arm\":%d,\"injected\":%d,\"injected_open_flags\":%d,"
           "\"baseline_rows\":%d,\"partial_rows\":%d,\"recovery_rc\":%d,"
           "\"final_rows\":%d,\"integrity_ok\":%d}\n",
           commit_rc, extended_rc, SQLITE_IOERR_FSYNC, sync_calls_after_arm, injected,
           injected_flags, baseline, partial, recovery_rc, final, ok);
    return injected && extended_rc == SQLITE_IOERR_FSYNC && baseline == 1 && partial == 0
        && recovery_rc == SQLITE_OK && final == 2 && ok ? 0 : 70;
}
'''


class FaultEvidenceError(RuntimeError):
    """The sandbox or resulting fault evidence cannot be trusted."""


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _validate_workdir(workdir: Path) -> Path:
    expanded = workdir.expanduser()
    if expanded.is_symlink():
        raise FaultEvidenceError("sandbox must not be a symlink")
    resolved = expanded.resolve()
    if resolved == LIVE_ROOT or _is_relative_to(resolved, LIVE_ROOT):
        raise FaultEvidenceError("sandbox must not be the live MAGI application-support tree")
    if resolved == REPO_ROOT or _is_relative_to(resolved, REPO_ROOT):
        raise FaultEvidenceError("sandbox must not be inside the source tree")
    if resolved.exists():
        if not resolved.is_dir():
            raise FaultEvidenceError("sandbox must be a directory")
        if any(resolved.iterdir()):
            raise FaultEvidenceError("sandbox must be empty and dedicated to this probe")
    else:
        resolved.mkdir(parents=True)
    marker = resolved / ".magi-v3-offline-fault-sandbox"
    marker.write_text("owned temporary fault sandbox\n", encoding="utf-8")
    return resolved


def _configure(connection: sqlite3.Connection) -> None:
    # Set the busy handler before changing journal mode.  A freshly attached
    # APFS image may briefly be inspected by macOS metadata services; without
    # an early busy timeout SQLite can fail the first WAL transition with a
    # transient ``database is locked`` even though no MAGI writer exists.
    connection.execute("PRAGMA busy_timeout=5000")
    deadline = time.monotonic() + 15.0
    while True:
        try:
            journal = str(
                connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]
            ).lower()
            break
        except sqlite3.OperationalError as exc:
            # A newly attached APFS sparse image can briefly be opened by
            # Spotlight/fseventsd.  SQLite's busy handler is not consistently
            # invoked while changing journal mode, so retry that one idempotent
            # transition within a fixed deadline and fail closed afterwards.
            if "locked" not in str(exc).lower() or time.monotonic() >= deadline:
                raise
            # Reset any read transaction left by the failed journal-mode
            # statement; retrying it on an open transaction can self-lock even
            # after the external metadata reader has gone away.
            connection.rollback()
            time.sleep(0.25)
    if journal != "wal":
        raise FaultEvidenceError(f"SQLite WAL mode unavailable: {journal}")
    connection.execute("PRAGMA synchronous=FULL")
    synchronous = int(connection.execute("PRAGMA synchronous").fetchone()[0])
    if synchronous != 2:
        raise FaultEvidenceError(f"SQLite FULL synchronous mode unavailable: {synchronous}")


def _initialize(path: Path) -> None:
    with sqlite3.connect(path, timeout=5) as connection:
        _configure(connection)
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                job_id TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                recovered INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS payloads (
                job_id TEXT NOT NULL,
                part INTEGER NOT NULL,
                payload BLOB NOT NULL,
                PRIMARY KEY (job_id, part),
                FOREIGN KEY (job_id) REFERENCES jobs(job_id)
            );
            """
        )


def _initialize_apfs_enospc_database(path: Path) -> int:
    """Create the APFS drill database in a short-lived owned process."""

    with sqlite3.connect(path, timeout=5) as connection:
        _configure(connection)
        connection.execute(
            "CREATE TABLE jobs(job_id TEXT PRIMARY KEY, payload BLOB NOT NULL)"
        )
        connection.execute("INSERT INTO jobs VALUES ('committed-before-full', X'01')")
    return 0


def _apfs_enospc_sqlite_full_worker(path: Path) -> int:
    """Attempt the full-disk WAL transaction in a crash-contained child."""

    connection = sqlite3.connect(path, timeout=5)
    try:
        _configure(connection)
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT INTO jobs VALUES ('must-not-partially-commit', zeroblob(8388608))"
        )
        connection.commit()
    except sqlite3.OperationalError as exc:
        connection.rollback()
        error_code = getattr(exc, "sqlite_errorcode", None)
        error_name = str(getattr(exc, "sqlite_errorname", "") or "")
        sqlite_full = error_code == sqlite3.SQLITE_FULL or "full" in str(exc).lower()
        sys.stdout.buffer.write(
            _canonical_json(
                {
                    "sqlite_full": sqlite_full,
                    "sqlite_error_code": error_code,
                    "sqlite_error_name": error_name,
                }
            )
            + b"\n"
        )
        return 0 if sqlite_full else 2
    finally:
        connection.close()
    return 3


def _apfs_enospc_recovery_worker(path: Path) -> int:
    """Verify WAL recovery in a fresh process after real APFS ENOSPC."""

    with sqlite3.connect(path, timeout=5) as connection:
        _configure(connection)
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        committed_before = int(
            connection.execute(
                "SELECT COUNT(*) FROM jobs WHERE job_id='committed-before-full'"
            ).fetchone()[0]
        )
        partial = int(
            connection.execute(
                "SELECT COUNT(*) FROM jobs WHERE job_id='must-not-partially-commit'"
            ).fetchone()[0]
        )
        connection.execute("INSERT INTO jobs VALUES ('recovered-after-full', X'02')")
        final_jobs = int(connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0])
    sys.stdout.buffer.write(
        _canonical_json(
            {
                "integrity_check": integrity,
                "committed_rows_preserved": committed_before,
                "partial_rows_visible": partial,
                "final_jobs": final_jobs,
            }
        )
        + b"\n"
    )
    return 0


def _worker(path: Path, job_id: str) -> int:
    """Perform one durable transaction and expose its real commit boundary."""

    connection = sqlite3.connect(path, timeout=5, isolation_level=None)
    try:
        _configure(connection)
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT INTO jobs(job_id, source, recovered) VALUES (?, 'worker', 0)",
            (job_id,),
        )
        payload = b"x" * PAYLOAD_BYTES
        connection.executemany(
            "INSERT INTO payloads(job_id, part, payload) VALUES (?, ?, ?)",
            ((job_id, part, payload) for part in range(PAYLOAD_ROWS_PER_JOB)),
        )
        # This line is the synchronization boundary: the parent schedules its
        # SIGKILL only after all writes are staged and immediately before COMMIT.
        print("COMMIT_STARTED", flush=True)
        connection.commit()
        print("COMMIT_ACK", flush=True)
        time.sleep(60)
    finally:
        connection.close()
    return 0


def _instruction_worker(path: Path, job_id: str) -> int:
    """Expose every logical transaction boundary for an owned SIGKILL sweep."""

    connection = sqlite3.connect(path, timeout=5, isolation_level=None)
    try:
        _configure(connection)
        print("STAGE:READY", flush=True)
        connection.execute("BEGIN IMMEDIATE")
        print("STAGE:BEGIN", flush=True)
        connection.execute(
            "INSERT INTO jobs(job_id, source, recovered) VALUES (?, 'instruction-worker', 0)",
            (job_id,),
        )
        print("STAGE:JOB_INSERT", flush=True)
        payload = b"x" * PAYLOAD_BYTES
        for part in range(PAYLOAD_ROWS_PER_JOB):
            connection.execute(
                "INSERT INTO payloads(job_id, part, payload) VALUES (?, ?, ?)",
                (job_id, part, payload),
            )
            print(f"STAGE:PAYLOAD_{part:02d}", flush=True)
        print("STAGE:COMMIT_STARTED", flush=True)
        connection.commit()
        print("STAGE:COMMIT_ACK", flush=True)
        time.sleep(60)
    finally:
        connection.close()
    return 0


def _inspect_job(path: Path, job_id: str) -> tuple[int, int, str]:
    with sqlite3.connect(path, timeout=5) as connection:
        _configure(connection)
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        job_count = int(
            connection.execute("SELECT COUNT(*) FROM jobs WHERE job_id = ?", (job_id,)).fetchone()[0]
        )
        payload_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM payloads WHERE job_id = ?", (job_id,)
            ).fetchone()[0]
        )
    return job_count, payload_count, integrity


def _recover_from_plan(path: Path, job_id: str) -> bool:
    """Idempotently reconstruct an uncommitted job from the durable test plan."""

    payload = b"x" * PAYLOAD_BYTES
    with sqlite3.connect(path, timeout=5) as connection:
        _configure(connection)
        inserted = (
            connection.execute(
                "INSERT OR IGNORE INTO jobs(job_id, source, recovered) VALUES (?, 'plan', 1)",
                (job_id,),
            ).rowcount
            == 1
        )
        connection.executemany(
            "INSERT OR IGNORE INTO payloads(job_id, part, payload) VALUES (?, ?, ?)",
            ((job_id, part, payload) for part in range(PAYLOAD_ROWS_PER_JOB)),
        )
    return inserted


def _run_checked(argv: Sequence[str], *, timeout: int = 30) -> subprocess.CompletedProcess[bytes]:
    result = subprocess.run(
        list(argv),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", "replace").strip()
        raise FaultEvidenceError(f"sandbox command failed: {Path(argv[0]).name}: {detail}")
    return result


def _run_sqlite_vfs_fsync_drill(sandbox: Path) -> dict[str, Any]:
    """Inject one real SQLITE_IOERR_FSYNC through an owned forwarding VFS."""

    if sys.platform != "darwin" or not CLANG.is_file():
        return {
            "status": "not_run",
            "reason": "custom SQLite VFS fsync drill requires macOS and /usr/bin/clang",
            "power_loss_simulated": False,
        }
    source = sandbox / "sqlite-vfs-fsync-probe.c"
    executable = sandbox / "sqlite-vfs-fsync-probe"
    database = sandbox / "sqlite-vfs-fsync.sqlite3"
    source.write_text(_SQLITE_VFS_FSYNC_PROBE_SOURCE, encoding="utf-8")
    _run_checked(
        (
            str(CLANG),
            "-std=c11",
            "-O2",
            "-Wall",
            "-Wextra",
            "-Werror",
            str(source),
            "-lsqlite3",
            "-o",
            str(executable),
        ),
        timeout=60,
    )
    result = _run_checked((str(executable), str(database)), timeout=30)
    try:
        measurement = json.loads(result.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FaultEvidenceError("custom SQLite VFS probe emitted invalid JSON") from exc
    expected_extended_rc = int(getattr(sqlite3, "SQLITE_IOERR_FSYNC", 10 | (4 << 8)))
    required = {
        "commit_rc": expected_extended_rc,
        "injected": 1,
        "extended_rc": expected_extended_rc,
        "expected_extended_rc": expected_extended_rc,
        "baseline_rows": 1,
        "partial_rows": 0,
        "recovery_rc": 0,
        "final_rows": 2,
        "integrity_ok": 1,
    }
    if not isinstance(measurement, dict) or any(
        measurement.get(key) != value for key, value in required.items()
    ):
        raise FaultEvidenceError("custom SQLite VFS fsync invariants failed")
    sync_calls = measurement.get("sync_calls_after_arm")
    if isinstance(sync_calls, bool) or not isinstance(sync_calls, int) or sync_calls < 1:
        raise FaultEvidenceError("custom SQLite VFS did not observe an armed xSync call")
    injected_open_flags = measurement.get("injected_open_flags")
    sqlite_open_wal = 0x00080000
    if (
        isinstance(injected_open_flags, bool)
        or not isinstance(injected_open_flags, int)
        or injected_open_flags & sqlite_open_wal == 0
    ):
        raise FaultEvidenceError("custom SQLite VFS did not inject against a WAL file xSync")
    return {
        "status": "passed",
        "injection_boundary": "custom SQLite VFS xSync",
        "injected_error": "SQLITE_IOERR_FSYNC",
        "injected_file_role": "wal",
        "journal_mode": "wal",
        "synchronous": "FULL",
        "power_loss_simulated": False,
        "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "executable_sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
        **measurement,
    }


def _attach_sparse_image(image: Path, mount_root: Path) -> tuple[str, Path]:
    result = _run_checked(
        (
            str(HDIUTIL),
            "attach",
            "-nobrowse",
            "-owners",
            "off",
            "-mountroot",
            str(mount_root),
            "-plist",
            str(image),
        )
    )
    try:
        payload = plistlib.loads(result.stdout)
        entities = payload["system-entities"]
    except (KeyError, TypeError, ValueError, plistlib.InvalidFileException) as exc:
        raise FaultEvidenceError("hdiutil attach emitted invalid plist") from exc
    devices = [str(row.get("dev-entry")) for row in entities if isinstance(row, dict)]
    # APFS images expose both the backing GUID-partition image and a synthesized
    # APFS container as whole devices.  hdiutil orders the backing image first;
    # that is the identity accepted by ``hdiutil detach`` for the full image.
    whole_devices = [device for device in devices if _OWNED_DISK_RE.fullmatch(device)]
    mount_points = [
        Path(str(row["mount-point"])).resolve()
        for row in entities
        if isinstance(row, dict) and row.get("mount-point")
    ]
    if not whole_devices or len(mount_points) != 1:
        if whole_devices:
            _detach_owned_image(whole_devices[0])
        raise FaultEvidenceError("sandbox image attachment identity is ambiguous")
    mounted = mount_points[0]
    if not _is_relative_to(mounted, mount_root.resolve()):
        _detach_owned_image(whole_devices[0])
        raise FaultEvidenceError("sandbox image mounted outside its dedicated mountroot")
    return whole_devices[0], mounted


def _detach_owned_image(device: str) -> None:
    if not _OWNED_DISK_RE.fullmatch(device):
        raise FaultEvidenceError("refusing to detach a non-owned device identity")
    normal = subprocess.run(
        (str(HDIUTIL), "detach", device),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=30,
    )
    if normal.returncode == 0:
        return
    forced = subprocess.run(
        (str(HDIUTIL), "detach", "-force", device),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=30,
    )
    if forced.returncode != 0:
        detail = forced.stderr.decode("utf-8", "replace").strip()
        raise FaultEvidenceError(f"owned sandbox image detach failed: {detail}")


def _run_apfs_sparse_image_drill(sandbox: Path) -> dict[str, Any]:
    """Exercise real APFS ENOSPC semantics inside a disposable sparse image."""

    if sys.platform != "darwin" or not HDIUTIL.is_file() or not DISKUTIL.is_file():
        raise FaultEvidenceError("APFS sparse-image drill requires macOS hdiutil and diskutil")
    image = sandbox / "apfs-enospc.sparsebundle"
    mount_root = sandbox / "apfs-mountroot"
    mount_root.mkdir()
    # A unique label avoids macOS metadata daemons carrying an old volume-name
    # lookup across repeated attach/detach drills in the same campaign process.
    volume_token = hashlib.sha256(str(sandbox).encode("utf-8")).hexdigest()[:10]
    volume_name = f"MAGI_V3_FAULT_{os.getpid()}_{volume_token}"
    _run_checked(
        (
            str(HDIUTIL),
            "create",
            "-size",
            "32m",
            "-fs",
            "APFS",
            "-volname",
            volume_name,
            "-type",
            "SPARSEBUNDLE",
            str(image),
        ),
        timeout=60,
    )
    device = ""
    mounted: Path | None = None
    detached = False
    try:
        device, mounted = _attach_sparse_image(image, mount_root)
        disk_info = plistlib.loads(
            _run_checked((str(DISKUTIL), "info", "-plist", str(mounted))).stdout
        )
        filesystem = str(
            disk_info.get("FilesystemType")
            or disk_info.get("FilesystemName")
            or disk_info.get("Content")
            or ""
        ).lower()
        if "apfs" not in filesystem:
            raise FaultEvidenceError(f"sandbox image is not APFS: {filesystem}")

        # Keep Spotlight out of this disposable 32 MiB evidence volume.  The
        # marker is written before SQLite creates its journal files so a real
        # journal-mode lock is attributable to the probe, not index discovery.
        no_index = mounted / ".metadata_never_index"
        no_index.touch(exist_ok=False)
        database = mounted / "fault.sqlite3"
        initialized = subprocess.run(
            (
                sys.executable,
                str(SCRIPT_PATH),
                "--apfs-enospc-initialize",
                str(database),
            ),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            env=_owned_worker_environment(mounted),
            timeout=30,
        )
        if initialized.returncode != 0:
            detail = initialized.stderr.decode("utf-8", "replace").strip()
            raise FaultEvidenceError(f"APFS SQLite initialization failed: {detail}")

        reserve = mounted / "recovery.reserve"
        with reserve.open("xb", buffering=0) as handle:
            handle.write(b"r" * (4 * 1024 * 1024))
            os.fsync(handle.fileno())
        sqlite_overhead_reserve = mounted / "sqlite-overhead.reserve"
        with sqlite_overhead_reserve.open("xb", buffering=0) as handle:
            handle.write(b"s" * (1024 * 1024))
            os.fsync(handle.fileno())
        filler = mounted / "fill.bin"
        bytes_written = 0
        filesystem_enospc = False
        enospc_operation = ""
        with filler.open("xb", buffering=0) as handle:
            chunk = b"f" * (1024 * 1024)
            for _index in range(128):
                try:
                    handle.write(chunk)
                    bytes_written += len(chunk)
                except OSError as exc:
                    if exc.errno != 28:
                        raise
                    filesystem_enospc = True
                    enospc_operation = "write"
                    break
                try:
                    os.fsync(handle.fileno())
                except OSError as exc:
                    if exc.errno != 28:
                        raise
                    filesystem_enospc = True
                    enospc_operation = "fsync"
                    break
        bytes_written = filler.stat().st_size
        if not filesystem_enospc:
            raise FaultEvidenceError("bounded APFS image did not produce ENOSPC")

        # Free only enough space for SQLite to open/map its WAL bookkeeping.
        # The 8 MiB transaction remains much larger than this reserve and must
        # still fail on the already-observed real APFS ENOSPC boundary.
        sqlite_overhead_reserve.unlink()
        full_attempt = subprocess.run(
            (
                sys.executable,
                str(SCRIPT_PATH),
                "--apfs-enospc-sqlite-full",
                str(database),
            ),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            env=_owned_worker_environment(mounted),
            timeout=30,
        )
        if full_attempt.returncode != 0:
            detail = full_attempt.stderr.decode("utf-8", "replace").strip()
            if full_attempt.returncode < 0:
                detail = f"owned child terminated by signal {-full_attempt.returncode}"
            raise FaultEvidenceError(f"SQLite full-disk child failed safely: {detail}")
        try:
            full_measurement = json.loads(full_attempt.stdout)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise FaultEvidenceError("SQLite full-disk child emitted invalid evidence") from exc
        sqlite_full = full_measurement.get("sqlite_full") is True
        sqlite_error_code = full_measurement.get("sqlite_error_code")
        sqlite_error_name = str(full_measurement.get("sqlite_error_name") or "")
        if not sqlite_full:
            raise FaultEvidenceError("SQLite did not surface SQLITE_FULL on full APFS image")

        # Model the real recovery action: reclaim the fault-inducing filler and
        # the protected reserve before asking SQLite to rebuild WAL metadata.
        # Merely unlinking a small reserve can leave APFS too constrained to
        # back a new shared-memory page and has produced a real SIGBUS on macOS.
        reserve.unlink()
        filler.unlink()
        directory_fd = os.open(mounted, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        recovery_attempt = subprocess.run(
            (
                sys.executable,
                str(SCRIPT_PATH),
                "--apfs-enospc-recover",
                str(database),
            ),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            env=_owned_worker_environment(mounted),
            timeout=30,
        )
        if recovery_attempt.returncode != 0:
            detail = recovery_attempt.stderr.decode("utf-8", "replace").strip()
            if recovery_attempt.returncode < 0:
                detail = f"owned child terminated by signal {-recovery_attempt.returncode}"
            raise FaultEvidenceError(f"SQLite recovery child failed safely: {detail}")
        try:
            recovery_measurement = json.loads(recovery_attempt.stdout)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise FaultEvidenceError("SQLite recovery child emitted invalid evidence") from exc
        integrity = recovery_measurement.get("integrity_check")
        committed_before = recovery_measurement.get("committed_rows_preserved")
        partial = recovery_measurement.get("partial_rows_visible")
        final_jobs = recovery_measurement.get("final_jobs")
        if integrity != "ok" or committed_before != 1 or partial != 0 or final_jobs != 2:
            raise FaultEvidenceError("APFS ENOSPC recovery invariants failed")
        return {
            "status": "passed",
            "filesystem": "apfs",
            "image_type": "sparsebundle",
            "image_capacity_bytes": 32 * 1024 * 1024,
            "filler_bytes_before_enospc": bytes_written,
            "filesystem_enospc_observed": filesystem_enospc,
            "filesystem_enospc_operation": enospc_operation,
            "sqlite_full_observed": sqlite_full,
            "sqlite_error_code": sqlite_error_code,
            "sqlite_error_name": sqlite_error_name,
            "committed_rows_preserved": committed_before,
            "partial_rows_visible": partial,
            "final_jobs": final_jobs,
            "integrity_check": integrity,
            "recovery_reserve_bytes": 4 * 1024 * 1024,
            "sqlite_overhead_reserve_bytes": 1024 * 1024,
            "sqlite_full_attempt_isolated_to_owned_child": True,
            "sqlite_recovery_isolated_to_owned_child": True,
            "fault_filler_removed_before_recovery": True,
        }
    finally:
        if device:
            _detach_owned_image(device)
            detached = True
        if image.exists() and detached:
            shutil.rmtree(image)
        if mount_root.exists() and not any(mount_root.iterdir()):
            mount_root.rmdir()


def _owned_worker_environment(root: Path) -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONHASHSEED": "0",
        "HOME": str(root),
        "TMPDIR": str(root),
    }


def _read_owned_marker(
    process: subprocess.Popen[str],
    *,
    expected: str,
    timeout: float = 10,
) -> str:
    """Read one complete marker without allowing an owned child to hang the probe."""

    if process.stdout is None:
        raise FaultEvidenceError("owned worker stdout pipe is unavailable")
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    deadline = time.monotonic() + timeout
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise FaultEvidenceError(f"owned worker did not emit marker: {expected}")
            if not selector.select(remaining):
                continue
            line = process.stdout.readline()
            if not line:
                raise FaultEvidenceError(f"owned worker exited before marker: {expected}")
            marker = line.strip()
            if marker != expected:
                raise FaultEvidenceError(
                    f"owned worker emitted unexpected marker {marker!r}; expected {expected!r}"
                )
            return marker
    finally:
        selector.close()


def _sigkill_and_reap_owned(process: subprocess.Popen[str]) -> tuple[str, str]:
    """SIGKILL and reap only the Popen child supplied by this probe."""

    if process.poll() is None:
        process.kill()
    try:
        return process.communicate(timeout=5)
    except subprocess.TimeoutExpired as exc:
        if process.poll() is None:
            process.kill()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired as wait_exc:
            raise FaultEvidenceError("owned worker could not be reaped after SIGKILL") from wait_exc
        stdout = process.stdout.read() if process.stdout is not None else ""
        stderr = process.stderr.read() if process.stderr is not None else ""
        if process.returncode is None:
            raise FaultEvidenceError("owned worker reap did not produce a return code") from exc
        return stdout, stderr


def _run_cycle(path: Path, job_id: str, delay_us: int) -> dict[str, Any]:
    process = subprocess.Popen(
        [sys.executable, str(SCRIPT_PATH), "--worker", str(path), job_id],
        cwd=path.parent,
        env=_owned_worker_environment(path.parent),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        _read_owned_marker(process, expected="COMMIT_STARTED")
        if delay_us:
            time.sleep(delay_us / 1_000_000)
        if process.poll() is not None:
            raise FaultEvidenceError("worker exited before owned SIGKILL")
        remainder, stderr = _sigkill_and_reap_owned(process)
    except Exception:
        _sigkill_and_reap_owned(process)
        raise
    if process.returncode != -signal.SIGKILL:
        raise FaultEvidenceError(f"owned worker was not reaped by SIGKILL: {process.returncode}")
    if stderr.strip():
        raise FaultEvidenceError(f"worker emitted stderr: {stderr.strip()}")
    acknowledged = "COMMIT_ACK" in remainder.splitlines()
    before_jobs, before_payloads, integrity = _inspect_job(path, job_id)
    if integrity != "ok" or before_jobs not in (0, 1):
        raise FaultEvidenceError("post-SIGKILL database state is not atomically recoverable")
    if before_payloads not in (0, PAYLOAD_ROWS_PER_JOB):
        raise FaultEvidenceError("post-SIGKILL payload transaction was partially visible")
    if before_jobs == 0 and before_payloads != 0:
        raise FaultEvidenceError("payload rows survived without their job")
    if before_jobs == 1 and before_payloads != PAYLOAD_ROWS_PER_JOB:
        raise FaultEvidenceError("committed job has an incomplete payload")
    if acknowledged and before_jobs != 1:
        raise FaultEvidenceError("an acknowledged commit was lost")

    recovered = _recover_from_plan(path, job_id)
    after_jobs, after_payloads, after_integrity = _inspect_job(path, job_id)
    if after_integrity != "ok" or after_jobs != 1 or after_payloads != PAYLOAD_ROWS_PER_JOB:
        raise FaultEvidenceError("idempotent recovery did not produce one complete job")
    return {
        "job_id": job_id,
        "kill_delay_us": delay_us,
        "commit_ack_observed": acknowledged,
        "committed_before_recovery": before_jobs == 1,
        "payload_rows_before_recovery": before_payloads,
        "reinserted_from_plan": recovered,
        "final_job_rows": after_jobs,
        "final_payload_rows": after_payloads,
        "integrity_check": after_integrity,
        "signal": "SIGKILL",
    }


def _run_instruction_cycle(path: Path, job_id: str, target_stage: str) -> dict[str, Any]:
    """Kill one owned child at an exact logical transaction boundary."""

    if target_stage not in TRANSACTION_STAGE_MARKERS:
        raise FaultEvidenceError(f"unknown transaction stage: {target_stage}")
    process = subprocess.Popen(
        [sys.executable, str(SCRIPT_PATH), "--instruction-worker", str(path), job_id],
        cwd=path.parent,
        env=_owned_worker_environment(path.parent),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        # Read the marker stream as raw bytes.  TextIOWrapper.readline() may
        # prefetch several markers into its private buffer; a selector on the
        # underlying fd then sees no new OS-level bytes and can falsely time
        # out even though later STAGE lines are already buffered in Python.
        text=False,
        bufsize=0,
        start_new_session=True,
    )
    if process.stdout is None:
        raise FaultEvidenceError("instruction worker stdout pipe is unavailable")
    observed: list[str] = []
    pending = b""
    deadline = time.monotonic() + 10
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    try:
        while target_stage not in observed:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise FaultEvidenceError(f"instruction worker did not reach stage: {target_stage}")
            if not selector.select(remaining):
                continue
            chunk = os.read(process.stdout.fileno(), 4096)
            if not chunk:
                _stdout, stderr_raw = process.communicate(timeout=5)
                stderr = bytes(stderr_raw or b"").decode("utf-8", errors="replace")
                raise FaultEvidenceError(
                    f"instruction worker exited before {target_stage}: {stderr.strip()}"
                )
            pending += chunk
            while b"\n" in pending:
                raw_line, pending = pending.split(b"\n", 1)
                line = raw_line.decode("utf-8", errors="strict").strip()
                if not line:
                    continue
                if not line.startswith("STAGE:"):
                    raise FaultEvidenceError("instruction worker emitted an unexpected marker")
                observed.append(line.removeprefix("STAGE:"))
                if target_stage in observed:
                    break
    except Exception:
        _sigkill_and_reap_owned(process)
        raise
    finally:
        selector.close()
    if process.poll() is not None:
        _stdout, stderr_raw = _sigkill_and_reap_owned(process)
        stderr = bytes(stderr_raw or b"").decode("utf-8", errors="replace")
        raise FaultEvidenceError(f"instruction worker exited before owned SIGKILL: {stderr.strip()}")
    _stdout, stderr_raw = _sigkill_and_reap_owned(process)
    stderr = bytes(stderr_raw or b"").decode("utf-8", errors="replace")
    if process.returncode != -signal.SIGKILL or stderr.strip():
        raise FaultEvidenceError("instruction worker was not cleanly reaped by owned SIGKILL")

    acknowledged = "COMMIT_ACK" in observed
    before_jobs, before_payloads, integrity = _inspect_job(path, job_id)
    if integrity != "ok" or before_jobs not in (0, 1):
        raise FaultEvidenceError("instruction-stage database is not atomically recoverable")
    if before_payloads not in (0, PAYLOAD_ROWS_PER_JOB):
        raise FaultEvidenceError("instruction-stage payload transaction was partially visible")
    if before_jobs == 0 and before_payloads != 0:
        raise FaultEvidenceError("instruction-stage payload survived without its job")
    if before_jobs == 1 and before_payloads != PAYLOAD_ROWS_PER_JOB:
        raise FaultEvidenceError("instruction-stage committed job has an incomplete payload")
    if acknowledged and before_jobs != 1:
        raise FaultEvidenceError("instruction-stage acknowledged commit was lost")
    recovered = _recover_from_plan(path, job_id)
    after_jobs, after_payloads, after_integrity = _inspect_job(path, job_id)
    if after_integrity != "ok" or after_jobs != 1 or after_payloads != PAYLOAD_ROWS_PER_JOB:
        raise FaultEvidenceError("instruction-stage recovery did not produce one complete job")
    return {
        "job_id": job_id,
        "target_stage": target_stage,
        "observed_stages": observed,
        "commit_ack_observed": acknowledged,
        "committed_before_recovery": before_jobs == 1,
        "payload_rows_before_recovery": before_payloads,
        "reinserted_from_plan": recovered,
        "final_job_rows": after_jobs,
        "final_payload_rows": after_payloads,
        "integrity_check": after_integrity,
        "signal": "SIGKILL",
    }


def _run_time_offset_cycle(path: Path, job_id: str, delay_us: int) -> dict[str, Any]:
    """Kill an owned worker after an unsynchronized bounded elapsed-time offset."""

    process = subprocess.Popen(
        [sys.executable, str(SCRIPT_PATH), "--instruction-worker", str(path), job_id],
        cwd=path.parent,
        env=_owned_worker_environment(path.parent),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        _read_owned_marker(process, expected="STAGE:READY")
        offset_started_ns = time.monotonic_ns()
        if delay_us:
            time.sleep(delay_us / 1_000_000)
        if process.poll() is not None:
            raise FaultEvidenceError("time-offset worker exited before owned SIGKILL")
        remainder, stderr = _sigkill_and_reap_owned(process)
        elapsed_us = (time.monotonic_ns() - offset_started_ns) // 1_000
    except Exception:
        _sigkill_and_reap_owned(process)
        raise
    if process.returncode != -signal.SIGKILL or stderr.strip():
        raise FaultEvidenceError("time-offset worker was not cleanly reaped by owned SIGKILL")
    observed = ["READY"]
    for line in remainder.splitlines():
        if not line.startswith("STAGE:"):
            raise FaultEvidenceError("time-offset worker emitted an unexpected marker")
        observed.append(line.removeprefix("STAGE:"))
    acknowledged = "COMMIT_ACK" in observed

    before_jobs, before_payloads, integrity = _inspect_job(path, job_id)
    if integrity != "ok" or before_jobs not in (0, 1):
        raise FaultEvidenceError("time-offset database is not atomically recoverable")
    if before_payloads not in (0, PAYLOAD_ROWS_PER_JOB):
        raise FaultEvidenceError("time-offset payload transaction was partially visible")
    if before_jobs == 0 and before_payloads != 0:
        raise FaultEvidenceError("time-offset payload survived without its job")
    if before_jobs == 1 and before_payloads != PAYLOAD_ROWS_PER_JOB:
        raise FaultEvidenceError("time-offset committed job has an incomplete payload")
    if acknowledged and before_jobs != 1:
        raise FaultEvidenceError("time-offset acknowledged commit was lost")
    recovered = _recover_from_plan(path, job_id)
    after_jobs, after_payloads, after_integrity = _inspect_job(path, job_id)
    if after_integrity != "ok" or after_jobs != 1 or after_payloads != PAYLOAD_ROWS_PER_JOB:
        raise FaultEvidenceError("time-offset recovery did not produce one complete job")
    return {
        "job_id": job_id,
        "scheduled_kill_offset_us": delay_us,
        "observed_elapsed_to_reap_us": int(elapsed_us),
        "observed_stages": observed,
        "commit_ack_observed": acknowledged,
        "committed_before_recovery": before_jobs == 1,
        "payload_rows_before_recovery": before_payloads,
        "reinserted_from_plan": recovered,
        "final_job_rows": after_jobs,
        "final_payload_rows": after_payloads,
        "integrity_check": after_integrity,
        "signal": "SIGKILL",
    }


def verify_evidence(evidence: Mapping[str, Any]) -> None:
    supplied = evidence.get("evidence_sha256")
    if not isinstance(supplied, str) or len(supplied) != 64:
        raise FaultEvidenceError("evidence_sha256 is missing")
    unhashed = dict(evidence)
    del unhashed["evidence_sha256"]
    if _sha256(unhashed) != supplied:
        raise FaultEvidenceError("evidence_sha256 does not match canonical evidence")


def run_fault_realism(
    workdir: Path,
    *,
    cycles: int = 12,
    include_apfs_sparse_image: bool = False,
) -> dict[str, Any]:
    if not 1 <= cycles <= 100:
        raise FaultEvidenceError("cycles must be between 1 and 100")
    sandbox = _validate_workdir(workdir)
    database = sandbox / "sigkill-commit-window.sqlite3"
    _initialize(database)
    instruction_database = sandbox / "sigkill-instruction-boundaries.sqlite3"
    _initialize(instruction_database)
    time_offset_database = sandbox / "sigkill-time-offsets.sqlite3"
    _initialize(time_offset_database)
    sqlite_vfs_fsync = _run_sqlite_vfs_fsync_drill(sandbox)
    sqlite_vfs_fsync_passed = sqlite_vfs_fsync.get("status") == "passed"
    apfs = _run_apfs_sparse_image_drill(sandbox) if include_apfs_sparse_image else {
        "status": "not_run",
        "reason": "include_apfs_sparse_image was false",
    }
    apfs_passed = apfs.get("status") == "passed"
    cycle_rows = [
        _run_cycle(
            database,
            f"sigkill-{index:03d}",
            DEFAULT_KILL_DELAYS_US[index % len(DEFAULT_KILL_DELAYS_US)],
        )
        for index in range(cycles)
    ]
    instruction_rows = [
        _run_instruction_cycle(
            instruction_database,
            f"instruction-{index:03d}",
            stage,
        )
        for index, stage in enumerate(TRANSACTION_STAGE_MARKERS)
    ]
    time_offset_rows = [
        _run_time_offset_cycle(
            time_offset_database,
            f"time-offset-{index:03d}",
            delay_us,
        )
        for index, delay_us in enumerate(DEFAULT_TIME_OFFSET_KILL_DELAYS_US)
    ]
    with sqlite3.connect(database, timeout=5) as connection:
        _configure(connection)
        final_jobs = int(connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0])
        unique_jobs = int(
            connection.execute("SELECT COUNT(DISTINCT job_id) FROM jobs").fetchone()[0]
        )
        final_payloads = int(connection.execute("SELECT COUNT(*) FROM payloads").fetchone()[0])
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
    with sqlite3.connect(instruction_database, timeout=5) as connection:
        _configure(connection)
        instruction_jobs = int(connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0])
        instruction_unique_jobs = int(
            connection.execute("SELECT COUNT(DISTINCT job_id) FROM jobs").fetchone()[0]
        )
        instruction_payloads = int(
            connection.execute("SELECT COUNT(*) FROM payloads").fetchone()[0]
        )
        instruction_integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
    with sqlite3.connect(time_offset_database, timeout=5) as connection:
        _configure(connection)
        time_offset_jobs = int(connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0])
        time_offset_unique_jobs = int(
            connection.execute("SELECT COUNT(DISTINCT job_id) FROM jobs").fetchone()[0]
        )
        time_offset_payloads = int(
            connection.execute("SELECT COUNT(*) FROM payloads").fetchone()[0]
        )
        time_offset_integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
    acknowledged = sum(bool(row["commit_ack_observed"]) for row in cycle_rows)
    committed = sum(bool(row["committed_before_recovery"]) for row in cycle_rows)
    recovered = sum(bool(row["reinserted_from_plan"]) for row in cycle_rows)
    unproven_requirements = [
        "APFS physical disk-full behavior",
        "power-loss or equivalent custom-VFS durability behavior",
        "machine-instruction-level SIGKILL offsets beyond the complete logical "
        "transaction-boundary sweep",
    ]
    if not sqlite_vfs_fsync_passed:
        unproven_requirements.insert(2, "SQLite VFS-boundary fsync I/O-error injection")
    evidence: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "script_sha256": hashlib.sha256(SCRIPT_PATH.read_bytes()).hexdigest(),
        "workload": WORKLOAD,
        "probe": "owned_sqlite_wal_sigkill_commit_window_sweep",
        "status": "passed_partial_evidence",
        "measurements": {
            "cycles_requested": cycles,
            "cycles_completed": len(cycle_rows),
            "kill_delays_us": [row["kill_delay_us"] for row in cycle_rows],
            "payload_rows_per_job": PAYLOAD_ROWS_PER_JOB,
            "payload_bytes_per_row": PAYLOAD_BYTES,
            "commit_acknowledged_before_kill": acknowledged,
            "committed_before_recovery": committed,
            "uncommitted_before_recovery": cycles - committed,
            "recovered_from_durable_plan": recovered,
            "acknowledged_commits_lost": 0,
            "partially_visible_transactions": 0,
            "final_job_rows": final_jobs,
            "final_unique_jobs": unique_jobs,
            "final_payload_rows": final_payloads,
            "duplicate_jobs": final_jobs - unique_jobs,
            "lost_jobs_after_recovery": cycles - unique_jobs,
            "integrity_check": integrity,
            "journal_mode": "wal",
            "synchronous": "FULL",
            "sqlite_vfs_fsync_io_error": sqlite_vfs_fsync,
            "machine_instruction_offset_sigkill": {
                "status": "blocked",
                "method_evaluated": "macOS ptrace PT_TRACE_ME/PT_STEP on an owned child",
                "reason": "no stable, reap-safe instruction-step trace evidence was available",
                "logical_transaction_boundary_sweep_substituted": False,
            },
            "apfs_sparse_image": apfs,
            "transaction_instruction_boundary_sweep": {
                "stages_requested": len(TRANSACTION_STAGE_MARKERS),
                "stages_completed": len(instruction_rows),
                "stage_markers": list(TRANSACTION_STAGE_MARKERS),
                "acknowledged_commits_lost": 0,
                "partially_visible_transactions": 0,
                "final_job_rows": instruction_jobs,
                "final_unique_jobs": instruction_unique_jobs,
                "final_payload_rows": instruction_payloads,
                "duplicate_jobs": instruction_jobs - instruction_unique_jobs,
                "lost_jobs_after_recovery": len(TRANSACTION_STAGE_MARKERS)
                - instruction_unique_jobs,
                "integrity_check": instruction_integrity,
                "cycles": instruction_rows,
            },
            "bounded_time_offset_sigkill_sweep": {
                "offsets_requested": len(DEFAULT_TIME_OFFSET_KILL_DELAYS_US),
                "offsets_completed": len(time_offset_rows),
                "scheduled_offsets_us": list(DEFAULT_TIME_OFFSET_KILL_DELAYS_US),
                "acknowledged_commits_lost": 0,
                "partially_visible_transactions": 0,
                "final_job_rows": time_offset_jobs,
                "final_unique_jobs": time_offset_unique_jobs,
                "final_payload_rows": time_offset_payloads,
                "duplicate_jobs": time_offset_jobs - time_offset_unique_jobs,
                "lost_jobs_after_recovery": len(DEFAULT_TIME_OFFSET_KILL_DELAYS_US)
                - time_offset_unique_jobs,
                "integrity_check": time_offset_integrity,
                "cycles": time_offset_rows,
            },
            "cycles": cycle_rows,
        },
        "coverage": {
            "owned_process_sigkill_at_commit_boundary": True,
            "owned_process_sigkill_at_bounded_time_offsets": True,
            "sqlite_wal_full_synchronous_sigkill": True,
            "sqlite_wal_reopen_and_integrity_check": True,
            "idempotent_recovery_from_known_input_plan": True,
            "all_logical_transaction_boundaries_sigkill": True,
            "sandbox_apfs_sparse_image_enospc": apfs_passed,
            "physical_apfs_enospc": False,
            "physical_power_interruption": False,
            "custom_sqlite_vfs_power_loss": False,
            "sqlite_vfs_fsync_io_error_injection": sqlite_vfs_fsync_passed,
            "arbitrary_instruction_offset_sigkill": False,
        },
        "blocker": {
            "code": BLOCKER_CODE,
            "eligible_to_clear": False,
            "decision": "blocker_retained",
            "unproven_requirements": unproven_requirements,
        },
        "safety": {
            "live_magi_state_accessed": False,
            "production_service_imported": False,
            "listener_started": False,
            "network_api_invoked": False,
            "production_port_accessed": False,
            "launchctl_invoked": False,
            "signals_sent_only_to_owned_children": True,
            "owned_custom_vfs_compiled_and_executed": sqlite_vfs_fsync_passed,
            "compiler_network_access": False,
            "owned_disk_image_attach_performed": apfs_passed,
            "owned_disk_image_detached_and_removed": apfs_passed,
            "sandbox_path_sha256": hashlib.sha256(str(sandbox).encode("utf-8")).hexdigest(),
        },
        "hash_scheme": "sha256(canonical-json-without-evidence_sha256)",
    }
    measurements = evidence["measurements"]
    instruction = measurements.get("transaction_instruction_boundary_sweep")
    time_offsets = measurements.get("bounded_time_offset_sigkill_sweep")
    vfs_fsync = measurements.get("sqlite_vfs_fsync_io_error")
    if not isinstance(measurements, dict) or any(
        (
            measurements["cycles_completed"] != cycles,
            measurements["acknowledged_commits_lost"] != 0,
            measurements["partially_visible_transactions"] != 0,
            measurements["final_job_rows"] != cycles,
            measurements["final_unique_jobs"] != cycles,
            measurements["final_payload_rows"] != cycles * PAYLOAD_ROWS_PER_JOB,
            measurements["duplicate_jobs"] != 0,
            measurements["lost_jobs_after_recovery"] != 0,
            measurements["integrity_check"] != "ok",
        )
    ):
        raise FaultEvidenceError("fault sweep thresholds failed")
    if not isinstance(instruction, dict) or any(
        (
            instruction.get("stages_completed") != len(TRANSACTION_STAGE_MARKERS),
            instruction.get("acknowledged_commits_lost") != 0,
            instruction.get("partially_visible_transactions") != 0,
            instruction.get("final_job_rows") != len(TRANSACTION_STAGE_MARKERS),
            instruction.get("final_unique_jobs") != len(TRANSACTION_STAGE_MARKERS),
            instruction.get("final_payload_rows")
            != len(TRANSACTION_STAGE_MARKERS) * PAYLOAD_ROWS_PER_JOB,
            instruction.get("duplicate_jobs") != 0,
            instruction.get("lost_jobs_after_recovery") != 0,
            instruction.get("integrity_check") != "ok",
        )
    ):
        raise FaultEvidenceError("transaction instruction-boundary sweep thresholds failed")
    if not isinstance(time_offsets, dict) or any(
        (
            time_offsets.get("offsets_completed") != len(DEFAULT_TIME_OFFSET_KILL_DELAYS_US),
            time_offsets.get("acknowledged_commits_lost") != 0,
            time_offsets.get("partially_visible_transactions") != 0,
            time_offsets.get("final_job_rows") != len(DEFAULT_TIME_OFFSET_KILL_DELAYS_US),
            time_offsets.get("final_unique_jobs") != len(DEFAULT_TIME_OFFSET_KILL_DELAYS_US),
            time_offsets.get("final_payload_rows")
            != len(DEFAULT_TIME_OFFSET_KILL_DELAYS_US) * PAYLOAD_ROWS_PER_JOB,
            time_offsets.get("duplicate_jobs") != 0,
            time_offsets.get("lost_jobs_after_recovery") != 0,
            time_offsets.get("integrity_check") != "ok",
        )
    ):
        raise FaultEvidenceError("bounded time-offset SIGKILL sweep thresholds failed")
    if sqlite_vfs_fsync_passed and (
        not isinstance(vfs_fsync, dict)
        or vfs_fsync.get("injected_error") != "SQLITE_IOERR_FSYNC"
        or vfs_fsync.get("partial_rows") != 0
        or vfs_fsync.get("final_rows") != 2
        or vfs_fsync.get("integrity_ok") != 1
        or vfs_fsync.get("power_loss_simulated") is not False
    ):
        raise FaultEvidenceError("custom SQLite VFS fsync evidence thresholds failed")
    evidence["evidence_sha256"] = _sha256(evidence)
    verify_evidence(evidence)
    return evidence


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workdir", type=Path)
    parser.add_argument("--cycles", type=int, default=12)
    parser.add_argument("--apfs-sparse-image", action="store_true")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--worker", nargs=2, metavar=("DATABASE", "JOB_ID"), help=argparse.SUPPRESS)
    parser.add_argument("--apfs-enospc-initialize", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--apfs-enospc-sqlite-full", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--apfs-enospc-recover", type=Path, help=argparse.SUPPRESS)
    parser.add_argument(
        "--instruction-worker",
        nargs=2,
        metavar=("DATABASE", "JOB_ID"),
        help=argparse.SUPPRESS,
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.apfs_enospc_initialize is not None:
        return _initialize_apfs_enospc_database(args.apfs_enospc_initialize)
    if args.apfs_enospc_sqlite_full is not None:
        return _apfs_enospc_sqlite_full_worker(args.apfs_enospc_sqlite_full)
    if args.apfs_enospc_recover is not None:
        return _apfs_enospc_recovery_worker(args.apfs_enospc_recover)
    if args.worker:
        return _worker(Path(args.worker[0]), args.worker[1])
    if args.instruction_worker:
        return _instruction_worker(Path(args.instruction_worker[0]), args.instruction_worker[1])
    if args.workdir is None:
        print(json.dumps({"ok": False, "error": "--workdir is required"}), file=sys.stderr)
        return 2
    try:
        evidence = run_fault_realism(
            args.workdir,
            cycles=args.cycles,
            include_apfs_sparse_image=args.apfs_sparse_image,
        )
    except (FaultEvidenceError, OSError, sqlite3.Error, subprocess.SubprocessError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 2
    encoded = _canonical_json(evidence) + b"\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.output.with_suffix(args.output.suffix + ".tmp")
        temporary.write_bytes(encoded)
        os.replace(temporary, args.output)
    sys.stdout.buffer.write(encoded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
