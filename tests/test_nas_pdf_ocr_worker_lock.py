from __future__ import annotations

import sqlite3
from types import SimpleNamespace


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

    monkeypatch.setattr(worker, "DB_PATH", str(db_path))
    monkeypatch.setattr(worker, "STALE_PROCESSING_MINUTES", 1)
    monkeypatch.setattr(worker, "ensure_nas_mount", lambda: True)
    monkeypatch.setattr(worker, "_is_digital_pdf", lambda _path: False)
    monkeypatch.setattr(worker, "OCRMYPDF_BIN", "/bin/echo")

    worker.init_db()
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO ocr_queue (file_path, status, last_attempt, attempt_count) "
        "VALUES (?, 'processing', datetime('now', '-2 hours'), 0)",
        (str(pdf),),
    )
    conn.commit()
    conn.close()

    def fake_run(cmd, capture_output=True, text=True, timeout=1200):
        out_path = cmd[-1]
        with open(out_path, "wb") as fh:
            fh.write(b"%PDF-1.4\n" + b"x" * 2048)
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(worker.subprocess, "run", fake_run)

    result = worker._run_worker_locked(batch_size=1)

    assert result["stale_requeued"] == 1
    assert result["completed"] == 1
    assert existing_archive.read_bytes() == b"do not overwrite"
    archived = sorted(archive_dir.glob("scan*.pdf"))
    assert len(archived) == 2
    assert not pdf.exists()
