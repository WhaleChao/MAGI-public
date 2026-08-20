"""Fail-closed executor for a temporary, single-active V3 LIVE validation.

This module deliberately contains no launchd or HTTP implementation.  A live
adapter must implement :class:`IsolatedLiveMachine`; tests use an in-memory
machine.  Keeping mutation behind that explicit capability makes importing or
mis-invoking this module inert while the orchestration, artifact verification,
one-time arming, and rollback rules remain independently testable.

The workflow is *not* a final cutover.  It always attempts to return the host
to a verified V2 state and it only accepts the isolated validation deployment
rendered by ``scripts.v3_deploy_prepare``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import plistlib
import re
import stat
import sqlite3
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence
from urllib.parse import urlsplit

from magi_v3.external_inputs import (
    NAMED_MUTABLE_STATE_BINDINGS,
    named_mutable_state_paths,
)
from scripts.v3_cutover.core import (
    CutoverError,
    Snapshot,
    assess_absolute_window,
    assess_cutover_window,
    assess_snapshot,
)
from scripts.v3_deploy_prepare import (
    VALIDATION_GOOGLE_CALENDAR_TOKEN_BYTES,
    VALIDATION_GOOGLE_CREDENTIALS_BYTES,
    VALIDATION_LAF_CONFIG_BYTES,
    VALIDATION_LAF_GMAIL_TOKEN_BYTES,
)
from scripts.v3_validation.paths import ISOLATED_LIVE_EXECUTION_PLAN_SCHEMA_PATH
from scripts.v3_validation.schema import (
    ContractValidationError,
    load_json as load_contract_json,
    validate_json,
)


SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
PLAN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
DEPLOYMENT_MODE = "isolated_live_validation"
START_ORDER = ("control", "gateway", "supervisor")
STOP_ORDER = tuple(reversed(START_ORDER))
ROLE_LABELS = {
    "control": "com.magi.v3.control",
    "gateway": "com.magi.v3.gateway",
    "supervisor": "com.magi.v3.supervisor",
}
REQUIRED_COVERAGE = frozenset({"process", "pidfile", "port", "launchd", "ownership"})
VALIDATION_ENV_BYTES = b"MAGI_V3_VALIDATION_FIXTURE=1\n"
VALIDATION_CRON_JOB = {
    "id": "v3_live_validation_inert",
    "enabled": False,
    "cron": "0 0 31 2 *",
    "command": "@MAGI validation_inert",
}

# Only evidence that can exist before the first isolated handoff belongs here.
# In particular, LIVE, cutover, rollback, native-IME, and human approval evidence
# is intentionally absent so this executor cannot create a circular gate.
OFFLINE_MACHINE_EVIDENCE = frozenset(
    {
        "portable_source_inventory_current",
        "runtime_route_inventory_current",
        "v2_regression_passed_in_release_venv",
        "v3_unit_contract_integration_e2e_passed",
        "interaction_agent_kernel_memory_quality_contracts_passed",
        "context_memory_tool_plan_answer_golden_sets_passed",
        "golden_side_effect_diff_approved",
        "matched_v2_warm_cold_performance_baseline_complete",
        "resource_policy_all_budgets_passed",
        "health_1000_probes_loaded_zero_models",
        "seven_day_schedule_10x_arrival_2x_duration_replay_passed",
        "heavy_plus_interactive_preemption_benchmark_passed",
        "hundred_cycle_worker_reap_soak_passed",
        "sqlite_wal_disk_full_fsync_faults_passed",
        "notification_storm_and_dlq_faults_passed",
        "database_backup_restore_drill_passed",
        "runtime_state_snapshot_verified",
        "rendered_launchagent_manifest_checksums_saved",
        "worker_process_group_footprint_and_metal_return_to_baseline",
    }
)
OFFLINE_MACHINE_GATE_SCHEMA = "magi.v3.offline-machine-gate/v1"
OFFLINE_GATE_CONTEXT_FIELDS = (
    "campaign_id",
    "release_sha",
    "hardware_id",
    "gate_config_sha256",
)

DOCUMENT_PATHS = frozenset(
    {
        "/validation/osc/document-preview",
        "/validation/osc/document-download",
    }
)
ALLOWED_PROBE_TARGETS = frozenset(
    {
        *((port, path) for port in (5002, 5003) for path in ("/livez", "/readyz", "/validation/ping")),
        *((port, path) for port in (5002, 5003) for path in DOCUMENT_PATHS),
        (8088, "/health"),
        (8088, "/validation/ping"),
    }
)
REQUIRED_PROBE_TARGETS = frozenset(
    {
        (5002, "/livez"),
        (5002, "/readyz"),
        (5002, "/validation/ping"),
        (5003, "/livez"),
        (5003, "/readyz"),
        (5003, "/validation/ping"),
        (5003, "/validation/osc/document-preview"),
        (5003, "/validation/osc/document-download"),
        (8088, "/health"),
    }
)


class IsolatedLiveBlocked(CutoverError):
    """The validation cannot safely proceed or cannot safely recover."""


@dataclass(frozen=True, slots=True)
class BoundArtifact:
    path: Path
    sha256: str


@dataclass(frozen=True, slots=True)
class ProbeSpec:
    method: str
    url: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ValidationRole:
    role: str
    label: str
    plist: BoundArtifact


@dataclass(frozen=True, slots=True)
class IsolatedLivePlan:
    plan_id: str
    plan_sha256: str
    release_manifest: BoundArtifact
    deploy_manifest: BoundArtifact
    deploy_prepared_marker: BoundArtifact
    offline_gate_report: BoundArtifact
    token_sha256: str
    probes: tuple[ProbeSpec, ...]


@dataclass(frozen=True, slots=True)
class VerifiedDeployment:
    release_id: str
    release_root: Path
    deployment_root: Path
    runtime_root: Path
    validation_input_root: Path
    service_manifest: BoundArtifact
    ownership_manifest: BoundArtifact
    roles: tuple[ValidationRole, ...]
    fixture_sha256: str
    payload: Mapping[str, Any]


class IsolatedLiveMachine(Protocol):
    """Explicit host-mutation capability required by the executor."""

    def activate_maintenance_blackout(self) -> Mapping[str, Any]: ...

    def deactivate_maintenance_blackout(self) -> Mapping[str, Any]: ...

    def collect_ownership_snapshot(self) -> Snapshot: ...

    def stop_v2(self) -> Mapping[str, Any]: ...

    def install_validation(
        self, deployment: VerifiedDeployment
    ) -> Mapping[str, Any]: ...

    def start_v3_role(self, role: ValidationRole) -> Mapping[str, Any]: ...

    def probe(self, probe: ProbeSpec) -> Mapping[str, Any]: ...

    def run_native_ime_candidate_probe(
        self, deployment: VerifiedDeployment
    ) -> Mapping[str, Any]: ...

    def stop_v3_role(self, role: ValidationRole) -> Mapping[str, Any]: ...

    def remove_validation(self, deployment: VerifiedDeployment) -> Mapping[str, Any]: ...

    def restore_v2(self) -> Mapping[str, Any]: ...

    def verify_v2_readiness_integrity(self) -> Mapping[str, Any]: ...


Clock = Callable[[], datetime]


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical_json_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_file(path: Path, *, description: str) -> Path:
    raw = path.expanduser()
    if not raw.is_absolute() or raw.resolve(strict=False) != raw:
        raise IsolatedLiveBlocked(f"{description} path must be canonical and absolute")
    try:
        metadata = raw.lstat()
    except OSError as exc:
        raise IsolatedLiveBlocked(f"{description} is unavailable: {exc}") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise IsolatedLiveBlocked(f"{description} must be a non-symlink regular file")
    return raw


def _verify_bound(binding: BoundArtifact, *, description: str) -> bytes:
    if not SHA256_RE.fullmatch(binding.sha256):
        raise IsolatedLiveBlocked(f"{description} has an invalid SHA-256 binding")
    path = _canonical_file(binding.path, description=description)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise IsolatedLiveBlocked(f"{description} cannot be opened safely: {exc}") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise IsolatedLiveBlocked(f"{description} is not a regular file")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            data = handle.read()
        after = os.fstat(descriptor)
        current = path.lstat()
        signature = lambda row: (row.st_dev, row.st_ino, row.st_size, row.st_mtime_ns, row.st_nlink)
        if (
            signature(before) != signature(after)
            or signature(after) != signature(current)
            or _sha256_bytes(data) != binding.sha256
        ):
            raise IsolatedLiveBlocked(f"{description} SHA-256 or identity drift detected")
        return data
    finally:
        os.close(descriptor)


def _json_object(data: bytes, *, description: str) -> dict[str, Any]:
    try:
        value = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IsolatedLiveBlocked(f"{description} is not valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise IsolatedLiveBlocked(f"{description} must be a JSON object")
    return value


def _release_cutover_window(
    plan: IsolatedLivePlan,
    deployment: VerifiedDeployment,
    *,
    now: datetime,
) -> dict[str, Any]:
    """Evaluate the immutable release window bound by the offline gate.

    The old executor embedded 02:00-04:00 in two separate modules.  Reading the
    already verified release policy removes that drift while retaining a hard,
    hash-bound gate.  A conditional daytime window is an absolute, one-day
    policy which is valid only after an independently verified human
    preauthorization; it is not a recurring operator bypass.  Legacy releases
    without that policy retain their existing recurring release window.
    """

    gate_path = deployment.release_root / "config" / "v3_cutover_gates.json"
    gate_path = _canonical_file(gate_path, description="release cutover gate config")
    gate_sha256 = _sha256_file(gate_path)
    offline = _json_object(
        _verify_bound(plan.offline_gate_report, description="offline machine gate report"),
        description="offline machine gate report",
    )
    if offline.get("gate_config_sha256") != gate_sha256:
        raise IsolatedLiveBlocked(
            "release cutover window is not hash-bound to the offline machine gate"
        )
    gate = _json_object(gate_path.read_bytes(), description="release cutover gate config")
    if gate.get("schema_version") != 1 or gate.get("timezone") != "Asia/Taipei":
        raise IsolatedLiveBlocked("release cutover gate timezone or schema is invalid")
    conditional_daytime_window = gate.get("conditional_daytime_window")
    if conditional_daytime_window is not None:
        if not isinstance(conditional_daytime_window, dict):
            raise IsolatedLiveBlocked("release conditional daytime window is invalid")
        try:
            return assess_absolute_window(conditional_daytime_window, now=now)
        except CutoverError as exc:
            raise IsolatedLiveBlocked(f"release conditional daytime window is invalid: {exc}") from exc
    window = gate.get("window")
    if not isinstance(window, dict):
        raise IsolatedLiveBlocked("release cutover gate window is invalid")
    try:
        return assess_cutover_window(window, timezone_name="Asia/Taipei", now=now)
    except CutoverError as exc:
        raise IsolatedLiveBlocked(f"release cutover gate window is invalid: {exc}") from exc


def _binding(value: Any, *, name: str) -> BoundArtifact:
    if not isinstance(value, dict):
        raise IsolatedLiveBlocked(f"plan {name} must be an object")
    raw_path = value.get("path")
    digest = value.get("sha256")
    if not isinstance(raw_path, str) or not Path(raw_path).expanduser().is_absolute():
        raise IsolatedLiveBlocked(f"plan {name}.path must be absolute")
    if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
        raise IsolatedLiveBlocked(f"plan {name}.sha256 is invalid")
    return BoundArtifact(Path(raw_path).expanduser().resolve(strict=False), digest)


def _validate_probe(probe: ProbeSpec) -> tuple[int, str]:
    method = probe.method.upper()
    if method not in {"GET", "HEAD"} or method != probe.method:
        raise IsolatedLiveBlocked("LIVE validation probes must use canonical GET or HEAD")
    parsed = urlsplit(probe.url)
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.port is None
        or (parsed.port, parsed.path) not in ALLOWED_PROBE_TARGETS
    ):
        raise IsolatedLiveBlocked(f"probe is outside the read-only allowlist: {probe.url}")
    return parsed.port, parsed.path


def load_isolated_live_plan(path: Path, expected_sha256: str) -> IsolatedLivePlan:
    """Load an immutable plan without granting any mutation capability."""

    binding = BoundArtifact(path.expanduser().resolve(strict=False), expected_sha256)
    payload = _json_object(_verify_bound(binding, description="isolated LIVE plan"), description="isolated LIVE plan")
    if payload.get("schema_version") != 1 or payload.get("operation") != DEPLOYMENT_MODE:
        raise IsolatedLiveBlocked("plan is not an isolated LIVE validation schema version 1 plan")
    plan_id = payload.get("plan_id")
    token_sha256 = payload.get("token_sha256")
    if not isinstance(plan_id, str) or not PLAN_ID_RE.fullmatch(plan_id):
        raise IsolatedLiveBlocked("plan_id is invalid")
    if not isinstance(token_sha256, str) or not SHA256_RE.fullmatch(token_sha256):
        raise IsolatedLiveBlocked("plan token SHA-256 is invalid")
    raw_probes = payload.get("probes")
    if not isinstance(raw_probes, list) or not raw_probes:
        raise IsolatedLiveBlocked("plan probes must be a non-empty list")
    probes: list[ProbeSpec] = []
    targets: set[tuple[int, str]] = set()
    seen: set[tuple[str, str]] = set()
    for index, row in enumerate(raw_probes):
        if not isinstance(row, dict) or set(row) != {"method", "url"}:
            raise IsolatedLiveBlocked(f"plan probe {index} must contain only method and url")
        method, url = row.get("method"), row.get("url")
        if not isinstance(method, str) or not isinstance(url, str):
            raise IsolatedLiveBlocked(f"plan probe {index} is invalid")
        probe = ProbeSpec(method, url)
        target = _validate_probe(probe)
        if (method, url) in seen:
            raise IsolatedLiveBlocked("plan contains a duplicate probe")
        seen.add((method, url))
        targets.add(target)
        probes.append(probe)
    missing = sorted(REQUIRED_PROBE_TARGETS - targets)
    if missing:
        raise IsolatedLiveBlocked(f"plan is missing required read-only probes: {missing}")
    try:
        validate_json(
            payload,
            load_contract_json(ISOLATED_LIVE_EXECUTION_PLAN_SCHEMA_PATH),
            label="isolated LIVE execution plan",
        )
    except (OSError, json.JSONDecodeError, ContractValidationError) as exc:
        raise IsolatedLiveBlocked(str(exc)) from exc
    return IsolatedLivePlan(
        plan_id=plan_id,
        plan_sha256=expected_sha256,
        release_manifest=_binding(payload.get("release_manifest"), name="release_manifest"),
        deploy_manifest=_binding(payload.get("deploy_manifest"), name="deploy_manifest"),
        deploy_prepared_marker=_binding(
            payload.get("deploy_prepared_marker"), name="deploy_prepared_marker"
        ),
        offline_gate_report=_binding(payload.get("offline_gate_report"), name="offline_gate_report"),
        token_sha256=token_sha256,
        probes=tuple(probes),
    )


def _path_inside(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _absolute_directory(value: Any, *, description: str) -> Path:
    if not isinstance(value, str):
        raise IsolatedLiveBlocked(f"{description} must be an absolute path")
    raw = Path(value).expanduser()
    if not raw.is_absolute() or raw.is_symlink():
        raise IsolatedLiveBlocked(f"{description} must be an absolute non-symlink directory")
    try:
        resolved = raw.resolve(strict=True)
    except OSError as exc:
        raise IsolatedLiveBlocked(f"{description} is unavailable: {exc}") from exc
    if resolved != raw or not resolved.is_dir():
        raise IsolatedLiveBlocked(f"{description} must be canonical and a directory")
    return resolved


def _release_inventory(payload: Mapping[str, Any]) -> dict[str, str]:
    rows = payload.get("files")
    if not isinstance(rows, list) or not rows:
        raise IsolatedLiveBlocked("release manifest has no immutable file inventory")
    result: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise IsolatedLiveBlocked("release manifest file inventory is invalid")
        relative, digest = row.get("path"), row.get("sha256")
        if (
            not isinstance(relative, str)
            or not relative
            or Path(relative).is_absolute()
            or ".." in Path(relative).parts
            or not isinstance(digest, str)
            or not SHA256_RE.fullmatch(digest)
            or relative in result
        ):
            raise IsolatedLiveBlocked("release manifest file inventory is invalid")
        result[relative] = digest
    return result


def _verify_service_manifest(
    path: Path,
    digest: str,
    *,
    release_root: Path,
    inventory: Mapping[str, str],
) -> BoundArtifact:
    path = path.expanduser().resolve(strict=False)
    try:
        relative = path.relative_to(release_root).as_posix()
    except ValueError as exc:
        raise IsolatedLiveBlocked("validation service manifest escapes the immutable release") from exc
    if relative != "config/v3_live_validation_service_manifest.json":
        raise IsolatedLiveBlocked("deployment selected the wrong validation service manifest")
    if inventory.get(relative) != digest:
        raise IsolatedLiveBlocked("validation service manifest is not release-inventory bound")
    binding = BoundArtifact(path, digest)
    payload = _json_object(
        _verify_bound(binding, description="validation service manifest"),
        description="validation service manifest",
    )
    if (
        payload.get("schema_version") != 1
        or payload.get("deployment_mode") != DEPLOYMENT_MODE
        or payload.get("release_mode") != "single_active_replacement"
    ):
        raise IsolatedLiveBlocked("validation service manifest safety mode is invalid")
    services = payload.get("services")
    if not isinstance(services, list):
        raise IsolatedLiveBlocked("validation service manifest services are invalid")
    expected = {
        "main_http": ("gateway", "wsgi", 5002, "magi_v3.live_validation:create_main_app"),
        "tools_http": ("gateway", "wsgi", 5003, "magi_v3.live_validation:create_tools_app"),
        "website_admin": ("control", "http_server", 8088, "magi_v3.live_validation:create_admin_server"),
    }
    seen: set[str] = set()
    process_seen = False
    for row in services:
        if not isinstance(row, dict) or row.get("required") is not True:
            raise IsolatedLiveBlocked("every validation service must be a required object")
        service_id = row.get("id")
        if not isinstance(service_id, str) or service_id in seen:
            raise IsolatedLiveBlocked("validation service IDs are invalid or duplicated")
        seen.add(service_id)
        if service_id in expected:
            role, kind, port, factory = expected[service_id]
            if (row.get("role"), row.get("kind"), row.get("port"), row.get("factory")) != (
                role,
                kind,
                port,
                factory,
            ):
                raise IsolatedLiveBlocked(f"validation service binding drifted: {service_id}")
            continue
        if service_id == "live_validation_probe":
            if (
                row.get("role") != "supervisor"
                or row.get("kind") != "process"
                or row.get("argv") != ["{python}", "magi_v3/live_validation_probe_service.py"]
            ):
                raise IsolatedLiveBlocked("validation probe service binding drifted")
            process_seen = True
            continue
        raise IsolatedLiveBlocked(f"unapproved validation service present: {service_id}")
    if seen != {*expected, "live_validation_probe"} or not process_seen:
        raise IsolatedLiveBlocked("validation service manifest is incomplete")
    for relative in (
        "magi_v3/live_validation.py",
        "magi_v3/live_validation_probe_service.py",
    ):
        member_digest = inventory.get(relative)
        if member_digest is None:
            raise IsolatedLiveBlocked(f"validation service entrypoint is absent from release: {relative}")
        _verify_bound(
            BoundArtifact(release_root / relative, member_digest),
            description=f"validation service entrypoint {relative}",
        )
    return binding


def _artifact_inventory(deploy_root: Path, payload: Mapping[str, Any]) -> dict[str, BoundArtifact]:
    rows = payload.get("artifacts")
    if not isinstance(rows, list) or not rows:
        raise IsolatedLiveBlocked("deployment artifact inventory is empty")
    result: dict[str, BoundArtifact] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise IsolatedLiveBlocked("deployment artifact row is invalid")
        relative, digest = row.get("path"), row.get("sha256")
        if (
            not isinstance(relative, str)
            or not relative
            or Path(relative).is_absolute()
            or ".." in Path(relative).parts
            or not isinstance(digest, str)
            or not SHA256_RE.fullmatch(digest)
            or relative in result
        ):
            raise IsolatedLiveBlocked("deployment artifact inventory is invalid")
        binding = BoundArtifact(deploy_root / relative, digest)
        _verify_bound(binding, description=f"deployment artifact {relative}")
        result[relative] = binding
    return result


def _verify_validation_inputs(
    deploy: Mapping[str, Any],
    *,
    deploy_root: Path,
    release_root: Path,
    runtime_root: Path,
) -> tuple[Path, str]:
    validation_root = _absolute_directory(
        deploy.get("validation_input_root"), description="validation input root"
    )
    if any(
        _path_inside(validation_root, other) or _path_inside(other, validation_root)
        for other in (deploy_root, release_root, runtime_root)
    ):
        raise IsolatedLiveBlocked("validation input root overlaps release, deployment, or runtime")
    external = deploy.get("external_inputs")
    if not isinstance(external, dict):
        raise IsolatedLiveBlocked("deployment external_inputs are missing")

    def bound_external(name: str, digest_name: str) -> Path:
        raw, digest = external.get(name), external.get(digest_name)
        if not isinstance(raw, str) or not isinstance(digest, str):
            raise IsolatedLiveBlocked(f"validation external input {name} is invalid")
        path = Path(raw).expanduser().resolve(strict=False)
        if not _path_inside(path, validation_root):
            raise IsolatedLiveBlocked(f"validation external input {name} escapes its sandbox")
        _verify_bound(BoundArtifact(path, digest), description=f"validation external input {name}")
        return path

    env_file = bound_external("env_file", "env_file_sha256")
    if env_file.read_bytes() != VALIDATION_ENV_BYTES:
        raise IsolatedLiveBlocked("validation environment contains non-inert configuration")
    cron_source = bound_external("cron_jobs_source_file", "cron_jobs_source_sha256")
    try:
        cron_payload = json.loads(cron_source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise IsolatedLiveBlocked(f"validation cron source is unreadable: {exc}") from exc
    if cron_payload != [VALIDATION_CRON_JOB]:
        raise IsolatedLiveBlocked("validation cron source is not the disabled inert job")
    laf_config = bound_external("laf_config_file", "laf_config_sha256")
    if laf_config.read_bytes() != VALIDATION_LAF_CONFIG_BYTES:
        raise IsolatedLiveBlocked("validation LAF config is not the inert local fixture")
    for name, digest_name, expected in (
        (
            "google_credentials_file",
            "google_credentials_sha256",
            VALIDATION_GOOGLE_CREDENTIALS_BYTES,
        ),
        (
            "google_calendar_token_source_file",
            "google_calendar_token_source_sha256",
            VALIDATION_GOOGLE_CALENDAR_TOKEN_BYTES,
        ),
        (
            "laf_gmail_token_source_file",
            "laf_gmail_token_source_sha256",
            VALIDATION_LAF_GMAIL_TOKEN_BYTES,
        ),
        (
            "file_review_token_source_file",
            "file_review_token_source_sha256",
            VALIDATION_LAF_GMAIL_TOKEN_BYTES,
        ),
    ):
        if bound_external(name, digest_name).read_bytes() != expected:
            raise IsolatedLiveBlocked(f"validation external input {name} is not inert")
    if (
        bound_external(
            "accounting_credentials_file", "accounting_credentials_sha256"
        ).read_bytes()
        != VALIDATION_GOOGLE_CREDENTIALS_BYTES
    ):
        raise IsolatedLiveBlocked("validation accounting credentials are not inert")
    for name, digest_name in (
        ("accounting_sheets_token_source_file", "accounting_sheets_token_source_sha256"),
        ("drive_sync_token_source_file", "drive_sync_token_source_sha256"),
        ("drive_sync_write_token_source_file", "drive_sync_write_token_source_sha256"),
    ):
        if bound_external(name, digest_name).read_bytes() != VALIDATION_GOOGLE_CALENDAR_TOKEN_BYTES:
            raise IsolatedLiveBlocked(f"validation external input {name} is not inert")
    ocr_queue = _canonical_file(
        Path(str(external.get("nas_ocr_queue_db_file") or "")),
        description="validation NAS OCR queue",
    )
    if not _path_inside(ocr_queue, validation_root):
        raise IsolatedLiveBlocked("validation NAS OCR queue escapes its sandbox")
    try:
        with sqlite3.connect(f"file:{ocr_queue}?mode=ro", uri=True) as connection:
            if connection.execute("PRAGMA quick_check").fetchone() != ("ok",):
                raise IsolatedLiveBlocked("validation NAS OCR queue quick_check failed")
    except sqlite3.Error as exc:
        raise IsolatedLiveBlocked(f"validation NAS OCR queue is invalid: {exc}") from exc
    website = _absolute_directory(external.get("website_root"), description="validation website root")
    if not _path_inside(website, validation_root):
        raise IsolatedLiveBlocked("validation website root escapes its sandbox")
    for candidate in website.rglob("*"):
        if candidate.is_symlink():
            raise IsolatedLiveBlocked("validation website tree contains a symlink")
    fixture = _canonical_file(
        website / "data" / "live-validation-document.txt",
        description="validation document fixture",
    )
    if not _path_inside(fixture, website) or fixture.stat().st_size > 1024 * 1024:
        raise IsolatedLiveBlocked("validation document fixture is unsafe")
    admin = _canonical_file(
        website / "admin" / "admin_server.py", description="validation admin fixture"
    )
    if _sha256_file(admin) != external.get("website_admin_sha256"):
        raise IsolatedLiveBlocked("validation admin fixture SHA-256 drift detected")
    cron_rendered = Path(str(external.get("cron_jobs_file", ""))).expanduser().resolve(strict=False)
    if not _path_inside(cron_rendered, deploy_root):
        raise IsolatedLiveBlocked("rendered validation cron snapshot escapes deployment")
    _verify_bound(
        BoundArtifact(cron_rendered, str(external.get("cron_jobs_sha256", ""))),
        description="rendered validation cron snapshot",
    )
    return validation_root, _sha256_file(fixture)


def _verify_role_plists(
    deploy: Mapping[str, Any],
    artifacts: Mapping[str, BoundArtifact],
    *,
    release_root: Path,
    runtime_root: Path,
    service_manifest: BoundArtifact,
    release_manifest: BoundArtifact,
    release_id: str,
    release_inventory: Mapping[str, str],
) -> tuple[ValidationRole, ...]:
    rows = deploy.get("roles")
    if not isinstance(rows, list) or len(rows) != 3:
        raise IsolatedLiveBlocked("validation deployment must contain exactly three roles")
    roles: dict[str, ValidationRole] = {}
    external = deploy.get("external_inputs")
    if not isinstance(external, dict):
        raise IsolatedLiveBlocked("validation external input bindings are missing")
    expected_named = named_mutable_state_paths(runtime_root)
    if any(external.get(name) != value for name, value in expected_named.items()):
        raise IsolatedLiveBlocked("validation named mutable-state binding drifted")
    if any(
        Path(value).resolve(strict=False).is_relative_to(release_root)
        for value in expected_named.values()
    ):
        raise IsolatedLiveBlocked("validation named mutable-state binding enters release")
    role_modules = {
        "control": "magi_v3.control",
        "gateway": "magi_v3.gateway",
        "supervisor": "magi_v3.supervisor_service",
    }
    for row in rows:
        if not isinstance(row, dict):
            raise IsolatedLiveBlocked("validation role binding is invalid")
        role, label = row.get("role"), row.get("label")
        if not isinstance(role, str) or ROLE_LABELS.get(role) != label or role in roles:
            raise IsolatedLiveBlocked("validation role or label binding is invalid")
        relative = f"launchagents/{label}.plist"
        binding = artifacts.get(relative)
        if binding is None:
            raise IsolatedLiveBlocked(f"validation plist is absent from artifact inventory: {label}")
        try:
            plist = plistlib.loads(_verify_bound(binding, description=f"validation plist {label}"))
        except plistlib.InvalidFileException as exc:
            raise IsolatedLiveBlocked(f"validation plist is invalid: {label}") from exc
        environment = plist.get("EnvironmentVariables")
        if not isinstance(environment, dict):
            raise IsolatedLiveBlocked(f"validation plist environment is invalid: {label}")
        required_environment = {
            "MAGI_V3_ROLE": role,
            "MAGI_V3_RELEASE_ID": release_id,
            "MAGI_V3_RELEASE_MANIFEST": str(release_manifest.path),
            "MAGI_V3_RELEASE_MANIFEST_SHA256": release_manifest.sha256,
            "MAGI_V3_DEPLOYMENT_MODE": DEPLOYMENT_MODE,
            "MAGI_V3_SERVICE_MANIFEST": str(service_manifest.path),
            "MAGI_V3_SERVICE_MANIFEST_SHA256": service_manifest.sha256,
            "MAGI_V3_LIVE_VALIDATION": "1",
            "MAGI_V3_EXTERNAL_WRITES_ENABLED": "0",
            "MAGI_V3_NOTIFICATIONS_ENABLED": "0",
            "MAGI_V3_SCHEDULER_ENABLED": "0",
            "MAGI_ENV_FILE": external.get("env_file"),
            "MAGI_ENV_FILE_SHA256": external.get("env_file_sha256"),
            "MAGI_CRON_JOBS_FILE": external.get("cron_jobs_file"),
            "MAGI_CRON_JOBS_SHA256": external.get("cron_jobs_sha256"),
            "MAGI_CRON_JOBS_SOURCE_SHA256": external.get("cron_jobs_source_sha256"),
            "MAGI_WEBSITE_ROOT": external.get("website_root"),
            "MAGI_LAF_CONFIG_FILE": external.get("laf_config_file"),
            "MAGI_LAF_CONFIG_SHA256": external.get("laf_config_sha256"),
            "MAGI_CONFIG_PATH": external.get("laf_config_file"),
            "MAGI_CONFIG_SHA256": external.get("laf_config_sha256"),
            "MAGI_CONFIG_MODE": external.get("laf_config_mode"),
            "MAGI_JSON_DIR": str(Path(str(external.get("laf_config_file"))).parent),
            "MAGI_GOOGLE_CREDENTIALS_PATH": external.get("google_credentials_file"),
            "MAGI_GOOGLE_CREDENTIALS_SHA256": external.get("google_credentials_sha256"),
            "MAGI_GOOGLE_CREDENTIALS_MODE": external.get("google_credentials_mode"),
            "MAGI_GMAIL_CREDENTIALS_PATH": external.get("google_credentials_file"),
            "MAGI_GOOGLE_CALENDAR_TOKEN_PATH": external.get("google_calendar_token_file"),
            "MAGI_LAF_GMAIL_TOKEN_PATH": external.get("laf_gmail_token_file"),
            "MAGI_FILE_REVIEW_TOKEN_PATH": external.get("file_review_token_file"),
            "MAGI_GMAIL_COMPOSE_TOKEN_PATH": external.get("gmail_compose_token_file"),
            "MAGI_ACCOUNTING_GOOGLE_CREDENTIALS_PATH": external.get(
                "accounting_credentials_file"
            ),
            "MAGI_ACCOUNTING_GOOGLE_CREDENTIALS_SHA256": external.get(
                "accounting_credentials_sha256"
            ),
            "MAGI_ACCOUNTING_GOOGLE_CREDENTIALS_MODE": external.get(
                "accounting_credentials_mode"
            ),
            "MAGI_ACCOUNTING_GOOGLE_SHEETS_TOKEN": external.get(
                "accounting_sheets_token_file"
            ),
            "MAGI_DRIVE_SYNC_CREDENTIALS_PATH": external.get("google_credentials_file"),
            "MAGI_DRIVE_SYNC_TOKEN": external.get("drive_sync_token_file"),
            "MAGI_DRIVE_SYNC_WRITE_TOKEN": external.get("drive_sync_write_token_file"),
            "MAGI_NAS_OCR_QUEUE_DB_PATH": external.get("nas_ocr_queue_db_file"),
            "MAGI_PUBLIC_SOURCE_ROOT_DIR": str(release_root),
            "OSC_CONFIG_PATH": external.get("laf_config_file"),
            **{
                env_name: external.get(binding_name)
                for env_name, (binding_name, _relative) in NAMED_MUTABLE_STATE_BINDINGS.items()
            },
        }
        if any(environment.get(name) != value for name, value in required_environment.items()):
            raise IsolatedLiveBlocked(f"validation plist safety binding drifted: {label}")
        if any(row.get(name) != value for name, value in expected_named.items()):
            raise IsolatedLiveBlocked(
                f"validation role named mutable-state binding drifted: {label}"
            )
        expected_arguments = row.get("ProgramArguments")
        if (
            not isinstance(expected_arguments, list)
            or len(expected_arguments) != 3
            or expected_arguments[1:] != ["-m", role_modules[role]]
            or not isinstance(expected_arguments[0], str)
        ):
            raise IsolatedLiveBlocked(f"validation role entrypoint is invalid: {label}")
        executable = Path(expected_arguments[0]).expanduser().resolve(strict=False)
        try:
            executable_relative = executable.relative_to(release_root).as_posix()
        except ValueError as exc:
            raise IsolatedLiveBlocked(f"validation role executable escapes release: {label}") from exc
        executable_digest = release_inventory.get(executable_relative)
        if executable_digest is None:
            raise IsolatedLiveBlocked(f"validation role executable is absent from release: {label}")
        _verify_bound(
            BoundArtifact(executable, executable_digest),
            description=f"validation role executable {label}",
        )
        if not os.access(executable, os.X_OK):
            raise IsolatedLiveBlocked(f"validation role executable is not executable: {label}")
        module_relative = role_modules[role].replace(".", "/") + ".py"
        module_digest = release_inventory.get(module_relative)
        if module_digest is None:
            raise IsolatedLiveBlocked(f"validation role module is absent from release: {role}")
        _verify_bound(
            BoundArtifact(release_root / module_relative, module_digest),
            description=f"validation role module {role}",
        )
        allowed_plist_keys = {
            "Label",
            "ProgramArguments",
            "WorkingDirectory",
            "EnvironmentVariables",
            "StandardOutPath",
            "StandardErrorPath",
            "RunAtLoad",
            "KeepAlive",
            "ProcessType",
        }
        if set(plist) - allowed_plist_keys:
            raise IsolatedLiveBlocked(f"validation plist has unapproved launch triggers: {label}")
        if (
            plist.get("Label") != label
            or plist.get("WorkingDirectory") != str(release_root)
            or plist.get("RunAtLoad") is not False
            or plist.get("KeepAlive") is not False
            or row.get("deployment_mode") != DEPLOYMENT_MODE
            or row.get("service_manifest") != str(service_manifest.path)
            or row.get("service_manifest_sha256") != service_manifest.sha256
            or row.get("runtime_root") != str(runtime_root)
            or row.get("release_id") != release_id
            or row.get("release_manifest") != str(release_manifest.path)
            or row.get("release_manifest_sha256") != release_manifest.sha256
            or plist.get("ProgramArguments") != row.get("ProgramArguments")
        ):
            raise IsolatedLiveBlocked(f"validation role/plist binding drifted: {label}")
        roles[role] = ValidationRole(role, label, binding)
    if set(roles) != set(START_ORDER):
        raise IsolatedLiveBlocked("validation deployment role set is incomplete")
    return tuple(roles[role] for role in START_ORDER)


def _offline_report_binding(value: Any, *, description: str) -> BoundArtifact:
    if not isinstance(value, dict) or set(value) != {"path", "sha256"}:
        raise IsolatedLiveBlocked(f"{description} binding is invalid")
    raw_path, digest = value.get("path"), value.get("sha256")
    if not isinstance(raw_path, str) or not isinstance(digest, str):
        raise IsolatedLiveBlocked(f"{description} binding is invalid")
    path = Path(raw_path).expanduser()
    if (
        not path.is_absolute()
        or path.resolve(strict=False) != path
        or not SHA256_RE.fullmatch(digest)
    ):
        raise IsolatedLiveBlocked(f"{description} binding is not canonical")
    return BoundArtifact(path, digest)


def _offline_gate_time(value: Any, *, description: str) -> datetime:
    if not isinstance(value, str):
        raise IsolatedLiveBlocked(f"{description} is invalid")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise IsolatedLiveBlocked(f"{description} is invalid") from exc
    if parsed.tzinfo is None:
        raise IsolatedLiveBlocked(f"{description} must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _verify_offline_gate(
    plan: IsolatedLivePlan,
    *,
    release: Mapping[str, Any],
    release_inventory: Mapping[str, str],
) -> dict[str, Any]:
    report = _json_object(
        _verify_bound(plan.offline_gate_report, description="offline machine gate report"),
        description="offline machine gate report",
    )
    context = {field: report.get(field) for field in OFFLINE_GATE_CONTEXT_FIELDS}
    if (
        any(not isinstance(value, str) or not value for value in context.values())
        or not SHA256_RE.fullmatch(str(context["release_sha"]))
        or not SHA256_RE.fullmatch(str(context["gate_config_sha256"]))
        or release.get("release_sha256") != context["release_sha"]
        or release.get("source_snapshot_sha256") != context["release_sha"]
        or release_inventory.get("config/v3_cutover_gates.json")
        != context["gate_config_sha256"]
    ):
        raise IsolatedLiveBlocked("offline machine gate candidate context is invalid")
    generated_at = _offline_gate_time(
        report.get("generated_at"), description="offline gate generated_at"
    )
    valid_until = _offline_gate_time(
        report.get("valid_until"), description="offline gate valid_until"
    )
    current = datetime.now(timezone.utc)
    if (
        report.get("schema_version") != 1
        or report.get("builder_schema") != OFFLINE_MACHINE_GATE_SCHEMA
        or report.get("status") != "GO"
        or report.get("deployment_mode") != DEPLOYMENT_MODE
        or report.get("release_manifest_sha256") != plan.release_manifest.sha256
        or report.get("deploy_manifest_sha256") != plan.deploy_manifest.sha256
        or report.get("deploy_prepared_marker_sha256")
        != plan.deploy_prepared_marker.sha256
        or report.get("unproven_gaps") != []
        or report.get("live_execution_performed") is not False
        or report.get("launchctl_invoked") is not False
        or generated_at > current + timedelta(minutes=5)
        or valid_until < generated_at
        or current > valid_until
    ):
        raise IsolatedLiveBlocked(
            "offline machine gate report is not a fresh, builder-bound clean GO"
        )
    runtime = report.get("candidate_runtime")
    if (
        not isinstance(runtime, dict)
        or set(runtime)
        != {"launcher_sha256", "python_runtime_sha256", "builder_source_sha256"}
        or runtime.get("launcher_sha256")
        != release_inventory.get("bin/magi-v3-python")
        or runtime.get("builder_source_sha256")
        != release_inventory.get(
            "scripts/v3_validation/offline_machine_gate_builder.py"
        )
        or not isinstance(runtime.get("python_runtime_sha256"), str)
        or not SHA256_RE.fullmatch(runtime["python_runtime_sha256"])
    ):
        raise IsolatedLiveBlocked("offline machine gate candidate runtime is not release-bound")
    source_reports = report.get("source_reports")
    if not isinstance(source_reports, dict) or set(source_reports) != {
        "compiler_summary",
        "release_gate_report",
    }:
        raise IsolatedLiveBlocked("offline machine gate source reports are incomplete")
    compiler_binding = _offline_report_binding(
        source_reports["compiler_summary"], description="evidence compiler summary"
    )
    gate_binding = _offline_report_binding(
        source_reports["release_gate_report"], description="release gate report"
    )
    compiler = _json_object(
        _verify_bound(compiler_binding, description="evidence compiler summary"),
        description="evidence compiler summary",
    )
    gate = _json_object(
        _verify_bound(gate_binding, description="release gate report"),
        description="release gate report",
    )
    emitted = compiler.get("emitted")
    gate_invalid = gate.get("invalid")
    # The full 28-item release gate is normally NO_GO before the first isolated
    # LIVE run.  This pre-LIVE gate accepts that overall decision only after it
    # independently proves that the exact 19 offline-machine members passed.
    if (
        compiler.get("schema_version") != 1
        or any(compiler.get(field) != context[field] for field in OFFLINE_GATE_CONTEXT_FIELDS)
        or compiler.get("normalizer") != "scripts.v3_evidence_compiler"
        or compiler.get("service_start_performed") is not False
        or compiler.get("live_state_accessed") is not False
        or not isinstance(emitted, dict)
        or gate.get("schema_version") != 1
        or gate.get("decision") not in {"GO", "NO_GO"}
        or gate.get("fail_closed") is not True
        or gate.get("expected_context") != context
        or gate.get("required_count") != 28
        or not isinstance(gate.get("passed"), list)
        or not isinstance(gate.get("missing"), list)
        or not isinstance(gate.get("failed"), list)
        or not isinstance(gate_invalid, dict)
    ):
        raise IsolatedLiveBlocked("offline machine code-owned source reports are invalid")
    evidence = report.get("evidence")
    if not isinstance(evidence, dict):
        raise IsolatedLiveBlocked("offline machine gate evidence is missing")
    forbidden = {
        "human_go_approval_recorded",
        "offline_replay_and_isolated_live_validation_satisfied",
        "isolated_live_validation_single_active_handoff_verified",
        "atomic_release_switch_and_cold_rollback_drill_passed",
    }
    declared_required = report.get("required_evidence")
    counts = report.get("counts")
    if (
        declared_required != sorted(OFFLINE_MACHINE_EVIDENCE)
        or set(evidence) != OFFLINE_MACHINE_EVIDENCE
        or forbidden.intersection(evidence)
        or counts
        != {"required": 19, "passed": 19, "failed": 0, "missing": 0, "invalid": 0}
        or not OFFLINE_MACHINE_EVIDENCE.issubset(set(gate["passed"]))
        or OFFLINE_MACHINE_EVIDENCE.intersection(gate["missing"])
        or OFFLINE_MACHINE_EVIDENCE.intersection(gate["failed"])
        or OFFLINE_MACHINE_EVIDENCE.intersection(gate_invalid)
    ):
        raise IsolatedLiveBlocked("offline gate scope contains future LIVE, cutover, or human evidence")
    evidence_root: Path | None = None
    failed: list[str] = []
    for evidence_id in sorted(OFFLINE_MACHINE_EVIDENCE):
        row = evidence.get(evidence_id)
        if (
            not isinstance(row, dict)
            or set(row) != {"status", "passed", "envelope", "artifacts", "errors"}
            or row.get("status") != "passed"
            or row.get("passed") is not True
            or row.get("errors") != []
            or emitted.get(evidence_id) != "passed"
        ):
            failed.append(evidence_id)
            continue
        envelope_binding = _offline_report_binding(
            row.get("envelope"), description=f"offline evidence envelope {evidence_id}"
        )
        if envelope_binding.path.name != f"{evidence_id}.json":
            failed.append(evidence_id)
            continue
        if evidence_root is None:
            evidence_root = envelope_binding.path.parent
        elif envelope_binding.path.parent != evidence_root:
            failed.append(evidence_id)
            continue
        envelope = _json_object(
            _verify_bound(
                envelope_binding, description=f"offline evidence envelope {evidence_id}"
            ),
            description=f"offline evidence envelope {evidence_id}",
        )
        declared_artifacts = envelope.get("artifacts")
        bound_artifacts = row.get("artifacts")
        if (
            envelope.get("schema_version") != 1
            or envelope.get("evidence_id") != evidence_id
            or envelope.get("status") != "passed"
            or any(envelope.get(field) != context[field] for field in OFFLINE_GATE_CONTEXT_FIELDS)
            or not isinstance(declared_artifacts, list)
            or not declared_artifacts
            or not isinstance(bound_artifacts, list)
            or len(bound_artifacts) != len(declared_artifacts)
        ):
            failed.append(evidence_id)
            continue
        expected_artifacts: list[tuple[str, str, str]] = []
        actual_artifacts: list[tuple[str, str, str]] = []
        artifact_error = False
        for artifact in declared_artifacts:
            if not isinstance(artifact, dict):
                artifact_error = True
                break
            relative, role, digest = (
                artifact.get("path"),
                artifact.get("role"),
                artifact.get("sha256"),
            )
            if (
                not isinstance(relative, str)
                or Path(relative).is_absolute()
                or ".." in Path(relative).parts
                or not isinstance(role, str)
                or not role
                or not isinstance(digest, str)
                or not SHA256_RE.fullmatch(digest)
            ):
                artifact_error = True
                break
            expected_artifacts.append((role, str(evidence_root / relative), digest))
        for artifact in bound_artifacts:
            if not isinstance(artifact, dict) or set(artifact) != {"role", "path", "sha256"}:
                artifact_error = True
                break
            try:
                binding = _offline_report_binding(
                    {"path": artifact.get("path"), "sha256": artifact.get("sha256")},
                    description=f"offline evidence artifact {evidence_id}",
                )
                _verify_bound(binding, description=f"offline evidence artifact {evidence_id}")
            except IsolatedLiveBlocked:
                artifact_error = True
                break
            actual_artifacts.append((str(artifact.get("role")), str(binding.path), binding.sha256))
        if artifact_error or actual_artifacts != expected_artifacts:
            failed.append(evidence_id)
    if failed:
        raise IsolatedLiveBlocked(f"offline machine hard gates are not proven: {failed}")
    return report


def verify_static_plan(
    plan: IsolatedLivePlan, *, require_offline_machine_gate: bool = True
) -> VerifiedDeployment:
    """Recompute immutable deployment bindings from the raw artifacts.

    Ordinary isolated LIVE always uses the default and therefore requires the
    complete 19/19 offline gate.  The provisional resource-window owner may
    set ``require_offline_machine_gate=False`` only after it has independently
    verified the circular-dependency-breaking 16/19 outer gate.
    """

    release = _json_object(
        _verify_bound(plan.release_manifest, description="release manifest"),
        description="release manifest",
    )
    if release.get("schema_version") != 1 or release.get("immutable") is not True:
        raise IsolatedLiveBlocked("release manifest is not immutable schema version 1")
    release_id = release.get("release_id")
    if not isinstance(release_id, str) or not PLAN_ID_RE.fullmatch(release_id):
        raise IsolatedLiveBlocked("release manifest release_id is invalid")
    release_root = plan.release_manifest.path.parent
    inventory = _release_inventory(release)
    release_marker_path = release_root / "RELEASE_COMPLETE.json"
    release_marker = _json_object(
        _verify_bound(
            BoundArtifact(
                release_marker_path,
                _sha256_file(
                    _canonical_file(
                        release_marker_path,
                        description="release completion marker",
                    )
                ),
            ),
            description="release completion marker",
        ),
        description="release completion marker",
    )
    if (
        release_marker.get("schema_version") != 1
        or release_marker.get("release_id") != release_id
        or release_marker.get("manifest") != "release-manifest.json"
        or release_marker.get("manifest_sha256") != plan.release_manifest.sha256
    ):
        raise IsolatedLiveBlocked("release completion marker does not bind the immutable manifest")

    deploy = _json_object(
        _verify_bound(plan.deploy_manifest, description="deploy manifest"),
        description="deploy manifest",
    )
    marker = _json_object(
        _verify_bound(plan.deploy_prepared_marker, description="deploy prepared marker"),
        description="deploy prepared marker",
    )
    deploy_root = plan.deploy_manifest.path.parent
    if (
        plan.deploy_manifest.path.name != "deploy-manifest.json"
        or plan.deploy_prepared_marker.path != deploy_root / "DEPLOY_PREPARED.json"
    ):
        raise IsolatedLiveBlocked("deployment manifest and prepared marker are not canonical siblings")
    if (
        deploy.get("schema_version") != 1
        or deploy.get("status") != "prepared_not_installed"
        or deploy.get("mutation_performed") is not False
        or deploy.get("deployment_mode") != DEPLOYMENT_MODE
        or deploy.get("release_id") != release_id
        or deploy.get("release_manifest") != str(plan.release_manifest.path)
        or deploy.get("release_manifest_sha256") != plan.release_manifest.sha256
    ):
        raise IsolatedLiveBlocked("deploy manifest is not an exact isolated validation binding")
    if (
        marker.get("schema_version") != 1
        or marker.get("status") != "prepared_not_installed"
        or marker.get("ready_to_install") is not True
        or marker.get("mutation_performed") is not False
        or marker.get("deployment_mode") != DEPLOYMENT_MODE
        or marker.get("release_id") != release_id
        or marker.get("release_manifest_sha256") != plan.release_manifest.sha256
        or marker.get("manifest") != "deploy-manifest.json"
        or marker.get("manifest_sha256") != plan.deploy_manifest.sha256
    ):
        raise IsolatedLiveBlocked("deploy prepared marker is not an exact isolated validation binding")
    service_path, service_digest = deploy.get("service_manifest"), deploy.get("service_manifest_sha256")
    if not isinstance(service_path, str) or not isinstance(service_digest, str):
        raise IsolatedLiveBlocked("deploy service manifest binding is invalid")
    service_manifest = _verify_service_manifest(
        Path(service_path), service_digest, release_root=release_root, inventory=inventory
    )
    runtime_raw = deploy.get("runtime_root")
    if not isinstance(runtime_raw, str) or not Path(runtime_raw).is_absolute():
        raise IsolatedLiveBlocked("validation runtime root is invalid")
    runtime_root = Path(runtime_raw).expanduser().resolve(strict=False)
    if any(
        _path_inside(runtime_root, root) or _path_inside(root, runtime_root)
        for root in (release_root, deploy_root)
    ):
        raise IsolatedLiveBlocked("validation runtime overlaps immutable artifacts")
    artifacts = _artifact_inventory(deploy_root, deploy)
    ownership_digest = deploy.get("ownership_manifest_sha256")
    ownership = artifacts.get("ownership/ownership-manifest.json")
    if ownership is None or ownership.sha256 != ownership_digest:
        raise IsolatedLiveBlocked("validation ownership manifest is not artifact-bound")
    ownership_payload = _json_object(
        _verify_bound(ownership, description="prepared ownership manifest"),
        description="prepared ownership manifest",
    )
    if (
        ownership_payload.get("deployment_mode") != DEPLOYMENT_MODE
        or ownership_payload.get("release_id") != release_id
        or ownership_payload.get("runtime_root") != str(runtime_root)
        or ownership_payload.get("service_manifest") != str(service_manifest.path)
        or ownership_payload.get("service_manifest_sha256") != service_manifest.sha256
    ):
        raise IsolatedLiveBlocked("prepared ownership manifest safety binding drifted")
    ownership_external = ownership_payload.get("external_inputs")
    ownership_roles = ownership_payload.get("roles")
    expected_named = named_mutable_state_paths(runtime_root)
    if (
        not isinstance(ownership_external, dict)
        or any(ownership_external.get(name) != value for name, value in expected_named.items())
        or not isinstance(ownership_roles, list)
        or len(ownership_roles) != 3
        or {
            row.get("label") for row in ownership_roles if isinstance(row, dict)
        }
        != set(ROLE_LABELS.values())
        or any(
            not isinstance(row, dict)
            or any(row.get(name) != value for name, value in expected_named.items())
            for row in ownership_roles
        )
    ):
        raise IsolatedLiveBlocked("prepared ownership named mutable-state binding drifted")
    validation_root, fixture_sha256 = _verify_validation_inputs(
        deploy,
        deploy_root=deploy_root,
        release_root=release_root,
        runtime_root=runtime_root,
    )
    roles = _verify_role_plists(
        deploy,
        artifacts,
        release_root=release_root,
        runtime_root=runtime_root,
        service_manifest=service_manifest,
        release_manifest=plan.release_manifest,
        release_id=release_id,
        release_inventory=inventory,
    )
    if require_offline_machine_gate:
        _verify_offline_gate(plan, release=release, release_inventory=inventory)
    return VerifiedDeployment(
        release_id=release_id,
        release_root=release_root,
        deployment_root=deploy_root,
        runtime_root=runtime_root,
        validation_input_root=validation_root,
        service_manifest=service_manifest,
        ownership_manifest=ownership,
        roles=roles,
        fixture_sha256=fixture_sha256,
        payload=deploy,
    )


def _consume_token(path: Path, expected_sha256: str) -> None:
    raw = path.expanduser()
    if not raw.is_absolute() or raw.resolve(strict=False) != raw or raw.is_symlink():
        raise IsolatedLiveBlocked("one-time token must be a canonical absolute non-symlink file")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(raw, flags)
    except OSError as exc:
        raise IsolatedLiveBlocked(f"one-time token is unavailable: {exc}") from exc
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_uid != os.getuid()
            or metadata.st_nlink != 1
        ):
            raise IsolatedLiveBlocked("one-time token must be owner-only 0600 with one link")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            token = handle.read(4097)
        if len(token) > 4096:
            raise IsolatedLiveBlocked("one-time token is too large")
        token = token.rstrip(b"\r\n")
        if not token or not hmac.compare_digest(_sha256_bytes(token), expected_sha256):
            raise IsolatedLiveBlocked("one-time token does not match the hash-bound plan")
        current = raw.lstat()
        if (metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns) != (
            current.st_dev,
            current.st_ino,
            current.st_size,
            current.st_mtime_ns,
        ):
            raise IsolatedLiveBlocked("one-time token changed while being consumed")
        raw.unlink()
        directory = os.open(raw.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        os.close(descriptor)


def _safe_detail(value: Any) -> Any:
    """Keep receipts useful without copying secrets or response bodies."""

    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, child in value.items():
            lowered = str(key).lower()
            if any(term in lowered for term in ("token", "secret", "authorization", "cookie")):
                continue
            if lowered in {"body", "body_bytes", "raw_body"}:
                continue
            result[str(key)] = _safe_detail(child)
        return result
    if isinstance(value, (list, tuple)):
        return [_safe_detail(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return repr(value)


def _snapshot_summary(snapshot: Snapshot, expected: str) -> dict[str, Any]:
    assessment = assess_snapshot(snapshot, expected=expected)  # type: ignore[arg-type]
    raw = json.dumps(snapshot.to_dict(), sort_keys=True, separators=(",", ":")).encode()
    return {
        "snapshot_sha256": _sha256_bytes(raw),
        "observed_at": snapshot.observed_at,
        "owner_count": len(snapshot.owners),
        "coverage": sorted(snapshot.coverage),
        "probe_error_count": len(snapshot.probe_errors),
        "expected": expected,
        "assessment": assessment.to_dict(),
    }


def _require_receipt_ok(receipt: Mapping[str, Any], *, action: str) -> None:
    if receipt.get("ok") is not True:
        raise IsolatedLiveBlocked(f"machine action did not prove success: {action}")


class IsolatedLiveExecutor:
    """Run one armed validation and always return to V2 or report a hard block."""

    def __init__(
        self,
        plan: IsolatedLivePlan,
        *,
        token_file: Path,
        machine: IsolatedLiveMachine,
        clock: Clock | None = None,
    ) -> None:
        self.plan = plan
        self.token_file = token_file
        self.machine = machine
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.events: list[dict[str, Any]] = []
        self.deployment: VerifiedDeployment | None = None
        self.blackout_active = False
        self.v2_stop_attempted = False
        self.installed = False
        self.started_roles: list[ValidationRole] = []

    def _event(self, action: str, *, ok: bool = True, **detail: Any) -> None:
        self.events.append(
            {
                "sequence": len(self.events) + 1,
                "at": self.clock().astimezone(timezone.utc).isoformat(),
                "action": action,
                "ok": ok,
                "detail": _safe_detail(detail),
            }
        )

    def _window(self, deployment: VerifiedDeployment) -> None:
        result = _release_cutover_window(self.plan, deployment, now=self.clock())
        self._event("verify_validation_window", ok=bool(result["within_window"]), window=result)
        if result["within_window"] is not True:
            raise IsolatedLiveBlocked("outside isolated LIVE validation window")

    def _snapshot(self, expected: str) -> dict[str, Any]:
        snapshot = self.machine.collect_ownership_snapshot()
        summary = _snapshot_summary(snapshot, expected)
        self._event("ownership_snapshot", ok=bool(summary["assessment"]["go"]), snapshot=summary)
        if summary["assessment"]["go"] is not True:
            raise IsolatedLiveBlocked(
                f"single-active ownership did not prove {expected}: {summary['assessment']['reasons']}"
            )
        return summary

    def _install(self, deployment: VerifiedDeployment) -> None:
        receipt = self.machine.install_validation(deployment)
        _require_receipt_ok(receipt, action="install_validation")
        expected_plists = {role.label: role.plist.sha256 for role in deployment.roles}
        if (
            receipt.get("deployment_mode") != DEPLOYMENT_MODE
            or receipt.get("ownership_manifest_sha256") != deployment.ownership_manifest.sha256
            or receipt.get("plist_sha256") != expected_plists
        ):
            raise IsolatedLiveBlocked("validation install receipt is not hash-bound to the deployment")
        self.installed = True
        self._event("install_validation", receipt=receipt)

    def _probe(self, probe: ProbeSpec, deployment: VerifiedDeployment) -> None:
        port, path = _validate_probe(probe)
        receipt = self.machine.probe(probe)
        _require_receipt_ok(receipt, action=f"probe {probe.method} {probe.url}")
        if receipt.get("status_code") != 200:
            raise IsolatedLiveBlocked(f"validation probe returned non-200: {probe.url}")
        payload = receipt.get("json")
        if path == "/validation/ping":
            if not isinstance(payload, dict) or payload.get("status") != "ok" or payload.get("mode") != DEPLOYMENT_MODE:
                raise IsolatedLiveBlocked("validation ping did not attest isolated mode")
            headers = receipt.get("headers")
            if not isinstance(headers, dict) or headers.get("X-MAGI-Validation-Mode") != DEPLOYMENT_MODE:
                raise IsolatedLiveBlocked("validation ping omitted its safety-mode header")
        elif path in {"/livez", "/readyz", "/health"}:
            if not isinstance(payload, dict) or payload.get("ready") is not True:
                raise IsolatedLiveBlocked(f"validation health probe is not ready: {port}{path}")
        elif path in DOCUMENT_PATHS:
            if probe.method == "GET" and receipt.get("body_sha256") != deployment.fixture_sha256:
                raise IsolatedLiveBlocked("validation document probe did not return the sandbox fixture")
            headers = receipt.get("headers")
            disposition = headers.get("Content-Disposition", "") if isinstance(headers, dict) else ""
            required = "attachment" if path.endswith("download") else "inline"
            if not isinstance(disposition, str) or not disposition.startswith(required):
                raise IsolatedLiveBlocked("validation document disposition is invalid")
        self._event(
            "read_only_probe",
            probe=probe.to_dict(),
            response={
                "status_code": receipt.get("status_code"),
                "headers": receipt.get("headers"),
                "body_sha256": receipt.get("body_sha256"),
                "json": payload,
            },
        )

    def _native_ime_probe(self, deployment: VerifiedDeployment) -> None:
        receipt = self.machine.run_native_ime_candidate_probe(deployment)
        _require_receipt_ok(receipt, action="native IME candidate-window probe")
        evidence = receipt.get("evidence")
        if not isinstance(evidence, dict):
            raise IsolatedLiveBlocked("native IME receipt omitted its raw evidence")
        measurements = evidence.get("measurements")
        observations = evidence.get("observations")
        if (
            evidence.get("schema_version") != 1
            or evidence.get("workload") != "ime_candidate_window_pressure_probe"
            or evidence.get("probe")
            != "native_mcbopomofo_candidate_window_pressure"
            or evidence.get("status") != "passed"
            or not isinstance(measurements, dict)
            or type(measurements.get("cycles_completed")) is not int
            or measurements["cycles_completed"] < 1
            or measurements.get("candidate_window_failures") != 0
            or type(measurements.get("pressure_touched_bytes")) is not int
            or measurements["pressure_touched_bytes"] <= 0
            or not isinstance(observations, list)
            or len(observations) != measurements["cycles_completed"]
            or evidence.get("unsaved_document_cleanup_performed") is not True
            or evidence.get("unsaved_documents_remaining") != 0
            or evidence.get("input_source_restored") is not True
            or evidence.get("frontmost_application_restored") is not True
            or evidence.get("textedit_state_restored") is not True
            or evidence.get("external_write_performed") is not False
            or evidence.get("network_access_performed") is not False
            or receipt.get("candidate_release_id") != deployment.release_id
            or receipt.get("candidate_launcher_verified") is not True
            or receipt.get("candidate_probe_source_verified") is not True
        ):
            raise IsolatedLiveBlocked("native IME candidate-window evidence is incomplete")
        digest = hashlib.sha256(_canonical_json_bytes(evidence)).hexdigest()
        if receipt.get("evidence_sha256") != digest:
            raise IsolatedLiveBlocked("native IME evidence digest does not match its receipt")
        for index, observation in enumerate(observations, start=1):
            new_windows = observation.get("new_candidate_windows") if isinstance(observation, dict) else None
            if (
                not isinstance(observation, dict)
                or observation.get("cycle") != index
                or observation.get("detected") is not True
                or not isinstance(new_windows, list)
                or not new_windows
                or any(
                    not isinstance(window, dict)
                    or type(window.get("window_id")) is not int
                    or window["window_id"] <= 0
                    for window in new_windows
                )
            ):
                raise IsolatedLiveBlocked(
                    "native IME probe did not prove a newly displayed candidate window"
                )
        self._event(
            "native_ime_candidate_window_probe",
            receipt={
                key: value
                for key, value in receipt.items()
                if key not in {"ok"}
            },
        )

    def _restore(self) -> tuple[bool, str, bool]:
        """Best-effort cleanup, but never start V2 while any V3 owner remains."""

        errors: list[str] = []
        incidents: list[str] = []
        deployment = self.deployment
        if deployment is not None:
            roles = {role.role: role for role in deployment.roles}
            for role_name in STOP_ORDER:
                role = roles[role_name]
                stopped = False
                last_error = ""
                for attempt in (1, 2):
                    try:
                        receipt = self.machine.stop_v3_role(role)
                        _require_receipt_ok(receipt, action=f"stop_v3_{role_name}")
                        self._event(
                            "stop_v3_role", role=role_name, attempt=attempt, receipt=receipt
                        )
                        stopped = True
                        break
                    except BaseException as exc:
                        last_error = str(exc)
                        incidents.append(f"stop {role_name} attempt {attempt}: {last_error}")
                        self._event(
                            "stop_v3_role",
                            ok=False,
                            role=role_name,
                            attempt=attempt,
                            error=last_error,
                        )
                if not stopped:
                    errors.append(f"stop {role_name}: {last_error}")
        zero_proven = False
        v2_already_active = False
        try:
            snapshot = self.machine.collect_ownership_snapshot()
            zero_summary = _snapshot_summary(snapshot, "zero")
            v2_summary = _snapshot_summary(snapshot, "v2")
            zero_proven = bool(zero_summary["assessment"]["go"])
            v2_already_active = bool(v2_summary["assessment"]["go"])
            selected = v2_summary if v2_already_active else zero_summary
            self._event(
                "ownership_snapshot",
                ok=zero_proven or v2_already_active,
                snapshot=selected,
                accepted_state="v2" if v2_already_active else "zero",
            )
            if not (zero_proven or v2_already_active):
                errors.append(
                    "safe rollback ownership not proven: "
                    + str(zero_summary["assessment"]["reasons"])
                )
        except BaseException as exc:
            errors.append(f"rollback ownership probe: {exc}")
        if (zero_proven or v2_already_active) and deployment is not None:
            removed = False
            last_error = ""
            for attempt in (1, 2):
                try:
                    receipt = self.machine.remove_validation(deployment)
                    _require_receipt_ok(receipt, action="remove_validation")
                    if (
                        receipt.get("validation_artifacts_removed") is not True
                        or receipt.get("runtime_ownership_removed") is not True
                        or receipt.get("remaining_validation_artifacts") != 0
                    ):
                        raise IsolatedLiveBlocked(
                            "validation removal receipt did not prove complete cleanup"
                        )
                    self._event("remove_validation", attempt=attempt, receipt=receipt)
                    self.installed = False
                    removed = True
                    break
                except BaseException as exc:
                    last_error = str(exc)
                    incidents.append(f"remove validation attempt {attempt}: {last_error}")
                    self._event(
                        "remove_validation", ok=False, attempt=attempt, error=last_error
                    )
            if not removed:
                errors.append(f"remove validation: {last_error}")

        v2_restored = False
        if zero_proven:
            try:
                receipt = self.machine.restore_v2()
                _require_receipt_ok(receipt, action="restore_v2")
                self._event("restore_v2", receipt=receipt)
                self._snapshot("v2")
                integrity = self.machine.verify_v2_readiness_integrity()
                _require_receipt_ok(integrity, action="verify_v2_readiness_integrity")
                if integrity.get("ready") is not True or integrity.get("integrity_ok") is not True:
                    raise IsolatedLiveBlocked("V2 readiness/integrity receipt is incomplete")
                self._event("verify_v2_readiness_integrity", receipt=integrity)
                v2_restored = True
            except BaseException as exc:
                errors.append(f"restore V2: {exc}")
                self._event("restore_v2", ok=False, error=str(exc))
        elif v2_already_active:
            try:
                integrity = self.machine.verify_v2_readiness_integrity()
                _require_receipt_ok(integrity, action="verify_v2_readiness_integrity")
                if integrity.get("ready") is not True or integrity.get("integrity_ok") is not True:
                    raise IsolatedLiveBlocked("V2 readiness/integrity receipt is incomplete")
                self._event(
                    "verify_v2_readiness_integrity",
                    receipt=integrity,
                    detail="V2 remained active",
                )
                v2_restored = True
            except BaseException as exc:
                incidents.append(f"verify partially active V2: {exc}")
                self._event("verify_v2_readiness_integrity", ok=False, error=str(exc))
                try:
                    receipt = self.machine.restore_v2()
                    _require_receipt_ok(receipt, action="restore_partial_v2")
                    self._event("restore_v2", receipt=receipt, detail="repair partial V2")
                    self._snapshot("v2")
                    integrity = self.machine.verify_v2_readiness_integrity()
                    _require_receipt_ok(integrity, action="verify_repaired_v2")
                    if (
                        integrity.get("ready") is not True
                        or integrity.get("integrity_ok") is not True
                    ):
                        raise IsolatedLiveBlocked(
                            "repaired V2 readiness/integrity receipt is incomplete"
                        )
                    self._event(
                        "verify_v2_readiness_integrity",
                        receipt=integrity,
                        detail="partial V2 repaired",
                    )
                    v2_restored = True
                except BaseException as repair_exc:
                    errors.append(f"repair partial V2: {repair_exc}")
                    self._event("restore_v2", ok=False, error=str(repair_exc))
        else:
            errors.append("V2 restore suppressed because zero ownership was not proven")

        if self.blackout_active:
            try:
                receipt = self.machine.deactivate_maintenance_blackout()
                _require_receipt_ok(receipt, action="deactivate_maintenance_blackout")
                if receipt.get("active") is not False:
                    raise IsolatedLiveBlocked("maintenance blackout remained active after cleanup")
                self._event("deactivate_maintenance_blackout", receipt=receipt)
                self.blackout_active = False
            except BaseException as exc:
                errors.append(f"deactivate blackout: {exc}")
                self._event("deactivate_maintenance_blackout", ok=False, error=str(exc))
        detail = "; ".join([*incidents, *errors])
        return not incidents and not errors and v2_restored, detail, v2_restored

    def execute(self) -> dict[str, Any]:
        started_at = self.clock().astimezone(timezone.utc).isoformat()
        primary_error = ""
        probes_completed = 0
        try:
            deployment = verify_static_plan(self.plan)
            self.deployment = deployment
            self._window(deployment)
            self._event(
                "verify_static_artifacts",
                release_id=deployment.release_id,
                release_manifest_sha256=self.plan.release_manifest.sha256,
                deploy_manifest_sha256=self.plan.deploy_manifest.sha256,
                service_manifest_sha256=deployment.service_manifest.sha256,
                offline_gate_report_sha256=self.plan.offline_gate_report.sha256,
            )
            self._snapshot("v2")
            _consume_token(self.token_file, self.plan.token_sha256)
            self._event("consume_one_time_token", token_sha256=self.plan.token_sha256)
            # Treat activation as potentially state-changing even if the adapter
            # raises; rollback must then verify V2 and attempt blackout removal.
            self.blackout_active = True
            blackout = self.machine.activate_maintenance_blackout()
            _require_receipt_ok(blackout, action="activate_maintenance_blackout")
            if (
                blackout.get("active") is not True
                or sorted(blackout.get("blocked_priorities", [])) != ["P2", "P3", "P4"]
                or blackout.get("portal_write_and_destructive_catchup") is not False
            ):
                raise IsolatedLiveBlocked("maintenance blackout receipt is incomplete")
            self._event("activate_maintenance_blackout", receipt=blackout)

            self._window(deployment)
            self.v2_stop_attempted = True
            stopped = self.machine.stop_v2()
            _require_receipt_ok(stopped, action="stop_v2")
            self._event("stop_v2", receipt=stopped)
            self._snapshot("zero")

            # Recompute hashes after V2 is stopped to close the preflight-to-install gap.
            deployment = verify_static_plan(self.plan)
            self.deployment = deployment
            self._snapshot("zero")
            self._install(deployment)
            for role in deployment.roles:
                self._window(deployment)
                _verify_bound(role.plist, description=f"validation plist before start: {role.label}")
                receipt = self.machine.start_v3_role(role)
                _require_receipt_ok(receipt, action=f"start_v3_{role.role}")
                if receipt.get("role") != role.role or receipt.get("label") != role.label:
                    raise IsolatedLiveBlocked("V3 role start receipt does not match requested role")
                self.started_roles.append(role)
                self._event("start_v3_role", role=role.role, receipt=receipt)
            self._snapshot("v3")

            for probe in self.plan.probes:
                self._window(deployment)
                self._probe(probe, deployment)
                probes_completed += 1

            # This physical UI observation is intentionally inside the
            # zero-overlap V3 interval.  Snapshots on both sides prove it did
            # not run against V2 or an inactive candidate.
            self._snapshot("v3")
            self._window(deployment)
            self._native_ime_probe(deployment)
            self._snapshot("v3")

            restored, restore_detail, v2_restored = self._restore()
            ok = restored and probes_completed == len(self.plan.probes)
            return self._report(
                started_at=started_at,
                ok=ok,
                status=(
                    "validation_passed_v2_restored"
                    if ok
                    else (
                        "validation_failed_v2_restored"
                        if v2_restored
                        else "blocked_v2_restore_failed"
                    )
                ),
                primary_error="" if ok else restore_detail,
                rollback_detail=restore_detail,
                v2_restored=v2_restored,
                probes_completed=probes_completed,
            )
        except BaseException as exc:
            primary_error = str(exc)
            self._event("validation_failure", ok=False, error=primary_error)
            if self.v2_stop_attempted or self.blackout_active:
                _restored_cleanly, restore_detail, v2_restored = self._restore()
                status = (
                    "validation_failed_v2_restored"
                    if v2_restored
                    else "blocked_v2_restore_failed"
                )
            else:
                restore_detail, v2_restored = "handoff not started", False
                status = "preflight_blocked"
            return self._report(
                started_at=started_at,
                ok=False,
                status=status,
                primary_error=primary_error,
                rollback_detail=restore_detail,
                v2_restored=v2_restored,
                probes_completed=probes_completed,
            )

    def _report(
        self,
        *,
        started_at: str,
        ok: bool,
        status: str,
        primary_error: str,
        rollback_detail: str,
        v2_restored: bool,
        probes_completed: int,
    ) -> dict[str, Any]:
        deployment = self.deployment
        return {
            "schema_version": 1,
            "report_id": f"isolated-live-{uuid.uuid4().hex}",
            "report_kind": "isolated_live_validation_execution",
            "final_cutover": False,
            "status": status,
            "ok": ok,
            "mutation_performed": self.v2_stop_attempted,
            "v2_restored": v2_restored,
            "started_at": started_at,
            "finished_at": self.clock().astimezone(timezone.utc).isoformat(),
            "plan_id": self.plan.plan_id,
            "hash_context": {
                "plan_sha256": self.plan.plan_sha256,
                "release_manifest_sha256": self.plan.release_manifest.sha256,
                "deploy_manifest_sha256": self.plan.deploy_manifest.sha256,
                "deploy_prepared_marker_sha256": self.plan.deploy_prepared_marker.sha256,
                "offline_gate_report_sha256": self.plan.offline_gate_report.sha256,
                "service_manifest_sha256": (
                    deployment.service_manifest.sha256 if deployment is not None else None
                ),
            },
            "deployment_mode": DEPLOYMENT_MODE,
            "probes_planned": len(self.plan.probes),
            "probes_completed": probes_completed,
            "primary_error": primary_error,
            "rollback_detail": rollback_detail,
            "events": self.events,
        }


def _reserve_report_output(path: Path) -> BoundArtifact:
    raw = path.expanduser()
    if not raw.is_absolute() or raw.resolve(strict=False) != raw or raw.is_symlink():
        raise IsolatedLiveBlocked("report output must be a canonical absolute non-symlink path")
    if raw.exists():
        raise IsolatedLiveBlocked("report output already exists")
    raw.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    reservation = (
        json.dumps(
            {
                "schema_version": 1,
                "status": "execution_reserved_not_a_result",
                "reservation_id": uuid.uuid4().hex,
            },
            sort_keys=True,
        )
        + "\n"
    ).encode()
    descriptor = os.open(
        raw,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(reservation)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(descriptor)
    directory = os.open(raw.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    return BoundArtifact(raw, _sha256_bytes(reservation))


def _finalize_report(reservation: BoundArtifact, report: Mapping[str, Any]) -> None:
    _verify_bound(reservation, description="isolated LIVE report reservation")
    raw = reservation.path
    data = (json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
    temporary = raw.parent / f".{raw.name}.final-{os.getpid()}-{uuid.uuid4().hex}"
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o400,
    )
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(descriptor)
    try:
        _verify_bound(reservation, description="isolated LIVE report reservation")
        os.replace(temporary, raw)
    finally:
        temporary.unlink(missing_ok=True)
    directory = os.open(raw.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def execute_isolated_live_validation(
    *,
    plan_path: Path,
    plan_sha256: str,
    token_file: Path,
    report_output: Path,
    machine: IsolatedLiveMachine,
    clock: Clock | None = None,
) -> dict[str, Any]:
    """Execute and persist one report.  No default live adapter is provided."""

    plan = load_isolated_live_plan(plan_path, plan_sha256)
    # Refuse a colliding report path before consuming the token or touching the
    # host.  The reservation also leaves a durable crash breadcrumb.
    reservation = _reserve_report_output(report_output)
    report = IsolatedLiveExecutor(
        plan,
        token_file=token_file,
        machine=machine,
        clock=clock,
    ).execute()
    _finalize_report(reservation, report)
    return report


__all__ = [
    "ALLOWED_PROBE_TARGETS",
    "DEPLOYMENT_MODE",
    "IsolatedLiveBlocked",
    "IsolatedLiveExecutor",
    "IsolatedLiveMachine",
    "IsolatedLivePlan",
    "OFFLINE_MACHINE_EVIDENCE",
    "ProbeSpec",
    "REQUIRED_PROBE_TARGETS",
    "START_ORDER",
    "STOP_ORDER",
    "ValidationRole",
    "VerifiedDeployment",
    "execute_isolated_live_validation",
    "load_isolated_live_plan",
    "verify_static_plan",
]
