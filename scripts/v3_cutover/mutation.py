"""Fail-closed, explicitly armed V2-to-V3 launchd cutover executor.

Importing this module is inert.  Live mutation is reachable only through a
hash-bound plan, a fresh GO pre-cutover report, a private one-time token file,
the maintenance window, and an exact V2-only ownership snapshot.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import plistlib
import re
import sqlite3
import stat
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from magi_v3.mutable_state_handoff import (
    ExactContext,
    MutableStateHandoffError,
    execute_handoff as execute_mutable_state_handoff,
)

from scripts.v3_laf_dedup_compat import (
    MariaDBDedupStore,
    _connect_from_environment,
    create_manifest,
    import_verified_manifest,
    load_verified_manifest,
    verify_imported_manifest,
)
from scripts.v3_python_runtime_snapshot import PythonRuntimeBlocked, verify_runtime_manifest
from scripts.v3_pdf_namer_handoff import (
    HandoffError as PdfNamerHandoffError,
    finalize as finalize_pdf_namer_state,
    verify_manifest as verify_pdf_namer_manifest,
)

from .core import (
    CutoverError,
    Snapshot,
    assess_absolute_window,
    assess_cutover_window,
    assess_snapshot,
)
from .activation import ActivationTransaction, active_release_marker
from .probe import HOST_SINGLETON_LAUNCHD_LABELS, ReleaseSpec
from .workflow import authorize_mutation

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
LABEL_RE = re.compile(r"^com\.magi\.[A-Za-z0-9._-]+$")
V3_LABELS = (
    "com.magi.v3.control",
    "com.magi.v3.gateway",
    "com.magi.v3.supervisor",
)
# Code-owned V2 application plane.  Host singletons are deliberately absent:
# they are inventoried separately by ``HOST_SINGLETON_LAUNCHD_LABELS`` and are
# never a substitute for a complete cold-rollback application plan.
REQUIRED_V2_APPLICATION_LABELS = (
    "com.magi.daemon",
    "com.magi.insight-sync",
    "com.magi.laf-nightly-audit",
    "com.magi.log-rotate",
    "com.magi.menubar",
    "com.magi.nightly-health-report",
    "com.magi.obsidian-ingest",
    "com.magi.omlx-restore",
    "com.magi.pdf-namer-nightly",
    "com.magi.purge-persona-memories",
    "com.magi.reprocess-insights",
    "com.magi.weekend-resummary",
)
READINESS_URLS = frozenset(
    {
        "http://127.0.0.1:5002/readyz",
        "http://127.0.0.1:5003/readyz",
        "http://127.0.0.1:8088/readyz",
    }
)
REQUIRED_PRE_CUTOVER_CHECKS = frozenset(
    {
        "cutover_window",
        "gate_config_binding",
        "v3_deploy_prepared",
        "v3_readiness_manifest",
        "v3_release_marker_manifest",
    }
)
LEGACY_V2_PRE_CUTOVER_CHECKS = frozenset(
    {"v2_only_ownership", "pdf_namer_handoff_precopy"}
)
CURRENT_V3_PRE_CUTOVER_CHECKS = frozenset({"previous_v3_only_ownership"})
MAX_PRE_CUTOVER_AGE_SECONDS = 15 * 60
OWNERSHIP_MANIFEST_NAME = "ownership/ownership-manifest.json"
ATOMIC_DRILL_EXCLUDED_EVIDENCE = (
    "atomic_release_switch_and_cold_rollback_drill_passed",
    "human_go_approval_recorded",
)
EXECUTION_PURPOSES = frozenset({"atomic_drill", "final_cutover"})


@dataclass(frozen=True)
class MutationResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str


@dataclass(frozen=True)
class BoundFile:
    path: Path
    sha256: str


@dataclass(frozen=True)
class LaunchAgent:
    label: str
    plist: BoundFile
    initial_loaded: bool = True
    initial_state: str = "running"
    keepalive_required_running: bool = False
    launchctl_receipt_sha256: str = ""


@dataclass(frozen=True)
class LAFDedupHandoffPlan:
    source_paths: tuple[Path, ...]
    manifest_output: Path
    db_env_file: BoundFile


@dataclass(frozen=True)
class PdfNamerHandoffPlan:
    source: Path
    destination: Path
    manifest: Path


@dataclass(frozen=True)
class MutableStateHandoffPlan:
    source_root: Path
    target_shared_root: Path
    dry_run_receipt: Path
    prepare_receipt: Path
    staging_root: Path
    release_id: str
    release_manifest_sha256: str
    deployment_manifest_sha256: str


@dataclass(frozen=True)
class PreparedCutoverPlan:
    operation: str
    execution_purpose: str
    source: BoundFile
    gate_config: BoundFile
    pre_cutover_report: BoundFile
    deploy_prepared_marker: BoundFile
    release_manifest: BoundFile
    token_sha256: str
    v2_launchagents: tuple[LaunchAgent, ...]
    v2_application_set_sha256: str
    v2_initial_loaded_set_sha256: str
    v2_keepalive_set_sha256: str
    v3_install_directory: Path
    readiness_urls: tuple[str, ...]
    laf_dedup_handoff: LAFDedupHandoffPlan | None
    pdf_namer_handoff: PdfNamerHandoffPlan | None
    mutable_state_handoff: MutableStateHandoffPlan | None = None
    plan_preparation_report: BoundFile | None = None


CommandRunner = Callable[..., object]
SnapshotCollector = Callable[[], Snapshot]
ReadinessProbe = Callable[[Sequence[str]], tuple[bool, Mapping[str, Any]]]
Clock = Callable[[], datetime]
LAFDedupHandoff = Callable[[LAFDedupHandoffPlan], Mapping[str, Any]]
PdfNamerHandoff = Callable[[PdfNamerHandoffPlan], Mapping[str, Any]]
ReconciliationProbe = Callable[[Path, Path, str], Mapping[str, Any]]
RuntimeStateHandoff = Callable[[Path, Path], Mapping[str, Any]]
MutableStateHandoff = Callable[..., tuple[dict[str, Any], str]]
FORMAL_STATE_DATABASES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (".agent/jobs/job_queue.db", ("jobs",)),
    (".agent/mq/message_queue.db", ("inbound_messages",)),
    (".runtime/conversation_history.sqlite3", ("conversation_history",)),
    (
        ".runtime/taiwan_legal_mcp/cache.sqlite3",
        ("judgment_cache", "regulation_cache", "search_cache"),
    ),
)


def v2_application_set_sha256(agents: Sequence[LaunchAgent]) -> str:
    """Validate and hash the exact code-owned V2 application rollback set."""

    expected = set(REQUIRED_V2_APPLICATION_LABELS)
    observed = {agent.label for agent in agents}
    if len(agents) != len(observed) or observed != expected:
        missing = sorted(expected - observed)
        extra = sorted(observed - expected)
        raise CutoverError(
            "V2 application launchagent set is incomplete or unexpected: "
            f"missing={missing}, extra={extra}"
        )
    if expected.intersection(HOST_SINGLETON_LAUNCHD_LABELS):
        raise CutoverError("V2 application contract overlaps host singleton launchagents")
    rows: list[dict[str, str]] = []
    for agent in sorted(agents, key=lambda item: item.label):
        if agent.plist.path.name != f"{agent.label}.plist":
            raise CutoverError(f"V2 application plist filename mismatch: {agent.label}")
        if not SHA256_RE.fullmatch(agent.plist.sha256):
            raise CutoverError(f"V2 application plist SHA-256 is invalid: {agent.label}")
        rows.append(
            {
                "label": agent.label,
                "plist_name": agent.plist.path.name,
                "plist_sha256": agent.plist.sha256,
            }
        )
    return hashlib.sha256(
        json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def v2_initial_loaded_set_sha256(agents: Sequence[LaunchAgent]) -> str:
    v2_application_set_sha256(agents)
    rows = [
        {
            "label": agent.label,
            "initial_loaded": agent.initial_loaded,
            "initial_state": agent.initial_state,
        }
        for agent in sorted(agents, key=lambda item: item.label)
    ]
    if any(
        type(row["initial_loaded"]) is not bool
        or not isinstance(row["initial_state"], str)
        or (
            row["initial_loaded"] is False
            and row["initial_state"] != ""
        )
        for row in rows
    ):
        raise CutoverError("V2 initial launchd state set is invalid")
    if any(
        not SHA256_RE.fullmatch(agent.launchctl_receipt_sha256)
        for agent in agents
    ):
        raise CutoverError("V2 initial launchd receipt hash is invalid")
    return hashlib.sha256(
        json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def v2_keepalive_set_sha256(agents: Sequence[LaunchAgent]) -> str:
    v2_application_set_sha256(agents)
    rows = [
        {
            "label": agent.label,
            "keepalive_required_running": agent.keepalive_required_running,
            "plist_sha256": agent.plist.sha256,
        }
        for agent in sorted(agents, key=lambda item: item.label)
    ]
    if any(type(row["keepalive_required_running"]) is not bool for row in rows):
        raise CutoverError("V2 KeepAlive launchagent set is invalid")
    return hashlib.sha256(
        json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _initial_launchd_state(
    value: Any, *, label: str
) -> tuple[bool, str, int | None, str]:
    if not isinstance(value, dict) or set(value) != {
        "loaded",
        "state",
        "pid",
        "launchctl_receipt",
        "launchctl_receipt_sha256",
    }:
        raise CutoverError(f"V2 initial launchd receipt is invalid: {label}")
    receipt = value.get("launchctl_receipt")
    receipt_sha = value.get("launchctl_receipt_sha256")
    expected_argv = ["/bin/launchctl", "print", f"gui/{os.getuid()}/{label}"]
    if (
        not isinstance(receipt, dict)
        or set(receipt) != {"argv", "returncode", "stdout", "stderr", "timed_out"}
        or receipt.get("argv") != expected_argv
        or type(receipt.get("returncode")) is not int
        or not isinstance(receipt.get("stdout"), str)
        or not isinstance(receipt.get("stderr"), str)
        or receipt.get("timed_out") is not False
        or not isinstance(receipt_sha, str)
        or not SHA256_RE.fullmatch(receipt_sha)
        or receipt_sha
        != hashlib.sha256(
            (json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n").encode()
        ).hexdigest()
    ):
        raise CutoverError(f"V2 initial raw launchctl receipt drifted: {label}")
    loaded = value.get("loaded")
    state = value.get("state")
    pid = value.get("pid")
    if type(loaded) is not bool or not isinstance(state, str):
        raise CutoverError(f"V2 initial launchd state is malformed: {label}")
    if loaded:
        state_match = re.search(r"(?m)^\s*state = ([^\n]+)\s*$", receipt["stdout"])
        pid_match = re.search(r"(?m)^\s*pid = (\d+)\s*$", receipt["stdout"])
        observed_state = state_match.group(1).strip() if state_match else ""
        observed_pid = int(pid_match.group(1)) if pid_match else None
        if receipt["returncode"] != 0 or not receipt["stdout"] or not state:
            raise CutoverError(f"V2 loaded launchd receipt is incomplete: {label}")
        if state != observed_state or pid != observed_pid:
            raise CutoverError(f"V2 launchd state does not derive from raw receipt: {label}")
        if pid is not None and (type(pid) is not int or pid <= 0):
            raise CutoverError(f"V2 initial launchd pid is invalid: {label}")
    elif not (
        receipt["returncode"] == 113
        and receipt["stdout"] == ""
        and state == ""
        and pid is None
    ):
        raise CutoverError(f"V2 unloaded launchd receipt is not a known-missing 113: {label}")
    return loaded, state, pid, receipt_sha


def _uncertified_reconciliation(
    _v2_root: Path, _v3_runtime: Path, active_owner: str
) -> Mapping[str, Any]:
    return {
        "schema_version": 1,
        "certifiable": False,
        "active_owner": active_owner,
        "active_job_store": "",
        "pending_ownership_certified": False,
        "pending_id_hashes": [],
        "pending_id_hash_occurrences": [],
        "orphaned_pending_id_hashes": [],
        "terminal_id_hashes": [],
        "committed_id_hashes": [],
        "committed_id_hash_occurrences": [],
        "sent_outbox_id_hashes": [],
        "sent_outbox_id_hash_occurrences": [],
        "duplicate_committed_jobs": 0,
        "duplicate_sent_outbox": 0,
    }


def _hash_ids(values: Sequence[str]) -> list[str]:
    return sorted(hashlib.sha256(value.encode()).hexdigest() for value in values)


def _compat_queue_rows(path: Path) -> dict[str, list[str]] | None:
    if not path.is_file() or path.is_symlink():
        return None
    uri = f"file:{path.as_posix()}?mode=ro"
    try:
        with sqlite3.connect(uri, uri=True, timeout=5) as connection:
            columns = {
                str(row[1]) for row in connection.execute("PRAGMA table_info(jobs)")
            }
            if not {"id", "status"}.issubset(columns):
                return None
            rows = [(str(row[0]), str(row[1])) for row in connection.execute(
                "SELECT id,status FROM jobs"
            )]
    except sqlite3.Error as exc:
        raise CutoverError(f"compatibility queue probe failed: {path.name}: {exc}") from exc
    pending_statuses = {"queued", "running"}
    terminal_statuses = {"done", "failed", "abandoned"}
    unknown = sorted({status for _job_id, status in rows} - pending_statuses - terminal_statuses)
    if unknown:
        raise CutoverError(f"compatibility queue has unknown states: {unknown[:3]}")
    return {
        "pending": [job_id for job_id, status in rows if status in pending_statuses],
        "terminal": [job_id for job_id, status in rows if status in terminal_statuses],
        "committed": [job_id for job_id, status in rows if status == "done"],
    }


def _ledger_rows(path: Path) -> dict[str, list[str]] | None:
    if not path.is_file() or path.is_symlink():
        return None
    uri = f"file:{path.as_posix()}?mode=ro"
    try:
        with sqlite3.connect(uri, uri=True, timeout=5) as connection:
            job_columns = {
                str(row[1]) for row in connection.execute("PRAGMA table_info(jobs)")
            }
            outbox_columns = {
                str(row[1]) for row in connection.execute("PRAGMA table_info(outbox)")
            }
            if not {"job_id", "status", "business_completed"}.issubset(job_columns):
                return None
            jobs = [
                (str(row[0]), str(row[1]), int(row[2]))
                for row in connection.execute(
                    "SELECT job_id,status,business_completed FROM jobs"
                )
            ]
            sent = (
                [str(row[0]) for row in connection.execute(
                    "SELECT outbox_id FROM outbox WHERE status='sent'"
                )]
                if {"outbox_id", "status"}.issubset(outbox_columns)
                else []
            )
    except (sqlite3.Error, ValueError) as exc:
        raise CutoverError(f"V3 ledger probe failed: {path.name}: {exc}") from exc
    terminal_statuses = {
        "succeeded", "degraded", "failed", "skipped", "cancelled", "timed_out"
    }
    known = terminal_statuses | {
        "queued", "leased", "running", "waiting_children", "awaiting_input",
        "needs_confirmation", "deferred",
    }
    unknown = sorted({status for _job_id, status, _completed in jobs} - known)
    if unknown:
        raise CutoverError(f"V3 ledger has unknown states: {unknown[:3]}")
    return {
        "pending": [job_id for job_id, status, _done in jobs if status not in terminal_statuses],
        "terminal": [job_id for job_id, status, _done in jobs if status in terminal_statuses],
        "committed": [job_id for job_id, _status, done in jobs if done == 1],
        "sent": sent,
    }


def _compat_delivery_receipts(agent_root: Path) -> list[str]:
    path = agent_root / "red_phone_delivery.jsonl"
    if not path.is_file() or path.is_symlink():
        return []
    receipts: list[str] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            if not isinstance(row, dict) or row.get("event") not in {"sent", "outbox_recovered"}:
                continue
            identity = row.get("entry_id")
            if not isinstance(identity, str) or not identity:
                # Direct delivery has no durable outbox id.  Hash a canonical,
                # payload-free delivery identity instead of copying previews.
                identity = "|".join(
                    str(row.get(key) or "")
                    for key in ("event", "source", "topic_key", "thread_id", "ts")
                )
            receipts.append(f"compat:{identity}")
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CutoverError(f"compatibility delivery receipt log is invalid: {exc}") from exc
    return receipts


def _state_database_path(v2_root: Path, v3_runtime: Path, owner: str, relative: str) -> Path:
    if owner == "v2":
        return v2_root / relative
    if relative.startswith(".agent/"):
        return v3_runtime / "shared" / "agent" / relative.removeprefix(".agent/")
    return v3_runtime / "shared" / "runtime" / relative.removeprefix(".runtime/")


def _stable_file_sha256(path: Path) -> str:
    before = path.stat()
    digest = _sha256_file(path)
    after = path.stat()
    if (before.st_ino, before.st_size, before.st_mtime_ns) != (
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        raise CutoverError(f"state database changed while hashing: {path.name}")
    return digest


def _database_inventory(
    path: Path, relative: str, expected_tables: Sequence[str]
) -> dict[str, Any]:
    if not path.is_file() or path.is_symlink():
        raise CutoverError(f"formal state database is unavailable: {relative}")
    uri = f"file:{path.as_posix()}?mode=ro"
    descriptor, temporary_name = tempfile.mkstemp(prefix="magi-v3-reconcile-", suffix=".sqlite3")
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with sqlite3.connect(uri, uri=True, timeout=30) as source:
            tables = sorted(
                str(row[0])
                for row in source.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                )
            )
            if tables != sorted(expected_tables):
                raise CutoverError(f"formal database table coverage drifted: {relative}")
            if source.execute("PRAGMA quick_check").fetchone() != ("ok",):
                raise CutoverError(f"formal database quick_check failed: {relative}")
            with sqlite3.connect(temporary) as snapshot:
                source.backup(snapshot)
                if snapshot.execute("PRAGMA quick_check").fetchone() != ("ok",):
                    raise CutoverError(f"formal database snapshot failed: {relative}")
        wal = Path(str(path) + "-wal")
        wal_present = wal.is_file() and not wal.is_symlink()
        if wal.exists() and not wal_present:
            raise CutoverError(f"formal database WAL is unsafe: {relative}")
        return {
            "relative_path": relative,
            "tables": tables,
            "database_file_sha256": _stable_file_sha256(path),
            "wal_present": wal_present,
            "wal_sha256": _stable_file_sha256(wal) if wal_present else "",
            "database_snapshot_sha256": _sha256_file(temporary),
        }
    except sqlite3.Error as exc:
        raise CutoverError(f"formal database inventory failed: {relative}: {exc}") from exc
    finally:
        temporary.unlink(missing_ok=True)


def _default_reconciliation_probe(
    v2_root: Path, v3_runtime: Path, active_owner: str
) -> Mapping[str, Any]:
    """Prove active pending ownership separately from immutable completion history."""

    if active_owner not in {"v2", "v3"}:
        raise CutoverError("reconciliation active owner is invalid")
    v2_agent = v2_root / ".agent"
    v3_agent = v3_runtime / "shared" / "agent"
    active_agent = v2_agent if active_owner == "v2" else v3_agent
    active_queue = active_agent / "jobs" / "job_queue.db"
    compatibility = _compat_queue_rows(active_queue)
    pending: list[str] = []
    terminal: list[str] = []
    committed: list[str] = []
    sent: list[str] = []
    orphaned: list[str] = []
    native_pending: list[str] = []
    sources_probed: list[str] = []
    delivery_path = active_agent / "red_phone_delivery.jsonl"
    if (delivery_path.exists() or delivery_path.is_symlink()) and (
        delivery_path.is_symlink() or not delivery_path.is_file()
    ):
        raise CutoverError("compatibility delivery receipt source is unsafe")
    delivery_receipts_present = delivery_path.is_file()
    if compatibility is not None:
        pending.extend(compatibility["pending"])
        terminal.extend(compatibility["terminal"])
        committed.extend(compatibility["committed"])
        sources_probed.append(f"{active_owner}_compat_job_queue")
        if delivery_receipts_present:
            sent.extend(_compat_delivery_receipts(active_agent))
            sources_probed.append(f"{active_owner}_compat_delivery_receipts")
    native_ledgers = sorted((v3_runtime / "state").glob("*/ledger.sqlite3"))
    for ledger in native_ledgers:
        rows = _ledger_rows(ledger)
        if rows is None:
            continue
        role = ledger.parent.name
        terminal.extend(rows["terminal"])
        committed.extend(rows["committed"])
        sent.extend(rows["sent"])
        if active_owner == "v3":
            pending.extend(rows["pending"])
            native_pending.extend(rows["pending"])
        else:
            orphaned.extend(rows["pending"])
        sources_probed.append(f"v3_{role}_ledger")
    pending_hashes = _hash_ids(pending)
    terminal_hashes = _hash_ids(terminal)
    committed_hashes = _hash_ids(committed)
    sent_hashes = _hash_ids(sent)
    orphaned_hashes = _hash_ids(orphaned)
    native_pending_hashes = _hash_ids(native_pending)
    source_inventory = [
        _database_inventory(
            _state_database_path(v2_root, v3_runtime, active_owner, relative),
            relative,
            tables,
        )
        for relative, tables in FORMAL_STATE_DATABASES
    ]
    sources_probed.extend(f"formal:{row['relative_path']}" for row in source_inventory)
    source_inventory_sha256 = hashlib.sha256(
        json.dumps(source_inventory, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    owner_certified = (
        compatibility is not None
        and not orphaned_hashes
        and len(pending_hashes) == len(set(pending_hashes))
        and len(source_inventory) == len(FORMAL_STATE_DATABASES)
        and (
            active_owner != "v3"
            or {ledger.parent.name for ledger in native_ledgers}
            == {"control", "gateway", "supervisor"}
        )
    )
    return {
        "schema_version": 1,
        "certifiable": owner_certified,
        "active_owner": active_owner,
        "active_job_store": str(active_queue.resolve(strict=False)),
        "pending_ownership_certified": owner_certified,
        "sources_probed": sorted(set(sources_probed)),
        "delivery_receipts_state": (
            "present_verified" if delivery_receipts_present else "absent_verified"
        ),
        "native_ledger_roles": sorted(ledger.parent.name for ledger in native_ledgers),
        "pending_id_hashes": sorted(set(pending_hashes)),
        "pending_id_hash_occurrences": sorted(pending_hashes),
        "native_pending_id_hashes": sorted(set(native_pending_hashes)),
        "orphaned_pending_id_hashes": sorted(set(orphaned_hashes)),
        "terminal_id_hashes": sorted(set(terminal_hashes)),
        "committed_id_hashes": sorted(set(committed_hashes)),
        "committed_id_hash_occurrences": sorted(committed_hashes),
        "sent_outbox_id_hashes": sorted(set(sent_hashes)),
        "sent_outbox_id_hash_occurrences": sorted(sent_hashes),
        "duplicate_committed_jobs": len(committed_hashes) - len(set(committed_hashes)),
        "duplicate_sent_outbox": len(sent_hashes) - len(set(sent_hashes)),
        "source_inventory": source_inventory,
        "source_inventory_sha256": source_inventory_sha256,
    }


def _atomic_copy_file(source: Path, target: Path) -> str:
    data = source.read_bytes()
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = target.with_name(f".{target.name}.tmp-{os.getpid()}-{uuid.uuid4().hex}")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        os.close(descriptor)
    try:
        os.replace(temporary, target)
        directory = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)
    return hashlib.sha256(data).hexdigest()


def _sqlite_snapshot(
    source: Path,
    target: Path,
    *,
    relative: str,
    expected_tables: Sequence[str],
) -> dict[str, Any]:
    source_inventory = _database_inventory(source, relative, expected_tables)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = target.with_name(f".{target.name}.tmp-{os.getpid()}-{uuid.uuid4().hex}")
    source_uri = f"file:{source.as_posix()}?mode=ro"
    try:
        with sqlite3.connect(source_uri, uri=True, timeout=30) as source_db:
            if source_db.execute("PRAGMA quick_check").fetchone() != ("ok",):
                raise CutoverError("runtime state handoff source quick_check failed")
            with sqlite3.connect(temporary) as destination_db:
                source_db.backup(destination_db)
                if destination_db.execute("PRAGMA quick_check").fetchone() != ("ok",):
                    raise CutoverError("runtime state handoff snapshot quick_check failed")
        os.chmod(temporary, 0o600)
        for suffix in ("-wal", "-shm"):
            sidecar = Path(str(target) + suffix)
            if sidecar.exists() or sidecar.is_symlink():
                if sidecar.is_symlink() or not sidecar.is_file():
                    raise CutoverError("runtime state handoff target SQLite sidecar is unsafe")
                sidecar.unlink()
        os.replace(temporary, target)
        directory = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except sqlite3.Error as exc:
        raise CutoverError(f"runtime state handoff SQLite backup failed: {exc}") from exc
    finally:
        temporary.unlink(missing_ok=True)
    return {
        "relative_path": relative,
        "tables": list(expected_tables),
        "source_snapshot_sha256": source_inventory["database_snapshot_sha256"],
        "target_database_sha256": _sha256_file(target),
    }


def execute_runtime_state_handoff(source_agent: Path, target_agent: Path) -> Mapping[str, Any]:
    """Snapshot the compatibility queue and delivery state after the old owner is zero."""

    source = source_agent.resolve(strict=True)
    target = target_agent.resolve(strict=False)
    if source.is_symlink() or not source.is_dir() or target.is_symlink():
        raise CutoverError("runtime state handoff roots are unsafe")
    def database_path(root: Path, relative: str) -> Path:
        if relative.startswith(".agent/"):
            return root / relative.removeprefix(".agent/")
        runtime_name = ".runtime" if root.name == ".agent" else "runtime"
        return root.parent / runtime_name / relative.removeprefix(".runtime/")

    databases = [
        _sqlite_snapshot(
            database_path(source, relative),
            database_path(target, relative),
            relative=relative,
            expected_tables=tables,
        )
        for relative, tables in FORMAL_STATE_DATABASES
    ]
    queue_rows = _compat_queue_rows(database_path(target, FORMAL_STATE_DATABASES[0][0]))
    if queue_rows is None:
        raise CutoverError("runtime state handoff target job queue verification failed")
    pending = _hash_ids(queue_rows["pending"])
    copied: list[str] = []
    for name in ("red_phone_outbox.json", "red_phone_delivery.jsonl"):
        source_file = source / name
        target_file = target / name
        if source_file.is_file() and not source_file.is_symlink():
            _atomic_copy_file(source_file, target_file)
            copied.append(name)
        elif target_file.exists() or target_file.is_symlink():
            if target_file.is_symlink() or not target_file.is_file():
                raise CutoverError("runtime state handoff target delivery state is unsafe")
            target_file.unlink()
    return {
        "schema_version": 1,
        "status": "complete",
        "databases": databases,
        "database_inventory_sha256": hashlib.sha256(
            json.dumps(databases, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        "pending_count": len(pending),
        "pending_id_set_sha256": hashlib.sha256(
            json.dumps(sorted(set(pending)), separators=(",", ":")).encode()
        ).hexdigest(),
        "delivery_state_files_copied": sorted(copied),
        "business_payload_copied": True,
        "business_payload_emitted": False,
    }
def execute_pdf_namer_handoff(plan: PdfNamerHandoffPlan) -> Mapping[str, Any]:
    return finalize_pdf_namer_state(plan.source, plan.destination, plan.manifest)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _file_signature(metadata: os.stat_result) -> tuple[int, int, int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        stat.S_IMODE(metadata.st_mode),
        metadata.st_uid,
        metadata.st_nlink,
    )


def _verify_bound_file(
    binding: BoundFile,
    *,
    description: str,
    private: bool = False,
) -> None:
    """Verify one binding through O_NOFOLLOW and a stable file descriptor."""

    raw = binding.path
    if not raw.is_absolute() or raw.resolve(strict=False) != raw:
        raise CutoverError(f"{description} path is not canonical")
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        directory = os.open(raw.parent, directory_flags)
    except OSError as exc:
        raise CutoverError(f"{description} parent is unavailable or symlinked") from exc
    try:
        parent_before = os.fstat(directory)
        try:
            parent_path = raw.parent.lstat()
        except OSError as exc:
            raise CutoverError(f"{description} parent changed while being verified") from exc
        if (
            not stat.S_ISDIR(parent_before.st_mode)
            or (parent_before.st_dev, parent_before.st_ino)
            != (parent_path.st_dev, parent_path.st_ino)
            or stat.S_ISLNK(parent_path.st_mode)
        ):
            raise CutoverError(f"{description} parent identity is unsafe")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(raw.name, flags, dir_fd=directory)
        except OSError as exc:
            raise CutoverError(f"{description} is unavailable or symlinked") from exc
        try:
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode):
                raise CutoverError(f"{description} is not a regular file")
            if private and (
                stat.S_IMODE(before.st_mode) != 0o600
                or before.st_uid != os.getuid()
                or before.st_nlink != 1
            ):
                raise CutoverError(
                    f"{description} must remain owner-only 0600 with one hard link"
                )
            digest = hashlib.sha256()
            with os.fdopen(descriptor, "rb", closefd=False) as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            after = os.fstat(descriptor)
            current = os.stat(raw.name, dir_fd=directory, follow_symlinks=False)
            if (
                _file_signature(before) != _file_signature(after)
                or (after.st_dev, after.st_ino) != (current.st_dev, current.st_ino)
                or stat.S_ISLNK(current.st_mode)
                or digest.hexdigest() != binding.sha256
            ):
                raise CutoverError(f"{description} SHA-256 drift detected")
        finally:
            os.close(descriptor)
        parent_after = os.fstat(directory)
        current_parent = raw.parent.lstat()
        if (
            (parent_before.st_dev, parent_before.st_ino)
            != (parent_after.st_dev, parent_after.st_ino)
            or (parent_after.st_dev, parent_after.st_ino)
            != (current_parent.st_dev, current_parent.st_ino)
            or stat.S_ISLNK(current_parent.st_mode)
        ):
            raise CutoverError(f"{description} parent changed while being verified")
    finally:
        os.close(directory)


def _verify_manifest_output_path(path: Path, *, created: bool) -> None:
    """Revalidate the planned output and its parent without following symlinks."""

    if not path.is_absolute() or path.resolve(strict=False) != path:
        raise CutoverError("LAF dedup manifest output path is not canonical")
    parent = path.parent
    if parent.resolve(strict=True) != parent:
        raise CutoverError("LAF dedup manifest output parent is not canonical")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        directory = os.open(parent, flags)
    except OSError as exc:
        raise CutoverError("LAF dedup manifest output parent is unavailable or symlinked") from exc
    try:
        parent_fd = os.fstat(directory)
        parent_path = parent.lstat()
        if (
            not stat.S_ISDIR(parent_fd.st_mode)
            or parent_fd.st_uid != os.getuid()
            or (parent_fd.st_dev, parent_fd.st_ino) != (parent_path.st_dev, parent_path.st_ino)
            or stat.S_ISLNK(parent_path.st_mode)
        ):
            raise CutoverError("LAF dedup manifest output parent identity is unsafe")
        try:
            output = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            if created:
                raise CutoverError("LAF dedup manifest output is missing after handoff")
        else:
            if not created:
                raise CutoverError("LAF dedup manifest output must still be a new path")
            if (
                not stat.S_ISREG(output.st_mode)
                or stat.S_IMODE(output.st_mode) != 0o600
                or output.st_uid != os.getuid()
                or output.st_nlink != 1
            ):
                raise CutoverError("LAF dedup manifest output identity is unsafe")
    finally:
        os.close(directory)


def _load_json(path: Path, *, description: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CutoverError(f"{description} is unreadable: {exc}") from exc
    if not isinstance(payload, dict):
        raise CutoverError(f"{description} must be a JSON object")
    return payload


def _absolute_regular(path: Path, *, description: str) -> Path:
    raw = path.expanduser()
    if not raw.is_absolute() or raw.is_symlink():
        raise CutoverError(f"{description} must be an absolute non-symlink file")
    try:
        resolved = raw.resolve(strict=True)
    except OSError as exc:
        raise CutoverError(f"{description} is unavailable: {exc}") from exc
    if not resolved.is_file():
        raise CutoverError(f"{description} must be a regular file")
    return resolved


def _bound_file(value: Any, *, description: str) -> BoundFile:
    if not isinstance(value, dict):
        raise CutoverError(f"{description} binding must be an object")
    raw_path, digest = value.get("path"), value.get("sha256")
    if not isinstance(raw_path, str) or not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
        raise CutoverError(f"{description} binding is invalid")
    path = _absolute_regular(Path(raw_path), description=description)
    if _sha256_file(path) != digest:
        raise CutoverError(f"{description} SHA-256 mismatch")
    return BoundFile(path, digest)


def _path_bound_file(value: Any, *, description: str) -> BoundFile:
    """Load a file whose canonical path, rather than future bytes, is plan-bound.

    This is used only for the final pre-cutover report.  The report binds the
    exact plan SHA in the opposite direction, removing the otherwise circular
    dependency without accepting an unbound or replaceable path.
    """

    if not isinstance(value, dict) or set(value) != {"path", "path_sha256"}:
        raise CutoverError(f"{description} path binding is invalid")
    raw_path = value.get("path")
    path_sha256 = value.get("path_sha256")
    if (
        not isinstance(raw_path, str)
        or not Path(raw_path).is_absolute()
        or not isinstance(path_sha256, str)
        or not SHA256_RE.fullmatch(path_sha256)
    ):
        raise CutoverError(f"{description} path binding is invalid")
    candidate = Path(raw_path).expanduser()
    path = _absolute_regular(candidate, description=description)
    if hashlib.sha256(str(path).encode()).hexdigest() != path_sha256:
        raise CutoverError(f"{description} path hash mismatch")
    return BoundFile(path, _sha256_file(path))


def _safe_artifact(root: Path, relative_value: Any) -> Path:
    if not isinstance(relative_value, str) or not relative_value:
        raise CutoverError("deployment artifact path is missing")
    relative = Path(relative_value)
    if relative.is_absolute() or ".." in relative.parts:
        raise CutoverError("deployment artifact path escapes staging")
    candidate = root
    for part in relative.parts:
        candidate /= part
        if candidate.is_symlink():
            raise CutoverError(f"deployment artifact is symlinked: {relative_value}")
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise CutoverError(f"deployment artifact is unavailable: {relative_value}") from exc
    if not resolved.is_file():
        raise CutoverError(f"deployment artifact is not a file: {relative_value}")
    return resolved


def _verify_release_inventory(root: Path, release: Mapping[str, Any]) -> None:
    rows = release.get("files")
    if not isinstance(rows, list) or not rows:
        raise CutoverError("release manifest file inventory is missing")
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise CutoverError("release manifest file inventory is invalid")
        relative = row.get("path")
        digest = row.get("sha256")
        size = row.get("size")
        mode = row.get("mode")
        if (
            not isinstance(relative, str)
            or relative in seen
            or not isinstance(digest, str)
            or not SHA256_RE.fullmatch(digest)
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
            or not isinstance(mode, str)
            or not re.fullmatch(r"0[0-7]{3}", mode)
        ):
            raise CutoverError("release manifest file inventory is invalid")
        path = _safe_artifact(root, relative)
        metadata = path.stat()
        if (
            _sha256_file(path) != digest
            or metadata.st_size != size
            or stat.S_IMODE(metadata.st_mode) != int(mode, 8)
        ):
            raise CutoverError(f"release member drift detected: {relative}")
        seen.add(relative)
        normalized.append({"path": relative, "sha256": digest, "size": size, "mode": mode})
    normalized.sort(key=lambda row: row["path"])
    inventory_sha256 = hashlib.sha256(
        json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    if not (
        release.get("immutable") is True
        and release.get("release_sha256") == inventory_sha256
        and release.get("source_snapshot_sha256") == inventory_sha256
    ):
        raise CutoverError("release content snapshot identity mismatch")
    actual: set[str] = set()
    for directory, directory_names, file_names in os.walk(root, followlinks=False):
        base = Path(directory)
        for name in directory_names:
            if (base / name).is_symlink():
                raise CutoverError(f"release contains symlinked directory: {name}")
        for name in file_names:
            path = base / name
            relative = path.relative_to(root).as_posix()
            if relative in {"release-manifest.json", "RELEASE_COMPLETE.json"}:
                continue
            if path.is_symlink() or not path.is_file():
                raise CutoverError(f"release contains unsafe member: {relative}")
            actual.add(relative)
    if actual != seen:
        missing = sorted(seen - actual)
        extra = sorted(actual - seen)
        raise CutoverError(
            f"release inventory differs from manifest: missing={missing[:3]}, extra={extra[:3]}"
        )


def load_prepared_plan(
    plan_path: Path,
    expected_sha256: str,
    *,
    canonical_launchagents_directory: Path | None = None,
    allow_completed_handoff_outputs: bool = False,
    require_mutable_state_handoff: bool = False,
) -> PreparedCutoverPlan:
    """Load an explicit plan; never infer mutable V2 launchd labels."""

    if not SHA256_RE.fullmatch(expected_sha256):
        raise CutoverError("cutover plan SHA-256 is invalid")
    source_path = _absolute_regular(plan_path, description="cutover plan")
    if _sha256_file(source_path) != expected_sha256:
        raise CutoverError("cutover plan SHA-256 mismatch")
    payload = _load_json(source_path, description="cutover plan")
    operation = payload.get("operation")
    execution_purpose = payload.get("execution_purpose")
    if payload.get("schema_version") != 1 or operation not in {
        "v2_to_v3_cutover",
        "v3_to_v2_rollback",
    }:
        raise CutoverError("cutover plan identity is invalid")
    if execution_purpose not in EXECUTION_PURPOSES:
        raise CutoverError("cutover plan execution purpose is invalid")
    token_sha256 = payload.get("token_sha256")
    if not isinstance(token_sha256, str) or not SHA256_RE.fullmatch(token_sha256):
        raise CutoverError("cutover plan token SHA-256 is invalid")
    raw_agents = payload.get("v2_launchagents")
    if not isinstance(raw_agents, list) or not raw_agents:
        raise CutoverError("explicit hash-bound V2 launchagent plan is required")
    agents: list[LaunchAgent] = []
    for index, row in enumerate(raw_agents):
        if not isinstance(row, dict) or set(row) != {
            "label",
            "plist",
            "initial_launchd",
            "keepalive_required_running",
        }:
            raise CutoverError(f"V2 launchagent {index} is invalid")
        label = row.get("label")
        if (
            not isinstance(label, str)
            or not LABEL_RE.fullmatch(label)
            or label in V3_LABELS
            or label in HOST_SINGLETON_LAUNCHD_LABELS
        ):
            raise CutoverError(f"V2 launchagent {index} label is unsafe")
        plist = _bound_file(row.get("plist"), description=f"V2 launchagent {label}")
        try:
            with plist.path.open("rb") as handle:
                plist_payload = plistlib.load(handle)
        except (OSError, plistlib.InvalidFileException) as exc:
            raise CutoverError(f"V2 launchagent plist is unreadable: {label}") from exc
        if not isinstance(plist_payload, dict) or plist_payload.get("Label") != label:
            raise CutoverError(f"V2 launchagent plist label mismatch: {label}")
        initial_loaded, initial_state, _initial_pid, receipt_sha = _initial_launchd_state(
            row.get("initial_launchd"), label=label
        )
        keepalive_required = row.get("keepalive_required_running")
        if (
            type(keepalive_required) is not bool
            or keepalive_required is not (plist_payload.get("KeepAlive") is True)
        ):
            raise CutoverError(f"V2 KeepAlive role does not match plist content: {label}")
        agents.append(
            LaunchAgent(
                label,
                plist,
                initial_loaded,
                initial_state,
                keepalive_required,
                receipt_sha,
            )
        )
    if len({agent.label for agent in agents}) != len(agents):
        raise CutoverError("V2 launchagent labels must be unique")
    raw_install = payload.get("v3_install_directory")
    if not isinstance(raw_install, str) or not Path(raw_install).expanduser().is_absolute():
        raise CutoverError("V3 launchagent install directory must be absolute")
    install_directory = Path(raw_install).expanduser().resolve(strict=False)
    canonical = (
        canonical_launchagents_directory
        or Path.home() / "Library" / "LaunchAgents"
    ).expanduser().resolve(strict=False)
    if install_directory != canonical:
        raise CutoverError(f"V3 launchagent install directory must equal {canonical}")
    if any(agent.plist.path.parent != canonical for agent in agents):
        raise CutoverError("V2 application plists must be in the canonical launchagent directory")
    application_set_sha256 = v2_application_set_sha256(tuple(agents))
    if payload.get("v2_application_set_sha256") != application_set_sha256:
        raise CutoverError("V2 application launchagent set hash mismatch")
    initial_loaded_set_sha256 = v2_initial_loaded_set_sha256(tuple(agents))
    if payload.get("v2_initial_loaded_set_sha256") != initial_loaded_set_sha256:
        raise CutoverError("V2 initial loaded launchagent set hash mismatch")
    keepalive_set_sha256 = v2_keepalive_set_sha256(tuple(agents))
    if payload.get("v2_keepalive_set_sha256") != keepalive_set_sha256:
        raise CutoverError("V2 KeepAlive launchagent set hash mismatch")
    raw_urls = payload.get("readiness_urls")
    if not isinstance(raw_urls, list) or frozenset(raw_urls) != READINESS_URLS or len(raw_urls) != 3:
        raise CutoverError("cutover plan must bind all three loopback V3 readiness URLs")
    laf_handoff: LAFDedupHandoffPlan | None = None
    pdf_handoff: PdfNamerHandoffPlan | None = None
    mutable_handoff: MutableStateHandoffPlan | None = None
    if operation == "v3_to_v2_rollback" and "laf_dedup_handoff" in payload:
        raise CutoverError("rollback plan must not contain a LAF dedup handoff")
    if operation == "v3_to_v2_rollback" and "pdf_namer_handoff" in payload:
        raise CutoverError("rollback plan must not contain a pdf-namer state handoff")
    if operation == "v3_to_v2_rollback" and "mutable_state_handoff" in payload:
        raise CutoverError("rollback plan must preserve and must not repeat mutable-state handoff")
    if operation == "v2_to_v3_cutover":
        raw_handoff = payload.get("laf_dedup_handoff")
        if not isinstance(raw_handoff, dict) or set(raw_handoff) != {
            "source_paths",
            "manifest_output",
            "db_env_file",
        }:
            raise CutoverError("cutover plan lacks the exact LAF dedup handoff binding")
        raw_sources = raw_handoff.get("source_paths")
        if not isinstance(raw_sources, list) or not raw_sources:
            raise CutoverError("LAF dedup source path binding is missing")
        source_paths: list[Path] = []
        for value in raw_sources:
            if not isinstance(value, str) or not Path(value).is_absolute():
                raise CutoverError("LAF dedup source path binding is invalid")
            source_paths.append(_absolute_regular(Path(value), description="LAF dedup source"))
        sources = tuple(source_paths)
        if list(map(str, sources)) != sorted(set(map(str, sources))):
            raise CutoverError("LAF dedup source paths must be canonical, unique, and sorted")
        raw_output = raw_handoff.get("manifest_output")
        if not isinstance(raw_output, str) or not Path(raw_output).expanduser().is_absolute():
            raise CutoverError("LAF dedup manifest output binding is invalid")
        manifest_output = Path(raw_output).expanduser()
        _verify_manifest_output_path(
            manifest_output,
            created=allow_completed_handoff_outputs,
        )
        env_binding = _bound_file(
            raw_handoff.get("db_env_file"), description="LAF dedup DB env file"
        )
        _verify_bound_file(env_binding, description="LAF dedup DB env file", private=True)
        laf_handoff = LAFDedupHandoffPlan(sources, manifest_output, env_binding)
        raw_pdf = payload.get("pdf_namer_handoff")
        if not isinstance(raw_pdf, dict) or set(raw_pdf) != {"source", "destination", "manifest"}:
            raise CutoverError("cutover plan lacks the exact pdf-namer handoff binding")
        if not all(isinstance(raw_pdf.get(key), str) and Path(raw_pdf[key]).is_absolute() for key in raw_pdf):
            raise CutoverError("pdf-namer handoff paths must be absolute")
        raw_source = Path(raw_pdf["source"]).expanduser()
        raw_destination = Path(raw_pdf["destination"]).expanduser()
        raw_manifest = Path(raw_pdf["manifest"]).expanduser()
        try:
            verify_pdf_namer_manifest(
                raw_manifest,
                source=raw_source,
                destination=raw_destination,
                allowed_statuses={"precopy_complete", "complete"},
            )
        except (PdfNamerHandoffError, OSError) as exc:
            raise CutoverError("pdf-namer precopy evidence is unavailable or invalid") from exc
        pdf_handoff = PdfNamerHandoffPlan(
            raw_source.resolve(strict=True),
            raw_destination.resolve(strict=False),
            raw_manifest.resolve(strict=True),
        )
        raw_mutable = payload.get("mutable_state_handoff")
        if raw_mutable is None and (
            require_mutable_state_handoff or execution_purpose == "final_cutover"
        ):
            raise CutoverError("cutover plan lacks the exact mutable-state handoff binding")
        if raw_mutable is not None:
            if not isinstance(raw_mutable, dict) or set(raw_mutable) != {
                "source_root",
                "target_shared_root",
                "dry_run_receipt",
                "prepare_receipt",
                "staging_root",
                "exact_context",
            }:
                raise CutoverError("mutable-state handoff binding is invalid")

            def bound_path(name: str, *, existing_directory: bool = False) -> Path:
                value = raw_mutable.get(name)
                if not isinstance(value, dict) or set(value) != {"path", "path_sha256"}:
                    raise CutoverError(f"mutable-state {name} binding is invalid")
                raw_path = value.get("path")
                if not isinstance(raw_path, str) or not Path(raw_path).is_absolute():
                    raise CutoverError(f"mutable-state {name} path is invalid")
                candidate = Path(raw_path).expanduser()
                path = candidate.resolve(strict=False)
                if hashlib.sha256(str(path).encode()).hexdigest() != value.get("path_sha256"):
                    raise CutoverError(f"mutable-state {name} path hash mismatch")
                if candidate.is_symlink():
                    raise CutoverError(f"mutable-state {name} path is symlinked")
                if existing_directory and not path.is_dir():
                    raise CutoverError(f"mutable-state {name} must be a directory")
                return path

            context = raw_mutable.get("exact_context")
            if not isinstance(context, dict) or set(context) != {
                "release_id",
                "release_manifest_sha256",
                "deployment_manifest_sha256",
            }:
                raise CutoverError("mutable-state exact context is invalid")
            mutable_handoff = MutableStateHandoffPlan(
                source_root=bound_path("source_root", existing_directory=True),
                target_shared_root=bound_path("target_shared_root"),
                dry_run_receipt=bound_path("dry_run_receipt"),
                prepare_receipt=bound_path("prepare_receipt"),
                staging_root=bound_path("staging_root"),
                release_id=str(context.get("release_id", "")),
                release_manifest_sha256=str(context.get("release_manifest_sha256", "")),
                deployment_manifest_sha256=str(context.get("deployment_manifest_sha256", "")),
            )
            try:
                ExactContext(
                    mutable_handoff.release_id,
                    mutable_handoff.release_manifest_sha256,
                    mutable_handoff.deployment_manifest_sha256,
                    expected_sha256,
                ).validate()
            except MutableStateHandoffError as exc:
                raise CutoverError("mutable-state exact context is invalid") from exc
            if mutable_handoff.release_manifest_sha256 != _sha256_file(
                _absolute_regular(Path(str(payload["release_manifest"]["path"])), description="release manifest")
            ):
                raise CutoverError("mutable-state release context mismatch")
    mutual_final_binding = (
        operation == "v2_to_v3_cutover"
        and execution_purpose == "final_cutover"
        and "plan_preparation_report" in payload
    )
    if mutual_final_binding:
        pre_cutover_binding = _path_bound_file(
            payload.get("pre_cutover_report"),
            description="final pre-cutover report",
        )
        plan_preparation_binding = _bound_file(
            payload.get("plan_preparation_report"),
            description="plan preparation report",
        )
    else:
        if "plan_preparation_report" in payload:
            raise CutoverError(
                "plan preparation report is valid only for a final mutual-bound cutover"
            )
        pre_cutover_binding = _bound_file(
            payload.get("pre_cutover_report"), description="pre-cutover report"
        )
        plan_preparation_binding = None
    return PreparedCutoverPlan(
        operation=operation,
        execution_purpose=execution_purpose,
        source=BoundFile(source_path, expected_sha256),
        gate_config=_bound_file(payload.get("gate_config"), description="gate config"),
        pre_cutover_report=pre_cutover_binding,
        deploy_prepared_marker=_bound_file(
            payload.get("deploy_prepared_marker"), description="deploy prepared marker"
        ),
        release_manifest=_bound_file(payload.get("release_manifest"), description="release manifest"),
        token_sha256=token_sha256,
        v2_launchagents=tuple(agents),
        v2_application_set_sha256=application_set_sha256,
        v2_initial_loaded_set_sha256=initial_loaded_set_sha256,
        v2_keepalive_set_sha256=keepalive_set_sha256,
        v3_install_directory=install_directory,
        readiness_urls=tuple(raw_urls),
        laf_dedup_handoff=laf_handoff,
        pdf_namer_handoff=pdf_handoff,
        mutable_state_handoff=mutable_handoff,
        plan_preparation_report=plan_preparation_binding,
    )


def execute_laf_dedup_handoff(
    plan: LAFDedupHandoffPlan,
    *,
    manifest_creator: Callable[..., Mapping[str, Any]] = create_manifest,
    manifest_loader: Callable[..., dict[str, Any]] = load_verified_manifest,
    connection_factory: Callable[..., Any] = _connect_from_environment,
    store_factory: Callable[[Any], Any] = MariaDBDedupStore,
    importer: Callable[..., Mapping[str, Any]] = import_verified_manifest,
    verifier: Callable[..., Mapping[str, Any]] = verify_imported_manifest,
) -> Mapping[str, Any]:
    """Run the mandatory post-quiesce compatibility sequence.

    All dependencies are injectable so unit tests prove ordering and failure
    semantics without opening a database connection or touching live services.
    The returned report intentionally omits source identifiers and DB secrets.
    """

    snapshot = manifest_creator(plan.source_paths, plan.manifest_output)
    manifest_sha256 = snapshot.get("manifest_sha256")
    if (
        snapshot.get("status") != "snapshot_created"
        or not isinstance(manifest_sha256, str)
        or not SHA256_RE.fullmatch(manifest_sha256)
    ):
        raise CutoverError("LAF dedup snapshot did not produce a hash-bound manifest")
    manifest = manifest_loader(plan.manifest_output, manifest_sha256)
    if manifest.get("record_count") != snapshot.get("record_count"):
        raise CutoverError("LAF dedup manifest verification count mismatch")

    _verify_bound_file(plan.db_env_file, description="LAF dedup DB env file", private=True)
    connection = connection_factory(
        plan.db_env_file.path,
        expected_sha256=plan.db_env_file.sha256,
    )
    try:
        store = store_factory(connection)
        dry_run = importer(manifest, store, apply=False)
        if (
            dry_run.get("status") != "dry_run"
            or dry_run.get("transaction_committed") is not False
            or dry_run.get("mutation_performed") is not False
        ):
            raise CutoverError("LAF dedup DB dry-run did not prove a non-mutating transaction")
        applied = importer(manifest, store, apply=True)
        if (
            applied.get("status") != "imported"
            or applied.get("transaction_committed") is not True
            or applied.get("mutation_performed") is not True
        ):
            raise CutoverError("LAF dedup DB apply did not commit exactly once")
        verified = verifier(manifest, store)
        if (
            verified.get("status") != "dual_store_verified"
            or verified.get("record_count") != manifest.get("record_count")
            or verified.get("laf_email_records_verified") != manifest.get("record_count")
            or verified.get("dedup_registry_verified") != manifest.get("record_count")
            or verified.get("mutation_performed") is not False
        ):
            raise CutoverError("LAF dedup committed dual-table verification did not pass")
        reverified = manifest_loader(plan.manifest_output, manifest_sha256)
        if any(
            reverified.get(name) != manifest.get(name)
            for name in (
                "record_count",
                "records_sha256",
                "source_count",
                "source_snapshot_sha256",
            )
        ):
            raise CutoverError("LAF dedup source union changed after dual-table verification")
    finally:
        try:
            connection.close()
        except Exception:
            pass
    return {
        "status": "complete",
        "stages": [
            "snapshot",
            "verify",
            "db_dry_run",
            "apply",
            "dual_table_verify",
            "source_reverify",
        ],
        "manifest_sha256": manifest_sha256,
        "record_count": manifest.get("record_count"),
        "records_sha256": manifest.get("records_sha256"),
        "dry_run_ok": True,
        "apply_ok": True,
        "dual_table_verified": True,
        "contains_business_payload": False,
    }


def _default_runner(argv: Sequence[str], **kwargs: Any) -> object:
    return subprocess.run(argv, check=False, capture_output=True, text=True, timeout=30, **kwargs)


def _default_readiness_probe(urls: Sequence[str]) -> tuple[bool, Mapping[str, Any]]:
    results: dict[str, Any] = {}
    for url in urls:
        parsed = urlparse(url)
        if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or url not in READINESS_URLS:
            return False, {"error": "unapproved readiness URL"}
        try:
            with urlopen(Request(url, headers={"Accept": "application/json"}), timeout=3) as response:
                body = json.loads(response.read(1024 * 1024).decode("utf-8"))
                results[url] = {"status": response.status, "ready": body.get("ready")}
                if response.status != 200 or body.get("ready") is not True:
                    return False, results
        except Exception as exc:
            results[url] = {"error": str(exc)}
            return False, results
    return True, results


class ArmedLaunchdRunner:
    """Deprecated phase-one API retained as an unconditionally disabled guard."""

    def __init__(
        self,
        specs: Sequence[ReleaseSpec],
        *,
        provided_token: str | None,
        environment_token: str | None = None,
        runner: Callable[..., object] | None = None,
    ) -> None:
        del specs, runner
        authorize_mutation(provided_token, environment_token)
        raise AssertionError("authorize_mutation must fail closed")  # pragma: no cover

    def _disabled(self) -> MutationResult:
        raise CutoverError("live mutation disabled: use explicit execute with a hash-bound plan")

    def bootout(self, release: str, label: str) -> MutationResult:
        del release, label
        return self._disabled()

    def bootstrap(self, release: str, label: str) -> MutationResult:
        del release, label
        return self._disabled()


class PreparedCutoverExecutor:
    """Perform one guarded V2-to-V3 handoff, rolling back on every failure."""

    def __init__(
        self,
        plan: PreparedCutoverPlan,
        *,
        token_file: Path,
        snapshot_collector: SnapshotCollector,
        runner: CommandRunner = _default_runner,
        readiness_probe: ReadinessProbe = _default_readiness_probe,
        laf_dedup_handoff: LAFDedupHandoff = execute_laf_dedup_handoff,
        pdf_namer_handoff: PdfNamerHandoff = execute_pdf_namer_handoff,
        reconciliation_probe: ReconciliationProbe | None = None,
        runtime_state_handoff: RuntimeStateHandoff = execute_runtime_state_handoff,
        mutable_state_handoff: MutableStateHandoff = execute_mutable_state_handoff,
        clock: Clock | None = None,
        uid: int | None = None,
    ) -> None:
        self.plan = plan
        self.token_file = token_file
        self.snapshot_collector = snapshot_collector
        self.runner = runner
        self.readiness_probe = readiness_probe
        self.laf_dedup_handoff = laf_dedup_handoff
        self.pdf_namer_handoff = pdf_namer_handoff
        self.reconciliation_probe = reconciliation_probe or _default_reconciliation_probe
        self.runtime_state_handoff = runtime_state_handoff
        self.mutable_state_handoff = mutable_state_handoff
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.uid = os.getuid() if uid is None else uid
        self.events: list[dict[str, Any]] = []
        self.started_v3: list[LaunchAgent] = []
        self.installed_v3: list[tuple[Path, str]] = []
        self.installed_ownership: tuple[Path, str] | None = None
        self._rollback_deploy: Mapping[str, Any] = {}
        self.handoff_started = False
        self.laf_handoff_completed = False
        self.pdf_namer_handoff_completed = False
        self.activation: ActivationTransaction | None = None
        self.reconciliation_before: Mapping[str, Any] | None = None
        self.reconciliation_after: Mapping[str, Any] | None = None
        self._runtime_root: Path | None = None
        self.runtime_state_handoff_completed = False
        self.mutable_state_dry_run: Mapping[str, Any] | None = None
        self.mutable_state_prepare: Mapping[str, Any] | None = None
        self.mutable_state_preflight: Mapping[str, Any] | None = None

    def _event(self, action: str, **detail: Any) -> None:
        self.events.append(
            {
                "sequence": len(self.events) + 1,
                "at": self.clock().astimezone(timezone.utc).isoformat(),
                "action": action,
                **detail,
            }
        )

    def _reconciliation(
        self, active_owner: str, *, require_v3_ledgers: bool = False
    ) -> Mapping[str, Any]:
        if self._runtime_root is None:
            raise CutoverError("V3 runtime root is unavailable for reconciliation")
        v2_root = (
            Path.home()
            / "Library"
            / "Application Support"
            / "MAGI"
            / "runtime"
            / "MAGI_v2"
        ).resolve(strict=False)
        report = dict(self.reconciliation_probe(v2_root, self._runtime_root, active_owner))
        arrays = (
            "pending_id_hashes",
            "native_pending_id_hashes",
            "orphaned_pending_id_hashes",
            "terminal_id_hashes",
            "committed_id_hashes",
            "sent_outbox_id_hashes",
        )
        occurrence_pairs = (
            ("pending_id_hashes", "pending_id_hash_occurrences"),
            ("committed_id_hashes", "committed_id_hash_occurrences"),
            ("sent_outbox_id_hashes", "sent_outbox_id_hash_occurrences"),
        )
        if (
            report.get("schema_version") != 1
            or report.get("active_owner") != active_owner
            or not isinstance(report.get("active_job_store"), str)
            or not report["active_job_store"]
            or type(report.get("pending_ownership_certified")) is not bool
            or any(
                not isinstance(report.get(name), list)
                or report[name] != sorted(set(report[name]))
                or any(
                    not isinstance(value, str) or not SHA256_RE.fullmatch(value)
                    for value in report[name]
                )
                for name in arrays
            )
            or any(
                not isinstance(report.get(occurrences), list)
                or report[occurrences] != sorted(report[occurrences])
                or any(
                    not isinstance(value, str) or not SHA256_RE.fullmatch(value)
                    for value in report[occurrences]
                )
                or report[unique] != sorted(set(report[occurrences]))
                for unique, occurrences in occurrence_pairs
            )
            or type(report.get("certifiable")) is not bool
            or not isinstance(report.get("sources_probed", []), list)
            or report.get("sources_probed", []) != sorted(set(report.get("sources_probed", [])))
            or any(not isinstance(value, str) or not value for value in report.get("sources_probed", []))
            or report.get("delivery_receipts_state")
            not in {"present_verified", "absent_verified"}
            or not isinstance(report.get("native_ledger_roles"), list)
            or report.get("native_ledger_roles")
            != sorted(set(report.get("native_ledger_roles", [])))
            or any(
                role not in {"control", "gateway", "supervisor"}
                for role in report.get("native_ledger_roles", [])
            )
            or type(report.get("duplicate_committed_jobs")) is not int
            or report["duplicate_committed_jobs"] < 0
            or type(report.get("duplicate_sent_outbox")) is not int
            or report["duplicate_sent_outbox"] < 0
            or report["duplicate_committed_jobs"]
            != len(report["committed_id_hash_occurrences"])
            - len(report["committed_id_hashes"])
            or report["duplicate_sent_outbox"]
            != len(report["sent_outbox_id_hash_occurrences"])
            - len(report["sent_outbox_id_hashes"])
            or len(report["pending_id_hash_occurrences"])
            != len(report["pending_id_hashes"])
            or report.get("certifiable") is not True
            or report.get("pending_ownership_certified") is not True
            or report.get("orphaned_pending_id_hashes") != []
            or not isinstance(report.get("source_inventory"), list)
            or [row.get("relative_path") for row in report.get("source_inventory", [])]
            != [relative for relative, _tables in FORMAL_STATE_DATABASES]
            or any(
                not isinstance(row, dict)
                or row.get("tables") != sorted(tables)
                or any(
                    not isinstance(row.get(field), str)
                    or not SHA256_RE.fullmatch(row[field])
                    for field in ("database_file_sha256", "database_snapshot_sha256")
                )
                or type(row.get("wal_present")) is not bool
                or (
                    row["wal_present"]
                    and (
                        not isinstance(row.get("wal_sha256"), str)
                        or not SHA256_RE.fullmatch(row["wal_sha256"])
                    )
                )
                or (not row["wal_present"] and row.get("wal_sha256") != "")
                for row, (_relative, tables) in zip(
                    report.get("source_inventory", []), FORMAL_STATE_DATABASES
                )
            )
            or report.get("source_inventory_sha256")
            != hashlib.sha256(
                json.dumps(
                    report.get("source_inventory", []),
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
            or (
                (active_owner == "v3" or require_v3_ledgers)
                and report.get("native_ledger_roles")
                != ["control", "gateway", "supervisor"]
            )
        ):
            raise CutoverError("ledger/outbox reconciliation receipt is invalid")
        return report

    def _agent_roots(self) -> tuple[Path, Path]:
        if self._runtime_root is None:
            raise CutoverError("V3 runtime root is unavailable for state handoff")
        v2 = (
            Path.home()
            / "Library"
            / "Application Support"
            / "MAGI"
            / "runtime"
            / "MAGI_v2"
            / ".agent"
        ).resolve(strict=False)
        return v2, self._runtime_root / "shared" / "agent"

    def _execute_runtime_state_handoff(self, *, to_owner: str) -> None:
        v2, v3 = self._agent_roots()
        source, target = (v2, v3) if to_owner == "v3" else (v3, v2)
        try:
            report = dict(self.runtime_state_handoff(source, target))
        except BaseException as exc:
            self._event(
                "runtime_state_handoff",
                ok=False,
                to_owner=to_owner,
                error_type=type(exc).__name__,
            )
            raise CutoverError("runtime queue/delivery state handoff failed closed") from exc
        if (
            report.get("schema_version") != 1
            or report.get("status") != "complete"
            or not isinstance(report.get("databases"), list)
            or [row.get("relative_path") for row in report.get("databases", [])]
            != [relative for relative, _tables in FORMAL_STATE_DATABASES]
            or any(
                not isinstance(row, dict)
                or row.get("tables") != list(tables)
                or not isinstance(row.get("source_snapshot_sha256"), str)
                or not SHA256_RE.fullmatch(row["source_snapshot_sha256"])
                or not isinstance(row.get("target_database_sha256"), str)
                or not SHA256_RE.fullmatch(row["target_database_sha256"])
                for row, (_relative, tables) in zip(
                    report.get("databases", []), FORMAL_STATE_DATABASES
                )
            )
            or report.get("database_inventory_sha256")
            != hashlib.sha256(
                json.dumps(
                    report.get("databases", []),
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
            or type(report.get("pending_count")) is not int
            or report["pending_count"] < 0
            or not isinstance(report.get("pending_id_set_sha256"), str)
            or not SHA256_RE.fullmatch(report["pending_id_set_sha256"])
            or not isinstance(report.get("delivery_state_files_copied"), list)
            or report.get("business_payload_copied") is not True
            or report.get("business_payload_emitted") is not False
        ):
            raise CutoverError("runtime state handoff returned an invalid receipt")
        self.runtime_state_handoff_completed = to_owner == "v3"
        self._event("runtime_state_handoff", ok=True, to_owner=to_owner, detail=report)

    def _mutable_context(self) -> ExactContext:
        handoff = self.plan.mutable_state_handoff
        if handoff is None:
            raise CutoverError("cutover plan lacks the mandatory mutable-state handoff")
        return ExactContext(
            release_id=handoff.release_id,
            release_manifest_sha256=handoff.release_manifest_sha256,
            deployment_manifest_sha256=handoff.deployment_manifest_sha256,
            cutover_plan_sha256=self.plan.source.sha256,
        )

    def _execute_mutable_state_dry_run(self) -> None:
        handoff = self.plan.mutable_state_handoff
        if handoff is None:
            return  # Legacy programmatic atomic-drill fixtures only; CLI forbids this.
        try:
            payload, receipt_sha256 = self.mutable_state_handoff(
                action="dry-run",
                source_root=handoff.source_root,
                target_shared_root=handoff.target_shared_root,
                receipt_path=handoff.dry_run_receipt,
                context=self._mutable_context(),
            )
            if not (
                payload.get("status") == "dry_run"
                and type(payload.get("ready")) is bool
                and payload.get("contains_business_payload") is False
                and payload.get("exact_context") == self._mutable_context().public()
                and isinstance(receipt_sha256, str)
                and SHA256_RE.fullmatch(receipt_sha256)
                and all(
                    isinstance(payload.get(key), str) and SHA256_RE.fullmatch(payload[key])
                    for key in (
                        "source_snapshot_sha256",
                        "target_before_snapshot_sha256",
                        "target_snapshot_sha256",
                    )
                )
            ):
                raise CutoverError("mutable-state dry-run receipt is incomplete")
            if self.mutable_state_preflight is not None and any(
                (
                    receipt_sha256
                    != self.mutable_state_preflight.get("receipt_sha256"),
                    payload.get("source_snapshot_sha256")
                    != self.mutable_state_preflight.get("source_snapshot_sha256"),
                    payload.get("target_before_snapshot_sha256")
                    != self.mutable_state_preflight.get(
                        "target_before_snapshot_sha256"
                    ),
                    payload.get("target_snapshot_sha256")
                    != self.mutable_state_preflight.get("target_snapshot_sha256"),
                    payload.get("allowlist_sha256")
                    != self.mutable_state_preflight.get("allowlist_sha256"),
                    payload.get("ready") is not True,
                )
            ):
                raise CutoverError(
                    "mutable-state dry-run does not replay the final pre-cutover receipt"
                )
            if self.mutable_state_dry_run is not None and any(
                payload.get(key) != self.mutable_state_dry_run.get(key)
                for key in ("source_snapshot_sha256", "target_before_snapshot_sha256")
            ):
                raise CutoverError("mutable-state source or target drifted after preflight")
        except (MutableStateHandoffError, OSError) as exc:
            self._event("mutable_state_dry_run", ok=False, error_type=type(exc).__name__)
            raise CutoverError("mutable-state dry-run failed closed") from exc
        self.mutable_state_dry_run = dict(payload)
        self._event(
            "mutable_state_dry_run",
            ok=True,
            receipt_path=str(handoff.dry_run_receipt),
            receipt_sha256=receipt_sha256,
            source_snapshot_sha256=payload["source_snapshot_sha256"],
            target_before_snapshot_sha256=payload["target_before_snapshot_sha256"],
        )

    def _execute_mutable_state_prepare(self) -> None:
        handoff = self.plan.mutable_state_handoff
        if handoff is None:
            return
        if self.mutable_state_dry_run is None:
            raise CutoverError("mutable-state dry-run was not completed")
        try:
            payload, receipt_sha256 = self.mutable_state_handoff(
                action="prepare",
                source_root=handoff.source_root,
                target_shared_root=handoff.target_shared_root,
                receipt_path=handoff.prepare_receipt,
                staging_root=handoff.staging_root,
                context=self._mutable_context(),
                refresh=self.mutable_state_dry_run.get("ready") is False,
                expected_target_snapshot_sha256=(
                    self.mutable_state_dry_run.get("target_before_snapshot_sha256")
                    if self.mutable_state_dry_run.get("ready") is False
                    else None
                ),
            )
            if not (
                payload.get("status") == "prepared"
                and payload.get("ready") is True
                and payload.get("contains_business_payload") is False
                and payload.get("exact_context") == self._mutable_context().public()
                and payload.get("source_snapshot_sha256")
                == self.mutable_state_dry_run.get("source_snapshot_sha256")
                and payload.get("target_before_snapshot_sha256")
                == self.mutable_state_dry_run.get("target_before_snapshot_sha256")
                and isinstance(receipt_sha256, str)
                and SHA256_RE.fullmatch(receipt_sha256)
                and isinstance(payload.get("target_snapshot_sha256"), str)
                and SHA256_RE.fullmatch(payload["target_snapshot_sha256"])
            ):
                raise CutoverError("mutable-state prepare receipt is incomplete or mismatched")
        except (MutableStateHandoffError, OSError) as exc:
            self._event("mutable_state_prepare", ok=False, error_type=type(exc).__name__)
            raise CutoverError("mutable-state prepare failed closed") from exc
        self.mutable_state_prepare = dict(payload)
        self._event(
            "mutable_state_prepare",
            ok=True,
            receipt_path=str(handoff.prepare_receipt),
            receipt_sha256=receipt_sha256,
            target_snapshot_sha256=payload["target_snapshot_sha256"],
        )

    def _v2_marker_identity(self) -> tuple[str, Path, str]:
        root = (
            Path.home()
            / "Library"
            / "Application Support"
            / "MAGI"
            / "runtime"
            / "MAGI_v2"
        ).resolve(strict=False)
        rows = [
            {"label": agent.label, "sha256": agent.plist.sha256}
            for agent in sorted(self.plan.v2_launchagents, key=lambda item: item.label)
        ]
        digest = hashlib.sha256(
            json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return "v2-cold-rollback", root, digest

    def _activation_receipt(self, receipt: Mapping[str, Any]) -> None:
        normalized = dict(receipt)
        intent = normalized.pop("intent_receipt", None)
        if isinstance(intent, Mapping):
            self._event("activation_transaction", receipt=dict(intent))
        self._event("activation_transaction", receipt=normalized)

    def _recover_interrupted_activation(
        self,
        deploy: Mapping[str, Any],
        v3_agents: Sequence[LaunchAgent],
    ) -> dict[str, Any] | None:
        if self._runtime_root is None:
            raise CutoverError("runtime root is unavailable for activation recovery")
        journal = self._runtime_root.parent / "cutover-activation.json"
        if not journal.exists() and not journal.is_symlink():
            return None
        activation = ActivationTransaction.resume(
            state_parent=self._runtime_root.parent,
            clock=lambda: self.clock().astimezone(timezone.utc).isoformat(),
        )
        document = activation.document()
        if document.get("phase") == "complete":
            return None
        if (
            document.get("release_id")
            != _load_json(self.plan.release_manifest.path, description="release manifest").get(
                "release_id"
            )
            or document.get("release_manifest_sha256")
            != self.plan.release_manifest.sha256
        ):
            raise CutoverError("interrupted activation belongs to a different release")
        self.activation = activation
        self.handoff_started = True
        self.reconciliation_before = document.get("reconciliation_before")
        self.runtime_state_handoff_completed = (
            self._runtime_root / "shared" / "agent" / "jobs" / "job_queue.db"
        ).is_file()
        self.started_v3 = list(v3_agents)
        for agent in v3_agents:
            target = self.plan.v3_install_directory / f"{agent.label}.plist"
            if target.exists() or target.is_symlink():
                _verify_bound_file(
                    BoundFile(target, agent.plist.sha256),
                    description=f"interrupted V3 launchagent {agent.label}",
                )
                self.installed_v3.append((target, agent.plist.sha256))
        ownership_target = Path(str(deploy.get("ownership_manifest", "")))
        ownership_digest = str(deploy.get("ownership_manifest_sha256", ""))
        if ownership_target.exists() or ownership_target.is_symlink():
            _verify_bound_file(
                BoundFile(ownership_target, ownership_digest),
                description="interrupted V3 ownership manifest",
            )
            self.installed_ownership = (ownership_target, ownership_digest)
        recovered, detail = self._rollback()
        return {
            "schema_version": 1,
            "report_kind": "interrupted_cutover_recovery",
            "status": "v2_restored" if recovered else "blocked",
            "ok": False,
            "mutation_performed": True,
            "rollback_performed": True,
            "rollback_ok": recovered,
            "rollback_detail": detail,
            "activation_transaction_id": activation.transaction_id,
            "events": self.events,
        }

    def _verify_conditional_daytime_authorization(
        self,
        *,
        gates: Mapping[str, Any],
        release_gate: Mapping[str, Any],
        expected_context: Mapping[str, Any],
        now: datetime,
    ) -> None:
        """Fail closed if a one-time daylight approval is stale, changed or reused.

        G28's compiler has already checked the request/receipt/consumption
        chain.  The executor nevertheless reopens its hash-bound evidence
        immediately before every irreversible phase so a prior GO cannot be
        replayed after its approved window or after a file swap.
        """
        binding = release_gate.get("conditional_authorization")
        if binding is None:
            if gates.get("conditional_daytime_authorization_required") is True:
                raise CutoverError("final cutover requires a redeemed conditional daytime authorization")
            return
        exact_keys = {
            "evidence_path",
            "evidence_sha256",
            "metrics_sha256",
            "conditional_daytime_window",
            "conditional_request_sha256",
            "conditional_receipt_sha256",
            "conditional_consumption_sha256",
        }
        if not isinstance(binding, Mapping) or set(binding) != exact_keys:
            raise CutoverError("conditional authorization binding is not exact")
        if not all(
            isinstance(binding.get(key), str) and SHA256_RE.fullmatch(str(binding[key]))
            for key in exact_keys - {"evidence_path", "conditional_daytime_window"}
        ):
            raise CutoverError("conditional authorization digest is invalid")
        evidence_path = Path(str(binding["evidence_path"])).expanduser()
        if (
            not evidence_path.is_absolute()
            or evidence_path.is_symlink()
            or not evidence_path.is_file()
            or _sha256_file(evidence_path) != binding["evidence_sha256"]
        ):
            raise CutoverError("conditional authorization evidence drifted")
        window = binding["conditional_daytime_window"]
        if not isinstance(window, Mapping) or set(window) != {"starts_at", "ends_at", "timezone"}:
            raise CutoverError("conditional daytime window is invalid")
        try:
            assessment = assess_absolute_window(dict(window), now=now)
        except Exception as exc:
            raise CutoverError("conditional daytime window is invalid") from exc
        if not assessment["within_window"]:
            raise CutoverError("outside the exact human-approved conditional daytime window")
        try:
            from scripts.v3_release_gate import (
                _canonical_json_bytes,
                freeze_artifacts,
                load_json,
                validate_evidence_semantics,
            )

            document = load_json(evidence_path)
            if (
                document.get("evidence_id") != "human_go_approval_recorded"
                or document.get("status") != "passed"
                or any(document.get(key) != value for key, value in expected_context.items())
            ):
                raise CutoverError("conditional authorization evidence context is invalid")
            artifacts, artifact_errors = freeze_artifacts(document, evidence_path.parent)
            if artifact_errors:
                raise CutoverError("conditional authorization evidence artifacts are invalid")
            semantic_errors = validate_evidence_semantics(
                document,
                "human_go_approval_recorded",
                config=dict(gates),
                bound_artifacts=artifacts,
                expected_context=dict(expected_context),
            )
            if semantic_errors:
                raise CutoverError("conditional authorization semantic verification failed")
            producer = next((item for item in artifacts if item.role == "producer_report"), None)
            if producer is None:
                raise CutoverError("conditional authorization producer report is missing")
            producer_document = json.loads(producer.data)
            metrics = producer_document.get("metrics") if isinstance(producer_document, dict) else None
            if not isinstance(metrics, Mapping):
                raise CutoverError("conditional authorization metrics are missing")
            if hashlib.sha256(_canonical_json_bytes(dict(metrics))).hexdigest() != binding["metrics_sha256"]:
                raise CutoverError("conditional authorization metrics drifted")
            if (
                metrics.get("authorization_mode") != "conditional_daytime_window"
                or metrics.get("conditional_daytime_window") != dict(window)
                or any(
                    metrics.get(key) != binding[key]
                    for key in (
                        "conditional_request_sha256",
                        "conditional_receipt_sha256",
                        "conditional_consumption_sha256",
                    )
                )
            ):
                raise CutoverError("conditional authorization G28 bindings drifted")
        except CutoverError:
            raise
        except Exception as exc:
            raise CutoverError("conditional authorization verification failed closed") from exc

    def _verify_effective_cutover_window(
        self,
        *,
        gates: Mapping[str, Any] | None = None,
        now: datetime | None = None,
        stage: str,
    ) -> dict[str, Any]:
        """Recheck the release-bound effective window before each mutation."""
        if gates is None:
            _verify_bound_file(self.plan.gate_config, description="gate config")
            gates = _load_json(self.plan.gate_config.path, description="gate config")
        instant = now or self.clock()
        if instant.tzinfo is None:
            raise CutoverError("cutover clock must be timezone-aware")
        if gates.get("conditional_daytime_authorization_required") is True:
            window = assess_absolute_window(
                dict(gates.get("conditional_daytime_window") or {}),
                now=instant,
            )
        else:
            window = assess_cutover_window(
                gates["window"],
                timezone_name="Asia/Taipei",
                now=instant,
            )
        self._event("verify_cutover_window", stage=stage, window=window)
        if not window["within_window"]:
            raise CutoverError("outside the release-bound effective cutover window")
        return window

    def _validate_static_gates(
        self, *, enforce_fresh_cutover: bool = True
    ) -> tuple[dict[str, Any], dict[str, Any], tuple[LaunchAgent, ...]]:
        # Recheck every plan-bound file immediately before arming.
        for description, binding in (
            ("cutover plan", self.plan.source),
            ("gate config", self.plan.gate_config),
            ("pre-cutover report", self.plan.pre_cutover_report),
            ("deploy prepared marker", self.plan.deploy_prepared_marker),
            ("release manifest", self.plan.release_manifest),
            *((f"V2 launchagent {agent.label}", agent.plist) for agent in self.plan.v2_launchagents),
            *((
                ("plan preparation report", self.plan.plan_preparation_report),
            ) if self.plan.plan_preparation_report is not None else ()),
            *((
                ("LAF dedup DB env file", self.plan.laf_dedup_handoff.db_env_file),
            ) if self.plan.laf_dedup_handoff is not None else ()),
        ):
            _verify_bound_file(
                binding,
                description=description,
                private=description == "LAF dedup DB env file",
            )
        if self.plan.laf_dedup_handoff is not None:
            _verify_manifest_output_path(
                self.plan.laf_dedup_handoff.manifest_output,
                created=self.laf_handoff_completed,
            )
        if self.plan.operation == "v2_to_v3_cutover":
            pdf_handoff = self.plan.pdf_namer_handoff
            if pdf_handoff is None:
                raise CutoverError("cutover plan lacks the mandatory pdf-namer state handoff")
            try:
                verify_pdf_namer_manifest(
                    pdf_handoff.manifest,
                    source=pdf_handoff.source,
                    destination=pdf_handoff.destination,
                    allowed_statuses=(
                        {"complete"}
                        if self.pdf_namer_handoff_completed
                        else {"precopy_complete", "complete"}
                    ),
                )
            except (PdfNamerHandoffError, OSError) as exc:
                raise CutoverError("pdf-namer handoff evidence is unavailable or invalid") from exc

        gates = _load_json(self.plan.gate_config.path, description="gate config")
        source_contract = gates.get("source_contract")
        legacy_v2_contract = (
            isinstance(source_contract, dict)
            and source_contract.get("legacy_v2_validation") != "disabled"
        )
        if (
            gates.get("schema_version") != 1
            or gates.get("timezone") != "Asia/Taipei"
            or not isinstance(gates.get("window"), dict)
            or not isinstance(source_contract, dict)
            or (
                legacy_v2_contract
                and source_contract.get("database_relatives")
                != [relative for relative, _tables in FORMAL_STATE_DATABASES]
            )
        ):
            raise CutoverError("execute requires a valid release-bound Asia/Taipei cutover window")
        now = self.clock()
        if now.tzinfo is None:
            raise CutoverError("cutover clock must be timezone-aware")
        if enforce_fresh_cutover:
            self._verify_effective_cutover_window(
                gates=gates,
                now=now,
                stage="static_gate",
            )

        report = _load_json(self.plan.pre_cutover_report.path, description="pre-cutover report")
        required_evidence = gates.get("required_evidence")
        if (
            not isinstance(required_evidence, list)
            or not required_evidence
            or any(not isinstance(item, str) or not item for item in required_evidence)
            or len(required_evidence) != len(set(required_evidence))
            or any(item not in required_evidence for item in ATOMIC_DRILL_EXCLUDED_EVIDENCE)
        ):
            raise CutoverError("execute requires a valid required evidence contract")
        required_count = len(required_evidence)
        drill_passed_count = required_count - len(ATOMIC_DRILL_EXCLUDED_EVIDENCE)
        legacy_v2 = (
            gates.get("source_contract", {}).get("legacy_v2_validation")
            != "disabled"
        )
        required_pre_cutover_checks = REQUIRED_PRE_CUTOVER_CHECKS | (
            LEGACY_V2_PRE_CUTOVER_CHECKS
            if legacy_v2
            else CURRENT_V3_PRE_CUTOVER_CHECKS
        )
        checks = report.get("checks")
        check_map = (
            {
                row.get("name"): row.get("ok")
                for row in checks
                if isinstance(row, dict) and isinstance(row.get("name"), str)
            }
            if isinstance(checks, list)
            else {}
        )
        expected_stage = (
            f"cutover_drill_{drill_passed_count}_of_{required_count}"
            if self.plan.execution_purpose == "atomic_drill"
            else f"final_cutover_{required_count}_of_{required_count}"
        )
        expected_decision = (
            "GO_FOR_CUTOVER_DRILL_ONLY"
            if self.plan.execution_purpose == "atomic_drill"
            else "GO"
        )
        expected_excluded = (
            list(ATOMIC_DRILL_EXCLUDED_EVIDENCE)
            if self.plan.execution_purpose == "atomic_drill"
            else []
        )
        expected_passed = (
            drill_passed_count
            if self.plan.execution_purpose == "atomic_drill"
            else required_count
        )
        if not (
            report.get("schema_version") == 1
            and report.get("decision") == expected_decision
            and report.get("gate_stage") == expected_stage
            and report.get("execution_purpose") == self.plan.execution_purpose
            and report.get("required_evidence_count") == required_count
            and report.get("passed_evidence_count") == expected_passed
            and report.get("excluded_evidence") == expected_excluded
            and report.get("fail_closed") is True
            and report.get("mutation_performed") is False
            and report.get("gaps") == []
            and isinstance(checks, list)
            and bool(checks)
            and len(check_map) == len(checks)
            and all(row.get("ok") is True for row in checks if isinstance(row, dict))
            and all(check_map.get(name) is True for name in required_pre_cutover_checks)
            and report.get("gate_config_sha256") == self.plan.gate_config.sha256
        ):
            raise CutoverError("pre-cutover report is not a fully passing hash-bound GO")
        cutover_plan_binding = report.get("cutover_plan")
        mutable_check = next(
            (
                row
                for row in checks
                if isinstance(row, dict) and row.get("name") == "mutable_state_handoff"
            ),
            None,
        )
        mutable_detail = (
            mutable_check.get("detail") if isinstance(mutable_check, dict) else None
        )
        if self.plan.execution_purpose == "final_cutover":
            if not (
                isinstance(cutover_plan_binding, dict)
                and cutover_plan_binding
                == {
                    "path": str(self.plan.source.path),
                    "sha256": self.plan.source.sha256,
                }
                and self.plan.plan_preparation_report is not None
                and self.plan.mutable_state_handoff is not None
                and isinstance(mutable_detail, dict)
                and mutable_detail.get("status") == "verified"
                and mutable_detail.get("receipt_path")
                == str(self.plan.mutable_state_handoff.dry_run_receipt)
                and all(
                    isinstance(mutable_detail.get(key), str)
                    and SHA256_RE.fullmatch(mutable_detail[key])
                    for key in (
                        "receipt_sha256",
                        "allowlist_sha256",
                        "source_snapshot_sha256",
                        "target_before_snapshot_sha256",
                        "target_snapshot_sha256",
                    )
                )
                and mutable_detail.get("ready") is True
                and mutable_detail.get("contains_business_payload") is False
            ):
                raise CutoverError(
                    "final pre-cutover report lacks exact mutable-state plan evidence"
                )
            self.mutable_state_preflight = dict(mutable_detail)
        else:
            legacy_fixture_exemption = (
                self.plan.mutable_state_handoff is None and mutable_detail is None
            )
            if not legacy_fixture_exemption and not (
                isinstance(mutable_detail, dict)
                and mutable_detail.get("status")
                in {"verified", "excluded_for_atomic_drill"}
            ):
                raise CutoverError(
                    "atomic drill mutable-state exemption is not explicit"
                )
            self.mutable_state_preflight = (
                dict(mutable_detail)
                if isinstance(mutable_detail, dict)
                and mutable_detail.get("status") == "verified"
                else None
            )
        gate_binding = report.get("release_gate_report")
        gate_report = _bound_file(gate_binding, description="bound release gate report")
        release_gate = _load_json(gate_report.path, description="bound release gate report")
        gate_invalid = release_gate.get("invalid")
        required_evidence = gates.get("required_evidence")
        if not isinstance(required_evidence, list) or len(required_evidence) != required_count:
            raise CutoverError(
                "cutover gate config required evidence contract changed during validation"
            )
        if self.plan.execution_purpose == "atomic_drill":
            expected_passed_evidence = [
                item for item in required_evidence if item not in ATOMIC_DRILL_EXCLUDED_EVIDENCE
            ]
            release_gate_ok = (
                release_gate.get("decision") == "NO_GO"
                and release_gate.get("passed") == expected_passed_evidence
                and release_gate.get("missing") == list(ATOMIC_DRILL_EXCLUDED_EVIDENCE)
                and release_gate.get("failed") == []
                and gate_invalid == {}
            )
        else:
            release_gate_ok = (
                release_gate.get("decision") == "GO"
                and release_gate.get("passed") == required_evidence
                and release_gate.get("missing") == []
                and release_gate.get("failed") == []
                and gate_invalid == {}
            )
        if not (
            gate_report.sha256 == gate_binding.get("sha256")
            and release_gate.get("schema_version") == 1
            and release_gate.get("fail_closed") is True
            and release_gate.get("required_count") == required_count
            and release_gate.get("expected_context") == report.get("expected_context")
            and release_gate_ok
        ):
            raise CutoverError("pre-cutover stage is not bound to the exact release gate result")
        if self.plan.execution_purpose == "final_cutover":
            self._verify_conditional_daytime_authorization(
                gates=gates,
                release_gate=release_gate,
                expected_context=report.get("expected_context", {}),
                now=now,
            )
        try:
            observed = datetime.fromisoformat(str(report["observed_at"]).replace("Z", "+00:00"))
            if observed.tzinfo is None:
                raise ValueError("timezone missing")
            age = (now.astimezone(timezone.utc) - observed.astimezone(timezone.utc)).total_seconds()
        except (KeyError, ValueError) as exc:
            raise CutoverError("pre-cutover report timestamp is invalid") from exc
        if enforce_fresh_cutover and (age < 0 or age > MAX_PRE_CUTOVER_AGE_SECONDS):
            raise CutoverError("pre-cutover GO report is stale or future-dated")

        marker = _load_json(self.plan.deploy_prepared_marker.path, description="deploy prepared marker")
        marker_root = self.plan.deploy_prepared_marker.path.parent
        if not (
            marker.get("schema_version") == 1
            and marker.get("status") == "prepared_not_installed"
            and marker.get("ready_to_install") is True
            and marker.get("mutation_performed") is False
            and marker.get("release_manifest_sha256") == self.plan.release_manifest.sha256
            and isinstance(marker.get("ownership_manifest_sha256"), str)
            and SHA256_RE.fullmatch(marker["ownership_manifest_sha256"])
        ):
            raise CutoverError("deploy prepared marker is not installable or release-bound")
        deploy_manifest = _safe_artifact(marker_root, marker.get("manifest"))
        if _sha256_file(deploy_manifest) != marker.get("manifest_sha256"):
            raise CutoverError("deploy manifest SHA-256 mismatch")
        deploy = _load_json(deploy_manifest, description="deploy manifest")
        release = _load_json(self.plan.release_manifest.path, description="release manifest")
        if self.plan.mutable_state_handoff is not None:
            context = self.plan.mutable_state_handoff
            if not (
                context.release_id == release.get("release_id")
                and context.release_manifest_sha256 == self.plan.release_manifest.sha256
                and context.deployment_manifest_sha256 == marker.get("manifest_sha256")
            ):
                raise CutoverError("mutable-state exact release/deployment context mismatch")
        if not (
            deploy.get("schema_version") == 1
            and deploy.get("status") == "prepared_not_installed"
            and deploy.get("mutation_performed") is False
            and deploy.get("release_id") == marker.get("release_id") == release.get("release_id")
            and deploy.get("release_manifest_sha256") == self.plan.release_manifest.sha256
            and Path(str(deploy.get("release_manifest"))).resolve(strict=False)
            == self.plan.release_manifest.path
            and release.get("schema_version") == 1
            and release.get("immutable") is True
            and report.get("release_sha")
            == release.get("source_snapshot_sha256")
            == release.get("release_sha256")
        ):
            raise CutoverError("release/deploy/pre-cutover identity mismatch")
        _verify_release_inventory(self.plan.release_manifest.path.parent, release)

        artifacts = deploy.get("artifacts")
        if not isinstance(artifacts, list) or not artifacts:
            raise CutoverError("deploy manifest artifact inventory is missing")
        artifact_by_path: dict[str, Path] = {}
        for row in artifacts:
            if not isinstance(row, dict) or not isinstance(row.get("path"), str):
                raise CutoverError("deploy manifest artifact inventory is invalid")
            path = _safe_artifact(marker_root, row["path"])
            if (
                not isinstance(row.get("sha256"), str)
                or not SHA256_RE.fullmatch(row["sha256"])
                or _sha256_file(path) != row["sha256"]
                or path.stat().st_size != row.get("size")
                or row["path"] in artifact_by_path
            ):
                raise CutoverError(f"deployment artifact drift detected: {row['path']}")
            artifact_by_path[row["path"]] = path

        external_inputs = deploy.get("external_inputs")
        runtime_manifest_relative = "runtime-inputs/python-runtime-manifest.json"
        runtime_manifest = artifact_by_path.get(runtime_manifest_relative)
        if not isinstance(external_inputs, dict) or runtime_manifest is None:
            raise CutoverError("deployment lacks a hash-bound Python runtime inventory")
        if (
            external_inputs.get("python_runtime_manifest") != str(runtime_manifest)
            or external_inputs.get("python_runtime_manifest_sha256") != _sha256_file(runtime_manifest)
            or not isinstance(external_inputs.get("python_runtime_tree_sha256"), str)
            or not SHA256_RE.fullmatch(external_inputs["python_runtime_tree_sha256"])
        ):
            raise CutoverError("deployment Python runtime inventory binding is invalid")
        try:
            runtime_report = verify_runtime_manifest(runtime_manifest)
        except (OSError, PythonRuntimeBlocked) as exc:
            raise CutoverError(f"external Python runtime drift detected: {exc}") from exc
        if runtime_report.get("tree_sha256") != external_inputs["python_runtime_tree_sha256"]:
            raise CutoverError("external Python runtime tree SHA-256 mismatch")

        roles = deploy.get("roles")
        if not isinstance(roles, list) or len(roles) != 3:
            raise CutoverError("deploy manifest must contain exactly three V3 roles")
        by_role = {row.get("role"): row for row in roles if isinstance(row, dict)}
        expected = {
            "control": "com.magi.v3.control",
            "gateway": "com.magi.v3.gateway",
            "supervisor": "com.magi.v3.supervisor",
        }
        if set(by_role) != set(expected):
            raise CutoverError("deploy manifest V3 roles are incomplete")
        raw_runtime = deploy.get("runtime_root")
        if not isinstance(raw_runtime, str) or not Path(raw_runtime).is_absolute():
            raise CutoverError("deploy manifest runtime root is invalid")
        runtime_root = Path(raw_runtime).resolve(strict=False)
        ownership_source = artifact_by_path.get(OWNERSHIP_MANIFEST_NAME)
        ownership_sha256 = marker["ownership_manifest_sha256"]
        ownership_target = runtime_root / OWNERSHIP_MANIFEST_NAME
        if not (
            ownership_source is not None
            and _sha256_file(ownership_source) == ownership_sha256
            and deploy.get("ownership_manifest") == str(ownership_target)
            and deploy.get("ownership_manifest_sha256") == ownership_sha256
        ):
            raise CutoverError("deployment ownership manifest binding is invalid")
        ownership = _load_json(ownership_source, description="prepared ownership manifest")
        if not (
            ownership.get("schema_version") == 1
            and ownership.get("status") == "prepared_not_installed"
            and ownership.get("release_id") == release.get("release_id")
            and ownership.get("release_manifest_sha256") == self.plan.release_manifest.sha256
            and ownership.get("runtime_root") == str(runtime_root)
            and isinstance(ownership.get("roles"), list)
        ):
            raise CutoverError("prepared ownership manifest identity is invalid")
        v3_agents: list[LaunchAgent] = []
        for role in ("control", "gateway", "supervisor"):
            row = by_role[role]
            label = row.get("label")
            relative = f"launchagents/{label}.plist"
            path = artifact_by_path.get(relative)
            if label != expected[role] or path is None:
                raise CutoverError(f"deploy manifest role binding is invalid: {role}")
            expected_paths = {
                "state_dir": runtime_root / "state" / role,
                "log_dir": runtime_root / "logs" / role,
                "pid_file": runtime_root / "pids" / f"{role}.pid",
            }
            if any(
                not isinstance(row.get(key), str)
                or not Path(row[key]).is_absolute()
                or Path(row[key]).resolve(strict=False) != expected_path
                for key, expected_path in expected_paths.items()
            ):
                raise CutoverError(f"deploy manifest runtime layout is invalid: {role}")
            with path.open("rb") as handle:
                plist = plistlib.load(handle)
            if plist.get("Label") != label:
                raise CutoverError(f"V3 launchagent plist label mismatch: {label}")
            environment = plist.get("EnvironmentVariables")
            if not (
                isinstance(environment, dict)
                and environment.get("MAGI_V3_OWNERSHIP_MANIFEST") == str(ownership_target)
                and environment.get("MAGI_V3_OWNERSHIP_MANIFEST_SHA256") == ownership_sha256
                and row.get("ownership_manifest") == str(ownership_target)
                and row.get("ownership_manifest_sha256") == ownership_sha256
            ):
                raise CutoverError(f"V3 ownership plist binding is invalid: {role}")
            v3_agents.append(LaunchAgent(label, BoundFile(path, _sha256_file(path))))
        return report, deploy, tuple(v3_agents)

    def _check_token(self, *, consume: bool) -> None:
        raw = self.token_file.expanduser()
        if not raw.is_absolute() or raw.is_symlink():
            raise CutoverError("one-time token must be an absolute non-symlink file")
        try:
            descriptor = os.open(raw, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except OSError as exc:
            raise CutoverError(f"one-time token file is unavailable: {exc}") from exc
        try:
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or metadata.st_uid != os.getuid()
                or metadata.st_nlink != 1
            ):
                raise CutoverError("one-time token file must be owner-only 0600 regular file")
            with os.fdopen(descriptor, "rb", closefd=False) as handle:
                token = handle.read(4097)
            if len(token) > 4096:
                raise CutoverError("one-time token is too large")
            token = token.rstrip(b"\r\n")
            if not token or not hmac.compare_digest(
                hashlib.sha256(token).hexdigest(), self.plan.token_sha256
            ):
                raise CutoverError("one-time token does not match the hash-bound plan")
            current = raw.lstat()
            if (current.st_dev, current.st_ino) != (metadata.st_dev, metadata.st_ino):
                raise CutoverError("one-time token file changed while arming")
            if consume:
                raw.unlink()
                directory = os.open(raw.parent, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
        finally:
            os.close(descriptor)
        self._event("consume_token" if consume else "verify_token", ok=True)

    def _verify_token(self) -> None:
        self._check_token(consume=False)

    def _consume_token(self) -> None:
        self._check_token(consume=True)

    def _launchctl(self, *arguments: str, tolerate_missing: bool = False) -> MutationResult:
        argv = ("launchctl", *arguments)
        raw = self.runner(list(argv))
        result = MutationResult(
            argv=argv,
            returncode=int(getattr(raw, "returncode", -1)),
            stdout=str(getattr(raw, "stdout", "") or ""),
            stderr=str(getattr(raw, "stderr", "") or ""),
        )
        self._event("launchctl", argv=list(argv), returncode=result.returncode)
        if result.returncode != 0 and not tolerate_missing:
            raise CutoverError(
                f"launchctl failed rc={result.returncode}: {' '.join(argv[1:])}: "
                f"{result.stderr.strip()[:200]}"
            )
        return result

    def _assess(self, expected: str) -> None:
        # Never let a caller-provided plan narrow the application-plane proof.
        # The code-owned exact set is revalidated at every ownership boundary.
        application_set_sha256 = v2_application_set_sha256(self.plan.v2_launchagents)
        if application_set_sha256 != self.plan.v2_application_set_sha256:
            raise CutoverError("V2 application launchagent set drifted after plan loading")
        if (
            v2_initial_loaded_set_sha256(self.plan.v2_launchagents)
            != self.plan.v2_initial_loaded_set_sha256
            or v2_keepalive_set_sha256(self.plan.v2_launchagents)
            != self.plan.v2_keepalive_set_sha256
        ):
            raise CutoverError("V2 initial/KeepAlive launchagent state drifted after plan loading")
        snapshot = self.snapshot_collector()
        assessment = assess_snapshot(snapshot, expected=expected)  # type: ignore[arg-type]
        self._event("verify_ownership", expected=expected, assessment=assessment.to_dict())
        if not assessment.go:
            raise CutoverError(
                f"ownership verification failed expected={expected}: {'; '.join(assessment.reasons)}"
            )
        if expected not in {"v2", "v3"}:
            return
        launchd = snapshot.metadata.get("launchd")
        if not isinstance(launchd, dict):
            raise CutoverError(f"{expected} launchd status coverage is missing")
        unsafe: list[str] = []
        if expected == "v2":
            for agent in self.plan.v2_launchagents:
                status = launchd.get(agent.label)
                if not isinstance(status, dict) or status.get("loaded") is not agent.initial_loaded:
                    unsafe.append(agent.label)
                    continue
                if (
                    agent.initial_loaded
                    and agent.keepalive_required_running
                    and not (
                        isinstance(status.get("pid"), int)
                        and status["pid"] > 0
                        and status.get("state") == "running"
                    )
                ):
                    unsafe.append(agent.label)
        else:
            for label in sorted(V3_LABELS):
                status = launchd.get(label)
                if not (
                    isinstance(status, dict)
                    and status.get("loaded") is True
                    and isinstance(status.get("pid"), int)
                    and status["pid"] > 0
                ):
                    unsafe.append(label)
        if unsafe:
            raise CutoverError(
                f"{expected} required launchagents are not running: {', '.join(unsafe)}"
            )

    def _execute_laf_dedup_handoff(self) -> None:
        handoff = self.plan.laf_dedup_handoff
        if handoff is None:
            raise CutoverError("cutover plan lacks the mandatory LAF dedup handoff")
        try:
            raw = self.laf_dedup_handoff(handoff)
            report = {
                "status": raw.get("status"),
                "stages": raw.get("stages"),
                "manifest_sha256": raw.get("manifest_sha256"),
                "record_count": raw.get("record_count"),
                "records_sha256": raw.get("records_sha256"),
                "dry_run_ok": raw.get("dry_run_ok"),
                "apply_ok": raw.get("apply_ok"),
                "dual_table_verified": raw.get("dual_table_verified"),
                "contains_business_payload": raw.get("contains_business_payload"),
            }
            if not (
                report["status"] == "complete"
                and report["stages"]
                == [
                    "snapshot",
                    "verify",
                    "db_dry_run",
                    "apply",
                    "dual_table_verify",
                    "source_reverify",
                ]
                and report["dry_run_ok"] is True
                and report["apply_ok"] is True
                and report["dual_table_verified"] is True
                and report["contains_business_payload"] is False
                and isinstance(report["manifest_sha256"], str)
                and SHA256_RE.fullmatch(report["manifest_sha256"])
                and isinstance(report["record_count"], int)
                and not isinstance(report["record_count"], bool)
                and report["record_count"] >= 0
            ):
                raise CutoverError("LAF dedup handoff returned incomplete stage evidence")
        except BaseException as exc:
            self._event("laf_dedup_handoff", ok=False, error_type=type(exc).__name__)
            # Never copy connector errors, environment values, or message ids
            # into the public cutover report.
            raise CutoverError("LAF dedup compatibility handoff failed closed") from exc
        self.laf_handoff_completed = True
        self._event("laf_dedup_handoff", ok=True, detail=report)

    def _execute_pdf_namer_handoff(self) -> None:
        handoff = self.plan.pdf_namer_handoff
        if handoff is None:
            raise CutoverError("cutover plan lacks the mandatory pdf-namer state handoff")
        try:
            raw = self.pdf_namer_handoff(handoff)
            report = {
                "status": raw.get("status"),
                "snapshot_sha256": raw.get("snapshot_sha256"),
                "file_count": raw.get("file_count"),
                "record_count": raw.get("record_count"),
                "contains_business_payload": raw.get("contains_business_payload"),
                "contains_file_names": raw.get("contains_file_names"),
            }
            if not (
                report["status"] == "complete"
                and isinstance(report["snapshot_sha256"], str)
                and SHA256_RE.fullmatch(report["snapshot_sha256"])
                and isinstance(report["file_count"], int)
                and not isinstance(report["file_count"], bool)
                and report["file_count"] >= 0
                and isinstance(report["record_count"], int)
                and not isinstance(report["record_count"], bool)
                and report["record_count"] >= 0
                and report["contains_business_payload"] is False
                and report["contains_file_names"] is False
            ):
                raise CutoverError("pdf-namer handoff returned incomplete evidence")
        except BaseException as exc:
            self._event("pdf_namer_handoff", ok=False, error_type=type(exc).__name__)
            raise CutoverError("pdf-namer mutable state handoff failed closed") from exc
        self.pdf_namer_handoff_completed = True
        self._event("pdf_namer_handoff", ok=True, detail=report)

    def _install_ownership_manifest(self, deploy: Mapping[str, Any]) -> None:
        runtime_root = Path(str(deploy["runtime_root"]))
        target = runtime_root / OWNERSHIP_MANIFEST_NAME
        source = _safe_artifact(
            self.plan.deploy_prepared_marker.path.parent,
            OWNERSHIP_MANIFEST_NAME,
        )
        expected_sha256 = str(deploy.get("ownership_manifest_sha256", ""))
        if not SHA256_RE.fullmatch(expected_sha256):
            raise CutoverError("prepared ownership manifest SHA-256 is invalid")
        binding = BoundFile(source, expected_sha256)
        _verify_bound_file(binding, description="prepared ownership manifest")
        data = source.read_bytes()
        if hashlib.sha256(data).hexdigest() != expected_sha256:
            raise CutoverError("prepared ownership manifest changed before install")

        if runtime_root.is_symlink():
            raise CutoverError("V3 runtime root is symlinked")
        runtime_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if runtime_root.resolve(strict=True) != runtime_root or not runtime_root.is_dir():
            raise CutoverError("V3 runtime root is non-canonical or unsafe")
        directory = target.parent
        if directory.is_symlink():
            raise CutoverError("V3 ownership directory is symlinked")
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if directory.resolve(strict=True) != directory or not directory.is_dir():
            raise CutoverError("V3 ownership directory is non-canonical or unsafe")
        if target.exists() or target.is_symlink():
            raise CutoverError(f"V3 ownership manifest target already exists: {target}")

        temporary = directory / f".{target.name}.tmp-{os.getpid()}-{uuid.uuid4().hex}"
        published = False
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o644,
            )
            try:
                with os.fdopen(descriptor, "wb", closefd=False) as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
            finally:
                os.close(descriptor)
            if _sha256_file(temporary) != expected_sha256:
                raise CutoverError("temporary ownership manifest failed hash verification")
            # link(2) is the portable same-filesystem no-clobber publish
            # primitive available on macOS.  Unlike replace(2), it fails if a
            # target appears after the earlier existence check.  Removing the
            # temporary name immediately afterwards leaves one independently
            # copied runtime inode (never the staging inode).
            try:
                os.link(temporary, target, follow_symlinks=False)
            except FileExistsError as exc:
                raise CutoverError(
                    f"V3 ownership manifest target appeared during install: {target}"
                ) from exc
            except OSError as exc:
                raise CutoverError("V3 ownership manifest no-clobber publish failed") from exc
            published = True
            self.installed_ownership = (target, expected_sha256)
            temporary.unlink()
            directory_fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            temporary.unlink(missing_ok=True)
            if published:
                directory_fd = os.open(directory, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        _verify_bound_file(
            BoundFile(target, expected_sha256),
            description="installed ownership manifest",
        )
        self._event(
            "install_ownership_manifest",
            path=str(target),
            sha256=expected_sha256,
        )

    def _verify_installed_ownership(self, deploy: Mapping[str, Any]) -> BoundFile:
        path = Path(str(deploy["runtime_root"])) / OWNERSHIP_MANIFEST_NAME
        expected_sha256 = str(deploy.get("ownership_manifest_sha256", ""))
        if not SHA256_RE.fullmatch(expected_sha256):
            raise CutoverError("installed ownership manifest SHA-256 binding is invalid")
        binding = BoundFile(path, expected_sha256)
        _verify_bound_file(binding, description="installed ownership manifest")
        return binding

    def _atomic_install(self, agents: Sequence[LaunchAgent], deploy: Mapping[str, Any]) -> None:
        directory = self.plan.v3_install_directory
        directory.mkdir(parents=True, exist_ok=True, mode=0o755)
        for role in deploy["roles"]:
            for key in ("state_dir", "log_dir"):
                Path(str(role[key])).mkdir(parents=True, exist_ok=True, mode=0o700)
            Path(str(role["pid_file"])).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._install_ownership_manifest(deploy)
        for agent in agents:
            target = directory / f"{agent.label}.plist"
            if target.exists() or target.is_symlink():
                raise CutoverError(f"V3 launchagent target already exists: {target}")
            data = agent.plist.path.read_bytes()
            if hashlib.sha256(data).hexdigest() != agent.plist.sha256:
                raise CutoverError(f"V3 launchagent changed before install: {agent.label}")
            temporary = directory / f".{target.name}.tmp-{os.getpid()}-{uuid.uuid4().hex}"
            try:
                descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
                try:
                    with os.fdopen(descriptor, "wb", closefd=False) as handle:
                        handle.write(data)
                        handle.flush()
                        os.fsync(handle.fileno())
                finally:
                    os.close(descriptor)
                if _sha256_file(temporary) != agent.plist.sha256:
                    raise CutoverError(f"temporary V3 launchagent failed hash verification: {agent.label}")
                try:
                    os.link(temporary, target, follow_symlinks=False)
                except FileExistsError as exc:
                    raise CutoverError(
                        f"V3 launchagent target appeared during install: {target}"
                    ) from exc
                except OSError as exc:
                    raise CutoverError(
                        f"V3 launchagent no-clobber publish failed: {agent.label}"
                    ) from exc
                self.installed_v3.append((target, agent.plist.sha256))
                temporary.unlink()
                _verify_bound_file(
                    BoundFile(target, agent.plist.sha256),
                    description=f"installed V3 launchagent {agent.label}",
                )
            finally:
                temporary.unlink(missing_ok=True)
        directory_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        self._event(
            "install_v3",
            labels=[agent.label for agent in agents],
            ownership_manifest=str(self.installed_ownership[0]) if self.installed_ownership else "",
        )

    def _remove_installed_v3(self) -> None:
        for path, expected_sha256 in reversed(self.installed_v3):
            try:
                if path.exists() and _sha256_file(path) != expected_sha256:
                    raise CutoverError(f"installed V3 plist changed before rollback removal: {path}")
                path.unlink(missing_ok=True)
            except OSError as exc:
                self._event("remove_v3_plist", path=str(path), ok=False, error=str(exc))
                raise
            else:
                self._event("remove_v3_plist", path=str(path), ok=True)
        self.installed_v3.clear()
        if self.installed_ownership is not None:
            path, expected_sha256 = self.installed_ownership
            try:
                if path.exists() or path.is_symlink():
                    _verify_bound_file(
                        BoundFile(path, expected_sha256),
                        description="installed ownership manifest before rollback removal",
                    )
                    path.unlink()
                    directory_fd = os.open(path.parent, os.O_RDONLY)
                    try:
                        os.fsync(directory_fd)
                    finally:
                        os.close(directory_fd)
            except OSError as exc:
                self._event(
                    "remove_ownership_manifest", path=str(path), ok=False, error=str(exc)
                )
                raise
            else:
                self._event("remove_ownership_manifest", path=str(path), ok=True)
            self.installed_ownership = None

    def _rollback(self) -> tuple[bool, str]:
        try:
            if self.plan.mutable_state_handoff is not None:
                # Mutable state may already have been consumed or advanced by
                # V3.  Rollback never deletes/reverses it and preserves both
                # audit receipts; only service ownership is restored to V2.
                self._event(
                    "preserve_mutable_state_handoff",
                    dry_run_receipt=str(self.plan.mutable_state_handoff.dry_run_receipt),
                    prepare_receipt=str(self.plan.mutable_state_handoff.prepare_receipt),
                )
            if self.activation is not None:
                phase = self.activation.document().get("phase")
                if phase not in {
                    "rollback_started",
                    "v3_zero",
                    "v2_commit_intent",
                    "v2_committed",
                }:
                    self._activation_receipt(self.activation.advance("rollback_started"))
            for agent in reversed(self.started_v3):
                self._launchctl("bootout", f"gui/{self.uid}/{agent.label}", tolerate_missing=True)
            self.started_v3.clear()
            self._remove_installed_v3()
            try:
                self._assess("v2")
            except CutoverError:
                pass
            else:
                activation_history = (
                    self.activation.document().get("history", [])
                    if self.activation is not None
                    else []
                )
                v3_was_active = any(
                    isinstance(row, dict) and row.get("phase") == "v3_active"
                    for row in activation_history
                )
                if self.runtime_state_handoff_completed and v3_was_active:
                    raise CutoverError(
                        "V2 relaunched before reverse runtime-state handoff; refusing unsafe rollback"
                    )
                if self.activation is not None:
                    if self.activation.document().get("phase") != "v2_committed":
                        release_id, root, digest = self._v2_marker_identity()
                        self._activation_receipt(
                            self.activation.commit_release(
                                release="v2",
                                release_id=release_id,
                                release_root=root,
                                release_manifest_sha256=digest,
                            )
                        )
                    self._activation_receipt(self.activation.advance("v2_restored"))
                    self.reconciliation_after = self._reconciliation(
                        "v2", require_v3_ledgers=v3_was_active
                    )
                    self._activation_receipt(
                        self.activation.advance(
                            "complete", reconciliation_after=dict(self.reconciliation_after)
                        )
                    )
                self._event("rollback", ok=True, detail="V2 remained active")
                return True, "V2 remained active"
            self._assess("zero")
            if self.activation is not None:
                phase = self.activation.document().get("phase")
                if phase == "rollback_started":
                    self._activation_receipt(self.activation.advance("v3_zero"))
                    phase = "v3_zero"
                if phase != "v2_committed":
                    if self.runtime_state_handoff_completed:
                        self._execute_runtime_state_handoff(to_owner="v2")
                    release_id, root, digest = self._v2_marker_identity()
                    self._activation_receipt(
                        self.activation.commit_release(
                            release="v2",
                            release_id=release_id,
                            release_root=root,
                            release_manifest_sha256=digest,
                        )
                    )
            for agent in (
                item for item in self.plan.v2_launchagents if item.initial_loaded
            ):
                if _sha256_file(agent.plist.path) != agent.plist.sha256:
                    raise CutoverError(f"V2 rollback plist drift detected: {agent.label}")
                self._launchctl("bootstrap", f"gui/{self.uid}", str(agent.plist.path))
            self._assess("v2")
            if self.activation is not None:
                self._activation_receipt(self.activation.advance("v2_restored"))
                self.reconciliation_after = self._reconciliation(
                    "v2", require_v3_ledgers=True
                )
                self._activation_receipt(
                    self.activation.advance(
                        "complete", reconciliation_after=dict(self.reconciliation_after)
                    )
                )
            self._event("rollback", ok=True, detail="V2 restored")
            return True, "V2 restored"
        except BaseException as exc:
            self._event("rollback", ok=False, error=str(exc))
            return False, str(exc)

    def execute(self) -> dict[str, Any]:
        """Execute exactly one cutover; return a non-secret machine report."""

        started_at = self.clock().astimezone(timezone.utc).isoformat()
        try:
            if self.plan.operation != "v2_to_v3_cutover":
                raise CutoverError("prepared plan is not a V2-to-V3 cutover")
            _report, deploy, v3_agents = self._validate_static_gates()
            self._runtime_root = Path(str(deploy["runtime_root"])).resolve(strict=False)
            interrupted = self._recover_interrupted_activation(deploy, v3_agents)
            if interrupted is not None:
                return interrupted
            self._assess("v2")
            # Complete every read-only check, including an exact state snapshot,
            # before stopping V2.  The token is verified but deliberately not
            # consumed until the post-quiesce handoff has succeeded.
            self._execute_mutable_state_dry_run()
            self._verify_token()
            release = _load_json(self.plan.release_manifest.path, description="release manifest")
            self.reconciliation_before = self._reconciliation("v2")
            self.activation = ActivationTransaction.begin(
                state_parent=self._runtime_root.parent,
                plan_sha256=self.plan.source.sha256,
                release_id=str(release["release_id"]),
                release_root=self.plan.release_manifest.path.parent,
                release_manifest_sha256=self.plan.release_manifest.sha256,
                reconciliation_before=self.reconciliation_before,
                clock=lambda: self.clock().astimezone(timezone.utc).isoformat(),
            )
            self._event(
                "activation_transaction",
                receipt=self.activation.initial_receipt(),
            )
            self.handoff_started = True
            for agent in (
                item for item in self.plan.v2_launchagents if item.initial_loaded
            ):
                self._verify_effective_cutover_window(
                    stage=f"before_v2_bootout:{agent.label}"
                )
                self._launchctl("bootout", f"gui/{self.uid}/{agent.label}")
            self._assess("zero")
            self._activation_receipt(self.activation.advance("v2_zero"))
            # Replaying the same receipt is an exact source/target drift check.
            # A mismatch restores V2 while leaving the one-time token intact.
            self._execute_mutable_state_dry_run()
            self._execute_mutable_state_prepare()
            self._execute_runtime_state_handoff(to_owner="v3")
            # Close the stop-to-install TOCTOU gap before writing or starting V3.
            self._validate_static_gates()
            self._execute_pdf_namer_handoff()
            self._execute_laf_dedup_handoff()
            # The DB handoff can take time.  Recheck every immutable release
            # input again before the first V3 plist is written.
            _report, deploy, v3_agents = self._validate_static_gates()
            # launchd or an operator may have relaunched V2 while the database
            # handoff ran.  Re-prove zero ownership before any V3 installation.
            self._assess("zero")
            self._verify_effective_cutover_window(stage="before_v3_install")
            self._consume_token()
            self._atomic_install(v3_agents, deploy)
            self._activation_receipt(self.activation.advance("v3_files_installed"))
            self._activation_receipt(
                self.activation.commit_release(
                    release="v3",
                    release_id=str(release["release_id"]),
                    release_root=self.plan.release_manifest.path.parent,
                    release_manifest_sha256=self.plan.release_manifest.sha256,
                )
            )
            for agent in v3_agents:
                self._verify_effective_cutover_window(
                    stage=f"before_v3_bootstrap:{agent.label}"
                )
                target = self.plan.v3_install_directory / f"{agent.label}.plist"
                if _sha256_file(target) != agent.plist.sha256:
                    raise CutoverError(f"installed V3 plist hash mismatch: {agent.label}")
                self._launchctl("bootstrap", f"gui/{self.uid}", str(target))
                self.started_v3.append(agent)
            self._assess("v3")
            ready, detail = self.readiness_probe(self.plan.readiness_urls)
            self._event("verify_readiness", ok=bool(ready), detail=dict(detail))
            if not ready:
                raise CutoverError("V3 readiness verification failed")
            self.reconciliation_after = self._reconciliation("v3")
            self._activation_receipt(
                self.activation.advance(
                    "v3_active", reconciliation_after=dict(self.reconciliation_after)
                )
            )
            return {
                "schema_version": 1,
                "report_id": uuid.uuid4().hex,
                "report_kind": "v2_to_v3_cutover_execution",
                "status": "cutover_complete",
                "ok": True,
                "mutation_performed": True,
                "rollback_performed": False,
                "started_at": started_at,
                "finished_at": self.clock().astimezone(timezone.utc).isoformat(),
                "hash_context": {
                    "plan_sha256": self.plan.source.sha256,
                    "release_manifest_sha256": self.plan.release_manifest.sha256,
                    "deploy_prepared_marker_sha256": self.plan.deploy_prepared_marker.sha256,
                    "mutable_state_prepare_receipt_sha256": (
                        next(
                            (
                                row.get("receipt_sha256")
                                for row in reversed(self.events)
                                if row.get("action") == "mutable_state_prepare" and row.get("ok") is True
                            ),
                            None,
                        )
                    ),
                    "mutable_state_target_snapshot_sha256": (
                        self.mutable_state_prepare.get("target_snapshot_sha256")
                        if self.mutable_state_prepare is not None
                        else None
                    ),
                },
                "activation_transaction_id": self.activation.transaction_id,
                "reconciliation_before": self.reconciliation_before,
                "reconciliation_after": self.reconciliation_after,
                "events": self.events,
            }
        except BaseException as exc:
            rollback_ok, rollback_detail = (
                self._rollback() if self.handoff_started else (False, "handoff not started")
            )
            return {
                "schema_version": 1,
                "report_id": uuid.uuid4().hex,
                "report_kind": "v2_to_v3_cutover_execution",
                "status": "rolled_back" if rollback_ok else "blocked",
                "ok": False,
                "mutation_performed": self.handoff_started,
                "rollback_performed": self.handoff_started,
                "rollback_ok": rollback_ok,
                "rollback_detail": rollback_detail,
                "error": str(exc),
                "started_at": started_at,
                "finished_at": self.clock().astimezone(timezone.utc).isoformat(),
                "hash_context": {
                    "plan_sha256": self.plan.source.sha256,
                    "release_manifest_sha256": self.plan.release_manifest.sha256,
                    "deploy_prepared_marker_sha256": self.plan.deploy_prepared_marker.sha256,
                    "mutable_state_prepare_receipt_sha256": next(
                        (
                            row.get("receipt_sha256")
                            for row in reversed(self.events)
                            if row.get("action") == "mutable_state_prepare" and row.get("ok") is True
                        ),
                        None,
                    ),
                    "mutable_state_target_snapshot_sha256": (
                        self.mutable_state_prepare.get("target_snapshot_sha256")
                        if self.mutable_state_prepare is not None
                        else None
                    ),
                },
                "activation_transaction_id": (
                    self.activation.transaction_id if self.activation is not None else None
                ),
                "reconciliation_before": self.reconciliation_before,
                "reconciliation_after": self.reconciliation_after,
                "events": self.events,
            }


class PreparedRollbackExecutor(PreparedCutoverExecutor):
    """Perform an independently armed V3-to-V2 rollback.

    A failed rollback attempts to restore the already installed, hash-bound V3
    launchagents.  It never removes either release's plist files.
    """

    def _restore_v3(self, v3_agents: Sequence[LaunchAgent]) -> tuple[bool, str]:
        try:
            for agent in (
                item for item in self.plan.v2_launchagents if item.initial_loaded
            ):
                self._launchctl(
                    "bootout", f"gui/{self.uid}/{agent.label}", tolerate_missing=True
                )
            # A rollback may fail after only some V3 roles stopped.  Quiesce
            # every planned V3 label before restoring the complete release.
            for agent in reversed(v3_agents):
                self._launchctl(
                    "bootout", f"gui/{self.uid}/{agent.label}", tolerate_missing=True
                )
            self._assess("zero")
            self._verify_installed_ownership(self._rollback_deploy)
            if self.activation is not None:
                release = _load_json(
                    self.plan.release_manifest.path, description="release manifest"
                )
                self._activation_receipt(
                    self.activation.commit_release(
                        release="v3",
                        release_id=str(release["release_id"]),
                        release_root=self.plan.release_manifest.path.parent,
                        release_manifest_sha256=self.plan.release_manifest.sha256,
                    )
                )
            for agent in v3_agents:
                target = self.plan.v3_install_directory / f"{agent.label}.plist"
                if not target.is_file() or _sha256_file(target) != agent.plist.sha256:
                    raise CutoverError(f"installed V3 rollback plist drift detected: {agent.label}")
                self._launchctl("bootstrap", f"gui/{self.uid}", str(target))
            self._assess("v3")
            if self.activation is not None:
                self._activation_receipt(self.activation.advance("v3_active"))
            self._event("rollback_recovery", ok=True, detail="V3 restored")
            return True, "V3 restored"
        except BaseException as exc:
            self._event("rollback_recovery", ok=False, error=str(exc))
            return False, str(exc)

    def execute(self) -> dict[str, Any]:
        started_at = self.clock().astimezone(timezone.utc)
        rollback_started_at: datetime | None = None
        try:
            if self.plan.operation != "v3_to_v2_rollback":
                raise CutoverError("prepared plan is not a V3-to-V2 rollback")
            _report, deploy, v3_agents = self._validate_static_gates(
                enforce_fresh_cutover=False
            )
            self._runtime_root = Path(str(deploy["runtime_root"])).resolve(strict=False)
            self._rollback_deploy = deploy
            self.activation = ActivationTransaction.resume(
                state_parent=self._runtime_root.parent,
                clock=lambda: self.clock().astimezone(timezone.utc).isoformat(),
            )
            activation_document = self.activation.document()
            release = _load_json(self.plan.release_manifest.path, description="release manifest")
            if (
                activation_document.get("phase") != "v3_active"
                or activation_document.get("release_id") != release.get("release_id")
                or activation_document.get("release_manifest_sha256")
                != self.plan.release_manifest.sha256
            ):
                raise CutoverError("rollback activation journal is not the active V3 transaction")
            active_release_marker(
                self.activation.marker_path,
                expected_release="v3",
                expected_release_id=str(release["release_id"]),
                expected_release_root=self.plan.release_manifest.path.parent,
                expected_manifest_sha256=self.plan.release_manifest.sha256,
            )
            self.reconciliation_before = self._reconciliation("v3")
            if self.reconciliation_before.get("native_pending_id_hashes") != []:
                raise CutoverError(
                    "cold rollback requires native V3 jobs to reach a terminal state first"
                )
            self._verify_installed_ownership(deploy)
            for agent in v3_agents:
                target = self.plan.v3_install_directory / f"{agent.label}.plist"
                if not target.is_file() or _sha256_file(target) != agent.plist.sha256:
                    raise CutoverError(f"installed V3 plist is missing or drifted: {agent.label}")
            self._assess("v3")
            self._consume_token()
            self.handoff_started = True
            rollback_started_at = self.clock().astimezone(timezone.utc)
            self._activation_receipt(self.activation.advance("rollback_started"))
            for agent in reversed(v3_agents):
                self._launchctl("bootout", f"gui/{self.uid}/{agent.label}")
            self._assess("zero")
            self._activation_receipt(self.activation.advance("v3_zero"))
            self.runtime_state_handoff_completed = True
            self._execute_runtime_state_handoff(to_owner="v2")
            release_id, v2_root, v2_digest = self._v2_marker_identity()
            self._activation_receipt(
                self.activation.commit_release(
                    release="v2",
                    release_id=release_id,
                    release_root=v2_root,
                    release_manifest_sha256=v2_digest,
                )
            )
            for agent in (
                item for item in self.plan.v2_launchagents if item.initial_loaded
            ):
                if _sha256_file(agent.plist.path) != agent.plist.sha256:
                    raise CutoverError(f"V2 rollback plist drift detected: {agent.label}")
                self._launchctl("bootstrap", f"gui/{self.uid}", str(agent.plist.path))
            self._assess("v2")
            v2_restored_at = self.clock().astimezone(timezone.utc)
            self._activation_receipt(self.activation.advance("v2_restored"))
            self.reconciliation_after = self._reconciliation(
                "v2", require_v3_ledgers=True
            )
            self._activation_receipt(
                self.activation.advance(
                    "complete", reconciliation_after=dict(self.reconciliation_after)
                )
            )
            finished = self.clock().astimezone(timezone.utc)
            return {
                "schema_version": 1,
                "report_id": uuid.uuid4().hex,
                "report_kind": "v3_to_v2_rollback_execution",
                "status": "rollback_complete",
                "ok": True,
                "mutation_performed": True,
                "rollback_performed": True,
                "started_at": started_at.isoformat(),
                "finished_at": finished.isoformat(),
                "rollback_rto_seconds": (
                    v2_restored_at - (rollback_started_at or started_at)
                ).total_seconds(),
                "hash_context": {
                    "plan_sha256": self.plan.source.sha256,
                    "release_manifest_sha256": self.plan.release_manifest.sha256,
                    "deploy_prepared_marker_sha256": self.plan.deploy_prepared_marker.sha256,
                },
                "activation_transaction_id": self.activation.transaction_id,
                "reconciliation_before": self.reconciliation_before,
                "reconciliation_after": self.reconciliation_after,
                "events": self.events,
            }
        except BaseException as exc:
            recovered, detail = (
                self._restore_v3(v3_agents)  # type: ignore[possibly-undefined]
                if self.handoff_started
                else (False, "handoff not started")
            )
            return {
                "schema_version": 1,
                "report_id": uuid.uuid4().hex,
                "report_kind": "v3_to_v2_rollback_execution",
                "status": "v3_restored" if recovered else "blocked",
                "ok": False,
                "mutation_performed": self.handoff_started,
                "rollback_performed": self.handoff_started,
                "recovery_ok": recovered,
                "recovery_detail": detail,
                "error": str(exc),
                "started_at": started_at.isoformat(),
                "finished_at": self.clock().astimezone(timezone.utc).isoformat(),
                "rollback_rto_seconds": None,
                "hash_context": {
                    "plan_sha256": self.plan.source.sha256,
                    "release_manifest_sha256": self.plan.release_manifest.sha256,
                    "deploy_prepared_marker_sha256": self.plan.deploy_prepared_marker.sha256,
                },
                "activation_transaction_id": (
                    self.activation.transaction_id if self.activation is not None else None
                ),
                "reconciliation_before": self.reconciliation_before,
                "reconciliation_after": self.reconciliation_after,
                "events": self.events,
            }
def execute_prepared_cutover(
    *,
    plan_path: Path,
    plan_sha256: str,
    token_file: Path,
    snapshot_collector: SnapshotCollector,
    runner: CommandRunner = _default_runner,
    readiness_probe: ReadinessProbe = _default_readiness_probe,
    clock: Clock | None = None,
    canonical_launchagents_directory: Path | None = None,
) -> dict[str, Any]:
    plan = load_prepared_plan(
        plan_path,
        plan_sha256,
        canonical_launchagents_directory=canonical_launchagents_directory,
    )
    return PreparedCutoverExecutor(
        plan,
        token_file=token_file,
        snapshot_collector=snapshot_collector,
        runner=runner,
        readiness_probe=readiness_probe,
        clock=clock,
    ).execute()
