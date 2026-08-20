from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from magi_v3.ocr_queue import (
    LEGACY_QUEUE_DB_NAME,
    OCRQueuePathError,
    resolve_nas_ocr_queue_db_path,
)


def test_producer_worker_and_status_share_explicit_queue_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from scripts import weekend_bookmark_batch as producer
    from skills.documents import nas_pdf_ocr_worker as worker

    queue = tmp_path / "shared" / "nas-ocr-queue.db"
    queue.parent.mkdir()
    monkeypatch.setenv("MAGI_NAS_OCR_QUEUE_DB_PATH", str(queue))

    producer_path = producer.resolve_nas_ocr_queue_db_path(
        release_root=producer.MAGI_ROOT
    )
    worker_path = Path(worker.queue_db_path())
    status_path = Path(worker.queue_db_path())

    assert producer_path == queue
    assert worker_path == producer_path
    assert status_path == producer_path

    added = producer.enqueue_ocr_followups(
        {"actions": [{"action": "ocr_then_bookmark", "path": "/fixture/scan.pdf"}]}
    )
    with sqlite3.connect(worker.queue_db_path()) as connection:
        queued = connection.execute(
            "SELECT file_path,status FROM ocr_queue"
        ).fetchall()

    assert added == 1
    assert queued == [("/fixture/scan.pdf", "pending")]


def test_sealed_v3_without_queue_binding_fails_closed(tmp_path: Path) -> None:
    release = tmp_path / "sealed-release"
    release.mkdir()
    (release / "RELEASE_COMPLETE.json").write_text("{}", encoding="utf-8")

    with pytest.raises(OCRQueuePathError, match="requires MAGI_NAS_OCR_QUEUE_DB_PATH"):
        resolve_nas_ocr_queue_db_path(environ={}, release_root=release)


def test_v2_without_queue_binding_keeps_legacy_home_database(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()

    resolved = resolve_nas_ocr_queue_db_path(
        environ={"MAGI_AGENT_DIR": str(tmp_path / "agent")},
        release_root=source,
    )

    assert resolved == Path.home() / LEGACY_QUEUE_DB_NAME


def test_sealed_v3_queue_must_be_external_and_canonical(tmp_path: Path) -> None:
    release = tmp_path / "release"
    release.mkdir()
    (release / "release-manifest.json").write_text("{}", encoding="utf-8")

    with pytest.raises(OCRQueuePathError, match="outside the sealed V3 release"):
        resolve_nas_ocr_queue_db_path(
            environ={"MAGI_NAS_OCR_QUEUE_DB_PATH": str(release / "queue.db")},
            release_root=release,
        )

    with pytest.raises(OCRQueuePathError, match="must be an absolute path"):
        resolve_nas_ocr_queue_db_path(
            environ={"MAGI_NAS_OCR_QUEUE_DB_PATH": "queue.db"},
            release_root=release,
        )
