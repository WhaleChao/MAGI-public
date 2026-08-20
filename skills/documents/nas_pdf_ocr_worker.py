#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os
import sys
import logging
import sqlite3
import subprocess
import time
import argparse
import hashlib
import json
import shutil
import tempfile
import fitz  # PyMuPDF
from pathlib import Path
from logging.handlers import RotatingFileHandler

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.ops.background_task_locks import acquire_lock, already_running_status
from magi_v3.ocr_queue import resolve_nas_ocr_queue_db_path

# Logger Setup
_RUNTIME_DIR = (os.environ.get("MAGI_RUNTIME_DIR") or "").strip()
_LOG_DIR = (
    (os.environ.get("MAGI_LOG_DIR") or "").strip()
    or (os.path.join(_RUNTIME_DIR, "logs") if _RUNTIME_DIR else tempfile.gettempdir())
)
LOG_FILE = os.path.join(_LOG_DIR, "magi_nas_ocr.log")
os.makedirs(_LOG_DIR, mode=0o700, exist_ok=True)
logger = logging.getLogger("NasOCRWorker")
logger.setLevel(logging.INFO)
fh = RotatingFileHandler(LOG_FILE, maxBytes=10*1024*1024, backupCount=3)
try:
    os.chmod(LOG_FILE, 0o600)
except OSError:
    pass
formatter = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s')
fh.setFormatter(formatter)
ch = logging.StreamHandler()
ch.setFormatter(formatter)
logger.addHandler(fh)
logger.addHandler(ch)

def queue_db_path() -> str:
    """Return the producer/consumer/diagnostic queue path contract."""

    return str(resolve_nas_ocr_queue_db_path(release_root=REPO_ROOT))


def _load_local_dotenv() -> None:
    try:
        from dotenv import load_dotenv as _load_dotenv
        repo_root = Path(__file__).resolve().parents[2]
        _load_dotenv(repo_root / ".env", override=False)
    except Exception:
        logger.debug("silent-catch dotenv load", exc_info=True)


_load_local_dotenv()

_NAS_HOME_USER = (
    os.environ.get("MAGI_NAS_HOME_USER")
    or os.environ.get("MAGI_NAS_USER")
    or "home"
).strip().strip("/\\") or "home"
NAS_ROOT = os.environ.get("MAGI_NAS_CASE_ROOT", f"/Volumes/homes/{_NAS_HOME_USER}/01_案件")
ARCHIVE_SUBDIR = "_Archive_No_OCR"
NAS_OCR_QUEUE_LOCK_NAME = "nas_pdf_ocr_queue_worker"
STALE_PROCESSING_MINUTES = int(os.environ.get("MAGI_NAS_OCR_STALE_MINUTES", "180") or "180")


def _private_path_ref(path: str) -> str:
    """Return a stable troubleshooting token without exposing a case path."""
    value = os.path.abspath(str(path or ""))
    digest = hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()[:12]
    try:
        size_mb = os.path.getsize(value) / (1024 * 1024)
        return f"pdf:{digest} ({size_mb:.1f} MB)"
    except OSError:
        return f"pdf:{digest}"

# OCR Tool Path
OCRMYPDF_BIN = os.environ.get("MAGI_NAS_OCRMY_PDF_BIN", "").strip() or "/opt/homebrew/bin/ocrmypdf"
OCRMYPDF_LIBEXEC_PYTHON = (
    os.environ.get("MAGI_NAS_OCRMY_PDF_PYTHON", "").strip()
    or "/opt/homebrew/opt/ocrmypdf/libexec/bin/python"
)
OCR_TEMP_ROOT = Path(
    os.environ.get(
        "MAGI_NAS_OCR_TEMP_ROOT",
        "~/Library/Caches/MAGI/nas-ocr",
    )
).expanduser()
LARGE_FILE_MIN_BYTES = max(
    1,
    int(os.environ.get("MAGI_NAS_OCR_LARGE_FILE_MIN_MB", "32") or "32"),
) * 1024 * 1024
LARGE_FILE_HOURS = frozenset(
    int(value)
    for value in (
        os.environ.get(
            "MAGI_NAS_OCR_LARGE_FILE_HOURS",
            "0,1,2,3,4,5,6,22,23",
        )
        or ""
    ).split(",")
    if value.strip().isdigit() and 0 <= int(value) <= 23
)
BASE_OCR_TIMEOUT_SECONDS = max(
    120,
    int(os.environ.get("MAGI_NAS_OCR_BASE_TIMEOUT_SECONDS", "1200") or "1200"),
)
MAX_OCR_TIMEOUT_SECONDS = max(
    BASE_OCR_TIMEOUT_SECONDS,
    int(os.environ.get("MAGI_NAS_OCR_MAX_TIMEOUT_SECONDS", "4800") or "4800"),
)


def _ocrmypdf_command_prefix() -> list[str]:
    """Use OCRmyPDF's private interpreter when Homebrew provides one.

    Calling the generic wrapper from MAGI's Python environment can import the
    wrong site-packages and fail with ``No module named ocrmypdf`` even though
    OCRmyPDF is installed.  The libexec interpreter is the supported isolated
    runtime for that formula.
    """
    interpreter = Path(
        os.environ.get("MAGI_NAS_OCRMY_PDF_PYTHON", "").strip()
        or OCRMYPDF_LIBEXEC_PYTHON
    ).expanduser()
    if interpreter.is_file() and os.access(interpreter, os.X_OK):
        return [str(interpreter), "-m", "ocrmypdf"]
    return [os.environ.get("MAGI_NAS_OCRMY_PDF_BIN", "").strip() or OCRMYPDF_BIN]


def _ocr_environment(work: Path) -> dict[str, str]:
    """Build an OCR-only environment without inheriting MAGI's virtualenv."""
    env = os.environ.copy()
    for key in (
        "PYTHONHOME",
        "PYTHONPATH",
        "PYTHONEXECUTABLE",
        "PYTHONPLATLIBDIR",
        "PYTHONUSERBASE",
        "VIRTUAL_ENV",
        "__PYVENV_LAUNCHER__",
    ):
        env.pop(key, None)
    env["PYTHONNOUSERSITE"] = "1"
    env["PATH"] = (
        "/opt/homebrew/opt/ocrmypdf/libexec/bin:"
        "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
    )
    env["TMPDIR"] = str(work)
    env["OMP_THREAD_LIMIT"] = "1"
    return env


def _large_ocr_allowed_now() -> bool:
    """Only let large NAS PDFs compete for I/O in declared off-peak hours."""
    return time.localtime().tm_hour in LARGE_FILE_HOURS


def _worker_exit_code(result: dict) -> int:
    if result.get("status") in {"deferred", "deferred_large_files"}:
        return 75
    return 0 if result.get("ok") is True else 1


def _ocr_timeout_seconds(file_size: int) -> int:
    """Give large scans enough time without exceeding the cron safety budget."""
    size_mb = max(0.0, float(file_size or 0) / (1024 * 1024))
    return min(
        MAX_OCR_TIMEOUT_SECONDS,
        max(BASE_OCR_TIMEOUT_SECONDS, BASE_OCR_TIMEOUT_SECONDS + int(size_mb * 20)),
    )


def acquire_nas_ocr_queue_lock(owner: str = "nas_pdf_ocr_worker"):
    return acquire_lock(
        NAS_OCR_QUEUE_LOCK_NAME,
        owner=owner,
        kind="singleton",
        blocking=False,
    )


def _empty_worker_counters():
    return {
        "processed": 0,
        "stale_requeued": 0,
        "missing": 0,
        "skipped_digital": 0,
        "completed": 0,
        "failed": 0,
        "deferred_large": 0,
        "deferred_timeout": 0,
    }


def _unique_path(path: str) -> str:
    """Return a non-existing path by appending a timestamp suffix if needed."""
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    for _ in range(50):
        candidate = f"{base}_{time.strftime('%Y%m%d_%H%M%S')}_{int(time.time() * 1000000) % 1000000:06d}{ext}"
        if not os.path.exists(candidate):
            return candidate
        time.sleep(0.001)
    return f"{base}_{time.strftime('%Y%m%d_%H%M%S')}_{os.getpid()}{ext}"


def _run_ocr_locally(pdf_path: str, out_path: str, *, timeout_seconds: int | None = None):
    """Run the expensive OCR pipeline on local disk and publish once complete.

    OCRmyPDF creates many short-lived raster files.  Keeping those files and
    the work output in a MAGI-owned local directory avoids NAS latency and
    prevents unrelated /tmp cleanup from invalidating an in-flight page.  The
    NAS destination is only replaced after a complete, non-trivial PDF exists.
    """
    OCR_TEMP_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(OCR_TEMP_ROOT, 0o700)
    except OSError:
        pass

    with tempfile.TemporaryDirectory(prefix="job-", dir=str(OCR_TEMP_ROOT)) as work_dir:
        work = Path(work_dir)
        local_input = work / "source.pdf"
        local_output = work / "output.pdf"
        command_prefix = _ocrmypdf_command_prefix()
        env = _ocr_environment(work)

        # Validate the exact isolated interpreter before spending minutes
        # copying a large NAS file.
        preflight = subprocess.run(
            [*command_prefix, "--version"],
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )
        if preflight.returncode != 0:
            return preflight, False

        shutil.copy2(pdf_path, local_input)

        # One OCR worker and one OpenMP thread keep the scheduled job from
        # contending with the API and the input method.  Deskew is deliberately
        # omitted: Tesseract's deskew preflight is fragile on blank court-record
        # pages and has repeatedly produced missing raster-file failures.
        cmd = [
            *command_prefix,
            "--skip-text",
            "-l", "chi_tra+eng",
            "--output-type", "pdf",
            "--rasterizer", "ghostscript",
            "--optimize", "0",
            "--jobs", "1",
            "--oversample", "300",
            str(local_input),
            str(local_output),
        ]
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=max(120, int(timeout_seconds or BASE_OCR_TIMEOUT_SECONDS)),
            env=env,
        )
        published = False
        if (
            result.returncode == 0
            and local_output.exists()
            and local_output.stat().st_size > 1000
        ):
            destination = Path(out_path)
            publish_tmp = destination.with_name(
                f".{destination.name}.magi-ocr-{os.getpid()}.tmp"
            )
            try:
                shutil.copy2(local_output, publish_tmp)
                if publish_tmp.stat().st_size <= 1000:
                    raise OSError("staged OCR output is unexpectedly small")
                os.replace(publish_tmp, destination)
                published = True
            finally:
                try:
                    publish_tmp.unlink(missing_ok=True)
                except OSError:
                    pass
        return result, published


def init_db():
    conn = sqlite3.connect(queue_db_path())
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS ocr_queue (
            file_path TEXT PRIMARY KEY,
            status TEXT DEFAULT 'pending',
            last_attempt TIMESTAMP,
            attempt_count INTEGER DEFAULT 0,
            error_msg TEXT
        )
    """)
    conn.commit()
    conn.close()

def ensure_nas_mount():
    # An explicitly bound root is authoritative for isolated validation and
    # operator-managed mounts.  Do not probe or mount the production NAS when
    # the caller supplied an existing alternate root.
    if os.environ.get("MAGI_NAS_CASE_ROOT", "").strip() and os.path.isdir(NAS_ROOT):
        return True
    try:
        from api.nas_mount_guard import ensure_nas_mounts
        res = ensure_nas_mounts()
        return any(res.values())
    except Exception as e:
        logger.warning(f"Failed to use nas_mount_guard: {e}. Checking manually.")
        return os.path.exists(NAS_ROOT)

def _is_digital_pdf(pdf_path: str, threshold: int = 150) -> bool:
    """判斷 PDF 是否為原生數位檔 (不需要做 OCR)"""
    try:
        doc = fitz.open(pdf_path)
        sample_pages = min(len(doc), 8)
        total_text_len = 0
        for i in range(sample_pages):
            page_text = doc[i].get_text()
            total_text_len += len(page_text.strip())
            
            # 單頁如果超過 threshold 字，通常就是原生數位 PDF
            if len(page_text.strip()) > threshold:
                return True
                
        # 加總平均
        if sample_pages > 0 and (total_text_len / sample_pages) > (threshold * 0.5):
            return True
            
        return False
    except Exception as e:
        logger.error(f"Error checking PDF type for {pdf_path}: {e}")
        return False

def scan_nas_for_pdfs(max_limit=1000, max_depth=5):
    """掃描 NAS 目錄，找出所有的未處理 PDF，放進 DB。
    NAS 友善：深度限制（預設 5）、每 50 目錄 sleep 0.05s 避免打掛 NAS。"""
    if not os.path.exists(NAS_ROOT):
        logger.error(f"NAS root {NAS_ROOT} not accessible.")
        return 0

    logger.info(f"Scanning {NAS_ROOT} for untreated PDFs (max_limit={max_limit}, max_depth={max_depth})...")
    conn = sqlite3.connect(queue_db_path())
    c = conn.cursor()

    added = 0
    dir_count = 0
    # 用有限深度的 stack-based DFS 取代無限 os.walk
    stack = [(NAS_ROOT, 0)]
    while stack:
        cur_dir, depth = stack.pop()
        dir_count += 1
        if dir_count % 50 == 0:
            time.sleep(0.05)  # NAS I/O 節流
        if dir_count > 5000:
            logger.warning(f"NAS scan safety cap reached ({dir_count} dirs). Stopping.")
            break

        try:
            with os.scandir(cur_dir) as it:
                for entry in it:
                    if entry.is_dir(follow_symlinks=False):
                        if ARCHIVE_SUBDIR in entry.name:
                            continue
                        if depth < max_depth:
                            stack.append((entry.path, depth + 1))
                    elif entry.is_file(follow_symlinks=False):
                        fname = entry.name
                        if not fname.lower().endswith('.pdf'):
                            continue
                        if "_OCR.pdf" in fname:
                            continue
                        full_path = entry.path
                        ocr_counterpart = full_path[:-4] + "_OCR.pdf"
                        if os.path.exists(ocr_counterpart):
                            continue
                        try:
                            c.execute("INSERT INTO ocr_queue (file_path) VALUES (?)", (full_path,))
                            added += 1
                            if max_limit > 0 and added >= max_limit:
                                conn.commit()
                                conn.close()
                                logger.info(f"Scan limit reached. Added {added} items ({dir_count} dirs visited).")
                                return added
                        except sqlite3.IntegrityError:
                            pass
        except Exception as e:
            logger.debug(f"scandir error on {cur_dir}: {e}")
            continue

    conn.commit()
    conn.close()
    logger.info(f"Scan complete. Added {added} new items to queue ({dir_count} dirs visited).")
    return added

def run_worker(batch_size=20):
    lock = acquire_nas_ocr_queue_lock()
    if not lock.acquired:
        status = already_running_status(lock, status="already_running")
        status["message"] = "NAS OCR queue worker is already running; skipped this run."
        logger.info(status["message"])
        return status

    try:
        return _run_worker_locked(batch_size=batch_size, counters=_empty_worker_counters())
    finally:
        lock.release()


def _select_queue_rows(cursor, batch_size: int) -> list[tuple[str]]:
    """Prefer smaller files within the same retry tier to reduce queue blocking."""
    limit = max(1, int(batch_size or 1))
    candidate_limit = max(limit, min(128, limit * 16))
    cursor.execute(
        """
        SELECT file_path, attempt_count FROM ocr_queue
        WHERE status IN ('pending', 'failed') AND attempt_count < 3
        ORDER BY attempt_count ASC, rowid ASC
        LIMIT ?
        """,
        (candidate_limit,),
    )
    candidates = cursor.fetchall()

    def queue_key(row):
        path, attempts = row
        try:
            size = os.path.getsize(path)
        except OSError:
            size = -1  # clean missing entries quickly
        return int(attempts or 0), size

    return [(str(path),) for path, _attempts in sorted(candidates, key=queue_key)[:limit]]


def _run_worker_locked(batch_size=20, counters=None):
    counters = counters or _empty_worker_counters()
    if not ensure_nas_mount():
        logger.error("NAS is not mounted. Exiting worker.")
        return {"ok": False, "status": "nas_not_mounted", "skipped": True, **counters}
        
    conn = sqlite3.connect(queue_db_path())
    c = conn.cursor()
    stale_minutes = max(1, int(STALE_PROCESSING_MINUTES))
    c.execute(
        f"""
        UPDATE ocr_queue
        SET status='failed',
            error_msg=COALESCE(error_msg, '') || ' | stale processing requeued'
        WHERE status='processing'
          AND attempt_count < 3
          AND (last_attempt IS NULL OR last_attempt < datetime('now', '-{stale_minutes} minutes'))
        """
    )
    counters["stale_requeued"] += int(c.rowcount or 0)
    if counters["stale_requeued"]:
        logger.warning("Requeued %d stale processing OCR jobs", counters["stale_requeued"])
        conn.commit()
    # Get pending items
    rows = _select_queue_rows(c, batch_size)
    if not rows:
        logger.info("Queue is empty. Nothing to do.")
        conn.close()
        return {"ok": True, "status": "empty", "skipped": False, **counters}
        
    logger.info(f"Processing batch of {len(rows)} files...")
    
    for row in rows:
        pdf_path = row[0]
        
        # Check if file still exists
        if not os.path.exists(pdf_path):
            c.execute("UPDATE ocr_queue SET status='missing' WHERE file_path=?", (pdf_path,))
            conn.commit()
            counters["missing"] += 1
            continue
            
        logger.info("Processing: %s", _private_path_ref(pdf_path))
        try:
            file_size = os.path.getsize(pdf_path)
        except OSError:
            file_size = -1
        if file_size >= LARGE_FILE_MIN_BYTES and not _large_ocr_allowed_now():
            logger.info(
                "   -> Deferred large OCR file to off-peak window (%0.1f MB)",
                file_size / (1024 * 1024),
            )
            c.execute(
                """
                UPDATE ocr_queue
                SET status='pending',
                    attempt_count=CASE
                        WHEN error_msg LIKE 'Timeout > 20m%' AND attempt_count > 0
                        THEN attempt_count - 1
                        ELSE attempt_count
                    END,
                    error_msg='Deferred: waiting for large-file off-peak window'
                WHERE file_path=?
                """,
                (pdf_path,),
            )
            conn.commit()
            counters["deferred_large"] += 1
            continue
        counters["processed"] += 1
        c.execute("UPDATE ocr_queue SET status='processing', attempt_count=attempt_count+1, last_attempt=datetime('now') WHERE file_path=?", (pdf_path,))
        conn.commit()
        
        # Check if native digital
        if _is_digital_pdf(pdf_path):
            logger.info("   -> Skipped (Detected as native digital PDF)")
            c.execute("UPDATE ocr_queue SET status='skipped_digital' WHERE file_path=?", (pdf_path,))
            conn.commit()
            counters["skipped_digital"] += 1
            continue
            
        out_path = _unique_path(pdf_path[:-4] + "_OCR.pdf")
        
        try:
            logger.info("   -> Running OCR (this may take a while)...")
            start_time = time.time()
            timeout_seconds = _ocr_timeout_seconds(file_size)
            result, published = _run_ocr_locally(
                pdf_path,
                out_path,
                timeout_seconds=timeout_seconds,
            )
            elapsed = time.time() - start_time
            
            if result.returncode == 0 and published:
                logger.info(f"   -> Success in {elapsed:.1f}s. Archive old file.")
                
                # Move old file
                parent_dir = os.path.dirname(pdf_path)
                archive_dir = os.path.join(parent_dir, ARCHIVE_SUBDIR)
                os.makedirs(archive_dir, exist_ok=True)
                
                old_filename = os.path.basename(pdf_path)
                archive_path = _unique_path(os.path.join(archive_dir, old_filename))
                
                try:
                    os.rename(pdf_path, archive_path)
                except Exception as e:
                    logger.warning(f"   -> Failed to move old file: {e}")
                    
                c.execute("UPDATE ocr_queue SET status='completed' WHERE file_path=?", (pdf_path,))
                counters["completed"] += 1
            else:
                error_msg = f"Returncode: {result.returncode}, Stderr: {result.stderr[-800:]}"
                logger.error(f"   -> Failed: {error_msg}")
                c.execute("UPDATE ocr_queue SET status='failed', error_msg=? WHERE file_path=?", (error_msg, pdf_path,))
                counters["failed"] += 1
                
        except subprocess.TimeoutExpired:
            logger.warning("   -> Deferred after OCR time budget was exhausted")
            c.execute(
                """
                UPDATE ocr_queue
                SET status='pending',
                    attempt_count=CASE WHEN attempt_count > 0 THEN attempt_count - 1 ELSE 0 END,
                    error_msg='Deferred: OCR time budget exhausted'
                WHERE file_path=?
                """,
                (pdf_path,),
            )
            counters["deferred_timeout"] += 1
        except Exception as e:
            logger.error(f"   -> Exception: {e}")
            c.execute("UPDATE ocr_queue SET status='failed', error_msg=? WHERE file_path=?", (str(e), pdf_path,))
            counters["failed"] += 1

        conn.commit()
    conn.close()
    deferred_only = (
        counters["failed"] == 0
        and (counters["deferred_large"] > 0 or counters["deferred_timeout"] > 0)
        and counters["completed"] == 0
        and counters["skipped_digital"] == 0
    )
    if deferred_only:
        reason = (
            "ocr_time_budget_exhausted"
            if counters["deferred_timeout"] > 0
            else "large_files_waiting_for_offpeak_window"
        )
        return {
            "ok": False,
            "success": False,
            "status": "deferred",
            "deferred": True,
            "partial": False,
            "reason": reason,
            "skipped": False,
            **counters,
        }
    return {
        "ok": counters["failed"] == 0 and counters["deferred_timeout"] == 0,
        "success": counters["failed"] == 0 and counters["deferred_timeout"] == 0,
        "status": "deferred" if counters["deferred_timeout"] > 0 else "completed",
        "deferred": counters["deferred_timeout"] > 0,
        "partial": counters["failed"] > 0,
        "skipped": False,
        **counters,
    }

if __name__ == "__main__":
    init_db()
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['scan', 'work', 'status'])
    parser.add_argument('--batch', type=int, default=20)
    args = parser.parse_args()
    
    # 確保 NAS script 能被 import
    sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))
    
    if args.command == 'scan':
        scan_nas_for_pdfs(max_limit=1000)
    elif args.command == 'work':
        result = run_worker(batch_size=args.batch)
        print(json.dumps(result, ensure_ascii=False))
        raise SystemExit(_worker_exit_code(result))
    elif args.command == 'status':
        conn = sqlite3.connect(queue_db_path())
        c = conn.cursor()
        c.execute("SELECT status, count(*) FROM ocr_queue GROUP BY status")
        print("\n--- OCR Queue Status ---")
        for row in c.fetchall():
            print(f"{row[0]:<20}: {row[1]}")
        print("------------------------\n")
        conn.close()
