"""Tests for skills.memory.job_queue — SQLite-backed persistent job queue."""

import os
import sys
import tempfile
import pytest

# Ensure project root is on path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


@pytest.fixture(autouse=True)
def _use_temp_db(monkeypatch, tmp_path):
    """Point job_queue at a temporary directory so tests don't touch production."""
    monkeypatch.setattr("skills.memory.job_queue._DB_DIR", str(tmp_path))
    monkeypatch.setattr("skills.memory.job_queue._DB_PATH", str(tmp_path / "test_jq.db"))
    # Reset thread-local connection so it picks up the new path
    import skills.memory.job_queue as jq
    jq._local.conn = None


def test_enqueue_and_read():
    from skills.memory import job_queue as jq

    job_id = jq.enqueue(platform="LINE", user_id="U123", user_text="hello")
    assert job_id
    job = jq.read(job_id)
    assert job["status"] == "queued"
    assert job["platform"] == "LINE"
    assert job["user_id"] == "U123"
    assert job["user_text"] == "hello"
    assert job["attempts"] == 0


def test_claim_and_complete():
    from skills.memory import job_queue as jq

    job_id = jq.enqueue(platform="Telegram", user_id="T456")
    assert jq.claim(job_id) is True
    job = jq.read(job_id)
    assert job["status"] == "running"
    assert job["attempts"] == 1
    assert job["worker_pid"] == os.getpid()

    jq.complete(job_id, result="done!")
    job = jq.read(job_id)
    assert job["status"] == "done"
    assert job["result"] == "done!"


def test_fail():
    from skills.memory import job_queue as jq

    job_id = jq.enqueue(platform="LINE", user_id="U789")
    jq.claim(job_id)
    jq.fail(job_id, error="something broke")
    job = jq.read(job_id)
    assert job["status"] == "failed"
    assert "something broke" in job["error"]


def test_abandon():
    from skills.memory import job_queue as jq

    job_id = jq.enqueue(platform="LINE", user_id="U000")
    jq.abandon(job_id, reason="too many retries")
    job = jq.read(job_id)
    assert job["status"] == "abandoned"


def test_list_by_status():
    from skills.memory import job_queue as jq

    id1 = jq.enqueue(platform="LINE", user_id="A")
    id2 = jq.enqueue(platform="LINE", user_id="B")
    jq.claim(id1)
    jq.complete(id1)

    queued = jq.list_by_status("queued")
    assert len(queued) == 1
    assert queued[0]["id"] == id2

    done = jq.list_by_status("done")
    assert len(done) == 1
    assert done[0]["id"] == id1


def test_stats():
    from skills.memory import job_queue as jq

    jq.enqueue(platform="LINE", user_id="A")
    jq.enqueue(platform="LINE", user_id="B")
    id3 = jq.enqueue(platform="LINE", user_id="C")
    jq.claim(id3)
    jq.complete(id3)

    s = jq.stats()
    assert s["total"] == 3
    assert s["active"] == 2  # 2 queued
    assert s["by_status"]["queued"] == 2
    assert s["by_status"]["done"] == 1


def test_recover_stale_running():
    from skills.memory import job_queue as jq

    # Simulate a job stuck in 'running' with a dead PID
    job_id = jq.enqueue(platform="LINE", user_id="X")
    jq.claim(job_id)
    # Manually set worker_pid to a non-existent PID
    conn = jq._get_conn()
    conn.execute("UPDATE jobs SET worker_pid = 99999 WHERE id = ?", (job_id,))
    conn.commit()

    resumed, abandoned = jq.recover_stale_running(max_attempts=3)
    assert resumed == 1
    assert abandoned == 0

    job = jq.read(job_id)
    assert job["status"] == "queued"  # Reset to queued for retry


def test_recover_abandons_after_max_attempts():
    from skills.memory import job_queue as jq

    job_id = jq.enqueue(platform="LINE", user_id="Y")
    # Simulate 3 failed attempts with dead worker
    conn = jq._get_conn()
    conn.execute(
        "UPDATE jobs SET status='running', attempts=3, worker_pid=99999 WHERE id=?",
        (job_id,),
    )
    conn.commit()

    resumed, abandoned = jq.recover_stale_running(max_attempts=3)
    assert resumed == 0
    assert abandoned == 1

    job = jq.read(job_id)
    assert job["status"] == "abandoned"


def test_cleanup_old():
    from skills.memory import job_queue as jq
    import time

    job_id = jq.enqueue(platform="LINE", user_id="OLD")
    jq.claim(job_id)
    jq.complete(job_id)

    # Backdate created_at to 60 days ago
    old_ts = time.time() - 60 * 86400
    conn = jq._get_conn()
    conn.execute("UPDATE jobs SET created_at = ? WHERE id = ?", (old_ts, job_id))
    conn.commit()

    deleted = jq.cleanup_old(days=30)
    assert deleted == 1
    assert jq.read(job_id) == {}


def test_payload_json_roundtrip():
    from skills.memory import job_queue as jq

    att = {"path": "/tmp/test.pdf", "filename": "test.pdf", "type": "file"}
    job_id = jq.enqueue(platform="LINE", user_id="Z", payload={"attachment": att})
    job = jq.read(job_id)
    assert isinstance(job["payload"], dict)
    assert job["payload"]["attachment"]["filename"] == "test.pdf"


def test_update_payload_merges_state():
    from skills.memory import job_queue as jq

    job_id = jq.enqueue(platform="LINE", user_id="P", payload={"attachment": {"filename": "x.pdf"}})
    job = jq.update_payload(job_id, {"progress": 25, "progress_phase": "compress"})

    assert job["payload"]["attachment"]["filename"] == "x.pdf"
    assert job["payload"]["progress"] == 25
    assert job["payload"]["progress_phase"] == "compress"
