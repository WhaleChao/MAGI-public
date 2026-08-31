#!/usr/bin/env python3
"""Read-only backup, restore, release, ownership, and cutover preflight."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import plistlib
import re
import shutil
import sqlite3
import stat
import sys
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

ROOT = Path(__file__).resolve().parents[1]
if __package__ in {None, ""}:
    sys.path.insert(0, str(ROOT))

from scripts.v3_cutover.core import (  # noqa: E402
    Snapshot,
    assess_absolute_window,
    assess_cutover_window,
    assess_snapshot,
    load_gate_config,
)
from scripts.v3_cutover.mutation import ATOMIC_DRILL_EXCLUDED_EVIDENCE  # noqa: E402
from scripts.v3_cutover.probe import (  # noqa: E402
    DEFAULT_PORTS,
    ReleaseSpec,
    collect_snapshot,
    discover_release_spec,
)
from scripts.v3_pdf_namer_handoff import HandoffError, verify_manifest  # noqa: E402
from scripts.v3_static_external_staging import (  # noqa: E402
    RECEIPT_NAME as STATIC_EXTERNAL_RECEIPT_NAME,
    RELEASE_BINDING_RECEIPT_NAME as STATIC_EXTERNAL_RELEASE_RECEIPT_NAME,
    StaticExternalStagingError,
    verify_static_external,
    verify_static_external_release_binding,
)
from magi_v3.mutable_state_handoff import (  # noqa: E402
    ExactContext,
    MutableStateHandoffError,
    execute_handoff as replay_mutable_state_handoff,
)
from magi_v3.external_inputs import (  # noqa: E402
    NAMED_MUTABLE_STATE_BINDINGS,
    named_mutable_state_paths,
)

SHA256 = frozenset("0123456789abcdef")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
REQUIRED_BACKUP_COVERAGE = frozenset({"sqlite", "website_assets", "website_data"})
EXPECTED_V3_LABELS = frozenset(
    {
        "com.magi.v3.control",
        "com.magi.v3.gateway",
        "com.magi.v3.supervisor",
    }
)
EXTERNAL_PLIST_BINDINGS = {
    "MAGI_ENV_FILE": "env_file",
    "MAGI_ENV_FILE_SHA256": "env_file_sha256",
    "MAGI_CRON_JOBS_FILE": "cron_jobs_file",
    "MAGI_CRON_JOBS_SHA256": "cron_jobs_sha256",
    "MAGI_CRON_JOBS_SOURCE_SHA256": "cron_jobs_source_sha256",
    "MAGI_WEBSITE_ROOT": "website_root",
    "MAGI_WEBSITE_ADMIN_SHA256": "website_admin_sha256",
    "MAGI_LAF_CONFIG_FILE": "laf_config_file",
    "MAGI_LAF_CONFIG_SHA256": "laf_config_sha256",
    "MAGI_CONFIG_PATH": "laf_config_file",
    "MAGI_CONFIG_SHA256": "laf_config_sha256",
    "MAGI_CONFIG_MODE": "laf_config_mode",
    "OSC_CONFIG_PATH": "laf_config_file",
    "MAGI_GOOGLE_CREDENTIALS_PATH": "google_credentials_file",
    "MAGI_GOOGLE_CREDENTIALS_SHA256": "google_credentials_sha256",
    "MAGI_GOOGLE_CREDENTIALS_MODE": "google_credentials_mode",
    "MAGI_GMAIL_CREDENTIALS_PATH": "google_credentials_file",
    "MAGI_GOOGLE_CALENDAR_TOKEN_PATH": "google_calendar_token_file",
    "MAGI_LAF_GMAIL_TOKEN_PATH": "laf_gmail_token_file",
    "MAGI_FILE_REVIEW_TOKEN_PATH": "file_review_token_file",
    "MAGI_GMAIL_COMPOSE_TOKEN_PATH": "gmail_compose_token_file",
    "MAGI_ACCOUNTING_GOOGLE_CREDENTIALS_PATH": "accounting_credentials_file",
    "MAGI_ACCOUNTING_GOOGLE_CREDENTIALS_SHA256": "accounting_credentials_sha256",
    "MAGI_ACCOUNTING_GOOGLE_CREDENTIALS_MODE": "accounting_credentials_mode",
    "MAGI_ACCOUNTING_GOOGLE_SHEETS_TOKEN": "accounting_sheets_token_file",
    "MAGI_DRIVE_SYNC_CREDENTIALS_PATH": "google_credentials_file",
    "MAGI_DRIVE_SYNC_TOKEN": "drive_sync_token_file",
    "MAGI_DRIVE_SYNC_WRITE_TOKEN": "drive_sync_write_token_file",
    "MAGI_NAS_OCR_QUEUE_DB_PATH": "nas_ocr_queue_db_file",
    "MAGI_V3_PYTHON_RUNTIME_MANIFEST": "python_runtime_manifest",
    "MAGI_V3_PYTHON_RUNTIME_MANIFEST_SHA256": "python_runtime_manifest_sha256",
    "MAGI_V3_STATIC_EXTERNAL_RECEIPT": "static_external_receipt",
    "MAGI_V3_STATIC_EXTERNAL_RECEIPT_SHA256": "static_external_receipt_sha256",
    "MAGI_V3_STATIC_EXTERNAL_TARGET_SNAPSHOT_SHA256": (
        "static_external_target_snapshot_sha256"
    ),
    **{
        env_name: binding_name
        for env_name, (binding_name, _relative) in NAMED_MUTABLE_STATE_BINDINGS.items()
    },
}


class PreCutoverError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ExpectedContext:
    campaign_id: str
    release_sha: str
    hardware_id: str
    gate_config_sha256: str

    def to_dict(self) -> dict[str, str]:
        return {
            "campaign_id": self.campaign_id,
            "release_sha": self.release_sha,
            "hardware_id": self.hardware_id,
            "gate_config_sha256": self.gate_config_sha256,
        }


@dataclass(frozen=True, slots=True)
class RequiredPaths:
    databases: tuple[Path, ...]
    state: tuple[Path, ...]
    nas: tuple[Path, ...]


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("top-level JSON must be an object")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    """Write a report durably without exposing a partially written document."""

    target = path.expanduser()
    if not target.is_absolute():
        target = Path.cwd() / target
    if ".." in target.parts:
        raise PreCutoverError("report output must not contain parent traversal")
    target.parent.mkdir(parents=True, exist_ok=True)
    cursor = Path(target.anchor)
    for part in target.parts[1:]:
        cursor /= part
        if cursor.is_symlink():
            raise PreCutoverError("report output path must not contain a symlink")
    # An older GO must never survive a failed replacement attempt.  Remove and
    # durably invalidate it before allocating or flushing the new temp file.
    if target.exists():
        if not target.is_file():
            raise PreCutoverError("report output target must be a regular file")
        target.unlink()
        directory_fd = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    temporary = target.with_name(f".{target.name}.tmp-{os.getpid()}-{uuid.uuid4().hex}")
    data = (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    replaced = False
    try:
        with temporary.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        replaced = True
        directory_fd = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except Exception:
        if replaced:
            target.unlink(missing_ok=True)
            directory_fd = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        raise
    finally:
        temporary.unlink(missing_ok=True)


def _valid_digest(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and set(value) <= SHA256


def _parse_time(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("timestamp missing")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("timestamp must include timezone")
    return result.astimezone(timezone.utc)


def _safe_relative(root: Path, value: Any) -> Path:
    if not isinstance(value, str) or not value:
        raise ValueError("relative artifact path missing")
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("artifact path escapes evidence root")
    candidate = root
    for part in relative.parts:
        candidate /= part
        if candidate.is_symlink():
            raise ValueError(f"artifact missing or symlinked: {relative}")
    path = candidate.resolve()
    path.relative_to(root.resolve())
    if not path.is_file():
        raise ValueError(f"artifact missing or symlinked: {relative}")
    return path


def _context_matches(document: dict[str, Any], context: ExpectedContext) -> bool:
    return all(document.get(key) == value for key, value in context.to_dict().items())


class PreCutoverPreflight:
    def __init__(
        self,
        *,
        context: ExpectedContext,
        gate_config_path: Path,
        campaign_config_path: Path,
        campaign_report_path: Path,
        release_gate_report_path: Path,
        backup_metadata_path: Path,
        readiness_manifest_path: Path,
        deploy_prepared_marker_path: Path,
        pdf_namer_handoff_manifest_path: Path,
        pdf_namer_source_path: Path,
        pdf_namer_destination_path: Path,
        release_dir: Path,
        required_paths: RequiredPaths,
        v2_root: Path,
        v3_root: Path,
        v2_namespace: str = "magi-v2-production",
        v3_namespace: str = "magi-v3-production",
        backup_max_age_hours: float = 24.0,
        report_max_age_hours: float = 24.0,
        clock: Callable[[], datetime] | None = None,
        mount_checker: Callable[[Path], bool] = os.path.ismount,
        disk_usage: Callable[[Path], Any] = shutil.disk_usage,
        snapshot_collector: Callable[[], Snapshot] | None = None,
        execution_purpose: str = "final_cutover",
        cutover_plan_path: Path | None = None,
        cutover_plan_sha256: str | None = None,
        mutable_state_source_root: Path | None = None,
        mutable_state_target_shared_root: Path | None = None,
        mutable_state_dry_run_receipt_path: Path | None = None,
        report_output_path: Path | None = None,
    ) -> None:
        self.context = context
        self.gate_config_path = gate_config_path.expanduser().resolve()
        self.campaign_config_path = campaign_config_path.expanduser().resolve()
        self.campaign_report_path = campaign_report_path.expanduser().resolve()
        self.release_gate_report_path = release_gate_report_path.expanduser().resolve()
        self.backup_metadata_path = backup_metadata_path.expanduser().resolve()
        self.readiness_manifest_path = readiness_manifest_path.expanduser().resolve()
        self.deploy_prepared_marker_path = deploy_prepared_marker_path.expanduser().resolve()
        self.pdf_namer_handoff_manifest_path = pdf_namer_handoff_manifest_path.expanduser()
        self.pdf_namer_source_path = pdf_namer_source_path.expanduser()
        self.pdf_namer_destination_path = pdf_namer_destination_path.expanduser()
        self.release_dir = release_dir.expanduser().resolve()
        self.required_paths = required_paths
        self.v2_root = v2_root.expanduser().resolve()
        self.v3_root = v3_root.expanduser().resolve()
        self.v2_namespace = v2_namespace
        self.v3_namespace = v3_namespace
        self.backup_max_age_hours = backup_max_age_hours
        self.report_max_age_hours = report_max_age_hours
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.mount_checker = mount_checker
        self.disk_usage = disk_usage
        self.snapshot_collector = snapshot_collector
        if execution_purpose not in {"atomic_drill", "final_cutover"}:
            raise PreCutoverError("execution purpose must be atomic_drill or final_cutover")
        self.execution_purpose = execution_purpose
        self.cutover_plan_path = cutover_plan_path.expanduser() if cutover_plan_path else None
        self.cutover_plan_sha256 = cutover_plan_sha256
        self.mutable_state_source_root = (
            mutable_state_source_root.expanduser() if mutable_state_source_root else None
        )
        self.mutable_state_target_shared_root = (
            mutable_state_target_shared_root.expanduser()
            if mutable_state_target_shared_root
            else None
        )
        self.mutable_state_dry_run_receipt_path = (
            mutable_state_dry_run_receipt_path.expanduser()
            if mutable_state_dry_run_receipt_path
            else None
        )
        self.report_output_path = report_output_path.expanduser() if report_output_path else None
        self.cutover_plan_binding: dict[str, str] | None = None
        self.checks: list[dict[str, Any]] = []

    @staticmethod
    def _planned_path(
        value: Any,
        actual: Path,
        *,
        label: str,
        require_directory: bool,
    ) -> Path:
        if not isinstance(value, dict) or set(value) != {"path", "path_sha256"}:
            raise PreCutoverError(f"{label} plan binding is invalid")
        raw = actual.expanduser()
        if not raw.is_absolute() or raw.is_symlink():
            raise PreCutoverError(f"{label} must be an absolute non-symlink path")
        canonical = raw.resolve(strict=require_directory)
        if canonical != raw:
            raise PreCutoverError(f"{label} must be canonical and symlink-free")
        if require_directory and not canonical.is_dir():
            raise PreCutoverError(f"{label} must be a directory")
        if value.get("path") != str(canonical):
            raise PreCutoverError(f"{label} path differs from the cutover plan")
        if value.get("path_sha256") != hashlib.sha256(str(canonical).encode()).hexdigest():
            raise PreCutoverError(f"{label} path hash mismatch")
        return canonical

    def _check_mutable_state_handoff(self) -> None:
        values = (
            self.cutover_plan_path,
            self.cutover_plan_sha256,
            self.mutable_state_source_root,
            self.mutable_state_target_shared_root,
            self.mutable_state_dry_run_receipt_path,
        )
        if not any(value is not None for value in values):
            if self.execution_purpose == "atomic_drill":
                self._check(
                    "mutable_state_handoff",
                    True,
                    {
                        "status": "excluded_for_atomic_drill",
                        "final_cutover_exemption": False,
                        "contains_business_payload": False,
                    },
                )
            else:
                self._check(
                    "mutable_state_handoff",
                    False,
                    "final cutover requires an exact plan-bound mutable-state dry-run receipt",
                )
            return
        if not all(value is not None for value in values):
            self._check(
                "mutable_state_handoff",
                False,
                "mutable-state preflight requires all plan, hash, root, and receipt bindings",
            )
            return

        assert self.cutover_plan_path is not None
        assert self.cutover_plan_sha256 is not None
        assert self.mutable_state_source_root is not None
        assert self.mutable_state_target_shared_root is not None
        assert self.mutable_state_dry_run_receipt_path is not None
        try:
            plan_raw = self.cutover_plan_path
            if (
                not plan_raw.is_absolute()
                or plan_raw.is_symlink()
                or not plan_raw.is_file()
            ):
                raise PreCutoverError("cutover plan must be an absolute non-symlink file")
            plan = plan_raw.resolve(strict=True)
            if plan != plan_raw:
                raise PreCutoverError("cutover plan must be canonical and symlink-free")
            if not _valid_digest(self.cutover_plan_sha256):
                raise PreCutoverError("cutover plan SHA-256 is invalid")
            if _sha256(plan) != self.cutover_plan_sha256:
                raise PreCutoverError("cutover plan SHA-256 mismatch")
            document = _load_json(plan)
            if not (
                document.get("schema_version") == 1
                and document.get("operation") == "v2_to_v3_cutover"
                and document.get("execution_purpose") == self.execution_purpose
            ):
                raise PreCutoverError("cutover plan purpose is invalid")
            handoff = document.get("mutable_state_handoff")
            if not isinstance(handoff, dict) or set(handoff) != {
                "source_root",
                "target_shared_root",
                "dry_run_receipt",
                "prepare_receipt",
                "staging_root",
                "exact_context",
            }:
                raise PreCutoverError("cutover plan mutable-state binding is invalid")
            source = self._planned_path(
                handoff.get("source_root"),
                self.mutable_state_source_root,
                label="mutable-state source root",
                require_directory=True,
            )
            target = self._planned_path(
                handoff.get("target_shared_root"),
                self.mutable_state_target_shared_root,
                label="mutable-state target shared root",
                require_directory=False,
            )
            deploy_marker = _load_json(self.deploy_prepared_marker_path)
            deploy_manifest = _safe_relative(
                self.deploy_prepared_marker_path.parent,
                deploy_marker.get("manifest"),
            )
            deployment = _load_json(deploy_manifest)
            runtime_raw = deployment.get("runtime_root")
            if not isinstance(runtime_raw, str) or not Path(runtime_raw).is_absolute():
                raise PreCutoverError("prepared deployment runtime root is invalid")
            expected_target = Path(runtime_raw).resolve(strict=False) / "shared"
            if target != expected_target:
                raise PreCutoverError(
                    "mutable-state receipt target differs from deployment runtime/shared"
                )
            receipt = self._planned_path(
                handoff.get("dry_run_receipt"),
                self.mutable_state_dry_run_receipt_path,
                label="mutable-state dry-run receipt",
                require_directory=False,
            )
            if receipt.is_symlink() or not receipt.is_file():
                raise PreCutoverError("mutable-state dry-run receipt is unavailable")
            receipt_meta = receipt.stat()
            if (
                receipt_meta.st_uid != os.getuid()
                or receipt_meta.st_nlink != 1
                or stat.S_IMODE(receipt_meta.st_mode) != 0o600
            ):
                raise PreCutoverError(
                    "mutable-state dry-run receipt must be owner-only 0600 with one hard link"
                )
            final_report = document.get("pre_cutover_report")
            if self.execution_purpose == "final_cutover":
                if self.report_output_path is None:
                    raise PreCutoverError(
                        "final pre-cutover output path is required for mutual plan binding"
                    )
                output = self.report_output_path
                if not output.is_absolute() or output.is_symlink():
                    raise PreCutoverError(
                        "final pre-cutover output must be an absolute non-symlink path"
                    )
                output = output.resolve(strict=False)
                if output != self.report_output_path:
                    raise PreCutoverError(
                        "final pre-cutover output must be canonical and symlink-free"
                    )
                if not isinstance(final_report, dict) or final_report != {
                    "path": str(output),
                    "path_sha256": hashlib.sha256(str(output).encode()).hexdigest(),
                }:
                    raise PreCutoverError(
                        "cutover plan is not bound to this final pre-cutover output path"
                    )

            context_raw = handoff.get("exact_context")
            if not isinstance(context_raw, dict) or set(context_raw) != {
                "release_id",
                "release_manifest_sha256",
                "deployment_manifest_sha256",
            }:
                raise PreCutoverError("mutable-state exact context is invalid")
            context = ExactContext(
                release_id=str(context_raw.get("release_id", "")),
                release_manifest_sha256=str(
                    context_raw.get("release_manifest_sha256", "")
                ),
                deployment_manifest_sha256=str(
                    context_raw.get("deployment_manifest_sha256", "")
                ),
                cutover_plan_sha256=self.cutover_plan_sha256,
            )
            context.validate()
            release_marker = _load_json(self.release_dir / "RELEASE_COMPLETE.json")
            deploy_marker = _load_json(self.deploy_prepared_marker_path)
            if not (
                context.release_id == release_marker.get("release_id")
                and context.release_manifest_sha256
                == release_marker.get("manifest_sha256")
                and context.deployment_manifest_sha256
                == deploy_marker.get("manifest_sha256")
            ):
                raise PreCutoverError(
                    "mutable-state plan context differs from the prepared release/deployment"
                )
            receipt_document = _load_json(receipt)
            receipt_keys = {
                "schema",
                "schema_version",
                "status",
                "ready",
                "refresh",
                "contains_business_payload",
                "contains_source_or_target_paths",
                "exact_context",
                "source_root_sha256",
                "target_shared_root_sha256",
                "allowlist_sha256",
                "source_snapshot_sha256",
                "target_before_snapshot_sha256",
                "target_snapshot_sha256",
                "state_count",
                "present_source_count",
                "required_count",
                "degraded",
                "degraded_state_ids",
                "states",
            }
            states = receipt_document.get("states")
            state_keys = {
                "state_id",
                "required",
                "status",
                "source_sha256",
                "source_size",
                "target_before_sha256",
                "target_sha256",
                "target_size",
            }
            if not (
                set(receipt_document) == receipt_keys
                and receipt_document.get("schema") == "magi.v3.mutable-state-handoff/v1"
                and receipt_document.get("schema_version") == 1
                and receipt_document.get("status") == "dry_run"
                and type(receipt_document.get("ready")) is bool
                and receipt_document.get("contains_business_payload") is False
                and receipt_document.get("contains_source_or_target_paths") is False
                and receipt_document.get("exact_context") == context.public()
                and receipt_document.get("source_root_sha256")
                == hashlib.sha256(str(source).encode()).hexdigest()
                and receipt_document.get("target_shared_root_sha256")
                == hashlib.sha256(str(target).encode()).hexdigest()
                and all(
                    _valid_digest(receipt_document.get(key))
                    for key in (
                        "allowlist_sha256",
                        "source_snapshot_sha256",
                        "target_before_snapshot_sha256",
                        "target_snapshot_sha256",
                    )
                )
                and isinstance(states, list)
                and receipt_document.get("state_count") == len(states)
                and all(
                    isinstance(row, dict)
                    and set(row) == state_keys
                    and isinstance(row.get("state_id"), str)
                    for row in states
                )
                and len({row["state_id"] for row in states}) == len(states)
            ):
                raise PreCutoverError(
                    "mutable-state dry-run receipt is incomplete or contains unapproved fields"
                )
            if (
                self.execution_purpose == "final_cutover"
                and receipt_document.get("ready") is not True
            ):
                raise PreCutoverError("mutable-state dry-run is not ready for final cutover")

            before_signature = (
                receipt_meta.st_dev,
                receipt_meta.st_ino,
                receipt_meta.st_size,
                receipt_meta.st_mtime_ns,
                receipt_meta.st_ctime_ns,
            )
            replay, replay_sha256 = replay_mutable_state_handoff(
                action="dry-run",
                source_root=source,
                target_shared_root=target,
                receipt_path=receipt,
                context=context,
            )
            after = receipt.stat()
            after_signature = (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            )
            if (
                replay != receipt_document
                or replay_sha256 != _sha256(receipt)
                or before_signature != after_signature
            ):
                raise PreCutoverError(
                    "mutable-state receipt replay or source/target snapshot binding mismatch"
                )
            self.cutover_plan_binding = {
                "path": str(plan),
                "sha256": self.cutover_plan_sha256,
            }
            self._check(
                "mutable_state_handoff",
                True,
                {
                    "status": "verified",
                    "receipt_path": str(receipt),
                    "receipt_sha256": replay_sha256,
                    "allowlist_sha256": replay["allowlist_sha256"],
                    "source_snapshot_sha256": replay["source_snapshot_sha256"],
                    "target_before_snapshot_sha256": replay[
                        "target_before_snapshot_sha256"
                    ],
                    "target_snapshot_sha256": replay["target_snapshot_sha256"],
                    "ready": replay["ready"],
                    "contains_business_payload": False,
                },
            )
        except (MutableStateHandoffError, OSError, ValueError, PreCutoverError) as exc:
            self._check("mutable_state_handoff", False, str(exc))

    def _check_pdf_namer_handoff(self) -> None:
        try:
            deploy_marker = _load_json(self.deploy_prepared_marker_path)
            deploy_manifest = _safe_relative(
                self.deploy_prepared_marker_path.parent,
                deploy_marker.get("manifest"),
            )
            deployment = _load_json(deploy_manifest)
            runtime_raw = deployment.get("runtime_root")
            if not isinstance(runtime_raw, str) or not Path(runtime_raw).is_absolute():
                raise PreCutoverError("prepared deployment runtime root is invalid")
            expected_destination = (
                Path(runtime_raw).resolve(strict=False) / "shared" / "pdf-namer"
            )
            if self.pdf_namer_destination_path.resolve(strict=False) != expected_destination:
                raise PreCutoverError(
                    "PDF namer handoff destination differs from deployment runtime/shared"
                )
            manifest = verify_manifest(
                self.pdf_namer_handoff_manifest_path,
                source=self.pdf_namer_source_path,
                destination=self.pdf_namer_destination_path,
                allowed_statuses={"precopy_complete", "complete"},
            )
            detail = {
                "status": manifest["status"],
                "snapshot_sha256": manifest["snapshot_sha256"],
                "file_count": manifest["file_count"],
                "record_count": manifest["record_count"],
                "contains_business_payload": False,
                "contains_file_names": False,
            }
            self._check("pdf_namer_handoff_precopy", True, detail)
        except (HandoffError, OSError, ValueError, PreCutoverError) as exc:
            # Handoff errors are intentionally generic and never include state
            # values, case names, parties, or source/destination file names.
            self._check("pdf_namer_handoff_precopy", False, str(exc))

    def _check(self, name: str, ok: bool, detail: Any = "") -> None:
        self.checks.append({"name": name, "ok": bool(ok), "detail": detail})

    def _document(self, name: str, path: Path) -> dict[str, Any] | None:
        try:
            if path.is_symlink():
                raise ValueError("evidence document must not be a symlink")
            return _load_json(path)
        except Exception as exc:
            self._check(name, False, str(exc))
            return None

    @staticmethod
    def _tree_material_bytes(root: Path) -> int:
        canonical = root.resolve(strict=True)
        if canonical != root or root.is_symlink() or not canonical.is_dir():
            raise PreCutoverError("release-bound disk material root is unsafe")
        total = 0
        identities: set[tuple[int, int]] = set()
        for directory, directory_names, file_names in os.walk(
            canonical,
            topdown=True,
            followlinks=False,
        ):
            base = Path(directory)
            for name in tuple(directory_names):
                child = base / name
                if child.is_symlink():
                    raise PreCutoverError("release-bound disk material contains a symlink")
            for name in file_names:
                child = base / name
                metadata = child.lstat()
                if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                    raise PreCutoverError(
                        "release-bound disk material contains an unsafe member"
                    )
                identity = (metadata.st_dev, metadata.st_ino)
                if identity not in identities:
                    identities.add(identity)
                    total += metadata.st_size
        return total

    def _disk_capacity_requirement(self, policy: Any) -> dict[str, Any]:
        expected_fields = {
            "schema_version",
            "absolute_floor_gib",
            "operational_headroom_gib",
            "material_multiplier",
            "material_scope",
        }
        if type(policy) is not dict or set(policy) != expected_fields:
            raise PreCutoverError("release-bound disk capacity policy is invalid")
        values = {
            key: policy.get(key)
            for key in (
                "absolute_floor_gib",
                "operational_headroom_gib",
                "material_multiplier",
            )
        }
        if (
            policy.get("schema_version") != 1
            or policy.get("material_scope")
            != ["candidate_release", "prepared_deployment"]
            or any(
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not math.isfinite(float(value))
                or float(value) <= 0
                for value in values.values()
            )
            or float(values["absolute_floor_gib"]) < 16
            or float(values["operational_headroom_gib"]) < 8
            or float(values["material_multiplier"]) < 4
        ):
            raise PreCutoverError("release-bound disk capacity policy is unsafe")
        material_roots = (
            self.release_dir,
            self.deploy_prepared_marker_path.parent,
        )
        material_bytes = sum(self._tree_material_bytes(root) for root in material_roots)
        gib = 1024**3
        absolute_floor_bytes = math.ceil(float(values["absolute_floor_gib"]) * gib)
        calculated_bytes = math.ceil(
            float(values["operational_headroom_gib"]) * gib
            + float(values["material_multiplier"]) * material_bytes
        )
        required_bytes = max(absolute_floor_bytes, calculated_bytes)
        return {
            "schema_version": 1,
            "policy": "release_bound_capacity",
            "absolute_floor_gib": float(values["absolute_floor_gib"]),
            "operational_headroom_gib": float(values["operational_headroom_gib"]),
            "material_multiplier": float(values["material_multiplier"]),
            "material_scope": list(policy["material_scope"]),
            "material_bytes": material_bytes,
            "calculated_required_bytes": calculated_bytes,
            "required_bytes": required_bytes,
            "required_gib": required_bytes / gib,
        }

    def _check_paths_and_disk(self, capacity: Mapping[str, Any] | None) -> None:
        groups = {
            "database_paths": self.required_paths.databases,
            "state_paths": self.required_paths.state,
            "nas_paths": self.required_paths.nas,
        }
        for name, paths in groups.items():
            if not paths:
                self._check(name, False, "no required paths declared")
                continue
            missing = [str(path) for path in paths if not path.expanduser().exists()]
            self._check(name, not missing, {"count": len(paths), "missing": missing})
        unmounted = [
            str(path)
            for path in self.required_paths.nas
            if path.expanduser().exists() and not self.mount_checker(path.expanduser())
        ]
        self._check("nas_mounts", not unmounted, {"unmounted": unmounted})
        disk_targets = tuple(
            dict.fromkeys(
                path.expanduser().resolve()
                for path in (
                    *self.required_paths.databases,
                    *self.required_paths.state,
                    *self.required_paths.nas,
                    self.release_dir,
                )
                if path.expanduser().exists()
            )
        )
        low: dict[str, float] = {}
        errors: list[str] = []
        if capacity is None:
            self._check(
                "disk_free",
                False,
                {"policy": "invalid", "low": {}, "errors": ["capacity policy invalid"]},
            )
            return
        minimum_free_gb = float(capacity["required_gib"])
        for path in disk_targets:
            try:
                free_gb = self.disk_usage(path).free / 1024**3
                if free_gb < minimum_free_gb:
                    low[str(path)] = round(free_gb, 3)
            except Exception as exc:
                errors.append(f"{path}: {exc}")
        self._check(
            "disk_free",
            bool(disk_targets) and not low and not errors,
            {
                **dict(capacity),
                "minimum_gb": minimum_free_gb,
                "low": low,
                "errors": errors,
            },
        )

    def _check_backup(self, now: datetime) -> None:
        document = self._document("backup_metadata", self.backup_metadata_path)
        if document is None:
            return
        root = self.backup_metadata_path.parent
        errors: list[str] = []
        try:
            if document.get("schema_version") != 2:
                errors.append("backup metadata schema_version must equal 2")
            if not _context_matches(document, self.context):
                errors.append("backup metadata context mismatch")
            artifact = _safe_relative(root, document.get("artifact_path"))
            digest = _sha256(artifact)
            if not _valid_digest(document.get("sha256")) or document["sha256"] != digest:
                errors.append("backup artifact SHA-256 mismatch")
            age = (now - _parse_time(document.get("created_at"))).total_seconds() / 3600
            if age < 0 or age > self.backup_max_age_hours:
                errors.append("backup artifact is stale or future-dated")
            if not isinstance(document.get("source_release_sha"), str) or not document["source_release_sha"]:
                errors.append("backup source release SHA missing")
            elif not COMMIT_RE.fullmatch(document["source_release_sha"]):
                errors.append("backup source release SHA invalid")
            coverage = document.get("coverage")
            if not isinstance(coverage, list) or not REQUIRED_BACKUP_COVERAGE.issubset(coverage):
                errors.append("backup does not cover SQLite plus website data/assets")
            content_binding = document.get("content_manifest")
            if not isinstance(content_binding, dict):
                errors.append("backup content manifest binding missing")
                content = {}
                content_digest = ""
            else:
                content_artifact = _safe_relative(root, content_binding.get("path"))
                content_digest = _sha256(content_artifact)
                if content_binding.get("sha256") != content_digest:
                    errors.append("backup content manifest SHA-256 mismatch")
                content = _load_json(content_artifact)
            databases = content.get("databases")
            mutable_files = content.get("mutable_files")
            mutable_directories = content.get("mutable_directories")
            if content.get("schema_version") != 2:
                errors.append("backup content manifest schema_version must equal 2")
            if content.get("coverage") != list(("sqlite", "website_assets", "website_data")):
                errors.append("backup content manifest coverage mismatch")
            if not isinstance(databases, list) or not databases:
                errors.append("backup content manifest has no SQLite databases")
                databases = []
            if not isinstance(mutable_files, list):
                errors.append("backup content manifest mutable files invalid")
                mutable_files = []
            if not isinstance(mutable_directories, list):
                errors.append("backup content manifest mutable directories invalid")
                mutable_directories = []
            mutable_scopes = {
                row.get("scope") for row in mutable_directories if isinstance(row, dict)
            }
            if not {"website_data", "website_assets"}.issubset(mutable_scopes):
                errors.append("backup content manifest lacks website data/assets roots")
            if document.get("database_count") != len(databases):
                errors.append("backup database count mismatch")
            if document.get("mutable_file_count") != len(mutable_files):
                errors.append("backup mutable file count mismatch")
            if document.get("mutable_directory_count") != len(mutable_directories):
                errors.append("backup mutable directory count mismatch")
            drill = document.get("restore_drill")
            if not isinstance(drill, dict):
                errors.append("restore drill evidence missing")
            else:
                if drill.get("actual_restore_performed") is not True or drill.get("status") != "passed":
                    errors.append("actual restore drill has not passed")
                if drill.get("backup_sha256") != digest:
                    errors.append("restore drill is not bound to this backup")
                if drill.get("content_manifest_sha256") != content_digest:
                    errors.append("restore drill is not bound to the content manifest")
                if set(drill.get("verified_scopes") or []) != REQUIRED_BACKUP_COVERAGE:
                    errors.append("restore drill did not verify all backup scopes")
                if drill.get("verified_databases") != len(databases):
                    errors.append("restore drill database count mismatch")
                if drill.get("verified_mutable_files") != len(mutable_files):
                    errors.append("restore drill mutable file count mismatch")
                if drill.get("verified_mutable_directories") != len(mutable_directories):
                    errors.append("restore drill mutable directory count mismatch")
                drill_artifact = _safe_relative(root, drill.get("evidence_path"))
                drill_digest = _sha256(drill_artifact)
                if drill.get("evidence_sha256") != drill_digest:
                    errors.append("restore drill evidence SHA-256 mismatch")
                drill_report = _load_json(drill_artifact)
                if not (
                    drill_report.get("schema_version") == 2
                    and drill_report.get("actual_restore_performed") is True
                    and drill_report.get("status") == "passed"
                    and drill_report.get("backup_sha256") == digest
                    and drill_report.get("content_manifest_sha256") == content_digest
                    and set(drill_report.get("verified_scopes") or [])
                    == REQUIRED_BACKUP_COVERAGE
                    and drill_report.get("verified_databases") == len(databases)
                    and drill_report.get("verified_mutable_files") == len(mutable_files)
                    and drill_report.get("verified_mutable_directories")
                    == len(mutable_directories)
                ):
                    errors.append("restore drill report does not verify the complete backup")
        except Exception as exc:
            errors.append(str(exc))
        self._check("backup_and_restore_drill", not errors, errors)

    def _check_release(self) -> None:
        errors: list[str] = []
        marker_path = self.release_dir / "RELEASE_COMPLETE.json"
        try:
            release_metadata = self.release_dir.lstat()
            if (
                stat.S_ISLNK(release_metadata.st_mode)
                or not stat.S_ISDIR(release_metadata.st_mode)
                or stat.S_IMODE(release_metadata.st_mode) != 0o555
            ):
                errors.append("release root must be a non-symlink immutable 0555 directory")
            marker_metadata = marker_path.lstat()
            if (
                stat.S_ISLNK(marker_metadata.st_mode)
                or not stat.S_ISREG(marker_metadata.st_mode)
                or marker_metadata.st_nlink != 1
                or stat.S_IMODE(marker_metadata.st_mode) != 0o444
            ):
                raise ValueError(
                    "release completion marker must be a single-link immutable 0444 file"
                )
            marker = _load_json(marker_path)
            if marker.get("schema_version") != 1:
                errors.append("release marker schema_version must equal 1")
            if marker.get("manifest") != "release-manifest.json":
                errors.append("release marker names the wrong manifest")
            manifest = _safe_relative(self.release_dir, marker.get("manifest"))
            manifest_metadata = manifest.stat()
            if (
                manifest_metadata.st_nlink != 1
                or stat.S_IMODE(manifest_metadata.st_mode) != 0o444
            ):
                errors.append("release manifest must be a single-link immutable 0444 file")
            manifest_digest = _sha256(manifest)
            if not _valid_digest(marker.get("manifest_sha256")) or marker["manifest_sha256"] != manifest_digest:
                errors.append("release manifest SHA-256 mismatch")
            payload = _load_json(manifest)
            if payload.get("schema_version") != 1:
                errors.append("release manifest schema_version must equal 1")
            marker_commit = marker.get("commit")
            manifest_commit = payload.get("commit")
            if (
                not isinstance(marker_commit, str)
                or not COMMIT_RE.fullmatch(marker_commit)
                or manifest_commit != marker_commit
            ):
                errors.append("release marker/manifest commit binding mismatch")
            if payload.get("release_id") != marker.get("release_id"):
                errors.append("release marker/manifest release_id mismatch")
            if payload.get("immutable") is not True:
                errors.append("release manifest is not immutable")
            snapshot_digest = payload.get("source_snapshot_sha256")
            if (
                not _valid_digest(snapshot_digest)
                or marker.get("source_snapshot_sha256") != snapshot_digest
                or snapshot_digest != self.context.release_sha
            ):
                errors.append("release source snapshot binding mismatch")
            files = payload.get("files")
            if not isinstance(files, list) or not files:
                errors.append("release manifest files missing")
            else:
                if payload.get("source_file_count") != len(files) or marker.get("source_file_count") != len(files):
                    errors.append("release file count mismatch")
                expected_files: dict[str, str] = {
                    "release-manifest.json": "0444",
                    "RELEASE_COMPLETE.json": "0444",
                }
                for index, item in enumerate(files):
                    if not isinstance(item, dict):
                        errors.append(f"release file {index} metadata invalid")
                        continue
                    relative = item.get("path")
                    mode = item.get("mode")
                    if (
                        not isinstance(relative, str)
                        or relative in expected_files
                        or mode not in {"0444", "0555"}
                    ):
                        errors.append(f"release file {index} path/mode invalid or duplicate")
                        continue
                    expected_files[relative] = mode
                    path = _safe_relative(self.release_dir, relative)
                    metadata = path.stat()
                    if not _valid_digest(item.get("sha256")) or item["sha256"] != _sha256(path):
                        errors.append(f"release file {index} SHA-256 mismatch")
                    if item.get("size") != metadata.st_size:
                        errors.append(f"release file {index} size mismatch")
                    if metadata.st_nlink != 1 or stat.S_IMODE(metadata.st_mode) != int(mode, 8):
                        errors.append(f"release file {index} immutable mode/link count mismatch")

                expected_directories = {
                    parent.as_posix()
                    for relative in expected_files
                    for parent in Path(relative).parents
                    if parent != Path(".")
                }
                actual_files: set[str] = set()
                actual_directories: set[str] = set()
                for root, directory_names, file_names in os.walk(
                    self.release_dir, followlinks=False
                ):
                    base = Path(root)
                    if base != self.release_dir:
                        base_metadata = base.lstat()
                        if (
                            stat.S_ISLNK(base_metadata.st_mode)
                            or not stat.S_ISDIR(base_metadata.st_mode)
                            or stat.S_IMODE(base_metadata.st_mode) != 0o555
                        ):
                            errors.append("release contains a mutable or unsafe directory")
                        actual_directories.add(base.relative_to(self.release_dir).as_posix())
                    for name in directory_names:
                        child = base / name
                        if child.is_symlink():
                            errors.append("release contains a symlinked directory")
                    for name in file_names:
                        child = base / name
                        relative = child.relative_to(self.release_dir).as_posix()
                        metadata = child.lstat()
                        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                            errors.append("release contains a symlinked or unsafe file")
                            continue
                        actual_files.add(relative)
                        expected_mode = expected_files.get(relative)
                        if (
                            expected_mode is not None
                            and (
                                metadata.st_nlink != 1
                                or stat.S_IMODE(metadata.st_mode) != int(expected_mode, 8)
                            )
                        ):
                            errors.append("release file immutable mode/link count mismatch")
                if actual_files != set(expected_files):
                    errors.append("release recursive file set differs from manifest")
                if actual_directories != expected_directories:
                    errors.append("release recursive directory set differs from manifest")
        except Exception as exc:
            errors.append(str(exc))
        self._check("v3_release_marker_manifest", not errors, errors)

    def _check_readiness(self) -> None:
        document = self._document("readiness_manifest", self.readiness_manifest_path)
        if document is None:
            return
        errors: list[str] = []
        if document.get("schema_version") != 1:
            errors.append("readiness manifest schema_version must equal 1")
        if document.get("replacement_ready") is not True:
            errors.append("V3 replacement is not ready")
        required_ids = document.get("required_surface_ids")
        surfaces = document.get("surfaces")
        if (
            not isinstance(required_ids, list)
            or not required_ids
            or not all(isinstance(item, str) and item for item in required_ids)
            or len(required_ids) != len(set(required_ids))
        ):
            errors.append("required surface IDs are missing or invalid")
            required_ids = []
        if not isinstance(surfaces, list):
            errors.append("readiness surfaces are missing")
            surfaces = []
        by_id: dict[str, dict[str, Any]] = {}
        for row in surfaces:
            if not isinstance(row, dict) or not isinstance(row.get("id"), str):
                errors.append("readiness surface metadata is invalid")
                continue
            if row["id"] in by_id:
                errors.append(f"duplicate readiness surface: {row['id']}")
            by_id[row["id"]] = row
        for surface_id in required_ids:
            row = by_id.get(surface_id)
            if row is None:
                errors.append(f"required readiness surface missing: {surface_id}")
            elif not (
                row.get("required") is True
                and row.get("implemented") is True
                and row.get("tested") is True
                and row.get("blocked") is False
                and row.get("status") == "ready"
            ):
                errors.append(f"required readiness surface is not ready: {surface_id}")
        summary = document.get("summary")
        required_count = len(required_ids)
        if not isinstance(summary, dict) or not (
            summary.get("required") == required_count
            and summary.get("implemented") == required_count
            and summary.get("tested") == required_count
            and summary.get("blocked") == 0
        ):
            errors.append("readiness summary is not fully implemented and tested")
        self._check("v3_readiness_manifest", not errors, errors)

    def _check_deploy_prepared(self) -> None:
        marker = self._document("deploy_prepared_marker", self.deploy_prepared_marker_path)
        if marker is None:
            return
        errors: list[str] = []
        try:
            release_marker = _load_json(self.release_dir / "RELEASE_COMPLETE.json")
            release_id = release_marker.get("release_id")
            release_manifest_sha256 = release_marker.get("manifest_sha256")
            if marker.get("schema_version") != 1:
                errors.append("deploy marker schema_version must equal 1")
            if marker.get("ready_to_install") is not True or marker.get("status") != "prepared_not_installed":
                errors.append("deployment is not prepared and ready to install")
            if marker.get("mutation_performed") is not False:
                errors.append("deploy preparation reports a live mutation")
            if marker.get("release_id") != release_id:
                errors.append("deploy marker release_id mismatch")
            if marker.get("release_manifest_sha256") != release_manifest_sha256:
                errors.append("deploy marker release manifest hash mismatch")
            manifest = _safe_relative(self.deploy_prepared_marker_path.parent, marker.get("manifest"))
            manifest_digest = _sha256(manifest)
            if not _valid_digest(marker.get("manifest_sha256")) or marker["manifest_sha256"] != manifest_digest:
                errors.append("deploy manifest SHA-256 mismatch")
            payload = _load_json(manifest)
            if payload.get("schema_version") != 1:
                errors.append("deploy manifest schema_version must equal 1")
            if payload.get("status") != "prepared_not_installed" or payload.get("mutation_performed") is not False:
                errors.append("deploy manifest is not non-mutating prepared state")
            if payload.get("release_id") != release_id:
                errors.append("deploy manifest release_id mismatch")
            if payload.get("release_manifest_sha256") != release_manifest_sha256:
                errors.append("deploy manifest release hash mismatch")
            expected_release_manifest = (self.release_dir / "release-manifest.json").resolve()
            if payload.get("release_manifest") != str(expected_release_manifest):
                errors.append("deploy manifest release path mismatch")
            deployment_mode = payload.get("deployment_mode")
            if deployment_mode not in {"production", "isolated_live_validation"}:
                errors.append("deploy manifest deployment mode is invalid")
            if marker.get("deployment_mode") != deployment_mode:
                errors.append("deploy marker deployment mode mismatch")
            if self.execution_purpose == "final_cutover" and deployment_mode != "production":
                errors.append("final cutover requires a production deployment")
            artifacts = payload.get("artifacts")
            artifact_paths: set[str] = set()
            if not isinstance(artifacts, list) or not artifacts:
                errors.append("deploy artifact inventory is missing")
            else:
                for index, row in enumerate(artifacts):
                    if not isinstance(row, dict):
                        errors.append(f"deploy artifact {index} metadata invalid")
                        continue
                    relative = row.get("path")
                    if not isinstance(relative, str) or relative in artifact_paths:
                        errors.append(f"deploy artifact {index} path invalid or duplicate")
                        continue
                    artifact_paths.add(relative)
                    artifact = _safe_relative(self.deploy_prepared_marker_path.parent, relative)
                    if row.get("size") != artifact.stat().st_size:
                        errors.append(f"deploy artifact {relative} size mismatch")
                    if not _valid_digest(row.get("sha256")) or row["sha256"] != _sha256(artifact):
                        errors.append(f"deploy artifact {relative} SHA-256 mismatch")
                actual_paths = {
                    str(path.relative_to(self.deploy_prepared_marker_path.parent))
                    for path in self.deploy_prepared_marker_path.parent.rglob("*")
                    if path.is_file()
                    and path not in {self.deploy_prepared_marker_path, manifest}
                }
                if actual_paths != artifact_paths:
                    errors.append("deploy artifact inventory differs from prepared files")
            external_inputs = payload.get("external_inputs")
            if not isinstance(external_inputs, dict):
                external_inputs = {}
            ownership: dict[str, Any] | None = None
            ownership_relative = "ownership/ownership-manifest.json"
            if ownership_relative not in artifact_paths:
                errors.append("prepared V3 ownership manifest is not hash-bound")
            else:
                ownership_path = self.deploy_prepared_marker_path.parent / ownership_relative
                if ownership_path.is_symlink() or not ownership_path.is_file():
                    errors.append("prepared V3 ownership manifest is unsafe")
                else:
                    ownership_sha256 = _sha256(ownership_path)
                    if (
                        not _valid_digest(payload.get("ownership_manifest_sha256"))
                        or payload.get("ownership_manifest_sha256") != ownership_sha256
                    ):
                        errors.append("prepared V3 ownership manifest SHA-256 mismatch")
                    else:
                        ownership = _load_json(ownership_path)
            if ownership is not None:
                if ownership.get("release_id") != release_id:
                    errors.append("prepared V3 ownership release ID mismatch")
                if ownership.get("release_manifest_sha256") != release_manifest_sha256:
                    errors.append("prepared V3 ownership release hash mismatch")
                if ownership.get("deployment_mode") != deployment_mode:
                    errors.append("prepared V3 ownership deployment mode mismatch")
                if ownership.get("external_inputs") != external_inputs:
                    errors.append("prepared V3 ownership external-input binding mismatch")
            self._check_deploy_external_inputs(
                payload,
                artifact_paths,
                errors,
            )
            roles = payload.get("roles")
            if not isinstance(roles, list) or {
                row.get("label") for row in roles if isinstance(row, dict)
            } != EXPECTED_V3_LABELS or len(roles) != len(EXPECTED_V3_LABELS):
                errors.append("prepared V3 deployment must define the exact three ownership roles")
            else:
                ownership_roles = {
                    row.get("label"): row
                    for row in (ownership or {}).get("roles", [])
                    if isinstance(row, dict) and isinstance(row.get("label"), str)
                }
                if set(ownership_roles) != EXPECTED_V3_LABELS:
                    errors.append("prepared V3 ownership role inventory mismatch")
                expected_executable = str((self.release_dir / "bin" / "magi-v3-python").resolve())
                for row in roles:
                    if not isinstance(row, dict):
                        errors.append("prepared V3 role is invalid")
                        continue
                    label = row.get("label")
                    arguments = row.get("ProgramArguments")
                    if row.get("WorkingDirectory") != str(self.release_dir):
                        errors.append(f"prepared V3 role working directory mismatch: {label}")
                    if not isinstance(arguments, list) or not arguments or arguments[0] != expected_executable:
                        errors.append(f"prepared V3 role executable mismatch: {label}")
                    if row.get("release_manifest") != str(expected_release_manifest):
                        errors.append(f"prepared V3 role release path mismatch: {label}")
                    if row.get("release_manifest_sha256") != release_manifest_sha256:
                        errors.append(f"prepared V3 role release hash mismatch: {label}")
                    plist_relative = f"launchagents/{label}.plist"
                    if plist_relative not in artifact_paths:
                        errors.append(f"prepared V3 plist is not hash-bound: {label}")
                        continue
                    plist_path = self.deploy_prepared_marker_path.parent / plist_relative
                    if plist_path.is_symlink() or not plist_path.is_file():
                        errors.append(f"prepared V3 plist is unsafe: {label}")
                        continue
                    try:
                        plist = plistlib.loads(plist_path.read_bytes())
                        environment = plist.get("EnvironmentVariables")
                        if (
                            plist.get("Label") != label
                            or plist.get("WorkingDirectory") != row.get("WorkingDirectory")
                            or plist.get("ProgramArguments") != arguments
                        ):
                            errors.append(f"prepared V3 plist binding mismatch: {label}")
                        if not isinstance(environment, dict):
                            errors.append(f"prepared V3 plist environment is missing: {label}")
                            continue
                        if environment.get("MAGI_V3_DEPLOYMENT_MODE") != deployment_mode:
                            errors.append(f"prepared V3 plist deployment mode mismatch: {label}")
                        if environment.get("MAGI_CONFIG_PATH") != external_inputs.get(
                            "laf_config_file"
                        ):
                            errors.append(f"prepared V3 generic config binding mismatch: {label}")
                        expected_json_dir = str(
                            Path(str(external_inputs.get("laf_config_file") or "")).parent
                        )
                        if environment.get("MAGI_JSON_DIR") != expected_json_dir:
                            errors.append(f"prepared V3 external JSON directory mismatch: {label}")
                        if environment.get("MAGI_PUBLIC_SOURCE_ROOT_DIR") != str(
                            self.release_dir
                        ):
                            errors.append(f"prepared V3 public source root mismatch: {label}")
                        owner_row = ownership_roles.get(label, {})
                        for plist_name, binding_name in EXTERNAL_PLIST_BINDINGS.items():
                            expected_value = external_inputs.get(binding_name)
                            if (
                                row.get(binding_name) != expected_value
                                or owner_row.get(binding_name) != expected_value
                                or environment.get(plist_name) != expected_value
                            ):
                                errors.append(
                                    f"prepared V3 external-input binding mismatch: {label}:{binding_name}"
                                )
                    except Exception as exc:
                        errors.append(f"prepared V3 plist invalid: {label}: {exc}")
        except Exception as exc:
            errors.append(str(exc))
        self._check("v3_deploy_prepared", not errors, errors)

    def _check_deploy_external_inputs(
        self,
        payload: dict[str, Any],
        artifact_paths: set[str],
        errors: list[str],
    ) -> None:
        external = payload.get("external_inputs")
        if not isinstance(external, dict):
            errors.append("prepared V3 external-input bindings are missing")
            return

        runtime_raw = payload.get("runtime_root")
        runtime_root: Path | None = None
        if not isinstance(runtime_raw, str) or not Path(runtime_raw).is_absolute():
            errors.append("prepared V3 runtime_root path is invalid")
        else:
            runtime_root = Path(runtime_raw)
            if runtime_root.resolve(strict=False) != runtime_root:
                errors.append("prepared V3 runtime_root path is not canonical")
                runtime_root = None
        if runtime_root is not None:
            expected_named = named_mutable_state_paths(runtime_root)
            release_root = self.release_dir.resolve()
            for binding_name, expected in expected_named.items():
                raw = external.get(binding_name)
                path = Path(raw) if isinstance(raw, str) else None
                if raw != expected or path is None or not path.is_absolute():
                    errors.append(
                        f"prepared V3 {binding_name} exact shared-state binding mismatch"
                    )
                    continue
                resolved = path.resolve(strict=False)
                if (
                    resolved != path
                    or resolved == release_root
                    or resolved.is_relative_to(release_root)
                ):
                    errors.append(
                        f"prepared V3 {binding_name} is not a canonical non-release path"
                    )

        deployment_root = self.deploy_prepared_marker_path.parent

        def bound_file(
            binding_name: str,
            expected_path: Path | None = None,
        ) -> Path | None:
            raw = external.get(binding_name)
            if not isinstance(raw, str) or not Path(raw).is_absolute():
                errors.append(f"prepared V3 {binding_name} path is invalid")
                return None
            path = Path(raw)
            try:
                if path.is_symlink() or not path.is_file() or path.resolve(strict=True) != path:
                    raise OSError("path is not a canonical non-symlink regular file")
            except OSError as exc:
                errors.append(f"prepared V3 {binding_name} is unsafe: {exc}")
                return None
            if expected_path is not None and path != expected_path:
                errors.append(f"prepared V3 {binding_name} path mismatch")
                return None
            return path

        cron_relative = "runtime-inputs/cron_jobs.v3.json"
        runtime_manifest_relative = "runtime-inputs/python-runtime-manifest.json"
        for relative in (cron_relative, runtime_manifest_relative):
            if relative not in artifact_paths:
                errors.append(f"prepared V3 external artifact is not hash-bound: {relative}")

        cron_file = bound_file("cron_jobs_file", deployment_root / cron_relative)
        runtime_manifest = bound_file(
            "python_runtime_manifest",
            deployment_root / runtime_manifest_relative,
        )
        cron_source = bound_file("cron_jobs_source_file")
        laf_config = bound_file("laf_config_file")
        google_credentials = bound_file("google_credentials_file")
        google_calendar_token_source = bound_file("google_calendar_token_source_file")
        laf_gmail_token_source = bound_file("laf_gmail_token_source_file")
        file_review_token_source = bound_file("file_review_token_source_file")
        accounting_credentials = bound_file("accounting_credentials_file")
        accounting_token_source = bound_file("accounting_sheets_token_source_file")
        drive_token_source = bound_file("drive_sync_token_source_file")
        drive_write_token_source = bound_file("drive_sync_write_token_source_file")
        ocr_queue = bound_file("nas_ocr_queue_db_file")
        static_receipt: Path | None = None

        if payload.get("deployment_mode") == "production":
            runtime_root = Path(str(payload.get("runtime_root") or ""))
            static_target = runtime_root / "shared" / "external"
            legacy_static_receipt = static_target / STATIC_EXTERNAL_RECEIPT_NAME
            release_receipt_relative = (
                Path("runtime-inputs") / STATIC_EXTERNAL_RELEASE_RECEIPT_NAME
            )
            release_static_receipt = deployment_root / release_receipt_relative
            raw_static_receipt = external.get("static_external_receipt")
            legacy_static_binding = raw_static_receipt == str(legacy_static_receipt)
            static_receipt = bound_file(
                "static_external_receipt",
                legacy_static_receipt
                if legacy_static_binding
                else release_static_receipt,
            )
            if not legacy_static_binding and release_receipt_relative.as_posix() not in artifact_paths:
                errors.append(
                    "prepared V3 static external release receipt is not hash-bound"
                )
            for key in (
                "static_external_receipt",
                "static_external_receipt_sha256",
                "static_external_source_snapshot_sha256",
                "static_external_target_snapshot_sha256",
            ):
                if payload.get(key) != external.get(key):
                    errors.append(f"prepared V3 {key} top-level binding mismatch")
            if static_receipt is not None:
                try:
                    if legacy_static_binding:
                        # Backward compatibility for already-installed r59-style
                        # deployments. New deployments always use the local
                        # release-binding receipt path above.
                        static_report = verify_static_external(
                            self.release_dir / "release-manifest.json",
                            expected_release_manifest_sha256=str(
                                payload.get("release_manifest_sha256") or ""
                            ),
                            target_root=static_target,
                        )
                        verified_receipt_sha = static_report["receipt_sha256"]
                    else:
                        static_report = verify_static_external_release_binding(
                            self.release_dir / "release-manifest.json",
                            expected_release_manifest_sha256=str(
                                payload.get("release_manifest_sha256") or ""
                            ),
                            binding_receipt=static_receipt,
                            expected_binding_receipt_sha256=str(
                                external.get("static_external_receipt_sha256") or ""
                            ),
                            target_root=static_target,
                            expected_target_snapshot_sha256=str(
                                external.get("static_external_target_snapshot_sha256") or ""
                            ),
                        )
                        verified_receipt_sha = static_report[
                            "binding_receipt_sha256"
                        ]
                    if (
                        verified_receipt_sha != external.get("static_external_receipt_sha256")
                        or static_report["source_snapshot_sha256"]
                        != external.get("static_external_source_snapshot_sha256")
                        or static_report["target_snapshot_sha256"]
                        != external.get("static_external_target_snapshot_sha256")
                    ):
                        errors.append("prepared V3 static external receipt binding mismatch")
                except (OSError, StaticExternalStagingError) as exc:
                    errors.append(f"prepared V3 static external receipt is invalid: {exc}")

        for path, digest_name in (
            (cron_file, "cron_jobs_sha256"),
            (runtime_manifest, "python_runtime_manifest_sha256"),
            (cron_source, "cron_jobs_source_sha256"),
            (laf_config, "laf_config_sha256"),
            (google_credentials, "google_credentials_sha256"),
            (google_calendar_token_source, "google_calendar_token_source_sha256"),
            (laf_gmail_token_source, "laf_gmail_token_source_sha256"),
            (file_review_token_source, "file_review_token_source_sha256"),
            (accounting_credentials, "accounting_credentials_sha256"),
            (accounting_token_source, "accounting_sheets_token_source_sha256"),
            (drive_token_source, "drive_sync_token_source_sha256"),
            (drive_write_token_source, "drive_sync_write_token_source_sha256"),
            (static_receipt if payload.get("deployment_mode") == "production" else None,
             "static_external_receipt_sha256"),
        ):
            expected = external.get(digest_name)
            if not _valid_digest(expected):
                errors.append(f"prepared V3 {digest_name} is invalid")
            elif path is not None and _sha256(path) != expected:
                errors.append(f"prepared V3 {digest_name} mismatch")

        shared_secrets = Path(str(payload.get("runtime_root") or "")) / "shared" / "secrets"
        for name, leaf, source_digest_name, required in (
            (
                "google_calendar_token_file",
                "google_calendar_token.json",
                "google_calendar_token_source_sha256",
                True,
            ),
            (
                "laf_gmail_token_file",
                "laf_gmail_token.pickle",
                "laf_gmail_token_source_sha256",
                True,
            ),
            (
                "file_review_token_file",
                "filereview_token.pickle",
                "file_review_token_source_sha256",
                True,
            ),
            (
                "gmail_compose_token_file",
                "gmail_compose_token.json",
                "gmail_compose_token_source_sha256",
                False,
            ),
            (
                "accounting_sheets_token_file",
                "accounting_sheets_token.json",
                "accounting_sheets_token_source_sha256",
                True,
            ),
            (
                "drive_sync_token_file",
                "drive_sync_token.json",
                "drive_sync_token_source_sha256",
                True,
            ),
            (
                "drive_sync_write_token_file",
                "drive_sync_write_token.json",
                "drive_sync_write_token_source_sha256",
                True,
            ),
        ):
            raw = external.get(name)
            expected = shared_secrets / leaf
            if raw != str(expected):
                errors.append(f"prepared V3 {name} target mismatch")
                continue
            if not expected.exists():
                if required:
                    errors.append(f"prepared V3 {name} target is not materialized")
                continue
            metadata = expected.lstat()
            if (
                expected.is_symlink()
                or not expected.is_file()
                or expected.resolve(strict=True) != expected
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or metadata.st_uid != os.getuid()
                or metadata.st_nlink != 1
            ):
                errors.append(f"prepared V3 {name} target is unsafe")
            elif _valid_digest(external.get(source_digest_name)):
                if _sha256(expected) != external[source_digest_name]:
                    errors.append(f"prepared V3 {name} target is stale")
            elif required or external.get(source_digest_name) is not None:
                errors.append(f"prepared V3 {source_digest_name} is invalid")
        if ocr_queue is not None:
            metadata = ocr_queue.lstat()
            expected_mode = external.get("nas_ocr_queue_db_mode")
            if (
                expected_mode != f"{stat.S_IMODE(metadata.st_mode):04o}"
                or metadata.st_uid != os.getuid()
                or metadata.st_nlink != 1
                or stat.S_IMODE(metadata.st_mode) not in {0o600, 0o640, 0o644}
            ):
                errors.append("prepared V3 NAS OCR queue owner or mode is unsafe")
            else:
                try:
                    with sqlite3.connect(f"file:{ocr_queue}?mode=ro", uri=True) as connection:
                        if connection.execute("PRAGMA quick_check").fetchone() != ("ok",):
                            errors.append("prepared V3 NAS OCR queue quick_check did not return ok")
                except sqlite3.Error as exc:
                    errors.append(f"prepared V3 NAS OCR queue quick_check failed: {exc}")

        website_raw = external.get("website_root")
        website: Path | None = None
        if not isinstance(website_raw, str) or not Path(website_raw).is_absolute():
            errors.append("prepared V3 website_root path is invalid")
        else:
            website = Path(website_raw)
            try:
                if (
                    website.is_symlink()
                    or not website.is_dir()
                    or website.resolve(strict=True) != website
                ):
                    raise OSError("path is not a canonical non-symlink directory")
            except OSError as exc:
                errors.append(f"prepared V3 website_root is unsafe: {exc}")
                website = None
        admin_sha256 = external.get("website_admin_sha256")
        if not _valid_digest(admin_sha256):
            errors.append("prepared V3 website_admin_sha256 is invalid")
        elif website is not None:
            admin = website / "admin" / "admin_server.py"
            if (
                admin.is_symlink()
                or not admin.is_file()
                or admin.resolve(strict=True) != admin
                or _sha256(admin) != admin_sha256
            ):
                errors.append("prepared V3 Website Admin SHA-256 mismatch")

        policy_path = self.release_dir / "config" / "v3_schedule_dispatch_policy.json"
        try:
            if policy_path.is_symlink() or not policy_path.is_file():
                raise ValueError("release cron dispatch policy is missing or unsafe")
            policy = _load_json(policy_path)
            policy_cron_sha256 = policy.get("cron_jobs_sha256")
            if not _valid_digest(policy_cron_sha256):
                raise ValueError("release cron dispatch policy SHA-256 is invalid")
            if policy_cron_sha256 != external.get("cron_jobs_source_sha256"):
                raise ValueError("release cron dispatch policy/source binding mismatch")
        except Exception as exc:
            errors.append(str(exc))

    def _check_campaign_and_gate(self, now: datetime) -> None:
        try:
            gate_config = _load_json(self.gate_config_path)
        except Exception:
            gate_config = {}
        legacy_v2 = (
            gate_config.get("source_contract", {}).get("legacy_v2_validation")
            != "disabled"
        )
        expected_production_release = "v2" if legacy_v2 else "v3"
        campaign_config = self._document("campaign_config", self.campaign_config_path)
        if campaign_config is not None:
            errors = []
            if campaign_config.get("armed") is not True:
                errors.append("campaign is not armed")
            if campaign_config.get("production_release") != expected_production_release:
                errors.append(
                    "campaign production release does not match the gate source contract"
                )
            self._check("campaign_configuration", not errors, errors)
        campaign = self._document("campaign_report", self.campaign_report_path)
        if campaign is not None:
            errors = []
            if not _context_matches(campaign, self.context):
                errors.append("campaign context mismatch")
            if campaign.get("certifying") is not True or campaign.get("offline_complete") is not True:
                errors.append("certifying offline campaign is incomplete")
            if campaign.get("decision") != "GO":
                errors.append("campaign decision is not GO")
            try:
                age = (now - _parse_time(campaign.get("generated_at"))).total_seconds() / 3600
                if age < 0 or age > self.report_max_age_hours:
                    errors.append("campaign report stale or future-dated")
            except Exception as exc:
                errors.append(str(exc))
            self._check("campaign_evidence", not errors, errors)
        gate = self._document("release_gate_report", self.release_gate_report_path)
        if gate is not None:
            errors = []
            try:
                required_evidence = gate_config["required_evidence"]
                if (
                    not isinstance(required_evidence, list)
                    or not required_evidence
                    or any(not isinstance(item, str) or not item for item in required_evidence)
                    or len(required_evidence) != len(set(required_evidence))
                ):
                    raise ValueError(
                        "gate config required evidence IDs are missing, invalid, or duplicated"
                    )
            except Exception as exc:
                required_evidence = []
                errors.append(str(exc))
            required_count = len(required_evidence)
            drill_passed = [
                item
                for item in required_evidence
                if item not in ATOMIC_DRILL_EXCLUDED_EVIDENCE
            ]
            expected = gate.get("expected_context")
            if not isinstance(expected, dict) or any(expected.get(k) != v for k, v in self.context.to_dict().items()):
                errors.append("release gate context mismatch")
            if self.execution_purpose == "atomic_drill":
                exact_gate = (
                    gate.get("decision") == "NO_GO"
                    and gate.get("required_count") == required_count
                    and gate.get("passed") == drill_passed
                    and gate.get("missing") == list(ATOMIC_DRILL_EXCLUDED_EVIDENCE)
                    and gate.get("failed") == []
                    and gate.get("invalid") == {}
                )
            else:
                exact_gate = (
                    gate.get("decision") == "GO"
                    and gate.get("required_count") == required_count
                    and gate.get("passed") == required_evidence
                    and gate.get("missing") == []
                    and gate.get("failed") == []
                    and gate.get("invalid") == {}
                )
            if not exact_gate or gate.get("fail_closed") is not True:
                errors.append("release gate does not match the exact execution stage")
            try:
                age = (now - _parse_time(gate.get("generated_at"))).total_seconds() / 3600
                if age < 0 or age > self.report_max_age_hours:
                    errors.append("release gate report stale or future-dated")
            except Exception as exc:
                errors.append(str(exc))
            self._check("release_gate_evidence", not errors, errors)
            if self.execution_purpose == "final_cutover":
                self._check_conditional_authorization(now, gate, required_evidence)

    def _check_conditional_authorization(
        self,
        now: datetime,
        gate: dict[str, Any],
        required_evidence: Sequence[Any],
    ) -> None:
        """Revalidate a redeemed daytime authorization immediately before GO.

        This is intentionally a second, independent check of the normalized
        human-approval envelope. A pre-cutover report cannot turn a prior GO
        decision into an evergreen permission to stop V2 later.
        """
        binding = gate.get("conditional_authorization")
        if binding is None:
            try:
                config = _load_json(self.gate_config_path)
            except Exception:
                config = {}
            if config.get("conditional_daytime_authorization_required") is True:
                self._check(
                    "conditional_daytime_authorization",
                    False,
                    ["final cutover requires a redeemed conditional daytime authorization"],
                )
            return
        errors: list[str] = []
        try:
            if not isinstance(binding, dict) or set(binding) != {
                "evidence_path",
                "evidence_sha256",
                "metrics_sha256",
                "conditional_daytime_window",
                "conditional_request_sha256",
                "conditional_receipt_sha256",
                "conditional_consumption_sha256",
            }:
                raise ValueError("conditional authorization binding is not exact")
            evidence_path = Path(str(binding["evidence_path"])).expanduser()
            if not evidence_path.is_absolute() or evidence_path.is_symlink() or not evidence_path.is_file():
                raise ValueError("conditional approval evidence path is unsafe")
            if _sha256(evidence_path) != binding["evidence_sha256"]:
                raise ValueError("conditional approval evidence SHA drifted")
            if not all(
                _valid_digest(binding.get(key))
                for key in (
                    "evidence_sha256",
                    "metrics_sha256",
                    "conditional_request_sha256",
                    "conditional_receipt_sha256",
                    "conditional_consumption_sha256",
                )
            ):
                raise ValueError("conditional authorization digest is invalid")
            window = binding["conditional_daytime_window"]
            if not isinstance(window, dict) or set(window) != {"starts_at", "ends_at", "timezone"}:
                raise ValueError("conditional daytime window is not exact")
            assessment = assess_absolute_window(dict(window), now=now)
            if not assessment["within_window"]:
                raise ValueError("outside human-approved conditional daytime window")
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
                or any(document.get(key) != value for key, value in self.context.to_dict().items())
            ):
                raise ValueError("conditional approval evidence context is invalid")
            artifacts, artifact_errors = freeze_artifacts(document, evidence_path.parent)
            if artifact_errors:
                raise ValueError("conditional approval evidence artifacts are invalid")
            config = _load_json(self.gate_config_path)
            semantic_errors = validate_evidence_semantics(
                document,
                "human_go_approval_recorded",
                config=config,
                bound_artifacts=artifacts,
                expected_context=self.context.to_dict(),
            )
            if semantic_errors:
                raise ValueError("conditional approval semantic verification failed")
            producer = next((item for item in artifacts if item.role == "producer_report"), None)
            if producer is None:
                raise ValueError("conditional approval producer report is missing")
            report = json.loads(producer.data)
            metrics = report.get("metrics") if isinstance(report, dict) else None
            if not isinstance(metrics, dict) or (
                hashlib.sha256(_canonical_json_bytes(metrics)).hexdigest()
                != binding["metrics_sha256"]
            ):
                raise ValueError("conditional approval metrics drifted")
            if (
                metrics.get("authorization_mode") != "conditional_daytime_window"
                or metrics.get("conditional_daytime_window") != window
                or any(metrics.get(key) != binding[key] for key in (
                    "conditional_request_sha256",
                    "conditional_receipt_sha256",
                    "conditional_consumption_sha256",
                ))
            ):
                raise ValueError("conditional approval bindings do not match G28")
        except Exception as exc:
            errors.append(str(exc))
        self._check("conditional_daytime_authorization", not errors, errors)

    def _check_window(self, now: datetime, gates: dict[str, Any]) -> None:
        try:
            if gates.get("conditional_daytime_authorization_required") is True:
                result = assess_absolute_window(
                    gates["conditional_daytime_window"],
                    now=now,
                )
            else:
                result = assess_cutover_window(
                    gates["window"],
                    timezone_name=str(gates["timezone"]),
                    now=now,
                )
            self._check("cutover_window", bool(result["within_window"]), result)
        except Exception as exc:
            self._check("cutover_window", False, str(exc))

    def _check_ownership(self, *, legacy_v2: bool) -> None:
        expected_release = "v2" if legacy_v2 else "v3"
        check_name = "v2_only_ownership" if legacy_v2 else "previous_v3_only_ownership"
        try:
            if self.snapshot_collector:
                snapshot = self.snapshot_collector()
            else:
                v2_spec = discover_release_spec("v2", self.v2_root, self.v2_namespace)
                v3_spec = self._prepared_v3_release_spec()
                snapshot = collect_snapshot((v2_spec, v3_spec), ports=DEFAULT_PORTS)
            assessment = assess_snapshot(snapshot, expected=expected_release)
            self._check(
                check_name,
                assessment.go,
                {"assessment": assessment.to_dict(), "snapshot": snapshot.to_dict()},
            )
        except Exception as exc:
            self._check(check_name, False, str(exc))

    def _prepared_v3_release_spec(self) -> ReleaseSpec:
        """Bind ownership probes to the rendered-but-not-installed deployment."""

        spec = discover_release_spec("v3", self.v3_root, self.v3_namespace)
        marker = _load_json(self.deploy_prepared_marker_path)
        manifest_path = _safe_relative(
            self.deploy_prepared_marker_path.parent,
            marker.get("manifest"),
        )
        deployment = _load_json(manifest_path)
        expected_release_manifest = (self.release_dir / "release-manifest.json").resolve()
        release_marker = _load_json(self.release_dir / "RELEASE_COMPLETE.json")
        if deployment.get("release_manifest") != str(expected_release_manifest):
            raise PreCutoverError("prepared V3 deployment is bound to another release path")
        if deployment.get("release_manifest_sha256") != release_marker.get("manifest_sha256"):
            raise PreCutoverError("prepared V3 deployment release hash mismatch")
        artifacts = deployment.get("artifacts")
        if not isinstance(artifacts, list) or not artifacts:
            raise PreCutoverError("prepared V3 deployment artifact inventory is missing")
        artifact_paths: dict[str, dict[str, Any]] = {}
        for row in artifacts:
            if not isinstance(row, dict) or not isinstance(row.get("path"), str):
                raise PreCutoverError("prepared V3 deployment artifact is invalid")
            relative = row["path"]
            if relative in artifact_paths:
                raise PreCutoverError("prepared V3 deployment artifact is duplicated")
            artifact = _safe_relative(self.deploy_prepared_marker_path.parent, relative)
            if (
                row.get("size") != artifact.stat().st_size
                or not _valid_digest(row.get("sha256"))
                or row["sha256"] != _sha256(artifact)
            ):
                raise PreCutoverError(f"prepared V3 deployment artifact mismatch: {relative}")
            artifact_paths[relative] = row
        roles = deployment.get("roles")
        if not isinstance(roles, list) or not roles:
            raise PreCutoverError("prepared V3 deployment has no ownership roles")
        labels: list[str] = []
        plists: dict[str, Path] = {}
        pidfiles: list[Path] = []
        for row in roles:
            if not isinstance(row, dict):
                raise PreCutoverError("prepared V3 role is invalid")
            label = row.get("label")
            pid_file = row.get("pid_file")
            if not isinstance(label, str) or not label.startswith("com.magi.v3."):
                raise PreCutoverError("prepared V3 launchd label is invalid")
            labels.append(label)
            plist = self.deploy_prepared_marker_path.parent / "launchagents" / f"{label}.plist"
            if (
                not plist.is_file()
                or plist.is_symlink()
                or f"launchagents/{label}.plist" not in artifact_paths
            ):
                raise PreCutoverError(f"prepared V3 launchd plist is missing: {label}")
            arguments = row.get("ProgramArguments")
            if (
                row.get("WorkingDirectory") != str(self.release_dir)
                or not isinstance(arguments, list)
                or not arguments
                or arguments[0] != str((self.release_dir / "bin" / "magi-v3-python").resolve())
                or row.get("release_manifest") != str(expected_release_manifest)
                or row.get("release_manifest_sha256") != release_marker.get("manifest_sha256")
            ):
                raise PreCutoverError(f"prepared V3 role release binding mismatch: {label}")
            try:
                plist_payload = plistlib.loads(plist.read_bytes())
            except Exception as exc:
                raise PreCutoverError(f"prepared V3 launchd plist is invalid: {label}") from exc
            if (
                plist_payload.get("Label") != label
                or plist_payload.get("WorkingDirectory") != row["WorkingDirectory"]
                or plist_payload.get("ProgramArguments") != arguments
            ):
                raise PreCutoverError(f"prepared V3 launchd plist binding mismatch: {label}")
            plists[label] = plist
            if isinstance(pid_file, str) and Path(pid_file).is_absolute():
                candidate = Path(pid_file)
                if candidate.exists():
                    pidfiles.append(candidate)
        return replace(
            spec,
            # Before installation the production runtime root intentionally does
            # not exist.  Probe the immutable release payload referenced by the
            # rendered deployment instead, while retaining the prepared labels,
            # plists, and any existing pid files as independent ownership signals.
            root=self.release_dir,
            pidfiles=tuple(dict.fromkeys(pidfiles)),
            launchd_labels=tuple(dict.fromkeys(labels)),
            launchd_plists=plists,
            pidfiles_required=False,
            launchd_labels_required=True,
        )

    def run(self) -> dict[str, Any]:
        self.checks = []
        self.cutover_plan_binding = None
        now = self.clock()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        now = now.astimezone(timezone.utc)
        context_errors = []
        if not self.context.campaign_id:
            context_errors.append("campaign_id missing")
        if not _valid_digest(self.context.release_sha):
            context_errors.append("release_sha must be a lowercase 64-character source snapshot")
        if not self.context.hardware_id:
            context_errors.append("hardware_id missing")
        if not _valid_digest(self.context.gate_config_sha256):
            context_errors.append("gate_config_sha256 must be lowercase SHA-256")
        self._check("expected_context", not context_errors, context_errors)
        try:
            actual_gate_hash = _sha256(self.gate_config_path)
            gates = load_gate_config(self.gate_config_path)
            self._check(
                "gate_config_binding",
                actual_gate_hash == self.context.gate_config_sha256,
                {"actual_sha256": actual_gate_hash},
            )
        except Exception as exc:
            gates = {}
            self._check("gate_config_binding", False, str(exc))
        legacy_v2 = (
            gates.get("source_contract", {}).get("legacy_v2_validation")
            != "disabled"
        )
        try:
            disk_capacity = self._disk_capacity_requirement(
                gates.get("disk_capacity_policy")
            )
        except (OSError, ValueError, PreCutoverError) as exc:
            disk_capacity = None
            self._check("disk_capacity_policy", False, str(exc))
        else:
            self._check("disk_capacity_policy", True, disk_capacity)
        self._check_paths_and_disk(disk_capacity)
        self._check_backup(now)
        self._check_release()
        self._check_readiness()
        self._check_deploy_prepared()
        if legacy_v2:
            self._check_pdf_namer_handoff()
            self._check_mutable_state_handoff()
        self._check_campaign_and_gate(now)
        self._check_window(now, gates)
        self._check_ownership(legacy_v2=legacy_v2)
        gaps = [check["name"] for check in self.checks if not check["ok"]]
        clean = not gaps
        decision = (
            "GO_FOR_CUTOVER_DRILL_ONLY"
            if clean and self.execution_purpose == "atomic_drill"
            else "GO" if clean else "NO_GO"
        )
        required_evidence = gates.get("required_evidence")
        required_count = len(required_evidence) if isinstance(required_evidence, list) else 0
        excluded_count = (
            len(ATOMIC_DRILL_EXCLUDED_EVIDENCE)
            if self.execution_purpose == "atomic_drill"
            else 0
        )
        passed_count = required_count - excluded_count
        report = {
            "schema_version": 1,
            "observed_at": now.isoformat(),
            **self.context.to_dict(),
            "decision": decision,
            "execution_purpose": self.execution_purpose,
            "gate_stage": (
                f"cutover_drill_{passed_count}_of_{required_count}"
                if self.execution_purpose == "atomic_drill"
                else f"final_cutover_{required_count}_of_{required_count}"
            ),
            "required_evidence_count": required_count,
            "passed_evidence_count": passed_count if clean else 0,
            "excluded_evidence": (
                list(ATOMIC_DRILL_EXCLUDED_EVIDENCE)
                if self.execution_purpose == "atomic_drill"
                else []
            ),
            "expected_context": self.context.to_dict(),
            "release_gate_report": {
                "path": str(self.release_gate_report_path),
                "sha256": _sha256(self.release_gate_report_path),
            },
            "fail_closed": True,
            "mutation_performed": False,
            "network_access_performed": False,
            "backup_performed": False,
            "restore_performed": False,
            "checks": self.checks,
            "gaps": gaps,
        }
        if self.cutover_plan_binding is not None:
            report["cutover_plan"] = self.cutover_plan_binding
        return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gates", type=Path, required=True)
    parser.add_argument("--campaign-config", type=Path, required=True)
    parser.add_argument("--campaign-report", type=Path, required=True)
    parser.add_argument("--release-gate-report", type=Path, required=True)
    parser.add_argument("--backup-metadata", type=Path, required=True)
    parser.add_argument("--readiness-manifest", type=Path, required=True)
    parser.add_argument("--deploy-prepared-marker", type=Path, required=True)
    parser.add_argument("--pdf-namer-handoff-manifest", type=Path, required=True)
    parser.add_argument("--pdf-namer-source", type=Path, required=True)
    parser.add_argument("--pdf-namer-destination", type=Path, required=True)
    parser.add_argument("--release-dir", type=Path, required=True)
    parser.add_argument("--v2-root", type=Path, required=True)
    parser.add_argument("--v3-root", type=Path, required=True)
    parser.add_argument("--database-path", type=Path, action="append", default=[])
    parser.add_argument("--state-path", type=Path, action="append", default=[])
    parser.add_argument("--nas-path", type=Path, action="append", default=[])
    parser.add_argument("--campaign-id", required=True)
    parser.add_argument("--release-sha", required=True)
    parser.add_argument("--hardware-id", required=True)
    parser.add_argument("--gate-config-sha256", required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--cutover-plan", type=Path)
    parser.add_argument("--cutover-plan-sha256")
    parser.add_argument("--mutable-state-source-root", type=Path)
    parser.add_argument("--mutable-state-target-shared-root", type=Path)
    parser.add_argument("--mutable-state-dry-run-receipt", type=Path)
    parser.add_argument(
        "--execution-purpose",
        choices=("atomic_drill", "final_cutover"),
        default="final_cutover",
    )
    args = parser.parse_args(argv)
    context = ExpectedContext(args.campaign_id, args.release_sha, args.hardware_id, args.gate_config_sha256)
    try:
        report = PreCutoverPreflight(
            context=context,
            gate_config_path=args.gates,
            campaign_config_path=args.campaign_config,
            campaign_report_path=args.campaign_report,
            release_gate_report_path=args.release_gate_report,
            backup_metadata_path=args.backup_metadata,
            readiness_manifest_path=args.readiness_manifest,
            deploy_prepared_marker_path=args.deploy_prepared_marker,
            pdf_namer_handoff_manifest_path=args.pdf_namer_handoff_manifest,
            pdf_namer_source_path=args.pdf_namer_source,
            pdf_namer_destination_path=args.pdf_namer_destination,
            release_dir=args.release_dir,
            required_paths=RequiredPaths(tuple(args.database_path), tuple(args.state_path), tuple(args.nas_path)),
            v2_root=args.v2_root,
            v3_root=args.v3_root,
            execution_purpose=args.execution_purpose,
            cutover_plan_path=args.cutover_plan,
            cutover_plan_sha256=args.cutover_plan_sha256,
            mutable_state_source_root=args.mutable_state_source_root,
            mutable_state_target_shared_root=args.mutable_state_target_shared_root,
            mutable_state_dry_run_receipt_path=args.mutable_state_dry_run_receipt,
            report_output_path=args.output,
        ).run()
    except Exception as exc:
        report = {
            "schema_version": 1,
            "observed_at": datetime.now(timezone.utc).isoformat(),
            **context.to_dict(),
            "decision": "NO_GO",
            "fail_closed": True,
            "mutation_performed": False,
            "network_access_performed": False,
            "backup_performed": False,
            "restore_performed": False,
            "checks": [{"name": "fatal_error", "ok": False, "detail": str(exc)}],
            "gaps": ["fatal_error"],
        }
    if args.output is not None:
        try:
            _write_json_atomic(args.output, report)
        except Exception as exc:
            report = {
                "schema_version": 1,
                "observed_at": datetime.now(timezone.utc).isoformat(),
                **context.to_dict(),
                "decision": "NO_GO",
                "fail_closed": True,
                "mutation_performed": False,
                "network_access_performed": False,
                "backup_performed": False,
                "restore_performed": False,
                "checks": [{"name": "report_output", "ok": False, "detail": str(exc)}],
                "gaps": ["report_output"],
            }
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report["decision"] in {"GO", "GO_FOR_CUTOVER_DRILL_ONLY"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
