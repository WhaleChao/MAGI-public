from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace


def test_explicit_existing_nas_root_bypasses_production_mount_probe(monkeypatch, tmp_path):
    from api import nas_mount_guard
    from skills.documents import nas_pdf_ocr_worker as worker

    fixture_root = tmp_path / "nas-cases"
    fixture_root.mkdir()
    monkeypatch.setenv("MAGI_NAS_CASE_ROOT", str(fixture_root))
    monkeypatch.setattr(worker, "NAS_ROOT", str(fixture_root))
    monkeypatch.setattr(
        nas_mount_guard,
        "ensure_nas_mounts",
        lambda: (_ for _ in ()).throw(AssertionError("production mount probe invoked")),
    )

    assert worker.ensure_nas_mount() is True


def test_nas_ocr_uses_homebrew_libexec_python(monkeypatch, tmp_path):
    from skills.documents import nas_pdf_ocr_worker as worker

    interpreter = tmp_path / "ocr-python"
    interpreter.write_text("#!/bin/sh\n", encoding="utf-8")
    interpreter.chmod(0o755)
    monkeypatch.setenv("MAGI_NAS_OCRMY_PDF_PYTHON", str(interpreter))

    assert worker._ocrmypdf_command_prefix() == [
        str(interpreter),
        "-m",
        "ocrmypdf",
    ]


def test_nas_pdf_ocr_worker_skips_before_queue_when_lock_busy(monkeypatch):
    from skills.documents import nas_pdf_ocr_worker as worker

    class HeldLock:
        acquired = False
        active_owner = {"owner": "first-worker", "pid": 4242}

        def as_dict(self):
            return {"acquired": False, "active_owner": self.active_owner}

    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("worker body should not run when queue lock is busy")

    monkeypatch.setattr(worker, "acquire_nas_ocr_queue_lock", lambda *_args, **_kwargs: HeldLock())
    monkeypatch.setattr(worker, "ensure_nas_mount", fail_if_called)
    monkeypatch.setattr(worker.sqlite3, "connect", fail_if_called)
    monkeypatch.setattr(worker, "_is_digital_pdf", fail_if_called)
    monkeypatch.setattr(worker.subprocess, "run", fail_if_called)

    result = worker.run_worker(batch_size=1)

    assert result["ok"] is True
    assert result["skipped"] is True
    assert result["reason"] == "already_running"
    assert result["active_pid"] == 4242
    assert result["active_owner"] == "first-worker"


def test_nas_pdf_ocr_worker_retries_stale_processing_and_archives_without_overwrite(monkeypatch, tmp_path):
    from skills.documents import nas_pdf_ocr_worker as worker

    db_path = tmp_path / "queue.db"
    pdf = tmp_path / "case" / "scan.pdf"
    archive_dir = pdf.parent / worker.ARCHIVE_SUBDIR
    archive_dir.mkdir(parents=True)
    pdf.write_bytes(b"%PDF-1.4\nscan")
    existing_archive = archive_dir / "scan.pdf"
    existing_archive.write_bytes(b"do not overwrite")

    monkeypatch.setenv("MAGI_NAS_OCR_QUEUE_DB_PATH", str(db_path))
    monkeypatch.setattr(worker, "STALE_PROCESSING_MINUTES", 1)
    monkeypatch.setattr(worker, "ensure_nas_mount", lambda: True)
    monkeypatch.setattr(worker, "_is_digital_pdf", lambda _path: False)
    monkeypatch.setattr(worker, "OCRMYPDF_BIN", "/bin/echo")
    monkeypatch.setattr(worker, "OCRMYPDF_LIBEXEC_PYTHON", str(tmp_path / "missing-python"))
    monkeypatch.setattr(worker, "OCR_TEMP_ROOT", tmp_path / "ocr-temp")

    worker.init_db()
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO ocr_queue (file_path, status, last_attempt, attempt_count) "
        "VALUES (?, 'processing', datetime('now', '-2 hours'), 0)",
        (str(pdf),),
    )
    conn.commit()
    conn.close()

    recorded_cmd = []

    recorded_env = {}

    def fake_run(cmd, capture_output=True, text=True, timeout=1200, env=None):
        if "--version" in cmd:
            return SimpleNamespace(returncode=0, stderr="", stdout="17.4.1")
        recorded_cmd.extend(cmd)
        recorded_env.update(env or {})
        out_path = cmd[-1]
        with open(out_path, "wb") as fh:
            fh.write(b"%PDF-1.4\n" + b"x" * 2048)
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(worker.subprocess, "run", fake_run)
    monkeypatch.setenv("__PYVENV_LAUNCHER__", "/private/fake-parent-venv/bin/python")
    monkeypatch.setenv("VIRTUAL_ENV", "/private/fake-parent-venv")

    result = worker._run_worker_locked(batch_size=1)

    assert result["stale_requeued"] == 1
    assert result["completed"] == 1
    assert existing_archive.read_bytes() == b"do not overwrite"
    archived = sorted(archive_dir.glob("scan*.pdf"))
    assert len(archived) == 2
    assert not pdf.exists()
    assert "--skip-text" in recorded_cmd
    assert "--force-ocr" not in recorded_cmd
    assert recorded_cmd[recorded_cmd.index("--jobs") + 1] == "1"
    assert recorded_cmd[recorded_cmd.index("--optimize") + 1] == "0"
    assert recorded_cmd[recorded_cmd.index("--output-type") + 1] == "pdf"
    assert recorded_cmd[recorded_cmd.index("--rasterizer") + 1] == "ghostscript"
    assert "--deskew" not in recorded_cmd
    assert recorded_env["OMP_THREAD_LIMIT"] == "1"
    assert recorded_env["PYTHONNOUSERSITE"] == "1"
    assert "__PYVENV_LAUNCHER__" not in recorded_env
    assert "VIRTUAL_ENV" not in recorded_env
    assert "PYTHONEXECUTABLE" not in recorded_env
    assert recorded_env["PATH"].startswith("/opt/homebrew/opt/ocrmypdf/libexec/bin:")
    assert recorded_env["TMPDIR"].startswith(str(worker.OCR_TEMP_ROOT))
    assert not recorded_cmd[-2].startswith(str(pdf.parent))
    assert not recorded_cmd[-1].startswith(str(pdf.parent))


def test_nas_pdf_ocr_worker_private_path_ref_does_not_expose_case_path(tmp_path):
    from skills.documents import nas_pdf_ocr_worker as worker

    pdf = tmp_path / "private-client" / "secret-case.pdf"
    pdf.parent.mkdir()
    pdf.write_bytes(b"x" * 1024)

    ref = worker._private_path_ref(str(pdf))

    assert ref.startswith("pdf:")
    assert "private-client" not in ref
    assert "secret-case" not in ref
    assert "0.0 MB" in ref


def test_nas_pdf_ocr_worker_log_is_private():
    from skills.documents import nas_pdf_ocr_worker as worker

    assert worker.LOG_FILE
    assert (worker.os.stat(worker.LOG_FILE).st_mode & 0o777) == 0o600


def test_nas_pdf_ocr_worker_log_fallback_honors_tmpdir(tmp_path):
    temporary = tmp_path / "bound-tmp"
    temporary.mkdir()
    env = os.environ.copy()
    env.pop("MAGI_RUNTIME_DIR", None)
    env.pop("MAGI_LOG_DIR", None)
    env["TMPDIR"] = str(temporary)
    env["PYTHONDONTWRITEBYTECODE"] = "1"

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import json; "
                "from skills.documents import nas_pdf_ocr_worker as worker; "
                "print(json.dumps({'log_file': worker.LOG_FILE, "
                "'mode': worker.os.stat(worker.LOG_FILE).st_mode & 0o777}))"
            ),
        ],
        cwd=str(Path(__file__).resolve().parents[1]),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout.strip())
    assert payload["log_file"] == str(temporary / "magi_nas_ocr.log")
    assert payload["mode"] == 0o600


def test_nas_pdf_ocr_worker_prefers_small_files_within_retry_tier(tmp_path):
    from skills.documents import nas_pdf_ocr_worker as worker

    db_path = tmp_path / "queue.db"
    small = tmp_path / "small.pdf"
    large = tmp_path / "large.pdf"
    small.write_bytes(b"x" * 100)
    large.write_bytes(b"x" * 10000)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE ocr_queue (file_path TEXT, status TEXT, attempt_count INTEGER)"
    )
    conn.execute("INSERT INTO ocr_queue VALUES (?, 'pending', 0)", (str(large),))
    conn.execute("INSERT INTO ocr_queue VALUES (?, 'pending', 0)", (str(small),))
    conn.commit()

    rows = worker._select_queue_rows(conn.cursor(), 1)

    assert rows == [(str(small),)]
    conn.close()


def test_nas_pdf_ocr_worker_preflights_before_copy(monkeypatch, tmp_path):
    from skills.documents import nas_pdf_ocr_worker as worker

    src = tmp_path / "source.pdf"
    src.write_bytes(b"%PDF-1.4\nscan")
    monkeypatch.setattr(worker, "OCR_TEMP_ROOT", tmp_path / "ocr-temp")
    monkeypatch.setattr(
        worker.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=1,
            stderr="isolated OCR runtime unavailable",
            stdout="",
        ),
    )
    monkeypatch.setattr(
        worker.shutil,
        "copy2",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("NAS input copied before OCR runtime preflight")
        ),
    )

    result, published = worker._run_ocr_locally(
        str(src),
        str(tmp_path / "output.pdf"),
    )

    assert result.returncode == 1
    assert published is False


def test_nas_pdf_ocr_worker_defers_large_files_outside_offpeak(monkeypatch, tmp_path):
    from skills.documents import nas_pdf_ocr_worker as worker

    db_path = tmp_path / "queue.db"
    pdf = tmp_path / "large.pdf"
    pdf.write_bytes(b"%PDF-1.4\nscan")
    monkeypatch.setenv("MAGI_NAS_OCR_QUEUE_DB_PATH", str(db_path))
    monkeypatch.setattr(worker, "LARGE_FILE_MIN_BYTES", 1)
    monkeypatch.setattr(worker, "_large_ocr_allowed_now", lambda: False)
    monkeypatch.setattr(worker, "ensure_nas_mount", lambda: True)
    monkeypatch.setattr(
        worker,
        "_is_digital_pdf",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("deferred large file was opened")
        ),
    )
    worker.init_db()
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO ocr_queue (file_path, status, attempt_count) "
        "VALUES (?, 'pending', 0)",
        (str(pdf),),
    )
    conn.commit()
    conn.close()

    result = worker._run_worker_locked(batch_size=1)

    assert result["ok"] is False
    assert result["success"] is False
    assert result["status"] == "deferred"
    assert result["deferred"] is True
    assert result["partial"] is False
    assert result["reason"] == "large_files_waiting_for_offpeak_window"
    assert result["deferred_large"] == 1
    assert result["processed"] == 0
    assert worker._worker_exit_code(result) == 75


def test_default_large_file_hours_cover_every_scheduled_offpeak_run() -> None:
    """The worker must not reject hours that its own cron deliberately schedules."""

    from skills.documents import nas_pdf_ocr_worker as worker

    assert worker.LARGE_FILE_HOURS == frozenset({0, 1, 2, 3, 4, 5, 6, 22, 23})


def test_nas_pdf_ocr_worker_scales_timeout_for_large_files(monkeypatch):
    from skills.documents import nas_pdf_ocr_worker as worker

    monkeypatch.setattr(worker, "BASE_OCR_TIMEOUT_SECONDS", 1200)
    monkeypatch.setattr(worker, "MAX_OCR_TIMEOUT_SECONDS", 4800)

    assert worker._ocr_timeout_seconds(1 * 1024 * 1024) == 1220
    assert worker._ocr_timeout_seconds(106 * 1024 * 1024) == 3320
    assert worker._ocr_timeout_seconds(1000 * 1024 * 1024) == 4800


def test_nas_pdf_ocr_worker_timeout_is_deferred_without_burning_retry(monkeypatch, tmp_path):
    from skills.documents import nas_pdf_ocr_worker as worker

    db_path = tmp_path / "queue.db"
    pdf = tmp_path / "scan.pdf"
    pdf.write_bytes(b"%PDF-1.4\nscan")
    monkeypatch.setenv("MAGI_NAS_OCR_QUEUE_DB_PATH", str(db_path))
    monkeypatch.setattr(worker, "ensure_nas_mount", lambda: True)
    monkeypatch.setattr(worker, "_is_digital_pdf", lambda _path: False)
    monkeypatch.setattr(
        worker,
        "_run_ocr_locally",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            subprocess.TimeoutExpired(["ocrmypdf"], 1200)
        ),
    )
    worker.init_db()
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO ocr_queue (file_path, status, attempt_count) VALUES (?, 'pending', 0)",
        (str(pdf),),
    )
    conn.commit()
    conn.close()

    result = worker._run_worker_locked(batch_size=1)
    conn = sqlite3.connect(db_path)
    status, attempts = conn.execute(
        "SELECT status, attempt_count FROM ocr_queue WHERE file_path=?",
        (str(pdf),),
    ).fetchone()
    conn.close()

    assert result["status"] == "deferred"
    assert result["reason"] == "ocr_time_budget_exhausted"
    assert result["deferred_timeout"] == 1
    assert result["failed"] == 0
    assert worker._worker_exit_code(result) == 75
    assert status == "pending"
    assert attempts == 0
