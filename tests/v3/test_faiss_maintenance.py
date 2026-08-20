from __future__ import annotations

import hashlib
import json
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import mysql.connector
import pytest
import magi_v3.faiss_maintenance as faiss_maintenance_module

from magi_v3.cron_policy import CronDispatchPolicy
from magi_v3.cron_service import CronService, CronServiceConfig, PendingCronJob
from magi_v3.faiss_maintenance import (
    INTERNAL_REBUILD_JOB_ID,
    FaissMaintenanceError,
    FaissRebuildCoordinator,
)
from scripts.ops import faiss_rebuild_worker
from skills.memory import faiss_index


ROOT = Path(__file__).resolve().parents[2]
WORKER_RSS_FIXTURE_BYTES = 128 * 1024 * 1024
WORKER_RSS_LIMIT_BYTES = 900 * 1024 * 1024


@pytest.fixture(autouse=True)
def _bind_source_tree_python(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unit tests isolate FAISS behavior from the removed 2.2 GB source venv."""

    original = faiss_maintenance_module._python_command

    def resolve(root: Path) -> Path:
        if root.expanduser().resolve() == ROOT.resolve():
            return Path(sys.executable).resolve()
        return original(root)

    monkeypatch.setattr(faiss_maintenance_module, "_python_command", resolve)


def _cron_policy(*, batches=()) -> CronDispatchPolicy:
    """Keep focused FAISS tests independent of mutable schedule inputs."""

    return CronDispatchPolicy(
        lane_caps={"light": 2, "batch": 1, "maintenance": 1},
        shared_caps={"heavy": (frozenset({"batch", "maintenance"}), 1)},
        batch_job_ids=frozenset(batches),
        phase_delay_seconds={},
        policy_sha256="a" * 64,
        cron_jobs_sha256="b" * 64,
    )


def _isolate_direct_worker_rss(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep direct-call worker tests independent of pytest's lifetime RSS peak.

    Production launches the worker in a fresh subprocess, so ``ru_maxrss`` is
    scoped to that worker.  These tests call ``run`` in the long-lived pytest
    process; a preceding memory-heavy test must not turn a CAS assertion into
    an unrelated RSS rejection.  The production RSS gate itself remains
    exercised with a deterministic below-limit worker measurement.
    """

    monkeypatch.setenv(
        "MAGI_FAISS_REBUILD_RSS_LIMIT_BYTES", str(WORKER_RSS_LIMIT_BYTES)
    )
    monkeypatch.setattr(
        faiss_rebuild_worker,
        "_peak_rss_bytes",
        lambda: WORKER_RSS_FIXTURE_BYTES,
    )


def test_streaming_rebuild_never_fetches_more_than_the_bound_and_is_consistent(
    tmp_path: Path, monkeypatch
) -> None:
    total = 10_000

    class CountCursor:
        def execute(self, _sql, _params=None):
            return None

        def fetchone(self):
            return (total,)

        def close(self):
            return None

    class StreamCursor:
        def __init__(self):
            self.offset = 0
            self.max_requested = 0

        def execute(self, _sql, _params=None):
            return None

        def fetchmany(self, size):
            self.max_requested = max(self.max_requested, size)
            if self.offset >= total:
                return []
            end = min(total, self.offset + size)
            rows = [
                (index + 1, json.dumps([float((index + axis) % 11 + 1) for axis in range(8)]))
                for index in range(self.offset, end)
            ]
            self.offset = end
            return rows

        def close(self):
            return None

    stream = StreamCursor()

    class Connection:
        def __init__(self):
            self.calls = 0

        def cursor(self, **_kwargs):
            self.calls += 1
            return CountCursor() if self.calls == 1 else stream

        def close(self):
            return None

    monkeypatch.setattr(mysql.connector, "connect", lambda **_kwargs: Connection())
    monkeypatch.setattr(faiss_index, "INDEX_DIR", str(tmp_path / "index"))
    index = faiss_index.FAISSMemoryIndex(dim=8, load_existing=False)

    result = index.build_from_db_streaming({}, batch_size=256)

    assert result["declared_rows"] == total
    assert result["indexed_rows"] == total
    assert result["invalid_rows"] == 0
    assert result["max_batch_rows"] == 256
    assert stream.max_requested == 256
    assert index.total == total
    assert len(index._id_map) == len(set(index._id_map)) == total
    paths = faiss_index.active_generation_paths(tmp_path / "index")
    assert paths["mem_index.faiss"].is_file()
    assert paths["mem_idmap.npy"].is_file()


def _small_index(index_dir: Path, monkeypatch, ids: list[int]):
    monkeypatch.setattr(faiss_index, "INDEX_DIR", str(index_dir))
    index = faiss_index.FAISSMemoryIndex(dim=8, load_existing=False)
    vectors = [[float((doc_id + axis) % 7 + 1) for axis in range(8)] for doc_id in ids]
    index.add_batch(ids, __import__("numpy").asarray(vectors, dtype="float32"))
    assert index.save_to_disk() is True
    return index


@pytest.mark.parametrize(
    "fault_point",
    [
        "index_written",
        "idmap_written",
        "meta_written",
        "generation_manifest_written",
        "generation_renamed",
        "active_manifest_replaced",
        "directory_fsynced",
    ],
)
def test_publish_fault_at_every_commit_stage_never_exposes_a_torn_generation(
    tmp_path: Path, monkeypatch, fault_point: str
) -> None:
    index_dir = tmp_path / fault_point
    _small_index(index_dir, monkeypatch, [1, 2])
    replacement = faiss_index.FAISSMemoryIndex(dim=8, load_existing=False)
    replacement.add_batch(
        [3, 4, 5],
        __import__("numpy").asarray([[1.0] * 8, [2.0] * 8, [3.0] * 8], dtype="float32"),
    )

    def fail(point: str) -> None:
        if point == fault_point:
            raise RuntimeError(f"injected:{point}")

    replacement._publish_fault_hook = fail
    assert replacement.save_to_disk() is False
    reader = faiss_index.FAISSMemoryIndex(dim=8)

    assert reader._id_map in ([1, 2], [3, 4, 5])
    assert reader.total == len(reader._id_map)
    assert len(reader._id_map) == len(set(reader._id_map))


def test_reader_holds_shared_lock_and_never_observes_precommit_generation(
    tmp_path: Path, monkeypatch
) -> None:
    index_dir = tmp_path / "concurrent"
    _small_index(index_dir, monkeypatch, [1, 2])
    replacement = faiss_index.FAISSMemoryIndex(dim=8, load_existing=False)
    replacement.add_batch(
        [7, 8, 9],
        __import__("numpy").asarray([[1.0] * 8, [2.0] * 8, [3.0] * 8], dtype="float32"),
    )
    generation_ready = threading.Event()
    allow_commit = threading.Event()

    def pause(point: str) -> None:
        if point == "generation_renamed":
            generation_ready.set()
            assert allow_commit.wait(5)

    replacement._publish_fault_hook = pause
    writer = threading.Thread(target=replacement.save_to_disk)
    writer.start()
    assert generation_ready.wait(5)
    observed: list[list[int]] = []

    def read() -> None:
        observed.append(faiss_index.FAISSMemoryIndex(dim=8)._id_map)

    reader = threading.Thread(target=read)
    reader.start()
    reader.join(timeout=0.1)
    assert reader.is_alive()
    allow_commit.set()
    writer.join(timeout=5)
    reader.join(timeout=5)

    assert observed == [[7, 8, 9]]


def test_corrupt_active_generation_falls_back_to_previous_complete_generation(
    tmp_path: Path, monkeypatch
) -> None:
    index_dir = tmp_path / "fallback"
    _small_index(index_dir, monkeypatch, [1, 2])
    _small_index(index_dir, monkeypatch, [3, 4, 5])
    active = json.loads((index_dir / faiss_index.ACTIVE_MANIFEST_FILE).read_text())
    active_dir = index_dir / faiss_index.GENERATIONS_DIR / active["active_generation"]
    (active_dir / faiss_index.IDMAP_FILE).write_bytes(b"corrupt")

    reader = faiss_index.FAISSMemoryIndex(dim=8)

    assert reader._id_map == [1, 2]
    assert reader.total == 2


def test_lightweight_api_consumers_follow_the_committed_generation(
    tmp_path: Path, monkeypatch
) -> None:
    shared = tmp_path / "shared"
    index_dir = shared / "memory/index_cache"
    generation = index_dir / "generations/g-fixture"
    generation.mkdir(parents=True)
    (generation / "meta.json").write_text(
        json.dumps({"total": 23, "index_type": "ivf_pq", "dim": 768}),
        encoding="utf-8",
    )
    (generation / "mem_index.faiss").write_bytes(b"committed-index")
    (index_dir / "active_generation.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "active_generation": "g-fixture",
                "dim": 768,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("MAGI_SHARED_STATE_DIR", str(shared))
    monkeypatch.delenv("FAISS_INDEX_DIR", raising=False)
    from api.blueprints.admin_runtime import _read_faiss_metadata
    from api.runtime_paths import get_faiss_active_artifact

    assert get_faiss_active_artifact("mem_index.faiss", tmp_path) == (
        generation / "mem_index.faiss"
    )
    assert _read_faiss_metadata(tmp_path) == {
        "ok": True,
        "vectors": 23,
        "index_type": "ivf_pq",
        "metadata_only": True,
    }


def test_coordinator_persists_and_coalesces_rebuild_requests(tmp_path: Path) -> None:
    coordinator = FaissRebuildCoordinator(ROOT, state_dir=tmp_path, now=lambda: 100.0)

    first = coordinator.mark_required("job_obsidian_ingest")
    second = coordinator.mark_required("job_obsidian_vector_reindex_notes")

    assert first["generation"] == 1
    assert second["generation"] == 2
    assert second["source_job_ids"] == [
        "job_obsidian_ingest",
        "job_obsidian_vector_reindex_notes",
    ]
    assert coordinator.ready() is True
    assert coordinator.low_memory_environment("job_obsidian_ingest") == {
        "MEMORY_ENABLE_FAISS": "0",
        "MAGI_FAISS_DEFER_REBUILD": "1",
    }


def _candidate_release_without_venv(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    release = tmp_path / "immutable-candidate"
    launcher = release / "bin/magi-v3-python"
    worker = release / "scripts/ops/faiss_rebuild_worker.py"
    guard = release / "scripts/ops/resource_guarded_run.py"
    for path, source in (
        (launcher, ROOT / "bin/magi-v3-python"),
        (worker, ROOT / "scripts/ops/faiss_rebuild_worker.py"),
        (guard, ROOT / "scripts/ops/resource_guarded_run.py"),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(source.read_bytes())
        path.chmod(0o555)

    runtime = tmp_path / "external-runtime/bin/python3"
    runtime.parent.mkdir(parents=True)
    runtime.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    runtime.chmod(0o555)
    runtime_manifest = tmp_path / "runtime-inputs/python-runtime-manifest.json"
    runtime_manifest.parent.mkdir()
    runtime_manifest.write_text('{"schema_version":1}\n', encoding="utf-8")
    runtime_manifest.chmod(0o444)
    monkeypatch.setenv("MAGI_V3_PYTHON_RUNTIME", str(runtime))
    monkeypatch.setenv("MAGI_V3_PYTHON_RUNTIME_REALPATH", str(runtime.resolve()))
    monkeypatch.setenv(
        "MAGI_V3_PYTHON_RUNTIME_SHA256", hashlib.sha256(runtime.read_bytes()).hexdigest()
    )
    monkeypatch.setenv("MAGI_V3_PYTHON_RUNTIME_MANIFEST", str(runtime_manifest))
    monkeypatch.setenv(
        "MAGI_V3_PYTHON_RUNTIME_MANIFEST_SHA256",
        hashlib.sha256(runtime_manifest.read_bytes()).hexdigest(),
    )
    monkeypatch.setenv("MAGI_V3_PYTHON_RUNTIME_TREE_SHA256", "c" * 64)
    assert not (release / "venv").exists()
    return release, launcher


def test_candidate_layout_uses_hash_bound_launcher_without_bundled_venv(
    tmp_path: Path, monkeypatch
) -> None:
    release, launcher = _candidate_release_without_venv(tmp_path, monkeypatch)
    coordinator = FaissRebuildCoordinator(release, state_dir=tmp_path / "state")
    request = coordinator.mark_required("job_obsidian_ingest")
    observed: list[list[str]] = []

    def runner(argv, **_kwargs):
        observed.append(argv)
        separator = argv.index("--")
        assert argv[0] == str(launcher)
        assert argv[separator + 1] == str(launcher)
        assert argv[separator + 2] == str(coordinator.worker_script)
        return SimpleNamespace(returncode=2, timed_out=False)

    assert coordinator.python == launcher
    assert coordinator.run_rebuild(runner) is False
    assert len(observed) == 1
    latest = json.loads(coordinator.request_path.read_text())
    assert latest["generation"] == request["generation"]
    assert latest["request_id"] == request["request_id"]
    assert latest["status"] == "retry_pending"


def test_candidate_layout_rejects_drifted_runtime_hash(
    tmp_path: Path, monkeypatch
) -> None:
    release, _launcher = _candidate_release_without_venv(tmp_path, monkeypatch)
    monkeypatch.setenv("MAGI_V3_PYTHON_RUNTIME_SHA256", "0" * 64)

    with pytest.raises(FaissMaintenanceError, match="runtime hash binding failed"):
        FaissRebuildCoordinator(release, state_dir=tmp_path / "state")


def test_failed_rebuild_keeps_request_for_restart_with_bounded_backoff(
    tmp_path: Path,
) -> None:
    now = [100.0]
    coordinator = FaissRebuildCoordinator(
        ROOT, state_dir=tmp_path, now=lambda: now[0]
    )
    coordinator.mark_required("job_obsidian_ingest")

    result = coordinator.run_rebuild(
        lambda *_args, **_kwargs: SimpleNamespace(returncode=2, timed_out=False)
    )

    assert result is False
    request = json.loads(coordinator.request_path.read_text())
    assert request["status"] == "retry_pending"
    assert request["attempts"] == 1
    assert request["next_attempt_at"] == 130.0
    assert coordinator.ready() is False
    now[0] = 131.0
    restarted = FaissRebuildCoordinator(
        ROOT, state_dir=tmp_path, now=lambda: now[0]
    )
    assert restarted.ready() is True


def test_legacy_request_without_request_id_is_migrated_before_execution(
    tmp_path: Path,
) -> None:
    coordinator = FaissRebuildCoordinator(ROOT, state_dir=tmp_path, now=lambda: 100.0)
    coordinator.request_path.parent.mkdir(parents=True)
    coordinator.request_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": "pending",
                "generation": 4,
                "source_job_ids": ["job_obsidian_ingest"],
                "requested_at": 90.0,
                "attempts": 0,
                "next_attempt_at": 0.0,
                "worker_script_sha256": hashlib.sha256(
                    coordinator.worker_script.read_bytes()
                ).hexdigest(),
            }
        )
    )
    observed: list[list[str]] = []

    def runner(argv, **_kwargs):
        observed.append(argv)
        return SimpleNamespace(returncode=2, timed_out=False)

    assert coordinator.run_rebuild(runner) is False
    request = json.loads(coordinator.request_path.read_text())
    assert request["generation"] == 5
    assert len(request["request_id"]) == 32
    assert request["request_id"] in observed[0]


def test_cron_source_job_disables_faiss_before_real_process_and_marks_rebuild(
    tmp_path: Path,
) -> None:
    order: list[str] = []

    class Coordinator:
        @staticmethod
        def is_source_job(job_id):
            return job_id == "job_obsidian_ingest"

        @staticmethod
        def low_memory_environment(_job_id):
            return {"MEMORY_ENABLE_FAISS": "0", "MAGI_FAISS_DEFER_REBUILD": "1"}

        def mark_required(self, _job_id):
            order.append("marked")

        def ready(self):
            return False

    class Scheduler:
        def mark_job_started(self, _job_id, **kwargs):
            assert len(kwargs["command_sha256"]) == 64
            order.append("started")
            return True

        def mark_job_result(self, _job_id, **kwargs):
            assert kwargs["success"] is True
            order.append("recorded")
            return True

    def runner(_argv, **kwargs):
        order.append("ran")
        assert kwargs["env_extra"]["MEMORY_ENABLE_FAISS"] == "0"
        assert kwargs["env_extra"]["MAGI_FAISS_DEFER_REBUILD"] == "1"
        return SimpleNamespace(
            returncode=0, stdout='{"success":true}', stderr="", timed_out=False
        )

    service = CronService(
        CronServiceConfig(ROOT),
        process_runner=runner,
        dispatch_policy=_cron_policy(),
        faiss_coordinator=Coordinator(),  # type: ignore[arg-type]
    )
    service._execute(
        Scheduler(),  # type: ignore[arg-type]
        {
            "id": "job_obsidian_ingest",
            "command": f"{ROOT / 'venv/bin/python3'} {ROOT / 'skills/obsidian/action.py'} --task ingest",
            "timeout_sec": 60,
        },
    )

    assert order == ["started", "ran", "marked", "recorded"]


def test_failed_cron_source_job_never_marks_a_rebuild_request(tmp_path: Path) -> None:
    marked: list[str] = []

    class Coordinator:
        @staticmethod
        def is_source_job(job_id):
            return job_id == "job_obsidian_ingest"

        @staticmethod
        def low_memory_environment(_job_id):
            return {"MEMORY_ENABLE_FAISS": "0", "MAGI_FAISS_DEFER_REBUILD": "1"}

        def mark_required(self, job_id):
            marked.append(job_id)

        def ready(self):
            return False

    class Scheduler:
        def mark_job_started(self, _job_id, **kwargs):
            assert len(kwargs["command_sha256"]) == 64
            return True

        def mark_job_result(self, _job_id, **kwargs):
            assert kwargs["success"] is False
            return True

    service = CronService(
        CronServiceConfig(ROOT),
        process_runner=lambda *_args, **_kwargs: SimpleNamespace(
            returncode=2, stdout="", stderr="failed", timed_out=False
        ),
        dispatch_policy=_cron_policy(),
        faiss_coordinator=Coordinator(),  # type: ignore[arg-type]
    )
    service._execute(
        Scheduler(),  # type: ignore[arg-type]
        {
            "id": "job_obsidian_ingest",
            "command": f"{ROOT / 'venv/bin/python3'} {ROOT / 'skills/obsidian/action.py'} --task ingest",
            "timeout_sec": 60,
        },
    )

    assert marked == []


def test_rebuild_occupies_the_shared_heavy_lane(tmp_path: Path) -> None:
    class Coordinator:
        @staticmethod
        def ready():
            return True

        @staticmethod
        def run_rebuild(_runner):
            return True

        @staticmethod
        def is_source_job(_job_id):
            return False

        @staticmethod
        def low_memory_environment(_job_id):
            return {}

    class Future:
        def done(self):
            return False

    class Executor:
        def __init__(self):
            self.calls = []

        def submit(self, function, *args):
            self.calls.append((function, args))
            return Future()

    executor = Executor()
    service = CronService(
        CronServiceConfig(ROOT),
        process_runner=lambda *_args, **_kwargs: None,
        dispatch_policy=_cron_policy(
            batches={
                "job_obsidian_ingest",
                "job_obsidian_vector_reindex_notes",
                "job_obsidian_vector_reindex_wiki",
            }
        ),
        faiss_coordinator=Coordinator(),  # type: ignore[arg-type]
    )
    assert {
        service.dispatch_policy.lane_for({"id": job_id})
        for job_id in (
            "job_obsidian_ingest",
            "job_obsidian_vector_reindex_notes",
            "job_obsidian_vector_reindex_wiki",
        )
    } == {"batch"}
    running = {}
    lanes = {}
    service._dispatch_faiss_rebuild(executor, running, lanes)

    assert set(running) == {INTERNAL_REBUILD_JOB_ID}
    assert lanes == {INTERNAL_REBUILD_JOB_ID: "batch"}
    assert len(executor.calls) == 1
    pending = {
        "job_obsidian_ingest": PendingCronJob(
            job={"id": "job_obsidian_ingest"},
            lane="batch",
            scheduled_at=0.0,
            not_before=0.0,
            latest_start_at=1.0,
            sequence=1,
        )
    }
    service._dispatch_ready(None, executor, pending, running, lanes)  # type: ignore[arg-type]
    assert "job_obsidian_ingest" in pending
    assert len(executor.calls) == 1


def test_worker_clears_only_the_generation_it_built_and_reports_rss(
    tmp_path: Path, monkeypatch
) -> None:
    _isolate_direct_worker_rss(monkeypatch)
    index_dir = tmp_path / "index"
    index_dir.mkdir()

    class DummyIndex:
        def __init__(self, dim=768, load_existing=False):
            assert dim == 768 and load_existing is False
            self.total = 2
            self._id_map = [1, 2]

        def build_from_db_streaming(self, _config, **kwargs):
            assert kwargs["batch_size"] == 512
            assert kwargs["publish"] is False
            return {
                "declared_rows": 2,
                "indexed_rows": 2,
                "invalid_rows": 0,
                "training_rows": 0,
                "max_batch_rows": 2,
                "index_type": "flat",
            }

        def save_to_disk(self, *, precommit_validator):
            assert precommit_validator() is True
            for name in (
                "mem_index.faiss",
                "mem_idmap.npy",
                "meta.json",
                "generation_manifest.json",
            ):
                (index_dir / name).write_bytes(name.encode())
            return True

    import skills.memory.faiss_index as index_module
    import skills.memory.mem_bridge as bridge_module

    monkeypatch.setattr(index_module, "FAISSMemoryIndex", DummyIndex)
    monkeypatch.setattr(index_module, "INDEX_DIR", str(index_dir))
    generated = {
        name: index_dir / name
        for name in ("mem_index.faiss", "mem_idmap.npy", "meta.json", "generation_manifest.json")
    }
    monkeypatch.setattr(index_module, "active_generation_paths", lambda _root: generated)
    monkeypatch.setattr(bridge_module, "DB_CONFIG", {})
    worker_sha = hashlib.sha256(faiss_rebuild_worker.Path(faiss_rebuild_worker.__file__).read_bytes()).hexdigest()
    request = tmp_path / "request.json"
    request.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": "pending",
                "generation": 1,
                "request_id": "a" * 32,
                "source_job_ids": ["job_obsidian_ingest"],
                "worker_script_sha256": worker_sha,
            }
        ),
        encoding="utf-8",
    )
    (index_dir / index_module.ACTIVE_MANIFEST_FILE).write_text("{}", encoding="utf-8")

    report = faiss_rebuild_worker.run(
        request_path=request,
        expected_generation=1,
        expected_request_id="a" * 32,
        expected_script_sha256=worker_sha,
        json_out=tmp_path / "report.json",
    )

    assert report["success"] is True
    assert report["request_cleared"] is True
    assert report["peak_rss_bytes"] == WORKER_RSS_FIXTURE_BYTES
    assert report["rss_limit_bytes"] == WORKER_RSS_LIMIT_BYTES
    assert report["peak_rss_bytes"] <= report["rss_limit_bytes"]
    assert not request.exists()
    assert json.loads((tmp_path / "report.json").read_text())["request_cleared"] is True


def test_precommit_validator_rejects_generation_without_changing_active(
    tmp_path: Path, monkeypatch
) -> None:
    index_dir = tmp_path / "precommit"
    _small_index(index_dir, monkeypatch, [1, 2])
    active_before = (index_dir / faiss_index.ACTIVE_MANIFEST_FILE).read_bytes()
    replacement = faiss_index.FAISSMemoryIndex(dim=8, load_existing=False)
    replacement.add_batch(
        [9], __import__("numpy").asarray([[1.0] * 8], dtype="float32")
    )

    assert replacement.save_to_disk(precommit_validator=lambda: False) is False
    assert (index_dir / faiss_index.ACTIVE_MANIFEST_FILE).read_bytes() == active_before
    assert faiss_index.FAISSMemoryIndex(dim=8)._id_map == [1, 2]


def test_failed_old_generation_cannot_backoff_a_newer_request(tmp_path: Path) -> None:
    coordinator = FaissRebuildCoordinator(ROOT, state_dir=tmp_path, now=lambda: 100.0)
    coordinator.mark_required("job_obsidian_ingest")

    def runner(*_args, **_kwargs):
        coordinator.mark_required("job_obsidian_vector_reindex_notes")
        return SimpleNamespace(returncode=2, timed_out=False)

    assert coordinator.run_rebuild(runner) is False
    request = json.loads(coordinator.request_path.read_text())
    assert request["generation"] == 2
    assert request["status"] == "pending"
    assert request["attempts"] == 0
    assert request["next_attempt_at"] == 0.0


def test_failed_old_request_cannot_backoff_aba_generation_after_reset(
    tmp_path: Path,
) -> None:
    coordinator = FaissRebuildCoordinator(ROOT, state_dir=tmp_path, now=lambda: 100.0)
    initial = coordinator.mark_required("job_obsidian_ingest")

    def runner(*_args, **_kwargs):
        coordinator.request_path.unlink()
        replacement = coordinator.mark_required("job_obsidian_vector_reindex_notes")
        assert replacement["generation"] == initial["generation"] == 1
        assert replacement["request_id"] != initial["request_id"]
        return SimpleNamespace(returncode=2, timed_out=False)

    assert coordinator.run_rebuild(runner) is False
    request = json.loads(coordinator.request_path.read_text())
    assert request["generation"] == 1
    assert request["request_id"] != initial["request_id"]
    assert request["status"] == "pending"
    assert request["attempts"] == 0
    assert request["next_attempt_at"] == 0.0


def test_concurrent_marks_are_serialized_without_lost_generations(tmp_path: Path) -> None:
    coordinator = FaissRebuildCoordinator(ROOT, state_dir=tmp_path, now=lambda: 100.0)
    threads = [
        threading.Thread(
            target=coordinator.mark_required,
            args=(
                "job_obsidian_ingest"
                if index % 2 == 0
                else "job_obsidian_vector_reindex_notes",
            ),
        )
        for index in range(20)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert all(not thread.is_alive() for thread in threads)
    request = json.loads(coordinator.request_path.read_text())
    assert request["generation"] == 20
    assert request["source_job_ids"] == [
        "job_obsidian_ingest",
        "job_obsidian_vector_reindex_notes",
    ]


def test_worker_does_not_clear_newer_generation_created_during_build(
    tmp_path: Path, monkeypatch
) -> None:
    _isolate_direct_worker_rss(monkeypatch)
    index_dir = tmp_path / "cas-index"
    index_dir.mkdir()
    coordinator = FaissRebuildCoordinator(ROOT, state_dir=tmp_path / "state")
    initial = coordinator.mark_required("job_obsidian_ingest")

    class DummyIndex:
        def __init__(self, dim=768, load_existing=False):
            self.total = 1
            self._id_map = [1]

        def build_from_db_streaming(self, _config, **_kwargs):
            return {
                "declared_rows": 1,
                "indexed_rows": 1,
                "invalid_rows": 0,
                "training_rows": 0,
                "max_batch_rows": 1,
                "index_type": "flat",
            }

        def save_to_disk(self, *, precommit_validator):
            assert precommit_validator() is True
            coordinator.mark_required("job_obsidian_vector_reindex_notes")
            for name in (
                "mem_index.faiss",
                "mem_idmap.npy",
                "meta.json",
                "generation_manifest.json",
            ):
                (index_dir / name).write_bytes(name.encode())
            return True

    import skills.memory.faiss_index as index_module
    import skills.memory.mem_bridge as bridge_module

    generated = {
        name: index_dir / name
        for name in (
            "mem_index.faiss",
            "mem_idmap.npy",
            "meta.json",
            "generation_manifest.json",
        )
    }
    (index_dir / index_module.ACTIVE_MANIFEST_FILE).write_text("{}")
    monkeypatch.setattr(index_module, "FAISSMemoryIndex", DummyIndex)
    monkeypatch.setattr(index_module, "INDEX_DIR", str(index_dir))
    monkeypatch.setattr(index_module, "active_generation_paths", lambda _root: generated)
    monkeypatch.setattr(bridge_module, "DB_CONFIG", {})

    report = faiss_rebuild_worker.run(
        request_path=coordinator.request_path,
        expected_generation=initial["generation"],
        expected_request_id=initial["request_id"],
        expected_script_sha256=initial["worker_script_sha256"],
        json_out=tmp_path / "cas-report.json",
    )

    request = json.loads(coordinator.request_path.read_text())
    assert report["success"] is True
    assert report["request_cleared"] is False
    assert request["generation"] == 2
    assert request["status"] == "pending"


def test_old_worker_cannot_clear_aba_generation_after_request_reset(
    tmp_path: Path, monkeypatch
) -> None:
    _isolate_direct_worker_rss(monkeypatch)
    index_dir = tmp_path / "aba-index"
    index_dir.mkdir()
    coordinator = FaissRebuildCoordinator(ROOT, state_dir=tmp_path / "state")
    initial = coordinator.mark_required("job_obsidian_ingest")
    replacement: dict[str, object] = {}

    class DummyIndex:
        def __init__(self, dim=768, load_existing=False):
            self.total = 1
            self._id_map = [1]

        def build_from_db_streaming(self, _config, **_kwargs):
            return {
                "declared_rows": 1,
                "indexed_rows": 1,
                "invalid_rows": 0,
                "training_rows": 0,
                "max_batch_rows": 1,
                "index_type": "flat",
            }

        def save_to_disk(self, *, precommit_validator):
            assert precommit_validator() is True
            coordinator.request_path.unlink()
            replacement.update(
                coordinator.mark_required("job_obsidian_vector_reindex_notes")
            )
            for name in (
                "mem_index.faiss",
                "mem_idmap.npy",
                "meta.json",
                "generation_manifest.json",
            ):
                (index_dir / name).write_bytes(name.encode())
            return True

    import skills.memory.faiss_index as index_module
    import skills.memory.mem_bridge as bridge_module

    generated = {
        name: index_dir / name
        for name in (
            "mem_index.faiss",
            "mem_idmap.npy",
            "meta.json",
            "generation_manifest.json",
        )
    }
    (index_dir / index_module.ACTIVE_MANIFEST_FILE).write_text("{}")
    monkeypatch.setattr(index_module, "FAISSMemoryIndex", DummyIndex)
    monkeypatch.setattr(index_module, "INDEX_DIR", str(index_dir))
    monkeypatch.setattr(index_module, "active_generation_paths", lambda _root: generated)
    monkeypatch.setattr(bridge_module, "DB_CONFIG", {})

    report = faiss_rebuild_worker.run(
        request_path=coordinator.request_path,
        expected_generation=initial["generation"],
        expected_request_id=initial["request_id"],
        expected_script_sha256=initial["worker_script_sha256"],
        json_out=tmp_path / "aba-report.json",
    )

    request = json.loads(coordinator.request_path.read_text())
    assert report["success"] is True
    assert report["request_cleared"] is False
    assert request["generation"] == initial["generation"] == 1
    assert request["request_id"] == replacement["request_id"]
    assert request["request_id"] != initial["request_id"]
    assert request["status"] == "pending"


def test_invalid_streamed_build_is_never_published(tmp_path: Path, monkeypatch) -> None:
    index_dir = tmp_path / "invalid-index"
    index_dir.mkdir()
    saved: list[bool] = []

    class DummyIndex:
        def __init__(self, dim=768, load_existing=False):
            self.total = 1
            self._id_map = [1]

        def build_from_db_streaming(self, _config, **_kwargs):
            assert _kwargs["publish"] is False
            return {
                "declared_rows": 2,
                "indexed_rows": 1,
                "invalid_rows": 1,
                "training_rows": 0,
                "max_batch_rows": 2,
                "index_type": "flat",
            }

        def save_to_disk(self, **_kwargs):
            saved.append(True)
            return True

    import skills.memory.faiss_index as index_module
    import skills.memory.mem_bridge as bridge_module

    monkeypatch.setattr(index_module, "FAISSMemoryIndex", DummyIndex)
    monkeypatch.setattr(index_module, "INDEX_DIR", str(index_dir))
    monkeypatch.setattr(bridge_module, "DB_CONFIG", {})
    worker_sha = hashlib.sha256(Path(faiss_rebuild_worker.__file__).read_bytes()).hexdigest()
    request = tmp_path / "invalid-request.json"
    request.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": "pending",
                "generation": 1,
                "request_id": "b" * 32,
                "source_job_ids": ["job_obsidian_ingest"],
                "worker_script_sha256": worker_sha,
            }
        )
    )

    report = faiss_rebuild_worker.run(
        request_path=request,
        expected_generation=1,
        expected_request_id="b" * 32,
        expected_script_sha256=worker_sha,
        json_out=tmp_path / "invalid-report.json",
    )

    assert report["success"] is False
    assert report["published_files"] == {}
    assert saved == []
    assert request.exists()
