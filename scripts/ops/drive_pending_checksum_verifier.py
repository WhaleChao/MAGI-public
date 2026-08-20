#!/usr/bin/env python3
"""Read-only checksum verifier for Drive/NAS ``pending_unverified`` items.

This command never calls the Drive API and has no code path for upload,
download, delete, rename, move, or directory creation.  It consumes one
existing Drive sync report, hashes a bounded batch of existing local files,
and emits de-identified evidence to stdout.

Items larger than one decimal gigabyte are excluded from normal batches.  A
large-file run must opt in with ``--only-over-1gb`` and is limited to one file.
Mounted network volumes also require explicit opt-in: filesystem reads can
enter a macOS uninterruptible-I/O state, where a userspace timeout cannot take
effect until the kernel call returns.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import stat
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence


SCHEMA_VERSION = 1
LARGE_FILE_THRESHOLD_BYTES = 1_000_000_000
DEFAULT_MAX_FILES = 4
DEFAULT_MAX_TOTAL_BYTES = 500_000_000
DEFAULT_MAX_FILE_BYTES = 1_500_000_000
DEFAULT_MAX_SECONDS = 900.0
DEFAULT_MAX_FILE_SECONDS = 600.0
DEFAULT_HASH_CHUNK_BYTES = 1024 * 1024
DEFAULT_MAX_READ_MBPS = 20.0


class VerificationError(RuntimeError):
    """The verifier input or policy is unsafe."""


class HashBudgetExceeded(TimeoutError):
    """A checksum did not finish inside the configured time budget."""


@dataclass(frozen=True, slots=True)
class HashEvidence:
    md5: str
    size: int
    mtime_ns_before: int
    mtime_ns_after: int
    device: int
    inode: int
    stable: bool


@dataclass(frozen=True, slots=True)
class PendingCandidate:
    item_id: str
    path: Path
    expected_md5: str
    expected_size: int | None
    source_report_sha256: str


Hasher = Callable[..., HashEvidence]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _json_sha256(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _coerce_nonnegative_int(value: Any) -> int | None:
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if result >= 0 else None


def _valid_md5(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    if len(normalized) != 32 or any(ch not in "0123456789abcdef" for ch in normalized):
        return ""
    return normalized


def _candidate_from_item(
    item: dict[str, Any],
    *,
    source_report_sha256: str,
) -> PendingCandidate | None:
    if str(item.get("status") or "").strip().lower() != "pending_unverified":
        return None

    local = item.get("local") if isinstance(item.get("local"), dict) else {}
    drive = item.get("drive") if isinstance(item.get("drive"), dict) else {}
    duplicate = (
        item.get("local_duplicate")
        if isinstance(item.get("local_duplicate"), dict)
        else {}
    )

    local_record = local or duplicate
    drive_record = drive or item
    raw_path = str(local_record.get("path") or "").strip()
    expected_md5 = _valid_md5(drive_record.get("md5"))
    if not raw_path or not expected_md5:
        return None

    local_size = _coerce_nonnegative_int(local_record.get("size"))
    drive_size = _coerce_nonnegative_int(drive_record.get("size"))
    expected_size = local_size if local_size is not None else drive_size
    identity = {
        "local_path": raw_path,
        "expected_md5": expected_md5,
        "expected_size": expected_size,
        "drive_id": str(drive_record.get("drive_id") or ""),
        "source_relative_path": str(item.get("source_relative_path") or ""),
    }
    return PendingCandidate(
        item_id=_json_sha256(identity),
        path=Path(raw_path).expanduser(),
        expected_md5=expected_md5,
        expected_size=expected_size,
        source_report_sha256=source_report_sha256,
    )


def pending_candidates(
    report: dict[str, Any],
    *,
    source_report_sha256: str,
) -> tuple[list[PendingCandidate], int]:
    candidates: list[PendingCandidate] = []
    input_pending = 0
    seen: set[str] = set()
    file_sync_plan = report.get("file_sync_plan")
    cases = file_sync_plan.get("cases") if isinstance(file_sync_plan, dict) else []
    for case in cases if isinstance(cases, list) else []:
        pending = case.get("pending") if isinstance(case, dict) else []
        for item in pending if isinstance(pending, list) else []:
            if not isinstance(item, dict):
                continue
            if str(item.get("status") or "").strip().lower() != "pending_unverified":
                continue
            input_pending += 1
            candidate = _candidate_from_item(
                item,
                source_report_sha256=source_report_sha256,
            )
            if candidate is not None and candidate.item_id not in seen:
                seen.add(candidate.item_id)
                candidates.append(candidate)
    return candidates, input_pending


def report_allowed_roots(report: dict[str, Any]) -> list[Path]:
    roots: list[Path] = []
    for raw in report.get("local_roots") or []:
        value = str(raw or "").strip()
        if value:
            roots.append(Path(value).expanduser())
    return roots


def _network_volume_like(path: Path) -> bool:
    """Conservatively classify mounted paths that need explicit I/O consent."""

    expanded = path.expanduser()
    return expanded.is_absolute() and len(expanded.parts) >= 2 and expanded.parts[:2] == ("/", "Volumes")


def _resolved_allowed_roots(roots: Sequence[Path]) -> tuple[Path, ...]:
    resolved: list[Path] = []
    for root in roots:
        try:
            value = root.resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        if value.is_dir() and value not in resolved:
            resolved.append(value)
    if not resolved:
        raise VerificationError("no_existing_allowed_root")
    return tuple(resolved)


def _resolve_candidate_path(path: Path, allowed_roots: Sequence[Path]) -> Path:
    if not path.is_absolute():
        raise VerificationError("candidate_path_not_absolute")
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise VerificationError(f"candidate_path_unavailable:{type(exc).__name__}") from exc
    if not any(resolved.is_relative_to(root) for root in allowed_roots):
        raise VerificationError("candidate_path_outside_allowed_roots")
    try:
        mode = resolved.stat().st_mode
    except OSError as exc:
        raise VerificationError(f"candidate_stat_failed:{type(exc).__name__}") from exc
    if not stat.S_ISREG(mode):
        raise VerificationError("candidate_not_regular_file")
    return resolved


@contextmanager
def _hash_alarm(seconds: float) -> Iterator[None]:
    can_alarm = (
        seconds > 0
        and hasattr(signal, "SIGALRM")
        and hasattr(signal, "setitimer")
    )
    if not can_alarm:
        yield
        return

    def _timeout(_signum: int, _frame: Any) -> None:
        raise HashBudgetExceeded("hash_timeout")

    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_timer = signal.setitimer(signal.ITIMER_REAL, 0)
    signal.signal(signal.SIGALRM, _timeout)
    signal.setitimer(signal.ITIMER_REAL, max(0.01, seconds))
    started = time.monotonic()
    try:
        yield
    finally:
        elapsed = time.monotonic() - started
        delay = max(0.0, float(previous_timer[0] or 0.0) - elapsed)
        interval = float(previous_timer[1] or 0.0)
        signal.setitimer(signal.ITIMER_REAL, delay, interval)
        signal.signal(signal.SIGALRM, previous_handler)


def hash_file_bounded(
    path: Path,
    *,
    max_seconds: float,
    chunk_bytes: int,
    max_read_mbps: float,
) -> HashEvidence:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise VerificationError("candidate_not_regular_file")
        digest = hashlib.md5()
        bytes_read = 0
        started = time.monotonic()
        with _hash_alarm(max_seconds):
            while True:
                chunk = os.read(descriptor, chunk_bytes)
                if not chunk:
                    break
                digest.update(chunk)
                bytes_read += len(chunk)
                elapsed = max(0.000001, time.monotonic() - started)
                if max_seconds > 0 and elapsed > max_seconds:
                    raise HashBudgetExceeded("hash_timeout")
                if max_read_mbps > 0:
                    target_elapsed = bytes_read / (max_read_mbps * 1_000_000)
                    if target_elapsed > elapsed:
                        time.sleep(min(target_elapsed - elapsed, 0.25))
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)

    stable = (
        before.st_dev == after.st_dev
        and before.st_ino == after.st_ino
        and before.st_size == after.st_size
        and before.st_mtime_ns == after.st_mtime_ns
        and bytes_read == after.st_size
    )
    return HashEvidence(
        md5=digest.hexdigest(),
        size=int(after.st_size),
        mtime_ns_before=int(before.st_mtime_ns),
        mtime_ns_after=int(after.st_mtime_ns),
        device=int(after.st_dev),
        inode=int(after.st_ino),
        stable=stable,
    )


def _retained(item_id: str, reason: str) -> dict[str, Any]:
    return {"item_id": item_id, "status": "pending_unverified", "reason": reason}


def verify_pending_report(
    report: dict[str, Any],
    *,
    source_report_sha256: str,
    allowed_roots: Sequence[Path],
    max_files: int = DEFAULT_MAX_FILES,
    max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    max_seconds: float = DEFAULT_MAX_SECONDS,
    max_file_seconds: float = DEFAULT_MAX_FILE_SECONDS,
    hash_chunk_bytes: int = DEFAULT_HASH_CHUNK_BYTES,
    max_read_mbps: float = DEFAULT_MAX_READ_MBPS,
    only_over_1gb: bool = False,
    start_offset: int = 0,
    hasher: Hasher = hash_file_bounded,
) -> dict[str, Any]:
    if max_files <= 0 or max_total_bytes <= 0 or max_file_bytes <= 0:
        raise VerificationError("resource_limits_must_be_positive")
    if max_seconds <= 0 or max_file_seconds <= 0:
        raise VerificationError("time_limits_must_be_positive")
    if start_offset < 0:
        raise VerificationError("start_offset_must_be_nonnegative")
    if not 64 * 1024 <= hash_chunk_bytes <= 8 * 1024 * 1024:
        raise VerificationError("hash_chunk_bytes_out_of_range")
    effective_max_files = 1 if only_over_1gb else max_files
    roots = _resolved_allowed_roots(allowed_roots)
    candidates, input_pending = pending_candidates(
        report,
        source_report_sha256=source_report_sha256,
    )
    recognized_ids = {candidate.item_id for candidate in candidates}
    retained: list[dict[str, Any]] = [
        _retained(_json_sha256({"report": source_report_sha256, "unusable": index}), "missing_verification_metadata")
        for index in range(max(0, input_pending - len(recognized_ids)))
    ]
    results: list[dict[str, Any]] = []
    selected_files = 0
    selected_bytes = 0
    started = time.monotonic()
    eligible_items = 0
    next_offset = start_offset
    batch_closed_reason = ""

    for candidate in candidates:
        try:
            resolved = _resolve_candidate_path(candidate.path, roots)
            current_size = int(resolved.stat().st_size)
        except VerificationError as exc:
            retained.append(_retained(candidate.item_id, str(exc).split(":", 1)[0]))
            continue

        is_over_1gb = current_size > LARGE_FILE_THRESHOLD_BYTES
        if only_over_1gb != is_over_1gb:
            retained.append(
                _retained(
                    candidate.item_id,
                    "normal_batch_excludes_over_1gb" if is_over_1gb else "large_batch_excludes_normal_files",
                )
            )
            continue
        item_offset = eligible_items
        eligible_items += 1
        if item_offset < start_offset:
            retained.append(_retained(candidate.item_id, "before_start_offset"))
            continue
        if batch_closed_reason:
            retained.append(_retained(candidate.item_id, batch_closed_reason))
            continue
        if current_size > max_file_bytes:
            retained.append(_retained(candidate.item_id, "max_file_bytes_exceeded"))
            batch_closed_reason = "batch_blocked_by_max_file_bytes"
            continue
        if selected_files >= effective_max_files:
            retained.append(_retained(candidate.item_id, "max_files_reached"))
            batch_closed_reason = "max_files_reached"
            continue
        if selected_bytes + current_size > max_total_bytes:
            retained.append(_retained(candidate.item_id, "max_total_bytes_reached"))
            batch_closed_reason = "batch_blocked_by_max_total_bytes"
            continue
        elapsed = time.monotonic() - started
        if elapsed >= max_seconds:
            retained.append(_retained(candidate.item_id, "max_seconds_reached"))
            batch_closed_reason = "batch_blocked_by_max_seconds"
            continue

        selected_files += 1
        selected_bytes += current_size
        if candidate.expected_size is not None and current_size != candidate.expected_size:
            results.append(
                {
                    "item_id": candidate.item_id,
                    "status": "explicit_conflict",
                    "evidence": {
                        "kind": "size_mismatch",
                        "expected_size": candidate.expected_size,
                        "actual_size": current_size,
                        "source_report_sha256": source_report_sha256,
                    },
                }
            )
            next_offset = item_offset + 1
            continue

        remaining_seconds = max(0.01, max_seconds - (time.monotonic() - started))
        per_file_seconds = min(max_file_seconds, remaining_seconds)
        try:
            evidence = hasher(
                resolved,
                max_seconds=per_file_seconds,
                chunk_bytes=hash_chunk_bytes,
                max_read_mbps=max_read_mbps,
            )
        except HashBudgetExceeded:
            retained.append(_retained(candidate.item_id, "hash_timeout"))
            batch_closed_reason = "batch_blocked_by_hash_timeout"
            continue
        except (OSError, VerificationError):
            retained.append(_retained(candidate.item_id, "hash_failed"))
            batch_closed_reason = "batch_blocked_by_hash_failure"
            continue
        if not evidence.stable:
            retained.append(_retained(candidate.item_id, "file_changed_during_hash"))
            batch_closed_reason = "batch_blocked_by_unstable_file"
            continue
        if candidate.expected_size is not None and evidence.size != candidate.expected_size:
            results.append(
                {
                    "item_id": candidate.item_id,
                    "status": "explicit_conflict",
                    "evidence": {
                        "kind": "size_changed_before_hash",
                        "expected_size": candidate.expected_size,
                        "actual_size": evidence.size,
                        "source_report_sha256": source_report_sha256,
                    },
                }
            )
            next_offset = item_offset + 1
            continue

        status_value = (
            "verified_equal"
            if evidence.md5.lower() == candidate.expected_md5
            else "explicit_conflict"
        )
        results.append(
            {
                "item_id": candidate.item_id,
                "status": status_value,
                "evidence": {
                    "kind": "md5_checksum",
                    "algorithm": "md5",
                    "expected_checksum": candidate.expected_md5,
                    "actual_checksum": evidence.md5.lower(),
                    "size": evidence.size,
                    "mtime_ns": evidence.mtime_ns_after,
                    "device": evidence.device,
                    "inode": evidence.inode,
                    "stable_during_hash": evidence.stable,
                    "source_report_sha256": source_report_sha256,
                },
            }
        )
        next_offset = item_offset + 1

    if start_offset > eligible_items:
        raise VerificationError("start_offset_out_of_range")
    verified_equal = sum(item["status"] == "verified_equal" for item in results)
    explicit_conflict = sum(item["status"] == "explicit_conflict" for item in results)
    accounted = len(results) + len(retained)
    accounting_ok = accounted == input_pending
    result_statuses_ok = all(
        item.get("status") in {"verified_equal", "explicit_conflict"}
        and isinstance(item.get("evidence"), dict)
        for item in results
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "mode": "verify_only_dry_run",
        "generated_at": _utc_now(),
        "ok": bool(accounting_ok and result_statuses_ok and explicit_conflict == 0),
        "action_required": bool(explicit_conflict),
        "data_actions": {
            "drive_api_calls": 0,
            "uploads": 0,
            "downloads": 0,
            "deletes": 0,
            "renames": 0,
            "moves": 0,
            "folders_created": 0,
            "files_written": 0,
        },
        "policy": {
            "max_files": effective_max_files,
            "max_total_bytes": max_total_bytes,
            "max_file_bytes": max_file_bytes,
            "max_seconds": max_seconds,
            "max_file_seconds": max_file_seconds,
            "hash_chunk_bytes": hash_chunk_bytes,
            "max_read_mbps": max_read_mbps,
            "large_file_threshold_bytes": LARGE_FILE_THRESHOLD_BYTES,
            "only_over_1gb": only_over_1gb,
            "start_offset": start_offset,
            "allowed_root_count": len(roots),
        },
        "source_report_sha256": source_report_sha256,
        "summary": {
            "input_pending_unverified": input_pending,
            "recognized_candidates": len(candidates),
            "eligible_items": eligible_items,
            "start_offset": start_offset,
            "next_offset": next_offset,
            "offset_out_of_range": start_offset > eligible_items,
            "batch_blocked_reason": batch_closed_reason,
            "selected_files": selected_files,
            "selected_bytes": selected_bytes,
            "verified_equal": verified_equal,
            "explicit_conflict": explicit_conflict,
            "retained_pending_unverified": len(retained),
            "accounted_items": accounted,
            "accounting_ok": accounting_ok,
            "result_statuses_ok": result_statuses_ok,
        },
        "results": results,
        "retained_pending": retained,
    }


def _load_report(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise VerificationError(f"report_unreadable:{type(exc).__name__}") from exc
    if not isinstance(data, dict):
        raise VerificationError("report_must_be_object")
    return data


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True, help="Existing Drive sync JSON report")
    parser.add_argument(
        "--allowed-root",
        action="append",
        type=Path,
        default=[],
        help="Allowed local root; repeatable. Defaults to report local_roots.",
    )
    parser.add_argument("--max-files", type=int, default=DEFAULT_MAX_FILES)
    parser.add_argument("--max-total-bytes", type=int, default=DEFAULT_MAX_TOTAL_BYTES)
    parser.add_argument("--max-file-bytes", type=int, default=DEFAULT_MAX_FILE_BYTES)
    parser.add_argument("--max-seconds", type=float, default=DEFAULT_MAX_SECONDS)
    parser.add_argument("--max-file-seconds", type=float, default=DEFAULT_MAX_FILE_SECONDS)
    parser.add_argument("--hash-chunk-bytes", type=int, default=DEFAULT_HASH_CHUNK_BYTES)
    parser.add_argument("--max-read-mbps", type=float, default=DEFAULT_MAX_READ_MBPS)
    parser.add_argument(
        "--offset",
        type=int,
        default=0,
        help="Deterministic eligible-item offset returned as next_offset for the next batch.",
    )
    parser.add_argument(
        "--only-over-1gb",
        action="store_true",
        help="Explicit isolated large-file mode; processes at most one >1GB item.",
    )
    parser.add_argument(
        "--allow-network-volume",
        action="store_true",
        help=(
            "Explicitly permit roots below /Volumes. Kernel-level SMB I/O can "
            "outlive userspace timeout signals."
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report_path = args.input.expanduser().resolve(strict=True)
        report = _load_report(report_path)
        roots = args.allowed_root or report_allowed_roots(report)
        if any(_network_volume_like(root) for root in roots) and not args.allow_network_volume:
            raise VerificationError("network_volume_requires_explicit_opt_in")
        result = verify_pending_report(
            report,
            source_report_sha256=_file_sha256(report_path),
            allowed_roots=roots,
            max_files=args.max_files,
            max_total_bytes=args.max_total_bytes,
            max_file_bytes=args.max_file_bytes,
            max_seconds=args.max_seconds,
            max_file_seconds=args.max_file_seconds,
            hash_chunk_bytes=args.hash_chunk_bytes,
            max_read_mbps=args.max_read_mbps,
            only_over_1gb=args.only_over_1gb,
            start_offset=args.offset,
        )
    except (OSError, VerificationError) as exc:
        error = str(exc).split(":", 1)[0] or type(exc).__name__
        print(json.dumps({"ok": False, "status": "refused", "error": error}, sort_keys=True))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 1 if result["action_required"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
