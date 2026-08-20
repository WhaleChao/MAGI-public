#!/usr/bin/env python3
"""Stream MariaDB vectors into a fresh low-memory FAISS index and publish atomically."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import resource
import sys
import time
from pathlib import Path
from typing import Any, Sequence


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from magi_v3.faiss_maintenance import _atomic_json, _request_lock


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _peak_rss_bytes() -> int:
    value = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    return value if sys.platform == "darwin" else value * 1024


def _load_request(
    path: Path, generation: int, request_id: str, script_sha256: str
) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != 1
        or payload.get("status") not in {"pending", "retry_pending"}
        or payload.get("generation") != generation
        or payload.get("request_id") != request_id
        or payload.get("worker_script_sha256") != script_sha256
    ):
        raise RuntimeError("FAISS rebuild request identity/generation/source binding failed")
    return payload


def run(
    *,
    request_path: Path,
    expected_generation: int,
    expected_request_id: str,
    expected_script_sha256: str,
    json_out: Path,
) -> dict[str, Any]:
    observed_script_sha256 = _sha256_file(Path(__file__).resolve())
    if observed_script_sha256 != expected_script_sha256:
        raise RuntimeError("FAISS rebuild worker source SHA-256 drifted")
    request = _load_request(
        request_path,
        expected_generation,
        expected_request_id,
        expected_script_sha256,
    )
    started = time.time()
    os.environ["MEMORY_ENABLE_FAISS"] = "1"
    from skills.memory.mem_bridge import DB_CONFIG
    from skills.memory.faiss_index import (
        ACTIVE_MANIFEST_FILE,
        FAISSMemoryIndex,
        INDEX_DIR,
        active_generation_paths,
    )

    index = FAISSMemoryIndex(dim=768, load_existing=False)
    build = index.build_from_db_streaming(
        DB_CONFIG,
        batch_size=int(os.environ.get("MAGI_FAISS_REBUILD_BATCH_SIZE", "512") or "512"),
        training_sample_size=int(
            os.environ.get("MAGI_FAISS_REBUILD_TRAINING_SAMPLE", "20000") or "20000"
        ),
        low_memory_ivfpq_threshold=int(
            os.environ.get("MAGI_FAISS_LOW_MEMORY_IVFPQ_THRESHOLD", "250000")
            or "250000"
        ),
        publish=False,
    )
    rss_limit = int(
        os.environ.get("MAGI_FAISS_REBUILD_RSS_LIMIT_BYTES", str(900 * 1024 * 1024))
        or str(900 * 1024 * 1024)
    )
    indexed = int(build["indexed_rows"])
    declared = int(build["declared_rows"])
    invalid = int(build["invalid_rows"])
    precommit_valid = bool(
        indexed + invalid == declared
        and invalid == 0
        and index.total == indexed
        and len(index._id_map) == indexed
        and len(set(index._id_map)) == indexed
        and int(build["max_batch_rows"]) <= int(
            os.environ.get("MAGI_FAISS_REBUILD_BATCH_SIZE", "512") or "512"
        )
    )
    published = False
    if precommit_valid and _peak_rss_bytes() <= rss_limit:
        # Serialization is staged under the write lock.  The final active
        # manifest is replaced only if the last RSS check still passes.
        published = index.save_to_disk(
            precommit_validator=lambda: _peak_rss_bytes() <= rss_limit
        )
    peak_rss = _peak_rss_bytes()
    success = precommit_valid and published and peak_rss <= rss_limit
    index_dir = Path(INDEX_DIR)
    files: dict[str, dict[str, Any]] = {}
    if success:
        active_paths = active_generation_paths(index_dir)
        files = {
            name: {
                "path": str(path),
                "sha256": _sha256_file(path),
                "size": path.stat().st_size,
            }
            for name, path in active_paths.items()
        }
        active_manifest = index_dir / ACTIVE_MANIFEST_FILE
        files[ACTIVE_MANIFEST_FILE] = {
            "path": str(active_manifest),
            "sha256": _sha256_file(active_manifest),
            "size": active_manifest.stat().st_size,
        }
    report: dict[str, Any] = {
        "schema_version": 1,
        "status": "passed" if success else "failed",
        "success": success,
        "generated_at": time.time(),
        "duration_seconds": round(time.time() - started, 6),
        "request_generation": expected_generation,
        "request_id": expected_request_id,
        "source_job_ids": request.get("source_job_ids", []),
        "worker_script_sha256": observed_script_sha256,
        "build": build,
        "peak_rss_bytes": peak_rss,
        "rss_limit_bytes": rss_limit,
        "published_files": files,
        "request_cleared": False,
    }
    _atomic_json(json_out, report)
    if not success:
        return report
    with _request_lock(request_path):
        latest = json.loads(request_path.read_text(encoding="utf-8"))
        if (
            isinstance(latest, dict)
            and latest.get("generation") == expected_generation
            and latest.get("request_id") == expected_request_id
            and latest.get("worker_script_sha256") == expected_script_sha256
        ):
            request_path.unlink()
            directory = os.open(request_path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            report["request_cleared"] = True
            _atomic_json(json_out, report)
    return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", required=True, type=Path)
    parser.add_argument("--expected-generation", required=True, type=int)
    parser.add_argument("--expected-request-id", required=True)
    parser.add_argument("--expected-script-sha256", required=True)
    parser.add_argument("--json-out", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        report = run(
            request_path=args.request.expanduser().resolve(),
            expected_generation=args.expected_generation,
            expected_request_id=args.expected_request_id,
            expected_script_sha256=args.expected_script_sha256,
            json_out=args.json_out.expanduser().resolve(),
        )
    except Exception as exc:
        report = {
            "schema_version": 1,
            "status": "failed",
            "success": False,
            "error": f"{type(exc).__name__}: {exc}"[:1000],
        }
        _atomic_json(args.json_out.expanduser().resolve(), report)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report.get("success") is True else 2


if __name__ == "__main__":
    raise SystemExit(main())
