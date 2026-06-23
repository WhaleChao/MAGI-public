from __future__ import annotations


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
