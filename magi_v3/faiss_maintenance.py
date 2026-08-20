"""V3-only durable handoff from low-memory ingest to one heavy FAISS rebuild."""

from __future__ import annotations

import hashlib
from . import fcntl_compat as fcntl
import json
import os
import stat
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Mapping


LOW_MEMORY_VECTOR_JOB_IDS = frozenset(
    {
        "job_obsidian_ingest",
        "job_obsidian_vector_reindex_notes",
        "job_obsidian_vector_reindex_wiki",
    }
)
INTERNAL_REBUILD_JOB_ID = "__v3_faiss_rebuild__"
REBUILD_LANE = "batch"
_RUNTIME_BINDING_NAMES = (
    "MAGI_V3_PYTHON_RUNTIME",
    "MAGI_V3_PYTHON_RUNTIME_REALPATH",
    "MAGI_V3_PYTHON_RUNTIME_SHA256",
    "MAGI_V3_PYTHON_RUNTIME_MANIFEST",
    "MAGI_V3_PYTHON_RUNTIME_MANIFEST_SHA256",
    "MAGI_V3_PYTHON_RUNTIME_TREE_SHA256",
)


class FaissMaintenanceError(RuntimeError):
    pass


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _valid_sha256(value: str) -> bool:
    return len(value) == 64 and all(
        character in "0123456789abcdef" for character in value
    )


def _python_command(release_root: Path) -> Path:
    """Select the release launcher for deployed candidates, or the source venv.

    Immutable release bundles deliberately exclude ``venv``.  Their launchd
    environment binds an external Python tree, while ``bin/magi-v3-python``
    verifies both the immutable release inventory and that complete runtime
    binding before every exec.  Never bypass that launcher by invoking the
    external interpreter directly.
    """

    binding = {
        name: str(os.environ.get(name) or "").strip()
        for name in _RUNTIME_BINDING_NAMES
    }
    populated = {name for name, value in binding.items() if value}
    if populated:
        if populated != set(_RUNTIME_BINDING_NAMES):
            raise FaissMaintenanceError(
                "hash-bound candidate Python runtime binding is incomplete"
            )
        launcher = release_root / "bin/magi-v3-python"
        try:
            launcher_metadata = launcher.lstat()
        except OSError as exc:
            raise FaissMaintenanceError(f"candidate Python launcher is unavailable: {exc}") from exc
        if (
            launcher.is_symlink()
            or not stat.S_ISREG(launcher_metadata.st_mode)
            or not os.access(launcher, os.X_OK)
        ):
            raise FaissMaintenanceError("candidate Python launcher is unsafe or not executable")

        runtime = Path(binding["MAGI_V3_PYTHON_RUNTIME"])
        runtime_realpath = Path(binding["MAGI_V3_PYTHON_RUNTIME_REALPATH"])
        manifest = Path(binding["MAGI_V3_PYTHON_RUNTIME_MANIFEST"])
        if (
            not runtime.is_absolute()
            or not runtime_realpath.is_absolute()
            or not manifest.is_absolute()
        ):
            raise FaissMaintenanceError("candidate Python runtime paths must be absolute")
        try:
            observed_realpath = runtime.resolve(strict=True)
            real_metadata = runtime_realpath.lstat()
            manifest_metadata = manifest.lstat()
        except OSError as exc:
            raise FaissMaintenanceError(
                f"candidate Python runtime binding is unavailable: {exc}"
            ) from exc
        if (
            observed_realpath != runtime_realpath
            or runtime_realpath.is_symlink()
            or not stat.S_ISREG(real_metadata.st_mode)
            or not os.access(runtime, os.X_OK)
            or manifest.is_symlink()
            or not stat.S_ISREG(manifest_metadata.st_mode)
        ):
            raise FaissMaintenanceError("candidate Python runtime binding is unsafe")
        runtime_sha = binding["MAGI_V3_PYTHON_RUNTIME_SHA256"]
        manifest_sha = binding["MAGI_V3_PYTHON_RUNTIME_MANIFEST_SHA256"]
        tree_sha = binding["MAGI_V3_PYTHON_RUNTIME_TREE_SHA256"]
        if (
            not _valid_sha256(runtime_sha)
            or not _valid_sha256(manifest_sha)
            or not _valid_sha256(tree_sha)
            or _sha256_file(runtime_realpath) != runtime_sha
            or _sha256_file(manifest) != manifest_sha
        ):
            raise FaissMaintenanceError("candidate Python runtime hash binding failed")
        return launcher

    source_python = release_root / "venv/bin/python3"
    if source_python.is_file() and os.access(source_python, os.X_OK):
        return source_python
    raise FaissMaintenanceError(
        "candidate has no bundled venv and no complete hash-bound Python runtime binding"
    )


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{threading.get_ident()}.{time.time_ns()}.tmp"
    )
    data = (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    )
    with temporary.open("x", encoding="utf-8") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


@contextmanager
def _request_lock(request_path: Path):
    """Serialize cross-process request read/modify/write and conditional clear."""

    request_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = request_path.with_name(f".{request_path.name}.lock")
    descriptor = os.open(
        lock_path,
        os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_nlink != 1
        ):
            raise FaissMaintenanceError("FAISS rebuild request lock is unsafe")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


class FaissRebuildCoordinator:
    def __init__(
        self,
        release_root: Path,
        *,
        state_dir: Path | None = None,
        now: Callable[[], float] = time.time,
    ) -> None:
        self.release_root = release_root.expanduser().resolve()
        configured = str(
            os.environ.get("MAGI_SHARED_STATE_DIR")
            or os.environ.get("MAGI_RUNTIME_DIR")
            or ""
        ).strip()
        self.state_dir = (
            state_dir
            or (Path(configured).expanduser() if configured else Path.home() / "Library/Application Support/MAGI/shared")
        ).resolve()
        self.request_path = self.state_dir / "memory" / "faiss_rebuild_request.json"
        self.report_path = self.state_dir / "memory" / "faiss_rebuild_latest.json"
        self.worker_script = self.release_root / "scripts/ops/faiss_rebuild_worker.py"
        self.guard_script = self.release_root / "scripts/ops/resource_guarded_run.py"
        self.python = _python_command(self.release_root)
        self.now = now
        for path in (self.worker_script, self.guard_script, self.python):
            if not path.is_file():
                raise FaissMaintenanceError(f"required FAISS maintenance member is missing: {path}")

    @staticmethod
    def is_source_job(job_id: str) -> bool:
        return job_id in LOW_MEMORY_VECTOR_JOB_IDS

    @staticmethod
    def low_memory_environment(job_id: str) -> dict[str, str]:
        if job_id not in LOW_MEMORY_VECTOR_JOB_IDS:
            return {}
        return {
            "MEMORY_ENABLE_FAISS": "0",
            "MAGI_FAISS_DEFER_REBUILD": "1",
        }

    def _load_request(self) -> dict[str, Any] | None:
        try:
            payload = json.loads(self.request_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise FaissMaintenanceError(f"FAISS rebuild request is unreadable: {exc}") from exc
        if (
            not isinstance(payload, dict)
            or payload.get("schema_version") != 1
            or not isinstance(payload.get("generation"), int)
            or payload.get("generation", 0) < 1
            or not isinstance(payload.get("worker_script_sha256"), str)
            or len(payload.get("worker_script_sha256", "")) != 64
            or any(
                character not in "0123456789abcdef"
                for character in payload.get("worker_script_sha256", "")
            )
            or (
                payload.get("request_id") is not None
                and payload.get("request_id") != ""
                and (
                    not isinstance(payload.get("request_id"), str)
                    or len(payload.get("request_id", "")) != 32
                    or any(
                        character not in "0123456789abcdef"
                        for character in payload.get("request_id", "")
                    )
                )
            )
        ):
            raise FaissMaintenanceError("FAISS rebuild request identity/source binding is invalid")
        return payload

    def mark_required(self, job_id: str) -> dict[str, Any]:
        if job_id not in LOW_MEMORY_VECTOR_JOB_IDS:
            raise FaissMaintenanceError(f"job is not a low-memory vector writer: {job_id}")
        with _request_lock(self.request_path):
            existing = self._load_request()
            sources = set(existing.get("source_job_ids", [])) if existing else set()
            sources.add(job_id)
            payload = {
                "schema_version": 1,
                "status": "pending",
                "generation": int(existing.get("generation", 0) if existing else 0) + 1,
                "request_id": uuid.uuid4().hex,
                "source_job_ids": sorted(sources),
                "requested_at": self.now(),
                "attempts": int(existing.get("attempts", 0) if existing else 0),
                "next_attempt_at": 0.0,
                "worker_script_sha256": _sha256_file(self.worker_script),
            }
            _atomic_json(self.request_path, payload)
        return payload

    def ready(self) -> bool:
        request = self._load_request()
        return bool(request and float(request.get("next_attempt_at") or 0.0) <= self.now())

    def run_rebuild(self, runner: Callable[..., Any]) -> bool:
        with _request_lock(self.request_path):
            request = self._load_request()
            if request is None:
                return True
            current_worker_sha = _sha256_file(self.worker_script)
            if (
                request.get("worker_script_sha256") != current_worker_sha
                or not request.get("request_id")
            ):
                request = dict(request)
                request["generation"] = int(request["generation"]) + 1
                request["request_id"] = uuid.uuid4().hex
                request["status"] = "pending"
                request["next_attempt_at"] = 0.0
                request["worker_script_sha256"] = current_worker_sha
                _atomic_json(self.request_path, request)
        generation = int(request["generation"])
        request_id = str(request["request_id"])
        worker_sha = str(request["worker_script_sha256"])
        argv = [
            str(self.python),
            str(self.guard_script),
            "--job-id",
            "v3_faiss_rebuild",
            "--block-at",
            "core_only",
            "--require-free-inactive-gb",
            "4",
            "--timeout-sec",
            "3600",
            "--",
            str(self.python),
            str(self.worker_script),
            "--request",
            str(self.request_path),
            "--expected-generation",
            str(generation),
            "--expected-request-id",
            request_id,
            "--expected-script-sha256",
            worker_sha,
            "--json-out",
            str(self.report_path),
        ]
        try:
            result = runner(
                argv,
                timeout_sec=3660,
                cwd=str(self.release_root),
                env_extra={
                    "MEMORY_ENABLE_FAISS": "1",
                    "MAGI_FAISS_REBUILD_STREAMING": "1",
                },
            )
            success = int(result.returncode or 0) == 0 and not bool(
                getattr(result, "timed_out", False)
            )
        except Exception:
            success = False
        if success:
            return True
        with _request_lock(self.request_path):
            latest = self._load_request()
            if latest is None:
                raise FaissMaintenanceError(
                    "failed rebuild unexpectedly removed its durable request"
                )
            # A newer successful ingest is a new CAS generation.  The old
            # worker must neither overwrite nor delay that pending request.
            if (
                int(latest["generation"]) != generation
                or latest.get("request_id") != request_id
            ):
                return False
            attempts = int(latest.get("attempts", 0)) + 1
            latest["attempts"] = attempts
            latest["status"] = "retry_pending"
            latest["next_attempt_at"] = self.now() + min(
                3600, 30 * (2 ** min(attempts - 1, 7))
            )
            _atomic_json(self.request_path, latest)
        return False
