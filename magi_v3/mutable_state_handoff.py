"""Fail-closed, allowlisted V2-to-V3 mutable-state handoff.

The handoff copies only named operational state files.  It never walks a V2
directory and receipts contain digests and state identifiers, not file
contents.  Callers must bind every run to the exact release/deployment/cutover
context that will consume the state.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import shutil
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Literal, Mapping


SCHEMA = "magi.v3.mutable-state-handoff/v1"
MAX_STATE_BYTES = 64 * 1024 * 1024
SHA256_RE = re.compile(r"[0-9a-f]{64}")
RELEASE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{2,127}")


class MutableStateHandoffError(RuntimeError):
    """A fail-closed error that never includes mutable-state contents."""


@dataclass(frozen=True)
class StateSpec:
    state_id: str
    source_relative: str
    target_relative: str
    required: bool
    encoding: Literal["json", "jsonl", "csv"] = "json"


# Exact files only.  In particular, no directory, glob, or caller-supplied
# relative path can enter the handoff.  Required entries prevent duplicate
# legal/email/payment side effects and loss of the judgment fallback export.
STATE_SPECS = (
    StateSpec("obsidian_wiki", ".agent/wiki_synthesizer_state.json", "agent/wiki_synthesizer_state.json", False),
    StateSpec("obsidian_vault", ".agent/obsidian_vault_config.json", "agent/obsidian_vault_config.json", False),
    StateSpec("obsidian_ingest", ".agent/obsidian_ingest_state.json", "agent/obsidian_ingest_state.json", False),
    StateSpec("obsidian_index", ".agent/obsidian_index.json", "agent/obsidian_index.json", False),
    StateSpec("transcript_sync", ".agent/transcript_sync_state.json", "agent/transcript_sync_state.json", False),
    StateSpec("transcript_manual_queue", "static/transcript_manual_queue.jsonl", "static/transcript_manual_queue.jsonl", False, "jsonl"),
    StateSpec("laf_portal_retry", ".agent/laf_pending_portal_downloads.json", "agent/laf_pending_portal_downloads.json", False),
    StateSpec("laf_seed_skip", ".agent/laf_seed_permanently_skipped.json", "agent/laf_seed_permanently_skipped.json", False),
    StateSpec("laf_processed_email", "json/processed_laf_emails.json", "agent/laf-orchestrator/processed_laf_emails.json", True),
    StateSpec("market_watchlist", ".agent/market_watchlist.json", "agent/market_watchlist.json", False),
    StateSpec("market_data_cache", ".agent/market_data_cache.json", "agent/market_data_cache.json", False),
    StateSpec("market_performance", ".agent/market_perf_history.json", "agent/market_perf_history.json", False),
    StateSpec("bookmark_batch", ".agent/bookmark_batch_state.json", "agent/bookmark_batch_state.json", False),
    StateSpec("discord_channel_map", ".agent/discord_channel_map.json", "agent/discord_channel_map.json", False),
    StateSpec("discord_last_channel", ".agent/discord_last_channel.json", "agent/discord_last_channel.json", False),
    StateSpec("telegram_channel", ".agent/telegram_channel_state.json", "agent/telegram_channel_state.json", False),
    StateSpec("telegram_topic_map", ".agent/telegram_topic_map.json", "agent/telegram_topic_map.json", False),
    StateSpec("telegram_poll_offset", ".agent/telegram_poll_offset.json", "agent/telegram_poll_offset.json", False),
    StateSpec("poa_chat", ".agent/poa_chat_state.json", "agent/poa_chat_state.json", False),
    StateSpec("hearing_reminder", ".agent/hearing_remind_state.json", "agent/hearing_remind_state.json", False),
    StateSpec("file_review_processed_email", "閱卷下載/processed_emails.json", "file-review/downloads/processed_emails.json", True),
    StateSpec("payment_registry", "閱卷下載/payment_registry.json", "file-review/downloads/payment_registry.json", True),
    StateSpec("payment_proof_registry", "閱卷下載/payment_proof_registry.json", "file-review/downloads/payment_proof_registry.json", True),
    StateSpec("judgments_export", "skills/judgment-collector/judgments.json", "agent/judgment-collector/judgments.json", True),
    StateSpec("cortex_cursor", "cortex_sync_state.json", "runtime/cortex_sync_state.json", False),
    StateSpec(
        "debt_address_bank_json",
        "integrations/debt_robot/document/all adress - bank.json",
        "debt/address-book/all adress - bank.json",
        False,
    ),
    StateSpec(
        "debt_address_company_json",
        "integrations/debt_robot/document/all adress - company.json",
        "debt/address-book/all adress - company.json",
        False,
    ),
    StateSpec(
        "debt_address_bank_csv",
        "integrations/debt_robot/document/all adress - bank.csv",
        "debt/address-book/all adress - bank.csv",
        False,
        "csv",
    ),
    StateSpec(
        "debt_address_company_csv",
        "integrations/debt_robot/document/all adress - company.csv",
        "debt/address-book/all adress - company.csv",
        False,
        "csv",
    ),
)


@dataclass(frozen=True)
class ExactContext:
    release_id: str
    release_manifest_sha256: str
    deployment_manifest_sha256: str
    cutover_plan_sha256: str

    def validate(self) -> None:
        if not RELEASE_ID_RE.fullmatch(self.release_id):
            raise MutableStateHandoffError("exact release context is invalid")
        for value in (
            self.release_manifest_sha256,
            self.deployment_manifest_sha256,
            self.cutover_plan_sha256,
        ):
            if not SHA256_RE.fullmatch(value):
                raise MutableStateHandoffError("exact digest context is invalid")

    def public(self) -> dict[str, str]:
        return {
            "release_id": self.release_id,
            "release_manifest_sha256": self.release_manifest_sha256,
            "deployment_manifest_sha256": self.deployment_manifest_sha256,
            "cutover_plan_sha256": self.cutover_plan_sha256,
        }


@dataclass(frozen=True)
class FileSnapshot:
    spec: StateSpec
    data: bytes
    sha256: str
    size: int
    record_count: int
    signature: tuple[int, int, int, int, int, int]

    def public_source(self) -> dict[str, Any]:
        return {
            "state_id": self.spec.state_id,
            "required": self.spec.required,
            "sha256": self.sha256,
            "size": self.size,
            "record_count": self.record_count,
        }


@dataclass(frozen=True)
class TargetSnapshot:
    spec: StateSpec
    present: bool
    sha256: str | None = None
    size: int = 0
    signature: tuple[int, int, int, int, int, int] | None = None

    def public(self) -> dict[str, Any]:
        return {
            "state_id": self.spec.state_id,
            "present": self.present,
            "sha256": self.sha256,
            "size": self.size,
        }


def _validate_allowlist() -> None:
    ids: set[str] = set()
    targets: set[str] = set()
    for spec in STATE_SPECS:
        if not re.fullmatch(r"[a-z0-9_]+", spec.state_id) or spec.state_id in ids:
            raise RuntimeError("mutable-state allowlist has an invalid state identifier")
        ids.add(spec.state_id)
        for raw in (spec.source_relative, spec.target_relative):
            relative = Path(raw)
            if relative.is_absolute() or not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
                raise RuntimeError("mutable-state allowlist has an unsafe relative path")
        if spec.target_relative in targets:
            raise RuntimeError("mutable-state allowlist has a duplicate target")
        targets.add(spec.target_relative)


_validate_allowlist()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _path_binding(path: Path) -> str:
    return _sha256(str(path).encode("utf-8"))


def _signature(metadata: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
        metadata.st_nlink,
    )


def _relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _reject_symlink_components(path: Path) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(metadata.st_mode):
            raise MutableStateHandoffError("handoff path contains a symlink component")


def _canonical_root(path: Path, *, must_exist: bool, label: str) -> Path:
    raw = path.expanduser()
    if not raw.is_absolute():
        raise MutableStateHandoffError(f"{label} must be absolute")
    _reject_symlink_components(raw)
    try:
        canonical = raw.resolve(strict=must_exist)
    except OSError as exc:
        raise MutableStateHandoffError(f"{label} is unavailable") from exc
    if must_exist and not canonical.is_dir():
        raise MutableStateHandoffError(f"{label} must be a directory")
    return canonical


def _bound_path(root: Path, relative: str) -> Path:
    result = root.joinpath(*Path(relative).parts)
    if not _relative_to(result, root):
        raise MutableStateHandoffError("allowlisted state escaped its bound root")
    return result


def _read_regular(path: Path, *, state_id: str, target: bool) -> tuple[bytes, tuple[int, int, int, int, int, int]]:
    _reject_symlink_components(path)
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        raise MutableStateHandoffError(f"allowlisted state is unreadable: {state_id}") from exc
    try:
        before = os.fstat(descriptor)
        unsafe_mode = bool(stat.S_IMODE(before.st_mode) & 0o077) if target else False
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.getuid()
            or before.st_nlink != 1
            or unsafe_mode
            or before.st_size > MAX_STATE_BYTES
        ):
            raise MutableStateHandoffError(f"allowlisted state is not a safe regular file: {state_id}")
        remaining = before.st_size
        chunks: list[bytes] = []
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        after = os.fstat(descriptor)
        current = path.lstat()
        if (
            _signature(before) != _signature(after)
            or (after.st_dev, after.st_ino) != (current.st_dev, current.st_ino)
            or stat.S_ISLNK(current.st_mode)
            or len(data) != before.st_size
        ):
            raise MutableStateHandoffError(f"allowlisted state changed while reading: {state_id}")
        return data, _signature(after)
    finally:
        os.close(descriptor)


def _record_count(data: bytes, spec: StateSpec) -> int:
    try:
        if spec.encoding == "json":
            value = json.loads(data.decode("utf-8"))
            if isinstance(value, (dict, list)):
                return len(value)
            return int(value is not None)
        if spec.encoding == "jsonl":
            count = 0
            for line in data.splitlines():
                if line.strip():
                    json.loads(line.decode("utf-8"))
                    count += 1
            return count
        rows = list(csv.reader(io.StringIO(data.decode("utf-8-sig"), newline="")))
        if not rows or len(rows[0]) < 2:
            raise csv.Error("address CSV requires a two-column header")
        if any(len(row) < 2 for row in rows[1:] if any(cell.strip() for cell in row)):
            raise csv.Error("address CSV contains an incomplete row")
        return sum(1 for row in rows[1:] if any(cell.strip() for cell in row))
    except (UnicodeDecodeError, json.JSONDecodeError, csv.Error) as exc:
        raise MutableStateHandoffError(f"allowlisted state has invalid {spec.encoding}: {spec.state_id}") from exc


def _read_sources(source_root: Path) -> tuple[dict[str, FileSnapshot], list[str]]:
    snapshots: dict[str, FileSnapshot] = {}
    missing_optional: list[str] = []
    missing_required: list[str] = []
    for spec in STATE_SPECS:
        path = _bound_path(source_root, spec.source_relative)
        _reject_symlink_components(path)
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            (missing_required if spec.required else missing_optional).append(spec.state_id)
            continue
        if stat.S_ISLNK(metadata.st_mode):
            raise MutableStateHandoffError(f"allowlisted source state is symlinked: {spec.state_id}")
        if not stat.S_ISREG(metadata.st_mode):
            raise MutableStateHandoffError(f"allowlisted source state is not regular: {spec.state_id}")
        data, signature = _read_regular(path, state_id=spec.state_id, target=False)
        snapshots[spec.state_id] = FileSnapshot(
            spec=spec,
            data=data,
            sha256=_sha256(data),
            size=len(data),
            record_count=_record_count(data, spec),
            signature=signature,
        )
    if missing_required:
        raise MutableStateHandoffError(
            "required mutable state is missing: " + ",".join(sorted(missing_required))
        )
    return snapshots, sorted(missing_optional)


def _read_targets(target_root: Path) -> dict[str, TargetSnapshot]:
    rows: dict[str, TargetSnapshot] = {}
    for spec in STATE_SPECS:
        path = _bound_path(target_root, spec.target_relative)
        _reject_symlink_components(path)
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            rows[spec.state_id] = TargetSnapshot(spec=spec, present=False)
            continue
        if stat.S_ISLNK(metadata.st_mode):
            raise MutableStateHandoffError(f"allowlisted target state is symlinked: {spec.state_id}")
        if not stat.S_ISREG(metadata.st_mode):
            raise MutableStateHandoffError(f"allowlisted target state is not regular: {spec.state_id}")
        data, signature = _read_regular(path, state_id=spec.state_id, target=True)
        rows[spec.state_id] = TargetSnapshot(
            spec=spec,
            present=True,
            sha256=_sha256(data),
            size=len(data),
            signature=signature,
        )
    return rows


def _snapshot_digest(rows: Iterable[Mapping[str, Any]]) -> str:
    encoded = json.dumps(list(rows), sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return _sha256(encoded)


def _allowlist_digest() -> str:
    return _snapshot_digest(
        {
            "state_id": spec.state_id,
            "source_relative": spec.source_relative,
            "target_relative": spec.target_relative,
            "required": spec.required,
            "encoding": spec.encoding,
        }
        for spec in STATE_SPECS
    )


def _target_digest(targets: Mapping[str, TargetSnapshot]) -> str:
    return _snapshot_digest(targets[spec.state_id].public() for spec in STATE_SPECS)


def _source_digest(sources: Mapping[str, FileSnapshot], missing_optional: Iterable[str]) -> str:
    rows: list[dict[str, Any]] = []
    missing = set(missing_optional)
    for spec in STATE_SPECS:
        if spec.state_id in sources:
            rows.append(sources[spec.state_id].public_source())
        elif spec.state_id in missing:
            rows.append({"state_id": spec.state_id, "required": False, "missing": True})
    return _snapshot_digest(rows)


def _predicted_targets(
    sources: Mapping[str, FileSnapshot], targets: Mapping[str, TargetSnapshot]
) -> dict[str, TargetSnapshot]:
    predicted: dict[str, TargetSnapshot] = {}
    for spec in STATE_SPECS:
        source = sources.get(spec.state_id)
        if source is None:
            predicted[spec.state_id] = targets[spec.state_id]
        else:
            predicted[spec.state_id] = TargetSnapshot(
                spec=spec, present=True, sha256=source.sha256, size=source.size
            )
    return predicted


def _ensure_private_directory(path: Path) -> None:
    _reject_symlink_components(path)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    _reject_symlink_components(path)
    metadata = path.stat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) & 0o022
    ):
        raise MutableStateHandoffError("handoff destination directory is unsafe")


def _write_private_new(path: Path, data: bytes) -> None:
    _ensure_private_directory(path.parent)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        offset = 0
        while offset < len(data):
            offset += os.write(descriptor, data[offset:])
        os.fchmod(descriptor, 0o600)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _stage_sources(staging_root: Path, sources: Mapping[str, FileSnapshot]) -> dict[str, Path]:
    if staging_root.exists() or staging_root.is_symlink():
        raise MutableStateHandoffError("staging root must be fresh")
    _ensure_private_directory(staging_root)
    staged: dict[str, Path] = {}
    for spec in STATE_SPECS:
        source = sources.get(spec.state_id)
        if source is None:
            continue
        path = _bound_path(staging_root, spec.target_relative)
        _write_private_new(path, source.data)
        check, _ = _read_regular(path, state_id=spec.state_id, target=True)
        if _sha256(check) != source.sha256:
            raise MutableStateHandoffError(f"staged state verification failed: {spec.state_id}")
        staged[spec.state_id] = path
    return staged


def _verify_target_precondition(path: Path, expected: TargetSnapshot) -> None:
    if not expected.present:
        if path.exists() or path.is_symlink():
            raise MutableStateHandoffError("target state changed before publish")
        return
    data, signature = _read_regular(path, state_id=expected.spec.state_id, target=True)
    if signature != expected.signature or len(data) != expected.size or _sha256(data) != expected.sha256:
        raise MutableStateHandoffError("target state changed before publish")


def _publish(
    *,
    target_root: Path,
    staged: Mapping[str, Path],
    sources: Mapping[str, FileSnapshot],
    targets: Mapping[str, TargetSnapshot],
    refresh: bool,
) -> dict[str, str]:
    # Reverify the entire old snapshot before the first mutation.
    for spec in STATE_SPECS:
        _verify_target_precondition(_bound_path(target_root, spec.target_relative), targets[spec.state_id])

    statuses: dict[str, str] = {}
    for spec in STATE_SPECS:
        source = sources.get(spec.state_id)
        if source is None:
            statuses[spec.state_id] = "missing_optional"
            continue
        target_path = _bound_path(target_root, spec.target_relative)
        before = targets[spec.state_id]
        if before.present and before.sha256 == source.sha256 and before.size == source.size:
            statuses[spec.state_id] = "unchanged"
            continue
        _ensure_private_directory(target_path.parent)
        _verify_target_precondition(target_path, before)
        staged_path = staged[spec.state_id]
        if before.present:
            if not refresh:
                raise MutableStateHandoffError("refusing to overwrite existing mutable state")
            os.replace(staged_path, target_path)
            statuses[spec.state_id] = "refreshed"
        else:
            try:
                os.link(staged_path, target_path, follow_symlinks=False)
            except FileExistsError as exc:
                raise MutableStateHandoffError("target state appeared during publish") from exc
            staged_path.unlink()
            statuses[spec.state_id] = "copied"
        check, _ = _read_regular(target_path, state_id=spec.state_id, target=True)
        if len(check) != source.size or _sha256(check) != source.sha256:
            raise MutableStateHandoffError(f"published state verification failed: {spec.state_id}")
        directory = os.open(target_path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    return statuses


def _receipt_payload(
    *,
    action: str,
    context: ExactContext,
    source_root: Path,
    target_root: Path,
    sources: Mapping[str, FileSnapshot],
    missing_optional: list[str],
    targets_before: Mapping[str, TargetSnapshot],
    targets_after: Mapping[str, TargetSnapshot],
    statuses: Mapping[str, str],
    refresh: bool,
    ready: bool = True,
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for spec in STATE_SPECS:
        source = sources.get(spec.state_id)
        before = targets_before[spec.state_id]
        after = targets_after[spec.state_id]
        rows.append(
            {
                "state_id": spec.state_id,
                "required": spec.required,
                "status": statuses[spec.state_id],
                "source_sha256": source.sha256 if source else None,
                "source_size": source.size if source else 0,
                "target_before_sha256": before.sha256,
                "target_sha256": after.sha256,
                "target_size": after.size,
            }
        )
    return {
        "schema": SCHEMA,
        "schema_version": 1,
        "status": "prepared" if action == "prepare" else "dry_run",
        "ready": ready,
        "refresh": refresh,
        "contains_business_payload": False,
        "contains_source_or_target_paths": False,
        "exact_context": context.public(),
        "source_root_sha256": _path_binding(source_root),
        "target_shared_root_sha256": _path_binding(target_root),
        "allowlist_sha256": _allowlist_digest(),
        "source_snapshot_sha256": _source_digest(sources, missing_optional),
        "target_before_snapshot_sha256": _target_digest(targets_before),
        "target_snapshot_sha256": _target_digest(targets_after),
        "state_count": len(rows),
        "present_source_count": len(sources),
        "required_count": sum(spec.required for spec in STATE_SPECS),
        "degraded": bool(missing_optional),
        "degraded_state_ids": missing_optional,
        "states": rows,
    }


def _write_receipt(path: Path, payload: Mapping[str, Any]) -> str:
    encoded = (json.dumps(dict(payload), ensure_ascii=True, sort_keys=True, indent=2) + "\n").encode()
    _ensure_private_directory(path.parent)
    if path.exists() or path.is_symlink():
        if path.is_symlink():
            raise MutableStateHandoffError("receipt path is symlinked")
        current, _ = _read_regular(path, state_id="receipt", target=True)
        if current != encoded:
            raise MutableStateHandoffError("refusing to overwrite a different handoff receipt")
        return _sha256(encoded)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}-{uuid.uuid4().hex}")
    try:
        _write_private_new(temporary, encoded)
        try:
            os.link(temporary, path, follow_symlinks=False)
        except FileExistsError as exc:
            raise MutableStateHandoffError("receipt appeared during atomic publish") from exc
        temporary.unlink()
        return _sha256(encoded)
    finally:
        temporary.unlink(missing_ok=True)


def execute_handoff(
    *,
    action: Literal["dry-run", "prepare"],
    source_root: Path,
    target_shared_root: Path,
    receipt_path: Path,
    context: ExactContext,
    staging_root: Path | None = None,
    refresh: bool = False,
    expected_target_snapshot_sha256: str | None = None,
) -> tuple[dict[str, Any], str]:
    """Validate or prepare the exact allowlisted handoff and publish a receipt."""

    if action not in {"dry-run", "prepare"}:
        raise MutableStateHandoffError("unsupported handoff action")
    context.validate()
    source = _canonical_root(source_root, must_exist=True, label="source root")
    target = _canonical_root(target_shared_root, must_exist=False, label="target shared root")
    receipt = _canonical_root(receipt_path, must_exist=False, label="receipt path")
    if receipt.exists() and receipt.is_dir():
        raise MutableStateHandoffError("receipt path must be a file")
    if source == target or _relative_to(target, source) or _relative_to(source, target):
        raise MutableStateHandoffError("source and target roots must be disjoint")
    if _relative_to(receipt, source) or _relative_to(receipt, target):
        raise MutableStateHandoffError("receipt must be outside mutable-state roots")

    sources, missing_optional = _read_sources(source)
    targets_before = _read_targets(target)
    before_digest = _target_digest(targets_before)
    conflicts = [
        state_id
        for state_id, source_row in sources.items()
        if targets_before[state_id].present
        and (
            targets_before[state_id].sha256 != source_row.sha256
            or targets_before[state_id].size != source_row.size
        )
    ]
    if refresh:
        if not expected_target_snapshot_sha256 or not SHA256_RE.fullmatch(expected_target_snapshot_sha256):
            raise MutableStateHandoffError("refresh requires an exact old target snapshot digest")
        if before_digest != expected_target_snapshot_sha256:
            raise MutableStateHandoffError("old target snapshot precondition failed")
    elif expected_target_snapshot_sha256 is not None:
        raise MutableStateHandoffError("old target snapshot digest is valid only in refresh mode")
    elif conflicts and action == "prepare":
        raise MutableStateHandoffError("existing target state differs; explicit refresh is required")

    predicted = _predicted_targets(sources, targets_before)
    dry_statuses = {
        spec.state_id: (
            "missing_optional"
            if spec.state_id not in sources
            else "unchanged"
            if targets_before[spec.state_id].present
            and targets_before[spec.state_id].sha256 == sources[spec.state_id].sha256
            and targets_before[spec.state_id].size == sources[spec.state_id].size
            else "would_refresh"
            if refresh
            else "conflict"
            if targets_before[spec.state_id].present
            else "would_copy"
        )
        for spec in STATE_SPECS
    }
    if action == "dry-run":
        payload = _receipt_payload(
            action=action,
            context=context,
            source_root=source,
            target_root=target,
            sources=sources,
            missing_optional=missing_optional,
            targets_before=targets_before,
            targets_after=predicted,
            statuses=dry_statuses,
            refresh=refresh,
            ready=not conflicts or refresh,
        )
        return payload, _write_receipt(receipt, payload)

    if staging_root is None:
        raise MutableStateHandoffError("prepare requires a fresh staging root")
    staging = _canonical_root(staging_root, must_exist=False, label="staging root")
    if any(
        left == right or _relative_to(left, right) or _relative_to(right, left)
        for left, right in ((staging, source), (staging, target), (staging, receipt.parent))
    ):
        raise MutableStateHandoffError("staging root must be disjoint from source, target, and receipt")
    try:
        staged = _stage_sources(staging, sources)
        statuses = _publish(
            target_root=target,
            staged=staged,
            sources=sources,
            targets=targets_before,
            refresh=refresh,
        )
    finally:
        if staging.exists() and not staging.is_symlink():
            shutil.rmtree(staging)
    targets_after = _read_targets(target)
    if _target_digest(targets_after) != _target_digest(predicted):
        raise MutableStateHandoffError("published target snapshot verification failed")
    payload = _receipt_payload(
        action=action,
        context=context,
        source_root=source,
        target_root=target,
        sources=sources,
        missing_optional=missing_optional,
        targets_before=targets_before,
        targets_after=targets_after,
        statuses=statuses,
        refresh=refresh,
    )
    return payload, _write_receipt(receipt, payload)


__all__ = [
    "ExactContext",
    "MutableStateHandoffError",
    "SCHEMA",
    "STATE_SPECS",
    "execute_handoff",
]
