#!/usr/bin/env python3
"""Build and verify exact-context human approval for the final V3 cutover."""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import re
import secrets
import stat
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from zoneinfo import ZoneInfo


EVIDENCE_ID = "human_go_approval_recorded"
REQUEST_SCHEMA = "magi.v3.human-approval-request/v1"
RECEIPT_SCHEMA = "magi.v3.human-approval-receipt/v1"
CONDITIONAL_REQUEST_SCHEMA = "magi.v3.conditional-human-approval-request/v2"
CONDITIONAL_RECEIPT_SCHEMA = "magi.v3.conditional-human-approval-receipt/v2"
CONSUMPTION_SCHEMA = "magi.v3.conditional-human-approval-consumption/v2"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
CONTEXT_FIELDS = ("campaign_id", "release_sha", "hardware_id", "gate_config_sha256")
AUTHORIZED_LOCAL_OWNERS = frozenset({(501, "ai")})


class HumanApprovalBlocked(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class FrozenSource:
    role: str
    path: Path
    data: bytes
    sha256: str


def _canonical(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_bytes(data: bytes, description: str) -> dict[str, Any]:
    try:
        value = json.loads(data)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise HumanApprovalBlocked(f"{description} is invalid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise HumanApprovalBlocked(f"{description} must be a JSON object")
    return value


def _freeze(path: Path, role: str) -> FrozenSource:
    raw = path.expanduser()
    if not raw.is_absolute() or raw.resolve(strict=False) != raw or raw.is_symlink():
        raise HumanApprovalBlocked(f"{role} path is unsafe")
    descriptor = os.open(raw, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise HumanApprovalBlocked(f"{role} is not a one-link regular file")
        chunks = []
        while chunk := os.read(descriptor, 1024 * 1024):
            chunks.append(chunk)
        data = b"".join(chunks)
        after = os.fstat(descriptor)
        current = raw.lstat()
        signature = lambda row: (
            row.st_dev, row.st_ino, row.st_size, row.st_mtime_ns, row.st_nlink
        )
        if signature(before) != signature(after) or signature(after) != signature(current):
            raise HumanApprovalBlocked(f"{role} changed while frozen")
    finally:
        os.close(descriptor)
    return FrozenSource(role, raw, data, hashlib.sha256(data).hexdigest())


def _safe_artifact(root: Path, relative_value: Any) -> Path:
    if not isinstance(relative_value, str) or not relative_value:
        raise HumanApprovalBlocked("machine evidence artifact path is missing")
    relative = Path(relative_value)
    if relative.is_absolute() or ".." in relative.parts:
        raise HumanApprovalBlocked("machine evidence artifact escapes its evidence root")
    target = root / relative
    if target.is_symlink() or not target.is_file():
        raise HumanApprovalBlocked("machine evidence artifact is unavailable or symlinked")
    target.resolve(strict=True).relative_to(root.resolve(strict=True))
    return target.resolve(strict=True)


def _time(value: Any, description: str) -> datetime:
    if not isinstance(value, str):
        raise HumanApprovalBlocked(f"{description} is missing")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HumanApprovalBlocked(f"{description} is invalid") from exc
    if result.tzinfo is None:
        raise HumanApprovalBlocked(f"{description} lacks a timezone")
    return result.astimezone(timezone.utc)


def _write_new(path: Path, payload: Mapping[str, Any], mode: int = 0o400) -> str:
    raw = path.expanduser()
    if not raw.is_absolute() or raw.resolve(strict=False) != raw or raw.is_symlink():
        raise HumanApprovalBlocked("approval output path must be canonical and absolute")
    raw.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    data = _canonical(dict(payload))
    descriptor = os.open(
        raw,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        mode,
    )
    try:
        os.write(descriptor, data)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    directory = os.open(raw.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    return hashlib.sha256(data).hexdigest()


def _context(value: Mapping[str, Any]) -> dict[str, str]:
    result = {field: value.get(field) for field in CONTEXT_FIELDS}
    if (
        any(not isinstance(item, str) or not item for item in result.values())
        or not SHA256_RE.fullmatch(str(result["release_sha"]))
        or not SHA256_RE.fullmatch(str(result["gate_config_sha256"]))
    ):
        raise HumanApprovalBlocked("approval release context is incomplete")
    return {key: str(item) for key, item in result.items()}


def _conditional_window(
    value: Any, *, now: datetime | None = None, require_future: bool = True
) -> dict[str, str]:
    """Validate an absolute, short cutover window before it is approved.

    This deliberately does not accept a recurring time-of-day.  A human must
    see (and approve) the exact date and bounds that an unattended redemption
    may use.
    """
    if not isinstance(value, Mapping) or set(value) != {"starts_at", "ends_at", "timezone"}:
        raise HumanApprovalBlocked("conditional approval window must have exact starts_at, ends_at, timezone")
    timezone_name = value.get("timezone")
    if timezone_name != "Asia/Taipei":
        raise HumanApprovalBlocked("conditional approval window timezone is invalid")
    starts = _time(value.get("starts_at"), "conditional approval window starts_at")
    ends = _time(value.get("ends_at"), "conditional approval window ends_at")
    taipei = ZoneInfo("Asia/Taipei")
    local_starts = starts.astimezone(taipei)
    local_ends = ends.astimezone(taipei)
    if (
        ends <= starts
        or ends - starts > timedelta(hours=13)
        or local_starts.date() != local_ends.date()
        or local_starts.time() < datetime.strptime("06:00", "%H:%M").time()
        or local_ends.time() > datetime.strptime("22:00", "%H:%M").time()
    ):
        raise HumanApprovalBlocked(
            "conditional approval window must be a single Asia/Taipei date, 06:00–22:00, and at most thirteen hours"
        )
    instant = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if require_future and (starts < instant or ends > instant + timedelta(days=7)):
        raise HumanApprovalBlocked("conditional approval window must start in the future and end within seven days")
    # Preserve the exact strings from the signed configuration.  Converting
    # them to UTC here would make a semantically equal but byte-different
    # window appear to be a different authorization at the final gate.
    return {
        "starts_at": str(value["starts_at"]),
        "ends_at": str(value["ends_at"]),
        "timezone": timezone_name,
    }


def _consumption_path(value: Path) -> Path:
    raw = value.expanduser()
    if not raw.is_absolute() or raw.resolve(strict=False) != raw or raw.is_symlink():
        raise HumanApprovalBlocked("conditional approval consumption path is unsafe")
    return raw


def _leaf_hash(row: Mapping[str, Any]) -> str:
    unsigned = {key: value for key, value in row.items() if key != "leaf_sha256"}
    return hashlib.sha256(b"\x00" + _canonical(unsigned)).hexdigest()


def _merkle_root(leaves: Sequence[str]) -> str:
    if not leaves or any(not SHA256_RE.fullmatch(value) for value in leaves):
        raise HumanApprovalBlocked("machine evidence Merkle leaves are invalid")
    level = [bytes.fromhex(value) for value in leaves]
    while len(level) > 1:
        if len(level) % 2:
            level.append(level[-1])
        level = [
            hashlib.sha256(b"\x01" + level[index] + level[index + 1]).digest()
            for index in range(0, len(level), 2)
        ]
    return level[0].hex()


def _verify_27_gate(
    gate: Mapping[str, Any], config: Mapping[str, Any], expected_context: Mapping[str, str]
) -> list[str]:
    required = config.get("required_evidence")
    if not isinstance(required, list) or len(required) != 28 or required[-1] != EVIDENCE_ID:
        raise HumanApprovalBlocked("cutover config does not define the exact final human gate")
    machine_ids = required[:-1]
    if (
        gate.get("schema_version") != 1
        or gate.get("decision") != "NO_GO"
        or gate.get("fail_closed") is not True
        or gate.get("required_count") != 28
        or gate.get("expected_context") != dict(expected_context)
        or gate.get("passed") != machine_ids
        or gate.get("missing") != [EVIDENCE_ID]
        or gate.get("failed") != []
        or gate.get("invalid") != {}
    ):
        raise HumanApprovalBlocked("release gate is not an exact 27/28 machine pass")
    return machine_ids


def _machine_sources(evidence_dir: Path, machine_ids: Sequence[str]) -> tuple[list[dict], list[FrozenSource]]:
    root = evidence_dir.resolve(strict=True)
    rows: list[dict[str, Any]] = []
    sources: list[FrozenSource] = []
    for index, evidence_id in enumerate(machine_ids, start=1):
        envelope_source = _freeze(root / f"{evidence_id}.json", f"machine_{index:02d}_envelope")
        envelope = _json_bytes(envelope_source.data, f"machine evidence {evidence_id}")
        artifacts = envelope.get("artifacts")
        if (
            envelope.get("schema_version") != 1
            or envelope.get("evidence_id") != evidence_id
            or envelope.get("status") != "passed"
            or not isinstance(artifacts, list)
            or not artifacts
        ):
            raise HumanApprovalBlocked(f"machine evidence is not a passing envelope: {evidence_id}")
        envelope_role = f"upstream_machine_{index:02d}_envelope"
        sources.append(FrozenSource(envelope_role, envelope_source.path, envelope_source.data, envelope_source.sha256))
        artifact_rows = []
        for artifact_index, artifact in enumerate(artifacts, start=1):
            if not isinstance(artifact, dict):
                raise HumanApprovalBlocked("machine evidence artifact declaration is invalid")
            path = _safe_artifact(root, artifact.get("path"))
            role = f"upstream_machine_{index:02d}_artifact_{artifact_index:02d}"
            frozen = _freeze(path, role)
            if (
                artifact.get("sha256") != frozen.sha256
                or not isinstance(artifact.get("role"), str)
                or not isinstance(artifact.get("media_type"), str)
            ):
                raise HumanApprovalBlocked("machine evidence artifact hash/role drifted")
            sources.append(frozen)
            artifact_rows.append(
                {
                    "source_role": role,
                    "envelope_role": artifact["role"],
                    "media_type": artifact["media_type"],
                    "sha256": frozen.sha256,
                }
            )
        row = {
            "index": index,
            "evidence_id": evidence_id,
            "envelope_source_role": envelope_role,
            "envelope_sha256": envelope_source.sha256,
            "artifacts": artifact_rows,
        }
        row["leaf_sha256"] = _leaf_hash(row)
        rows.append(row)
    return rows, sources


def build_approval_request(
    *,
    evidence_dir: Path,
    release_gate_report: Path,
    gate_config: Path,
    expected_context: Mapping[str, str],
    output: Path,
    now: datetime | None = None,
    ttl_minutes: int = 30,
) -> dict[str, Any]:
    context = _context(expected_context)
    gate_source = _freeze(release_gate_report, "27-of-28 release gate report")
    config_source = _freeze(gate_config, "cutover gate config")
    if config_source.sha256 != context["gate_config_sha256"]:
        raise HumanApprovalBlocked("approval gate config SHA-256 mismatch")
    gate = _json_bytes(gate_source.data, "27-of-28 release gate report")
    config = _json_bytes(config_source.data, "cutover gate config")
    machine_ids = _verify_27_gate(gate, config, context)
    rows, _sources = _machine_sources(evidence_dir, machine_ids)
    instant = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if ttl_minutes < 5 or ttl_minutes > 60:
        raise HumanApprovalBlocked("approval request TTL must be within 5..60 minutes")
    phrase = f"APPROVE {uuid.uuid4().hex[:12]} {rows[-1]['leaf_sha256'][:12]}"
    request = {
        "schema": REQUEST_SCHEMA,
        "schema_version": 1,
        "request_id": uuid.uuid4().hex,
        **context,
        "status": "awaiting_human_approval",
        "required_approver_role": "authorized_release_owner",
        "approval_scope": "exact_release_and_campaign",
        "requested_at": instant.isoformat(),
        "expires_at": (instant + timedelta(minutes=ttl_minutes)).isoformat(),
        "release_gate_report_sha256": gate_source.sha256,
        "gate_config_sha256": config_source.sha256,
        "machine_evidence": rows,
        "evidence_leaf_count": len(rows),
        "evidence_root_sha256": _merkle_root([row["leaf_sha256"] for row in rows]),
        "approval_phrase": phrase,
        "approval_phrase_sha256": hashlib.sha256(phrase.encode()).hexdigest(),
        "mutation_performed": False,
    }
    digest = _write_new(output, request)
    return {
        "status": "awaiting_human_approval",
        "request": str(output),
        "request_sha256": digest,
        "request_id": request["request_id"],
        "evidence_root_sha256": request["evidence_root_sha256"],
        "approval_phrase": phrase,
    }


def capture_local_approval(
    *,
    request_path: Path,
    output: Path,
    input_reader: Callable[[str], str] = input,
    isatty: Callable[[], bool] = lambda: sys.stdin.isatty(),
    uid: int | None = None,
    user: str | None = None,
    tty_name: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    request_source = _freeze(request_path, "human approval request")
    request = _json_bytes(request_source.data, "human approval request")
    context = _context(request)
    instant = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    local_uid = os.getuid() if uid is None else uid
    local_user = getpass.getuser() if user is None else user
    if (
        request.get("schema") != REQUEST_SCHEMA
        or request.get("status") != "awaiting_human_approval"
        or request.get("required_approver_role") != "authorized_release_owner"
        or request.get("approval_scope") != "exact_release_and_campaign"
        or instant < _time(request.get("requested_at"), "approval request requested_at")
        or instant > _time(request.get("expires_at"), "approval request expires_at")
        or (local_uid, local_user) not in AUTHORIZED_LOCAL_OWNERS
        or not isatty()
    ):
        raise HumanApprovalBlocked("approval requires a fresh request and allowlisted interactive local owner")
    terminal = tty_name
    if terminal is None:
        try:
            terminal = os.ttyname(0)
        except OSError as exc:
            raise HumanApprovalBlocked("approval terminal identity is unavailable") from exc
    phrase = request.get("approval_phrase")
    if (
        not isinstance(phrase, str)
        or hashlib.sha256(phrase.encode()).hexdigest() != request.get("approval_phrase_sha256")
    ):
        raise HumanApprovalBlocked("approval request phrase binding is invalid")
    supplied = input_reader(
        "Confirm MAGI V3 exact release/campaign approval by typing the displayed phrase: "
    )
    if not hmac_compare(supplied, phrase):
        raise HumanApprovalBlocked("human approval phrase did not match")
    receipt = {
        "schema": RECEIPT_SCHEMA,
        "schema_version": 1,
        "status": "approved",
        **context,
        "request_id": request["request_id"],
        "request_sha256": request_source.sha256,
        "evidence_root_sha256": request["evidence_root_sha256"],
        "approval_phrase_sha256": request["approval_phrase_sha256"],
        "approved": True,
        "approver_id": f"local:{local_uid}:{local_user}",
        "approver_uid": local_uid,
        "approver_user": local_user,
        "approver_role": "authorized_release_owner",
        "approval_scope": "exact_release_and_campaign",
        "auth_method": "allowlisted_local_owner_interactive_tty",
        "tty_session_sha256": hashlib.sha256(str(terminal).encode()).hexdigest(),
        "approved_at": instant.isoformat(),
        "human_interaction_performed": True,
    }
    digest = _write_new(output, receipt)
    return {"status": "approved", "receipt": str(output), "receipt_sha256": digest}


def build_conditional_approval_request(
    *,
    expected_context: Mapping[str, str],
    cutover_window: Mapping[str, Any],
    output: Path,
    g8_smb_target_sha256: str | None = None,
    release_manifest_sha256: str | None = None,
    release_id: str | None = None,
    g8_plan_id: str | None = None,
    g8_plan_file_sha256: str | None = None,
    g8_plan_semantic_sha256: str | None = None,
    g8_usage_receipt_path: Path | None = None,
    now: datetime | None = None,
    approval_ttl_minutes: int = 60,
    consumption_path: Path | None = None,
) -> dict[str, Any]:
    """Create an approval request that is redeemable once in one exact window.

    Unlike the legacy request, this does not assert that machine evidence has
    already passed.  Evidence is re-frozen and Merkle-bound only when the
    request is redeemed, so a daytime approval cannot authorize a changed
    release, campaign, machine, gate configuration, or evidence set.
    """
    context = _context(expected_context)
    instant = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    window = _conditional_window(cutover_window, now=instant)
    if approval_ttl_minutes < 5 or approval_ttl_minutes > 60:
        raise HumanApprovalBlocked("conditional approval request TTL must be within 5..60 minutes")
    starts = _time(window["starts_at"], "conditional approval window starts_at")
    expires = min(instant + timedelta(minutes=approval_ttl_minutes), starts)
    if expires - instant < timedelta(minutes=5):
        raise HumanApprovalBlocked("conditional approval window starts too soon for the 5 minute human approval TTL")
    marker = _consumption_path(
        consumption_path
        or output.expanduser().with_name(f"{output.expanduser().name}.consumed.json")
    )
    target_hint = g8_smb_target_sha256[:12] if isinstance(g8_smb_target_sha256, str) else "NO-G8-TARGET"
    plan_hint = g8_plan_id[:12] if isinstance(g8_plan_id, str) else "NO-G8-PLAN"
    phrase = (
        f"PREAUTHORIZE MAGI V3 {str(release_id or context['release_sha'])[:12]} "
        f"{hashlib.sha256(_canonical(window)).hexdigest()[:12]} {target_hint} {plan_hint} {uuid.uuid4().hex[:8]}"
    )
    request = {
        "schema": CONDITIONAL_REQUEST_SCHEMA,
        "schema_version": 2,
        "request_id": uuid.uuid4().hex,
        **context,
        "status": "awaiting_conditional_human_approval",
        "required_approver_role": "authorized_release_owner",
        "approval_scope": "conditional_exact_release_campaign_hardware_gate_and_window",
        "requested_at": instant.isoformat(),
        # Human interaction must be fresh.  This is intentionally independent
        # from the later, explicitly-bound automatic cutover window.
        "expires_at": expires.isoformat(),
        "cutover_window": window,
        "cutover_window_sha256": hashlib.sha256(_canonical(window)).hexdigest(),
        "consumption_path": str(marker),
        "approval_phrase": phrase,
        "approval_phrase_sha256": hashlib.sha256(phrase.encode()).hexdigest(),
        "mutation_performed": False,
    }
    g8_values = (
        g8_smb_target_sha256, release_manifest_sha256, release_id,
        g8_plan_id, g8_plan_file_sha256, g8_plan_semantic_sha256,
        g8_usage_receipt_path,
    )
    if any(value is not None for value in g8_values):
        if not (
            isinstance(g8_smb_target_sha256, str)
            and SHA256_RE.fullmatch(g8_smb_target_sha256)
            and isinstance(release_manifest_sha256, str)
            and SHA256_RE.fullmatch(release_manifest_sha256)
            and isinstance(release_id, str)
            and release_id
            and isinstance(g8_plan_id, str)
            and g8_plan_id
            and isinstance(g8_plan_file_sha256, str)
            and SHA256_RE.fullmatch(g8_plan_file_sha256)
            and isinstance(g8_plan_semantic_sha256, str)
            and SHA256_RE.fullmatch(g8_plan_semantic_sha256)
            and g8_usage_receipt_path is not None
            and g8_usage_receipt_path.is_absolute()
            and g8_usage_receipt_path.resolve(strict=False) == g8_usage_receipt_path
            and not g8_usage_receipt_path.is_symlink()
        ):
            raise HumanApprovalBlocked("conditional G8 binding is incomplete")
        request["g8_smb_authorization"] = {
            "target_sha256": g8_smb_target_sha256,
            "release_manifest_sha256": release_manifest_sha256,
            "release_id": release_id,
            "plan_id": g8_plan_id,
            "plan_file_sha256": g8_plan_file_sha256,
            "plan_semantic_sha256": g8_plan_semantic_sha256,
            "operations": ["g8_smb_resource_validation", "final_v2_to_v3_cutover"],
            "usage_receipt_path": str(g8_usage_receipt_path),
        }
    digest = _write_new(output, request)
    return {
        "status": "awaiting_conditional_human_approval",
        "request": str(output),
        "request_sha256": digest,
        "request_id": request["request_id"],
        "cutover_window": window,
        "consumption_marker": str(marker),
        "approval_phrase": phrase,
    }


def capture_conditional_local_approval(
    *,
    request_path: Path,
    output: Path,
    input_reader: Callable[[str], str] = input,
    isatty: Callable[[], bool] = lambda: sys.stdin.isatty(),
    uid: int | None = None,
    user: str | None = None,
    tty_name: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    request_source = _freeze(request_path, "conditional human approval request")
    request = _json_bytes(request_source.data, "conditional human approval request")
    context = _context(request)
    instant = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    window = _conditional_window(request.get("cutover_window"), require_future=False)
    local_uid = os.getuid() if uid is None else uid
    local_user = getpass.getuser() if user is None else user
    if (
        request.get("schema") != CONDITIONAL_REQUEST_SCHEMA
        or request.get("status") != "awaiting_conditional_human_approval"
        or request.get("required_approver_role") != "authorized_release_owner"
        or request.get("approval_scope") != "conditional_exact_release_campaign_hardware_gate_and_window"
        or request.get("cutover_window_sha256") != hashlib.sha256(_canonical(window)).hexdigest()
        or not isinstance(request.get("consumption_path"), str)
        or str(_consumption_path(Path(request["consumption_path"]))) != request["consumption_path"]
        or instant < _time(request.get("requested_at"), "conditional approval requested_at")
        or instant > _time(request.get("expires_at"), "conditional approval expires_at")
        or instant > _time(window["starts_at"], "conditional approval window starts_at")
        or (local_uid, local_user) not in AUTHORIZED_LOCAL_OWNERS
        or not isatty()
    ):
        raise HumanApprovalBlocked("conditional approval requires a fresh request and allowlisted interactive local owner")
    terminal = tty_name
    if terminal is None:
        try:
            terminal = os.ttyname(0)
        except OSError as exc:
            raise HumanApprovalBlocked("conditional approval terminal identity is unavailable") from exc
    phrase = request.get("approval_phrase")
    if not isinstance(phrase, str) or hashlib.sha256(phrase.encode()).hexdigest() != request.get("approval_phrase_sha256"):
        raise HumanApprovalBlocked("conditional approval request phrase binding is invalid")
    supplied = input_reader("Confirm MAGI V3 conditional release approval by typing the displayed phrase: ")
    if not hmac_compare(supplied, phrase):
        raise HumanApprovalBlocked("conditional human approval phrase did not match")
    receipt = {
        "schema": CONDITIONAL_RECEIPT_SCHEMA,
        "schema_version": 2,
        "status": "preauthorized",
        **context,
        "request_id": request["request_id"],
        "request_sha256": request_source.sha256,
        "cutover_window": window,
        "cutover_window_sha256": request["cutover_window_sha256"],
        "consumption_path": request["consumption_path"],
        "approval_phrase_sha256": request["approval_phrase_sha256"],
        "approved": True,
        "approver_id": f"local:{local_uid}:{local_user}",
        "approver_uid": local_uid,
        "approver_user": local_user,
        "approver_role": "authorized_release_owner",
        "approval_scope": "conditional_exact_release_campaign_hardware_gate_and_window",
        "auth_method": "allowlisted_local_owner_interactive_tty",
        "tty_session_sha256": hashlib.sha256(str(terminal).encode()).hexdigest(),
        "approved_at": instant.isoformat(),
        "human_interaction_performed": True,
    }
    if "g8_smb_authorization" in request:
        receipt["g8_smb_authorization"] = request["g8_smb_authorization"]
    digest = _write_new(output, receipt)
    return {"status": "preauthorized", "receipt": str(output), "receipt_sha256": digest,
            "consumption_marker": str(_consumption_path(Path(request["consumption_path"])))}


def verify_conditional_g8_preauthorization(
    *,
    request_path: Path,
    receipt_path: Path,
    expected_context: Mapping[str, str],
    target_sha256: str,
    release_manifest_sha256: str,
    release_id: str,
    plan_id: str,
    plan_file_sha256: str,
    plan_semantic_sha256: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Verify a genuine preapproval for one G8 target without consuming G28.

    This deliberately only reads the conditional request and its interactive
    receipt.  Final cutover redemption remains the sole writer of the G28
    consumption marker.
    """
    request_source = _freeze(request_path, "conditional G8 approval request")
    receipt_source = _freeze(receipt_path, "conditional G8 approval receipt")
    request = _json_bytes(request_source.data, "conditional G8 approval request")
    receipt = _json_bytes(receipt_source.data, "conditional G8 approval receipt")
    context = _context(expected_context)
    window = _conditional_window(request.get("cutover_window"), require_future=False)
    instant = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    starts = _time(window["starts_at"], "conditional G8 starts_at")
    ends = _time(window["ends_at"], "conditional G8 ends_at")
    requested = _time(request.get("requested_at"), "conditional G8 requested_at")
    expires = _time(request.get("expires_at"), "conditional G8 expires_at")
    approved = _time(receipt.get("approved_at"), "conditional G8 approved_at")
    phrase = request.get("approval_phrase")
    marker = request.get("consumption_path")
    usage_path_value = request.get("g8_smb_authorization", {}).get("usage_receipt_path")
    expected_g8 = {
        "target_sha256": target_sha256,
        "release_manifest_sha256": release_manifest_sha256,
        "release_id": release_id,
        "plan_id": plan_id,
        "plan_file_sha256": plan_file_sha256,
        "plan_semantic_sha256": plan_semantic_sha256,
        "operations": ["g8_smb_resource_validation", "final_v2_to_v3_cutover"],
        "usage_receipt_path": usage_path_value,
    }
    if (
        not SHA256_RE.fullmatch(target_sha256)
        or not SHA256_RE.fullmatch(release_manifest_sha256)
        or not release_id
        or not plan_id
        or not SHA256_RE.fullmatch(plan_file_sha256)
        or not SHA256_RE.fullmatch(plan_semantic_sha256)
        or request.get("schema") != CONDITIONAL_REQUEST_SCHEMA
        or request.get("schema_version") != 2
        or request.get("status") != "awaiting_conditional_human_approval"
        or request.get("required_approver_role") != "authorized_release_owner"
        or request.get("approval_scope")
        != "conditional_exact_release_campaign_hardware_gate_and_window"
        or request.get("mutation_performed") is not False
        or _context(request) != context
        or receipt.get("schema") != CONDITIONAL_RECEIPT_SCHEMA
        or receipt.get("schema_version") != 2
        or _context(receipt) != context
        or request.get("cutover_window_sha256")
        != hashlib.sha256(_canonical(window)).hexdigest()
        or not isinstance(marker, str)
        or str(_consumption_path(Path(marker))) != marker
        or not isinstance(usage_path_value, str)
        or str(_consumption_path(Path(usage_path_value))) != usage_path_value
        or not isinstance(phrase, str)
        or hashlib.sha256(phrase.encode()).hexdigest()
        != request.get("approval_phrase_sha256")
        or request.get("g8_smb_authorization") != expected_g8
        or receipt.get("g8_smb_authorization") != expected_g8
        or receipt.get("request_id") != request.get("request_id")
        or receipt.get("request_sha256") != request_source.sha256
        or receipt.get("status") != "preauthorized"
        or receipt.get("approved") is not True
        or receipt.get("approval_phrase_sha256") != request.get("approval_phrase_sha256")
        or receipt.get("consumption_path") != marker
        or receipt.get("approver_role") != "authorized_release_owner"
        or receipt.get("approval_scope")
        != "conditional_exact_release_campaign_hardware_gate_and_window"
        or receipt.get("auth_method") != "allowlisted_local_owner_interactive_tty"
        or (receipt.get("approver_uid"), receipt.get("approver_user")) not in AUTHORIZED_LOCAL_OWNERS
        or receipt.get("approver_id")
        != f"local:{receipt.get('approver_uid')}:{receipt.get('approver_user')}"
        or receipt.get("human_interaction_performed") is not True
        or not isinstance(receipt.get("tty_session_sha256"), str)
        or not SHA256_RE.fullmatch(receipt["tty_session_sha256"])
        or receipt.get("cutover_window") != window
        or receipt.get("cutover_window_sha256") != request.get("cutover_window_sha256")
        or not (requested <= approved <= expires <= starts)
        or not (starts <= instant < ends)
    ):
        raise HumanApprovalBlocked("conditional G8 preauthorization is invalid, expired, or does not bind this target")
    return {
        "request_sha256": request_source.sha256,
        "receipt_sha256": receipt_source.sha256,
        "request_id": request["request_id"],
        "context": context,
        "cutover_window": window,
        "approver_uid": receipt["approver_uid"],
        "approver_user": receipt["approver_user"],
        "tty_session_sha256": receipt["tty_session_sha256"],
        "usage_receipt_path": expected_g8["usage_receipt_path"],
    }


def redeem_conditional_approval(
    *,
    evidence_dir: Path,
    release_gate_report: Path,
    gate_config: Path,
    request_path: Path,
    receipt_path: Path,
    expected_context: Mapping[str, str],
    now: datetime | None = None,
    allow_existing_exact: bool = False,
) -> dict[str, Any]:
    """Redeem a preapproval once, only after the current 27/28 gate is exact.

    The marker path is committed into the approved request and written with
    O_EXCL.  A copied receipt cannot choose a new marker or replay approval.
    """
    context = _context(expected_context)
    request_source = _freeze(request_path, "conditional approval request")
    receipt_source = _freeze(receipt_path, "conditional approval receipt")
    gate_source = _freeze(release_gate_report, "conditional approval gate report")
    config_source = _freeze(gate_config, "conditional approval gate config")
    request = _json_bytes(request_source.data, "conditional approval request")
    receipt = _json_bytes(receipt_source.data, "conditional approval receipt")
    gate = _json_bytes(gate_source.data, "conditional approval gate report")
    config = _json_bytes(config_source.data, "conditional approval gate config")
    window = _conditional_window(request.get("cutover_window"), require_future=False)
    instant = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    starts = _time(window["starts_at"], "conditional approval window starts_at")
    ends = _time(window["ends_at"], "conditional approval window ends_at")
    requested = _time(request.get("requested_at"), "conditional approval requested_at")
    request_expires = _time(request.get("expires_at"), "conditional approval expires_at")
    approved_at = _time(receipt.get("approved_at"), "conditional approval approved_at")
    g8_binding = request.get("g8_smb_authorization")
    if (
        request.get("schema") != CONDITIONAL_REQUEST_SCHEMA
        or request.get("schema_version") != 2
        or request.get("status") != "awaiting_conditional_human_approval"
        or request.get("required_approver_role") != "authorized_release_owner"
        or request.get("approval_scope")
        != "conditional_exact_release_campaign_hardware_gate_and_window"
        or request.get("mutation_performed") is not False
        or receipt.get("schema") != CONDITIONAL_RECEIPT_SCHEMA
        or receipt.get("schema_version") != 2
        or _context(request) != context
        or _context(receipt) != context
        or request.get("cutover_window_sha256") != hashlib.sha256(_canonical(window)).hexdigest()
        or config.get("conditional_daytime_window") != window
        or receipt.get("cutover_window") != window
        or receipt.get("cutover_window_sha256") != request.get("cutover_window_sha256")
        or receipt.get("consumption_path") != request.get("consumption_path")
        or receipt.get("g8_smb_authorization") != g8_binding
        or receipt.get("request_id") != request.get("request_id")
        or receipt.get("request_sha256") != request_source.sha256
        or receipt.get("status") != "preauthorized"
        or receipt.get("approved") is not True
        or receipt.get("approval_phrase_sha256") != request.get("approval_phrase_sha256")
        or receipt.get("approver_role") != "authorized_release_owner"
        or receipt.get("approval_scope") != "conditional_exact_release_campaign_hardware_gate_and_window"
        or receipt.get("auth_method") != "allowlisted_local_owner_interactive_tty"
        or (receipt.get("approver_uid"), receipt.get("approver_user")) not in AUTHORIZED_LOCAL_OWNERS
        or receipt.get("approver_id") != f"local:{receipt.get('approver_uid')}:{receipt.get('approver_user')}"
        or receipt.get("human_interaction_performed") is not True
        or not isinstance(receipt.get("tty_session_sha256"), str)
        or not SHA256_RE.fullmatch(receipt["tty_session_sha256"])
        or not (requested <= approved_at <= request_expires <= starts)
        or not (starts <= instant < ends)
        or config_source.sha256 != context["gate_config_sha256"]
    ):
        raise HumanApprovalBlocked("conditional approval is invalid, stale, outside its approved window, or unauthorized")
    machine_ids = _verify_27_gate(gate, config, context)
    rows, _sources = _machine_sources(evidence_dir, machine_ids)
    root = _merkle_root([row["leaf_sha256"] for row in rows])
    if not isinstance(request.get("consumption_path"), str):
        raise HumanApprovalBlocked("conditional approval consumption path is missing")
    marker = _consumption_path(Path(request["consumption_path"]))
    consumption = {
        "schema": CONSUMPTION_SCHEMA,
        "schema_version": 2,
        "status": "redeemed",
        **context,
        "request_id": request["request_id"],
        "request_sha256": request_source.sha256,
        "receipt_sha256": receipt_source.sha256,
        "release_gate_report_sha256": gate_source.sha256,
        "gate_config_sha256": config_source.sha256,
        "cutover_window": window,
        "cutover_window_sha256": request["cutover_window_sha256"],
        "consumption_path": str(marker),
        "machine_evidence": rows,
        "evidence_leaf_count": len(rows),
        "evidence_root_sha256": root,
        "consumed_at": instant.isoformat(),
        "mutation_performed": False,
    }
    try:
        digest = _write_new(marker, consumption)
    except FileExistsError as exc:
        if not allow_existing_exact:
            raise HumanApprovalBlocked("conditional approval has already been consumed") from exc
        existing_source = _freeze(marker, "existing conditional approval consumption")
        existing = _json_bytes(
            existing_source.data, "existing conditional approval consumption"
        )
        try:
            existing_consumed = _time(
                existing.get("consumed_at"),
                "existing conditional approval consumed_at",
            )
        except HumanApprovalBlocked as validation_error:
            raise HumanApprovalBlocked(
                "existing conditional approval consumption is not resumable"
            ) from validation_error
        expected_existing = dict(consumption)
        expected_existing["consumed_at"] = existing.get("consumed_at")
        if (
            existing != expected_existing
            or not (starts <= existing_consumed < ends)
        ):
            raise HumanApprovalBlocked(
                "existing conditional approval consumption does not match this exact redemption"
            ) from exc
        digest = existing_source.sha256
        return {
            "status": "resumed_exact_redemption",
            "consumption": str(marker),
            "consumption_sha256": digest,
            "evidence_root_sha256": root,
            "cutover_window": window,
        }
    return {"status": "redeemed", "consumption": str(marker), "consumption_sha256": digest,
            "evidence_root_sha256": root, "cutover_window": window}


def hmac_compare(left: str, right: str) -> bool:
    import hmac

    return hmac.compare_digest(left.encode(), right.encode())


def derive_conditional_human_approval_metrics(
    *,
    request: Mapping[str, Any],
    request_sha256: str,
    receipt: Mapping[str, Any],
    receipt_sha256: str,
    consumption: Mapping[str, Any],
    consumption_sha256: str,
    gate_report: Mapping[str, Any],
    gate_report_sha256: str,
    gate_config: Mapping[str, Any],
    gate_config_sha256: str,
    machine_sources: Mapping[str, FrozenSource],
    expected_context: Mapping[str, str],
) -> dict[str, Any]:
    """Validate a v2 preapproval at its one permitted redemption point.

    Callers must pass only the 27 frozen machine source roles.  The release
    gate normalizer separately requires the five fixed conditional source
    roles, preventing an unbound request or a second consumption record from
    being substituted after the fact.
    """
    context = _context(expected_context)
    config_window = _conditional_window(
        gate_config.get("conditional_daytime_window"), require_future=False
    )
    g8_binding = request.get("g8_smb_authorization")
    if g8_binding is not None and (
        not isinstance(g8_binding, Mapping)
        or set(g8_binding) != {
            "target_sha256", "release_manifest_sha256", "release_id", "plan_id",
            "plan_file_sha256", "plan_semantic_sha256", "operations", "usage_receipt_path",
        }
        or g8_binding.get("operations") != ["g8_smb_resource_validation", "final_v2_to_v3_cutover"]
        or any(not isinstance(g8_binding.get(key), str) or not g8_binding.get(key) for key in g8_binding if key != "operations")
        or any(
            not SHA256_RE.fullmatch(str(g8_binding.get(key)))
            for key in (
                "target_sha256",
                "release_manifest_sha256",
                "plan_file_sha256",
                "plan_semantic_sha256",
            )
        )
        or str(_consumption_path(Path(str(g8_binding.get("usage_receipt_path")))))
        != g8_binding.get("usage_receipt_path")
    ):
        raise HumanApprovalBlocked("conditional G8 binding is malformed")
    if (
        _context(request) != context
        or _context(receipt) != context
        or _context(consumption) != context
        or request.get("schema") != CONDITIONAL_REQUEST_SCHEMA
        or receipt.get("schema") != CONDITIONAL_RECEIPT_SCHEMA
        or consumption.get("schema") != CONSUMPTION_SCHEMA
        or request.get("status") != "awaiting_conditional_human_approval"
        or receipt.get("status") != "preauthorized"
        or receipt.get("g8_smb_authorization") != g8_binding
        or consumption.get("status") != "redeemed"
        or request.get("cutover_window") != config_window
        or receipt.get("cutover_window") != config_window
        or consumption.get("cutover_window") != config_window
        or request.get("cutover_window_sha256") != hashlib.sha256(_canonical(config_window)).hexdigest()
        or receipt.get("cutover_window_sha256") != request.get("cutover_window_sha256")
        or consumption.get("cutover_window_sha256") != request.get("cutover_window_sha256")
        or receipt.get("consumption_path") != request.get("consumption_path")
        or consumption.get("consumption_path") != request.get("consumption_path")
        or receipt.get("request_id") != request.get("request_id")
        or receipt.get("request_sha256") != request_sha256
        or consumption.get("request_id") != request.get("request_id")
        or consumption.get("request_sha256") != request_sha256
        or consumption.get("receipt_sha256") != receipt_sha256
        or consumption.get("release_gate_report_sha256") != gate_report_sha256
        or consumption.get("gate_config_sha256") != gate_config_sha256
        or gate_config_sha256 != context["gate_config_sha256"]
        or receipt.get("approved") is not True
        or receipt.get("approver_role") != "authorized_release_owner"
        or receipt.get("approval_scope") != "conditional_exact_release_campaign_hardware_gate_and_window"
        or receipt.get("auth_method") != "allowlisted_local_owner_interactive_tty"
        or (receipt.get("approver_uid"), receipt.get("approver_user")) not in AUTHORIZED_LOCAL_OWNERS
        or receipt.get("approver_id") != f"local:{receipt.get('approver_uid')}:{receipt.get('approver_user')}"
        or receipt.get("human_interaction_performed") is not True
    ):
        raise HumanApprovalBlocked("conditional approval request, receipt, consumption, or context drifted")
    starts = _time(config_window["starts_at"], "conditional daytime window starts_at")
    ends = _time(config_window["ends_at"], "conditional daytime window ends_at")
    requested = _time(request.get("requested_at"), "conditional approval requested_at")
    request_expires = _time(request.get("expires_at"), "conditional approval expires_at")
    approved = _time(receipt.get("approved_at"), "conditional approval approved_at")
    consumed = _time(consumption.get("consumed_at"), "conditional approval consumed_at")
    if (
        not (requested <= approved <= request_expires <= starts)
        or not (starts <= consumed < ends)
    ):
        raise HumanApprovalBlocked("conditional approval was not preapproved then consumed within its exact window")
    machine_ids = _verify_27_gate(gate_report, gate_config, context)
    rows = consumption.get("machine_evidence")
    if not isinstance(rows, list) or len(rows) != 27:
        raise HumanApprovalBlocked("conditional consumption machine evidence manifest is incomplete")
    expected_roles: set[str] = set()
    leaves: list[str] = []
    for index, (row, evidence_id) in enumerate(zip(rows, machine_ids, strict=True), start=1):
        if (
            not isinstance(row, dict)
            or row.get("index") != index
            or row.get("evidence_id") != evidence_id
            or row.get("leaf_sha256") != _leaf_hash(row)
        ):
            raise HumanApprovalBlocked("conditional consumption machine evidence leaf is invalid")
        envelope_role = str(row.get("envelope_source_role"))
        envelope_source = machine_sources.get(envelope_role)
        if envelope_source is None or envelope_source.sha256 != row.get("envelope_sha256"):
            raise HumanApprovalBlocked("conditional consumption machine envelope source is missing")
        expected_roles.add(envelope_role)
        envelope = _json_bytes(envelope_source.data, "conditional consumption machine envelope")
        artifact_rows = row.get("artifacts")
        declared = envelope.get("artifacts")
        if (
            envelope.get("evidence_id") != evidence_id
            or envelope.get("status") != "passed"
            or not isinstance(artifact_rows, list)
            or not isinstance(declared, list)
            or len(artifact_rows) != len(declared)
        ):
            raise HumanApprovalBlocked("conditional consumption machine envelope drifted")
        for artifact, source_row in zip(declared, artifact_rows, strict=True):
            if not isinstance(artifact, dict) or not isinstance(source_row, dict):
                raise HumanApprovalBlocked("conditional consumption artifact row is invalid")
            role = str(source_row.get("source_role"))
            source = machine_sources.get(role)
            if (
                source is None
                or source.sha256 != source_row.get("sha256")
                or artifact.get("sha256") != source.sha256
                or artifact.get("role") != source_row.get("envelope_role")
                or artifact.get("media_type") != source_row.get("media_type")
            ):
                raise HumanApprovalBlocked("conditional consumption machine artifact source drifted")
            expected_roles.add(role)
        leaves.append(row["leaf_sha256"])
    root = _merkle_root(leaves)
    if (
        set(machine_sources) != expected_roles
        or consumption.get("evidence_leaf_count") != 27
        or consumption.get("evidence_root_sha256") != root
    ):
        raise HumanApprovalBlocked("conditional consumption machine evidence root is not exact")
    return {
        "approved": True,
        "approver_id": receipt["approver_id"],
        "approver_role": "authorized_release_owner",
        # Kept compatible with the currently sealed evidence specification;
        # authorization_mode carries the stronger conditional semantics.
        "approval_scope": "exact_release_and_campaign",
        "authorization_mode": "conditional_daytime_window",
        "conditional_daytime_window": config_window,
        "conditional_request_sha256": request_sha256,
        "conditional_receipt_sha256": receipt_sha256,
        "conditional_consumption_sha256": consumption_sha256,
        "evidence_root_sha256": root,
    }


def compile_conditional_human_approval_evidence(
    *,
    output: Path,
    evidence_dir: Path,
    request_path: Path,
    receipt_path: Path,
    release_gate_report: Path,
    gate_config: Path,
    expected_context: Mapping[str, str],
    now: datetime | None = None,
) -> str:
    """Redeem a v2 preapproval and emit the final G28 evidence envelope."""
    from scripts.v3_evidence_compiler import CompileContext, SourceArtifact, _emit

    context_values = _context(expected_context)
    context = CompileContext(**context_values)
    context.validate()
    # All validation and the 27-source freeze occur before this writes the
    # O_EXCL marker.  A stale gate or altered artifact therefore never burns
    # an otherwise valid human preauthorization.
    redemption = redeem_conditional_approval(
        evidence_dir=evidence_dir,
        release_gate_report=release_gate_report,
        gate_config=gate_config,
        request_path=request_path,
        receipt_path=receipt_path,
        expected_context=context_values,
        now=now,
        allow_existing_exact=True,
    )
    request_source = _freeze(request_path, "conditional approval request")
    receipt_source = _freeze(receipt_path, "conditional approval receipt")
    consumption_path = Path(redemption["consumption"])
    consumption_source = _freeze(consumption_path, "conditional approval consumption")
    gate_source = _freeze(release_gate_report, "conditional approval gate report")
    config_source = _freeze(gate_config, "conditional approval gate config")
    request = _json_bytes(request_source.data, "conditional approval request")
    receipt = _json_bytes(receipt_source.data, "conditional approval receipt")
    consumption = _json_bytes(consumption_source.data, "conditional approval consumption")
    gate = _json_bytes(gate_source.data, "conditional approval gate report")
    config = _json_bytes(config_source.data, "conditional approval gate config")
    machine_ids = _verify_27_gate(gate, config, context_values)
    _rows, machine = _machine_sources(evidence_dir, machine_ids)
    metrics = derive_conditional_human_approval_metrics(
        request=request,
        request_sha256=request_source.sha256,
        receipt=receipt,
        receipt_sha256=receipt_source.sha256,
        consumption=consumption,
        consumption_sha256=consumption_source.sha256,
        gate_report=gate,
        gate_report_sha256=gate_source.sha256,
        gate_config=config,
        gate_config_sha256=config_source.sha256,
        machine_sources={item.role: item for item in machine},
        expected_context=context_values,
    )
    sources = [
        SourceArtifact("upstream_conditional_request", request_path),
        SourceArtifact("upstream_conditional_receipt", receipt_path),
        SourceArtifact("upstream_conditional_consumption", consumption_path),
        SourceArtifact("upstream_approval_gate_report", release_gate_report),
        SourceArtifact("upstream_approval_gate_config", gate_config),
        *(SourceArtifact(item.role, item.path) for item in machine),
    ]
    return _emit(
        output=output,
        evidence_id=EVIDENCE_ID,
        context=context,
        config=config,
        metrics=metrics,
        sources=sources,
        started_at=_time(request.get("requested_at"), "conditional approval requested_at"),
        completed_at=_time(consumption.get("consumed_at"), "conditional approval consumed_at"),
    )


def derive_human_approval_metrics(
    *,
    request: Mapping[str, Any],
    request_sha256: str,
    receipt: Mapping[str, Any],
    gate_report: Mapping[str, Any],
    gate_report_sha256: str,
    gate_config: Mapping[str, Any],
    gate_config_sha256: str,
    machine_sources: Mapping[str, FrozenSource],
    expected_context: Mapping[str, str],
) -> dict[str, Any]:
    context = _context(expected_context)
    if (
        _context(request) != context
        or _context(receipt) != context
        or request.get("schema") != REQUEST_SCHEMA
        or request.get("release_gate_report_sha256") != gate_report_sha256
        or request.get("gate_config_sha256") != gate_config_sha256
        or gate_config_sha256 != context["gate_config_sha256"]
    ):
        raise HumanApprovalBlocked("approval request/receipt release context drifted")
    machine_ids = _verify_27_gate(gate_report, gate_config, context)
    rows = request.get("machine_evidence")
    if not isinstance(rows, list) or len(rows) != 27:
        raise HumanApprovalBlocked("approval request machine evidence manifest is incomplete")
    expected_roles: set[str] = set()
    leaf_hashes: list[str] = []
    for index, (row, evidence_id) in enumerate(zip(rows, machine_ids, strict=True), start=1):
        if (
            not isinstance(row, dict)
            or row.get("index") != index
            or row.get("evidence_id") != evidence_id
            or row.get("leaf_sha256") != _leaf_hash(row)
        ):
            raise HumanApprovalBlocked("approval request machine evidence leaf is invalid")
        envelope_role = row.get("envelope_source_role")
        envelope_source = machine_sources.get(str(envelope_role))
        if envelope_source is None or envelope_source.sha256 != row.get("envelope_sha256"):
            raise HumanApprovalBlocked("approval machine evidence envelope source is missing")
        expected_roles.add(str(envelope_role))
        envelope = _json_bytes(envelope_source.data, "approval machine evidence envelope")
        declared = envelope.get("artifacts")
        artifact_rows = row.get("artifacts")
        if (
            envelope.get("evidence_id") != evidence_id
            or envelope.get("status") != "passed"
            or not isinstance(declared, list)
            or not isinstance(artifact_rows, list)
            or len(declared) != len(artifact_rows)
        ):
            raise HumanApprovalBlocked("approval machine evidence envelope drifted")
        for artifact, source_row in zip(declared, artifact_rows, strict=True):
            if not isinstance(artifact, dict) or not isinstance(source_row, dict):
                raise HumanApprovalBlocked("approval machine evidence artifact row is invalid")
            role = source_row.get("source_role")
            source = machine_sources.get(str(role))
            if (
                source is None
                or source.sha256 != source_row.get("sha256")
                or artifact.get("sha256") != source.sha256
                or artifact.get("role") != source_row.get("envelope_role")
                or artifact.get("media_type") != source_row.get("media_type")
            ):
                raise HumanApprovalBlocked("approval machine evidence artifact source drifted")
            expected_roles.add(str(role))
        leaf_hashes.append(row["leaf_sha256"])
    if set(machine_sources) != expected_roles:
        raise HumanApprovalBlocked("approval normalized machine source roles are not exact")
    evidence_root = _merkle_root(leaf_hashes)
    requested = _time(request.get("requested_at"), "approval request requested_at")
    expires = _time(request.get("expires_at"), "approval request expires_at")
    approved = _time(receipt.get("approved_at"), "approval receipt approved_at")
    if (
        request.get("evidence_leaf_count") != 27
        or request.get("evidence_root_sha256") != evidence_root
        or receipt.get("request_sha256") != request_sha256
        or receipt.get("request_id") != request.get("request_id")
        or receipt.get("evidence_root_sha256") != evidence_root
        or receipt.get("approval_phrase_sha256") != request.get("approval_phrase_sha256")
        or receipt.get("schema") != RECEIPT_SCHEMA
        or receipt.get("status") != "approved"
        or receipt.get("approved") is not True
        or receipt.get("approver_role") != "authorized_release_owner"
        or receipt.get("approval_scope") != "exact_release_and_campaign"
        or receipt.get("auth_method") != "allowlisted_local_owner_interactive_tty"
        or (receipt.get("approver_uid"), receipt.get("approver_user"))
        not in AUTHORIZED_LOCAL_OWNERS
        or receipt.get("approver_id")
        != f"local:{receipt.get('approver_uid')}:{receipt.get('approver_user')}"
        or receipt.get("human_interaction_performed") is not True
        or not isinstance(receipt.get("tty_session_sha256"), str)
        or not SHA256_RE.fullmatch(receipt["tty_session_sha256"])
        or approved < requested
        or approved > expires
    ):
        raise HumanApprovalBlocked("human approval receipt is invalid, stale, or unauthorized")
    return {
        "approved": True,
        "approver_id": receipt["approver_id"],
        "approver_role": "authorized_release_owner",
        "approval_scope": "exact_release_and_campaign",
        "evidence_root_sha256": evidence_root,
    }


def compile_human_approval_evidence(
    *,
    output: Path,
    evidence_dir: Path,
    request_path: Path,
    receipt_path: Path,
    release_gate_report: Path,
    gate_config: Path,
    expected_context: Mapping[str, str],
) -> str:
    from scripts.v3_evidence_compiler import CompileContext, SourceArtifact, _emit

    context_values = _context(expected_context)
    context = CompileContext(**context_values)
    context.validate()
    request_source = _freeze(request_path, "approval request")
    receipt_source = _freeze(receipt_path, "approval receipt")
    gate_source = _freeze(release_gate_report, "approval gate report")
    config_source = _freeze(gate_config, "approval gate config")
    request = _json_bytes(request_source.data, "approval request")
    receipt = _json_bytes(receipt_source.data, "approval receipt")
    gate = _json_bytes(gate_source.data, "approval gate report")
    config = _json_bytes(config_source.data, "approval gate config")
    machine_ids = _verify_27_gate(gate, config, context_values)
    rows, machine = _machine_sources(evidence_dir, machine_ids)
    if request.get("machine_evidence") != rows:
        raise HumanApprovalBlocked("approval request machine evidence changed before compilation")
    machine_by_role = {item.role: item for item in machine}
    metrics = derive_human_approval_metrics(
        request=request,
        request_sha256=request_source.sha256,
        receipt=receipt,
        gate_report=gate,
        gate_report_sha256=gate_source.sha256,
        gate_config=config,
        gate_config_sha256=config_source.sha256,
        machine_sources=machine_by_role,
        expected_context=context_values,
    )
    sources = [
        SourceArtifact("upstream_approval_request", request_path),
        SourceArtifact("upstream_approval_receipt", receipt_path),
        SourceArtifact("upstream_approval_gate_report", release_gate_report),
        SourceArtifact("upstream_approval_gate_config", gate_config),
        *(SourceArtifact(item.role, item.path) for item in machine),
    ]
    return _emit(
        output=output,
        evidence_id=EVIDENCE_ID,
        context=context,
        config=config,
        metrics=metrics,
        sources=sources,
        started_at=_time(request.get("requested_at"), "approval requested_at"),
        completed_at=_time(receipt.get("approved_at"), "approval approved_at"),
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    request = commands.add_parser("prepare-request")
    request.add_argument("--evidence-dir", type=Path, required=True)
    request.add_argument("--release-gate-report", type=Path, required=True)
    request.add_argument("--gate-config", type=Path, required=True)
    request.add_argument("--campaign-id", required=True)
    request.add_argument("--release-sha", required=True)
    request.add_argument("--hardware-id", required=True)
    request.add_argument("--gate-config-sha256", required=True)
    request.add_argument("--output", type=Path, required=True)
    conditional_request = commands.add_parser("prepare-conditional-request")
    conditional_request.add_argument("--campaign-id", required=True)
    conditional_request.add_argument("--release-sha", required=True)
    conditional_request.add_argument("--hardware-id", required=True)
    conditional_request.add_argument("--gate-config-sha256", required=True)
    conditional_request.add_argument("--window-starts-at", required=True)
    conditional_request.add_argument("--window-ends-at", required=True)
    conditional_request.add_argument("--window-timezone", required=True)
    conditional_request.add_argument("--output", type=Path, required=True)
    conditional_request.add_argument("--g8-target-sha256")
    conditional_request.add_argument("--release-manifest-sha256")
    conditional_request.add_argument("--release-id")
    conditional_request.add_argument("--g8-plan-id")
    conditional_request.add_argument("--g8-plan-file-sha256")
    conditional_request.add_argument("--g8-plan-semantic-sha256")
    conditional_request.add_argument("--g8-usage-receipt-path", type=Path)
    approve = commands.add_parser("approve")
    approve.add_argument("--request", type=Path, required=True)
    approve.add_argument("--output", type=Path, required=True)
    conditional_approve = commands.add_parser("preauthorize")
    conditional_approve.add_argument("--request", type=Path, required=True)
    conditional_approve.add_argument("--output", type=Path, required=True)
    redeem = commands.add_parser("redeem")
    redeem.add_argument("--evidence-dir", type=Path, required=True)
    redeem.add_argument("--request", type=Path, required=True)
    redeem.add_argument("--receipt", type=Path, required=True)
    redeem.add_argument("--release-gate-report", type=Path, required=True)
    redeem.add_argument("--gate-config", type=Path, required=True)
    redeem.add_argument("--campaign-id", required=True)
    redeem.add_argument("--release-sha", required=True)
    redeem.add_argument("--hardware-id", required=True)
    redeem.add_argument("--gate-config-sha256", required=True)
    redeem.add_argument("--output", type=Path, required=True)
    compile_parser = commands.add_parser("compile")
    compile_parser.add_argument("--evidence-dir", type=Path, required=True)
    compile_parser.add_argument("--request", type=Path, required=True)
    compile_parser.add_argument("--receipt", type=Path, required=True)
    compile_parser.add_argument("--release-gate-report", type=Path, required=True)
    compile_parser.add_argument("--gate-config", type=Path, required=True)
    compile_parser.add_argument("--campaign-id", required=True)
    compile_parser.add_argument("--release-sha", required=True)
    compile_parser.add_argument("--hardware-id", required=True)
    compile_parser.add_argument("--gate-config-sha256", required=True)
    compile_parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    context = {
        field: getattr(args, field)
        for field in CONTEXT_FIELDS
        if hasattr(args, field)
    }
    try:
        if args.command == "prepare-request":
            result = build_approval_request(
                evidence_dir=args.evidence_dir,
                release_gate_report=args.release_gate_report,
                gate_config=args.gate_config,
                expected_context=context,
                output=args.output,
            )
        elif args.command == "prepare-conditional-request":
            result = build_conditional_approval_request(
                expected_context=context,
                cutover_window={
                    "starts_at": args.window_starts_at,
                    "ends_at": args.window_ends_at,
                    "timezone": args.window_timezone,
                },
                output=args.output,
                g8_smb_target_sha256=args.g8_target_sha256,
                release_manifest_sha256=args.release_manifest_sha256,
                release_id=args.release_id,
                g8_plan_id=args.g8_plan_id,
                g8_plan_file_sha256=args.g8_plan_file_sha256,
                g8_plan_semantic_sha256=args.g8_plan_semantic_sha256,
                g8_usage_receipt_path=args.g8_usage_receipt_path,
            )
        elif args.command == "approve":
            result = capture_local_approval(request_path=args.request, output=args.output)
        elif args.command == "preauthorize":
            result = capture_conditional_local_approval(request_path=args.request, output=args.output)
        elif args.command == "redeem":
            result = {"status": compile_conditional_human_approval_evidence(
                output=args.output,
                evidence_dir=args.evidence_dir,
                release_gate_report=args.release_gate_report,
                gate_config=args.gate_config,
                request_path=args.request,
                receipt_path=args.receipt,
                expected_context=context,
            )}
        else:
            result = {
                "status": compile_human_approval_evidence(
                    output=args.output,
                    evidence_dir=args.evidence_dir,
                    request_path=args.request,
                    receipt_path=args.receipt,
                    release_gate_report=args.release_gate_report,
                    gate_config=args.gate_config,
                    expected_context=context,
                )
            }
    except HumanApprovalBlocked as exc:
        raise SystemExit(f"human approval blocked: {exc}") from exc
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "EVIDENCE_ID",
    "CONDITIONAL_REQUEST_SCHEMA",
    "CONDITIONAL_RECEIPT_SCHEMA",
    "CONSUMPTION_SCHEMA",
    "HumanApprovalBlocked",
    "build_approval_request",
    "build_conditional_approval_request",
    "capture_local_approval",
    "capture_conditional_local_approval",
    "compile_human_approval_evidence",
    "compile_conditional_human_approval_evidence",
    "derive_human_approval_metrics",
    "derive_conditional_human_approval_metrics",
    "redeem_conditional_approval",
    "verify_conditional_g8_preauthorization",
]
