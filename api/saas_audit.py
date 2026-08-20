from __future__ import annotations

import hashlib
import json
import os
import stat
import threading
import time
from pathlib import Path
from typing import Any

from flask import has_request_context, request, session
from flask_login import current_user

from api.runtime_paths import get_runtime_dir
from magi_v3 import fcntl_compat as fcntl


ROOT = Path(os.environ.get("MAGI_ROOT_DIR") or Path(__file__).resolve().parents[1]).resolve()
AUDIT_PATH = Path(
    os.environ.get("MAGI_SAAS_AUDIT_PATH") or get_runtime_dir() / "saas_audit_events.jsonl"
)
_LOCK = threading.Lock()
_SCHEMA_VERSION = 2
_GENESIS = "GENESIS"
_MAX_VERIFY_BYTES = 64 * 1024 * 1024
_SENSITIVE_KEYS = {
    "api_key",
    "authorization",
    "cookie",
    "password",
    "secret",
    "token",
}


def _sha(value: Any) -> str:
    text = str(value or "")
    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()[:20]


def _actor() -> dict[str, str]:
    try:
        if current_user and current_user.is_authenticated:
            return {
                "id": str(getattr(current_user, "id", "") or ""),
                "role": str(getattr(current_user, "role", "") or ""),
                "tenant_id": str(getattr(current_user, "tenant_id", "") or session.get("tenant_id") or ""),
            }
    except Exception:
        pass
    return {"id": "anonymous", "role": ""}


def _request_meta() -> dict[str, str]:
    if not has_request_context():
        return {}
    return {
        "method": str(request.method or ""),
        "path": str(request.path or ""),
        # ProxyFix already normalizes the configured trusted hop.  Do not hash
        # a second raw forwarding header that the client may have supplied.
        "remote_addr_hash": _sha(request.remote_addr or ""),
        "user_agent_hash": _sha(request.headers.get("User-Agent") or ""),
    }


def _clean_value(key: str, value: Any) -> Any:
    lowered = key.lower()
    if any(marker in lowered for marker in _SENSITIVE_KEYS):
        return "[redacted]"
    if isinstance(value, Path):
        value = str(value)
    if isinstance(value, str):
        if len(value) > 512:
            return value[:509] + "..."
        return value
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, dict):
        return {str(k): _clean_value(str(k), v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean_value(key, item) for item in value[:50]]
    return str(value)[:512]


def file_ref(path: str | os.PathLike[str] | None) -> dict[str, Any]:
    """Return an audit-safe file reference without exposing full NAS paths."""
    text = str(path or "")
    p = Path(text)
    return {
        "name": p.name,
        "ext": p.suffix.lower(),
        "path_hash": _sha(text),
    }


def _canonical_event_bytes(event: dict[str, Any]) -> bytes:
    payload = dict(event)
    payload.pop("event_hash", None)
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _event_hash(event: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_event_bytes(event)).hexdigest()


def verify_audit_chain(
    path: Path | str | None = None,
    *,
    maximum_bytes: int = _MAX_VERIFY_BYTES,
) -> dict[str, Any]:
    """Verify the append-only hash chain without exposing event contents.

    Existing version-1 JSONL rows are accepted only as an immutable prefix.
    The first version-2 event binds that prefix by its complete byte digest;
    legacy rows are never allowed after the chain starts.
    """

    target = Path(path or AUDIT_PATH).expanduser()
    report: dict[str, Any] = {
        "ok": True,
        "status": "empty",
        "path": str(target),
        "legacy_events": 0,
        "chained_events": 0,
        "event_count": 0,
        "latest_hash": "",
        "anchor_hash": _GENESIS,
        "issue": "",
    }
    try:
        if target.is_symlink():
            raise ValueError("audit path is a symlink")
        if not target.exists():
            return report
        metadata = target.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise ValueError("audit path is not one regular file")
        if metadata.st_size > max(1, int(maximum_bytes)):
            raise ValueError("audit file exceeds bounded verifier size")
        raw = target.read_bytes()
    except (OSError, ValueError) as exc:
        report.update(ok=False, status="invalid", issue=str(exc))
        return report

    legacy_prefix = bytearray()
    previous_hash = ""
    expected_sequence = 1
    chain_started = False
    for line_number, raw_line in enumerate(raw.splitlines(keepends=True), start=1):
        stripped = raw_line.strip()
        if not stripped:
            report.update(ok=False, status="invalid", issue=f"blank line at {line_number}")
            return report
        try:
            row = json.loads(stripped.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError):
            report.update(ok=False, status="invalid", issue=f"invalid JSON at {line_number}")
            return report
        if not isinstance(row, dict):
            report.update(ok=False, status="invalid", issue=f"non-object event at {line_number}")
            return report

        if row.get("schema_version") != _SCHEMA_VERSION:
            if chain_started:
                report.update(ok=False, status="invalid", issue=f"legacy event after chain at {line_number}")
                return report
            legacy_prefix.extend(raw_line)
            report["legacy_events"] += 1
            continue

        if not chain_started:
            chain_started = True
            previous_hash = (
                "legacy:" + hashlib.sha256(bytes(legacy_prefix)).hexdigest()
                if legacy_prefix
                else _GENESIS
            )
            report["anchor_hash"] = previous_hash

        sequence = row.get("sequence")
        declared_previous = row.get("previous_hash")
        declared_hash = row.get("event_hash")
        if type(sequence) is not int or sequence != expected_sequence:
            report.update(ok=False, status="invalid", issue=f"sequence mismatch at {line_number}")
            return report
        if declared_previous != previous_hash:
            report.update(ok=False, status="invalid", issue=f"previous hash mismatch at {line_number}")
            return report
        if not isinstance(declared_hash, str) or declared_hash != _event_hash(row):
            report.update(ok=False, status="invalid", issue=f"event hash mismatch at {line_number}")
            return report
        previous_hash = declared_hash
        expected_sequence += 1
        report["chained_events"] += 1

    report["event_count"] = report["legacy_events"] + report["chained_events"]
    report["latest_hash"] = previous_hash
    if report["chained_events"]:
        report["status"] = "verified"
    elif report["legacy_events"]:
        report["status"] = "legacy_unsealed"
        report["anchor_hash"] = "legacy:" + hashlib.sha256(bytes(legacy_prefix)).hexdigest()
    return report


def _safe_append(path: Path, payload: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, stat.S_IRUSR | stat.S_IWUSR)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise OSError("audit sink is not one regular file")
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("audit append made no progress")
            view = view[written:]
        os.fsync(descriptor)
        os.fchmod(descriptor, stat.S_IRUSR | stat.S_IWUSR)
    finally:
        os.close(descriptor)


def append_audit_event(
    action: str,
    *,
    resource_type: str = "",
    resource_id: str = "",
    status: str = "ok",
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    target = Path(AUDIT_PATH).expanduser()
    if not target.is_absolute():
        raise OSError("audit sink path must be absolute")
    target.parent.mkdir(parents=True, exist_ok=True)
    lock_path = Path(str(target) + ".lock")
    if target.is_symlink() or lock_path.is_symlink():
        raise OSError("audit sink and lock must not be symlinks")

    with _LOCK, lock_path.open("a+b") as lock_handle:
        try:
            os.chmod(lock_path, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        try:
            chain = verify_audit_chain(target)
            if not chain.get("ok"):
                raise OSError(f"audit chain verification failed: {chain.get('issue') or 'invalid'}")
            event = {
                "schema_version": _SCHEMA_VERSION,
                "sequence": int(chain.get("chained_events") or 0) + 1,
                "previous_hash": str(chain.get("latest_hash") or chain.get("anchor_hash") or _GENESIS),
                "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "action": str(action or ""),
                "resource_type": str(resource_type or ""),
                "resource_id": str(resource_id or ""),
                "status": str(status or "ok"),
                "actor": _actor(),
                "request": _request_meta(),
                "metadata": _clean_value("metadata", metadata or {}),
            }
            event["event_hash"] = _event_hash(event)
            line = json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            _safe_append(target, line.encode("utf-8") + b"\n")
        finally:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
    return event


__all__ = ("AUDIT_PATH", "append_audit_event", "file_ref", "verify_audit_chain")
