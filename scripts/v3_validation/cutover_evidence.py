#!/usr/bin/env python3
"""Normalize three paired atomic cutover/cold-rollback execution receipts."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from scripts.v3_validation.controlled_restart_evidence import (
    ControlledRestartBlocked,
    verify_plan as verify_controlled_restart_plan,
    verify_report as verify_controlled_restart_report,
)
from scripts.v3_cutover.core import CutoverError
from scripts.v3_cutover.mutation import (
    FORMAL_STATE_DATABASES,
    load_prepared_plan,
    _verify_release_inventory,
    v2_application_set_sha256,
    v2_initial_loaded_set_sha256,
    v2_keepalive_set_sha256,
)


EVIDENCE_ID = "atomic_release_switch_and_cold_rollback_drill_passed"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
TRANSACTION_RE = re.compile(r"^[0-9a-f]{32}$")
CUTOVER_PHASES = (
    "prepared",
    "v2_zero",
    "v3_files_installed",
    "v3_commit_intent",
    "v3_committed",
    "v3_active",
)
ROLLBACK_PHASES = (
    "rollback_started",
    "v3_zero",
    "v2_commit_intent",
    "v2_committed",
    "v2_restored",
    "complete",
)


class CutoverEvidenceBlocked(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class RawPair:
    cutover_plan_path: Path
    cutover_plan_sha256: str
    cutover_report_path: Path
    rollback_plan_path: Path
    rollback_plan_sha256: str
    rollback_report_path: Path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical(payload: Mapping[str, Any]) -> bytes:
    return (json.dumps(dict(payload), sort_keys=True, separators=(",", ":")) + "\n").encode()


def _json(path: Path, description: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CutoverEvidenceBlocked(f"{description} is unreadable: {exc}") from exc
    if not isinstance(value, dict):
        raise CutoverEvidenceBlocked(f"{description} must be a JSON object")
    return value


def _time(value: Any, description: str) -> datetime:
    if not isinstance(value, str):
        raise CutoverEvidenceBlocked(f"{description} is missing")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CutoverEvidenceBlocked(f"{description} is invalid") from exc
    if parsed.tzinfo is None:
        raise CutoverEvidenceBlocked(f"{description} lacks a timezone")
    return parsed.astimezone(timezone.utc)


def _hash_context(plan: Any) -> dict[str, str]:
    return {
        "plan_sha256": plan.source.sha256,
        "release_manifest_sha256": plan.release_manifest.sha256,
        "deploy_prepared_marker_sha256": plan.deploy_prepared_marker.sha256,
    }


def _load_plan(path: Path, digest: str, operation: str) -> tuple[Any, dict[str, Any]]:
    if not SHA256_RE.fullmatch(digest) or _sha256(path) != digest:
        raise CutoverEvidenceBlocked("cutover drill plan SHA-256 mismatch")
    try:
        plan = load_prepared_plan(
            path,
            digest,
            allow_completed_handoff_outputs=operation == "v2_to_v3_cutover",
        )
        release = _json(plan.release_manifest.path, "candidate release manifest")
        _verify_release_inventory(plan.release_manifest.path.parent, release)
    except (OSError, CutoverError) as exc:
        raise CutoverEvidenceBlocked(f"cutover drill plan is invalid: {exc}") from exc
    if plan.operation != operation:
        raise CutoverEvidenceBlocked("cutover drill plan operation mismatch")
    if plan.execution_purpose != "atomic_drill":
        raise CutoverEvidenceBlocked("atomic evidence refuses a final-cutover plan")
    marker = _json(plan.deploy_prepared_marker.path, "deploy prepared marker")
    preflight = _json(plan.pre_cutover_report.path, "pre-cutover report")
    if (
        marker.get("schema_version") != 1
        or marker.get("status") != "prepared_not_installed"
        or marker.get("ready_to_install") is not True
        or marker.get("mutation_performed") is not False
        or marker.get("deployment_mode") != "production"
        or marker.get("release_manifest_sha256") != plan.release_manifest.sha256
        or preflight.get("schema_version") != 1
        or preflight.get("decision") != "GO"
        or preflight.get("fail_closed") is not True
        or preflight.get("gaps") != []
        or preflight.get("mutation_performed") is not False
        or preflight.get("gate_config_sha256") != plan.gate_config.sha256
        or release.get("immutable") is not True
    ):
        raise CutoverEvidenceBlocked("cutover drill plan is not production/GO/release bound")
    return plan, release


def _reconciliation(
    value: Any,
    description: str,
    *,
    expected_owner: str,
    require_v3_ledgers: bool,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CutoverEvidenceBlocked(f"{description} is missing")
    array_names = (
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
    inventory = value.get("source_inventory")
    sources = value.get("sources_probed")
    expected_formal_sources = {
        f"formal:{relative}" for relative, _tables in FORMAL_STATE_DATABASES
    }
    if (
        value.get("schema_version") != 1
        or value.get("certifiable") is not True
        or value.get("active_owner") != expected_owner
        or not isinstance(value.get("active_job_store"), str)
        or not value["active_job_store"]
        or value.get("pending_ownership_certified") is not True
        or value.get("orphaned_pending_id_hashes") != []
        or any(
            not isinstance(value.get(name), list)
            or value[name] != sorted(set(value[name]))
            or any(
                not isinstance(item, str) or not SHA256_RE.fullmatch(item)
                for item in value[name]
            )
            for name in array_names
        )
        or any(
            not isinstance(value.get(occurrences), list)
            or value[occurrences] != sorted(value[occurrences])
            or any(
                not isinstance(item, str) or not SHA256_RE.fullmatch(item)
                for item in value[occurrences]
            )
            or value[unique] != sorted(set(value[occurrences]))
            for unique, occurrences in occurrence_pairs
        )
        or len(value["pending_id_hash_occurrences"]) != len(value["pending_id_hashes"])
        or type(value.get("duplicate_committed_jobs")) is not int
        or value["duplicate_committed_jobs"] < 0
        or type(value.get("duplicate_sent_outbox")) is not int
        or value["duplicate_sent_outbox"] < 0
        or value["duplicate_committed_jobs"]
        != len(value["committed_id_hash_occurrences"])
        - len(value["committed_id_hashes"])
        or value["duplicate_sent_outbox"]
        != len(value["sent_outbox_id_hash_occurrences"])
        - len(value["sent_outbox_id_hashes"])
        or not isinstance(sources, list)
        or sources != sorted(set(sources))
        or not expected_formal_sources.issubset(set(sources))
        or f"{expected_owner}_compat_job_queue" not in sources
        or value.get("delivery_receipts_state")
        not in {"present_verified", "absent_verified"}
        or (
            value.get("delivery_receipts_state") == "present_verified"
            and f"{expected_owner}_compat_delivery_receipts" not in sources
        )
        or (
            value.get("delivery_receipts_state") == "absent_verified"
            and f"{expected_owner}_compat_delivery_receipts" in sources
        )
        or not isinstance(value.get("native_ledger_roles"), list)
        or value["native_ledger_roles"] != sorted(set(value["native_ledger_roles"]))
        or (
            require_v3_ledgers
            and value["native_ledger_roles"] != ["control", "gateway", "supervisor"]
        )
        or not isinstance(inventory, list)
        or [row.get("relative_path") for row in inventory]
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
            for row, (_relative, tables) in zip(inventory or [], FORMAL_STATE_DATABASES)
        )
        or value.get("source_inventory_sha256")
        != hashlib.sha256(
            json.dumps(inventory or [], sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    ):
        raise CutoverEvidenceBlocked(f"{description} is uncertifiable or malformed")
    return value


def _v2_marker_identity(plan: Any) -> tuple[str, str, str]:
    rows = [
        {"label": agent.label, "sha256": agent.plist.sha256}
        for agent in sorted(plan.v2_launchagents, key=lambda item: item.label)
    ]
    digest = hashlib.sha256(
        json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    root = (
        Path.home()
        / "Library"
        / "Application Support"
        / "MAGI"
        / "runtime"
        / "MAGI_v2"
    ).resolve(strict=False)
    return "v2-cold-rollback", str(root), digest


def _activation_phases(
    report: Mapping[str, Any],
    *,
    transaction_id: str,
    phases: Sequence[str],
    first_sequence: int,
    release_commit: tuple[str, str, str, str],
    started: datetime,
    finished: datetime,
) -> dict[str, str]:
    events = report.get("events")
    if not isinstance(events, list) or not events:
        raise CutoverEvidenceBlocked("cutover drill report has no ordered execution trace")
    receipts: list[Mapping[str, Any]] = []
    previous = started
    for index, event in enumerate(events, start=1):
        if not isinstance(event, dict) or event.get("sequence") != index:
            raise CutoverEvidenceBlocked("cutover drill event sequence is invalid")
        observed = _time(event.get("at"), f"cutover drill event {index} timestamp")
        if observed < previous or observed > finished:
            raise CutoverEvidenceBlocked("cutover drill event timestamps are inconsistent")
        previous = observed
        if event.get("action") == "activation_transaction":
            receipt = event.get("receipt")
            if not isinstance(receipt, dict):
                raise CutoverEvidenceBlocked("activation receipt is missing")
            receipts.append(receipt)
    if [row.get("phase") for row in receipts] != list(phases):
        raise CutoverEvidenceBlocked("activation journal phase receipts are incomplete or reordered")
    expected_sequences = list(range(first_sequence, first_sequence + len(phases)))
    if [row.get("sequence") for row in receipts] != expected_sequences:
        raise CutoverEvidenceBlocked("activation journal sequence continuity is invalid")
    previous_entry: str | None = None
    first_previous = ""
    for index, receipt in enumerate(receipts):
        evidence = receipt.get("evidence")
        chain = {
            "transaction_id": receipt.get("transaction_id"),
            "sequence": receipt.get("sequence"),
            "phase": receipt.get("phase"),
            "at": receipt.get("at"),
            "previous_entry_sha256": receipt.get("previous_entry_sha256"),
            "evidence": evidence,
        }
        calculated_entry = hashlib.sha256(_canonical(chain)).hexdigest()
        if index == 0:
            first_previous = str(receipt.get("previous_entry_sha256") or "")
        elif receipt.get("previous_entry_sha256") != previous_entry:
            raise CutoverEvidenceBlocked("activation receipt hash chain is discontinuous")
        if (
            receipt.get("transaction_id") != transaction_id
            or not isinstance(evidence, dict)
            or not isinstance(receipt.get("previous_entry_sha256"), str)
            or not SHA256_RE.fullmatch(receipt["previous_entry_sha256"])
            or receipt.get("entry_sha256") != calculated_entry
            or not isinstance(receipt.get("journal_sha256"), str)
            or not SHA256_RE.fullmatch(receipt["journal_sha256"])
            or _time(receipt.get("at"), "activation receipt timestamp") < started
            or _time(receipt.get("at"), "activation receipt timestamp") > finished
        ):
            raise CutoverEvidenceBlocked("activation receipt is not transaction/hash/time bound")
        previous_entry = calculated_entry
    commit_phase, release, release_id, release_root, manifest_sha = release_commit
    committed = next(row for row in receipts if row.get("phase") == commit_phase)
    commit_evidence = committed.get("evidence")
    marker = commit_evidence.get("active_release_marker") if isinstance(commit_evidence, dict) else None
    marker_sha = (
        hashlib.sha256(_canonical(marker)).hexdigest()
        if isinstance(marker, dict)
        else ""
    )
    if (
        not isinstance(marker, dict)
        or marker.get("schema") != "magi.v3.active-release/v1"
        or marker.get("schema_version") != 1
        or marker.get("transaction_id") != transaction_id
        or marker.get("release") != release
        or marker.get("release_id") != release_id
        or marker.get("release_root") != release_root
        or marker.get("release_manifest_sha256") != manifest_sha
        or commit_evidence.get("active_release_marker_sha256") != marker_sha
        or _time(marker.get("committed_at"), "active marker commit timestamp") < started
        or _time(marker.get("committed_at"), "active marker commit timestamp") > finished
    ):
        raise CutoverEvidenceBlocked("atomic active-release marker receipt is invalid")
    return {
        "first_previous_entry_sha256": first_previous,
        "last_entry_sha256": str(previous_entry or ""),
    }


def _report(
    path: Path,
    *,
    plan: Any,
    release: Mapping[str, Any],
    rollback: bool,
) -> dict[str, Any]:
    report = _json(path, "cutover drill execution report")
    expected = {
        "schema_version": 1,
        "report_kind": (
            "v3_to_v2_rollback_execution" if rollback else "v2_to_v3_cutover_execution"
        ),
        "status": "rollback_complete" if rollback else "cutover_complete",
        "ok": True,
        "mutation_performed": True,
        "rollback_performed": rollback,
    }
    if any(report.get(field) != value for field, value in expected.items()):
        raise CutoverEvidenceBlocked("cutover/rollback execution report did not pass cleanly")
    report_id = report.get("report_id")
    transaction_id = report.get("activation_transaction_id")
    if (
        not isinstance(report_id, str)
        or not report_id
        or not isinstance(transaction_id, str)
        or not TRANSACTION_RE.fullmatch(transaction_id)
        or report.get("hash_context") != _hash_context(plan)
    ):
        raise CutoverEvidenceBlocked("cutover report identity/hash context is invalid")
    started = _time(report.get("started_at"), "cutover report started_at")
    finished = _time(report.get("finished_at"), "cutover report finished_at")
    if finished < started:
        raise CutoverEvidenceBlocked("cutover report finished before it started")
    if rollback:
        release_id, release_root, digest = _v2_marker_identity(plan)
        commit = ("v2_committed", "v2", release_id, release_root, digest)
        phases, first_sequence = ROLLBACK_PHASES, 7
    else:
        commit = (
            "v3_committed",
            "v3",
            str(release.get("release_id")),
            str(plan.release_manifest.path.parent),
            plan.release_manifest.sha256,
        )
        phases, first_sequence = CUTOVER_PHASES, 1
    activation_chain = _activation_phases(
        report,
        transaction_id=transaction_id,
        phases=phases,
        first_sequence=first_sequence,
        release_commit=commit,
        started=started,
        finished=finished,
    )
    expected_before_owner = "v3" if rollback else "v2"
    expected_after_owner = "v2" if rollback else "v3"
    before = _reconciliation(
        report.get("reconciliation_before"),
        "reconciliation_before",
        expected_owner=expected_before_owner,
        require_v3_ledgers=rollback,
    )
    after = _reconciliation(
        report.get("reconciliation_after"),
        "reconciliation_after",
        expected_owner=expected_after_owner,
        require_v3_ledgers=True,
    )
    rto = report.get("rollback_rto_seconds")
    if rollback and (
        not isinstance(rto, (int, float))
        or isinstance(rto, bool)
        or not math.isfinite(float(rto))
        or float(rto) < 0
        or float(rto) > (finished - started).total_seconds()
    ):
        raise CutoverEvidenceBlocked("rollback RTO is invalid or exceeds the raw report duration")
    return {
        "report_id": report_id,
        "report_sha256": _sha256(path),
        "transaction_id": transaction_id,
        "started_at": started,
        "finished_at": finished,
        "rto": float(rto) if rollback else None,
        "before": before,
        "after": after,
        **activation_chain,
    }


def derive_cutover_metrics(
    *,
    pairs: Sequence[RawPair],
    controlled_restart_plan_path: Path,
    controlled_restart_plan_sha256: str,
    controlled_restart_report_path: Path,
    controlled_restart_sentinel_path: Path,
    expected_context: Mapping[str, str],
    gate_config: Mapping[str, Any],
) -> dict[str, Any]:
    if len(pairs) != 3:
        raise CutoverEvidenceBlocked("exactly three paired cutover/cold-rollback drills are required")
    if set(expected_context) != {
        "campaign_id",
        "release_sha",
        "hardware_id",
        "gate_config_sha256",
    }:
        raise CutoverEvidenceBlocked("cutover evidence release context is incomplete")
    if not isinstance(gate_config.get("promotion_thresholds"), dict):
        raise CutoverEvidenceBlocked("cutover gate thresholds are missing")
    if (
        not SHA256_RE.fullmatch(controlled_restart_plan_sha256)
        or _sha256(controlled_restart_plan_path) != controlled_restart_plan_sha256
    ):
        raise CutoverEvidenceBlocked("controlled-restart plan SHA-256 mismatch")
    restart_plan = _json(controlled_restart_plan_path, "controlled-restart plan")
    restart_report = _json(controlled_restart_report_path, "controlled-restart report")
    try:
        verify_controlled_restart_plan(
            restart_plan,
            plan_sha256=controlled_restart_plan_sha256,
            verify_release_binding=True,
        )
        verify_controlled_restart_report(
            restart_report,
            plan=restart_plan,
            plan_sha256=controlled_restart_plan_sha256,
            sentinel_path_override=controlled_restart_sentinel_path,
        )
    except ControlledRestartBlocked as exc:
        raise CutoverEvidenceBlocked(f"controlled-restart evidence is invalid: {exc}") from exc
    if any(restart_report.get(key) != value for key, value in expected_context.items()):
        raise CutoverEvidenceBlocked("controlled-restart release context drifted")
    restart_binding = restart_report.get("release_binding")
    if not isinstance(restart_binding, dict):
        raise CutoverEvidenceBlocked("controlled-restart release binding is missing")
    restart_completed = _time(
        restart_report.get("generated_at"), "controlled-restart generated_at"
    )
    source_contract = gate_config.get("source_contract")
    if (
        not isinstance(source_contract, dict)
        or source_contract.get("database_relatives")
        != [relative for relative, _tables in FORMAL_STATE_DATABASES]
    ):
        raise CutoverEvidenceBlocked("cutover gate formal database contract drifted")
    analyzed: list[dict[str, Any]] = []
    release_hashes: set[str] = set()
    deploy_hashes: set[str] = set()
    v2_application_set_hashes: set[str] = set()
    v2_initial_loaded_set_hashes: set[str] = set()
    v2_keepalive_set_hashes: set[str] = set()
    for pair in pairs:
        cutover_plan, cutover_release = _load_plan(
            pair.cutover_plan_path,
            pair.cutover_plan_sha256,
            "v2_to_v3_cutover",
        )
        rollback_plan, rollback_release = _load_plan(
            pair.rollback_plan_path,
            pair.rollback_plan_sha256,
            "v3_to_v2_rollback",
        )
        if (
            cutover_plan.release_manifest.sha256 != rollback_plan.release_manifest.sha256
            or cutover_plan.deploy_prepared_marker.sha256
            != rollback_plan.deploy_prepared_marker.sha256
            or cutover_release.get("release_id") != rollback_release.get("release_id")
            or cutover_release.get("source_snapshot_sha256") != expected_context["release_sha"]
            or cutover_plan.gate_config.sha256 != expected_context["gate_config_sha256"]
            or rollback_plan.gate_config.sha256 != expected_context["gate_config_sha256"]
            or cutover_plan.v2_application_set_sha256
            != rollback_plan.v2_application_set_sha256
            or cutover_plan.v2_application_set_sha256
            != v2_application_set_sha256(cutover_plan.v2_launchagents)
            or rollback_plan.v2_application_set_sha256
            != v2_application_set_sha256(rollback_plan.v2_launchagents)
            or cutover_plan.v2_initial_loaded_set_sha256
            != rollback_plan.v2_initial_loaded_set_sha256
            or cutover_plan.v2_initial_loaded_set_sha256
            != v2_initial_loaded_set_sha256(cutover_plan.v2_launchagents)
            or rollback_plan.v2_initial_loaded_set_sha256
            != v2_initial_loaded_set_sha256(rollback_plan.v2_launchagents)
            or cutover_plan.v2_keepalive_set_sha256
            != rollback_plan.v2_keepalive_set_sha256
            or cutover_plan.v2_keepalive_set_sha256
            != v2_keepalive_set_sha256(cutover_plan.v2_launchagents)
            or rollback_plan.v2_keepalive_set_sha256
            != v2_keepalive_set_sha256(rollback_plan.v2_launchagents)
        ):
            raise CutoverEvidenceBlocked("paired drill candidate/deploy/release context drifted")
        cutover = _report(
            pair.cutover_report_path,
            plan=cutover_plan,
            release=cutover_release,
            rollback=False,
        )
        rollback = _report(
            pair.rollback_report_path,
            plan=rollback_plan,
            release=rollback_release,
            rollback=True,
        )
        if (
            cutover["transaction_id"] != rollback["transaction_id"]
            or rollback["started_at"] < cutover["finished_at"]
            or cutover["first_previous_entry_sha256"] != "0" * 64
            or rollback["first_previous_entry_sha256"]
            != cutover["last_entry_sha256"]
        ):
            raise CutoverEvidenceBlocked("cold rollback is not paired after its atomic cutover")
        baseline_committed: set[str] = set()
        baseline_outbox: set[str] = set()
        snapshots = (cutover["before"], cutover["after"], rollback["before"])
        for snapshot in snapshots:
            baseline_committed.update(snapshot["committed_id_hashes"])
            baseline_outbox.update(snapshot["sent_outbox_id_hashes"])
        final = rollback["after"]
        cutover_pending_survivors = set(cutover["after"]["pending_id_hashes"]) | set(
            cutover["after"]["terminal_id_hashes"]
        )
        if not set(cutover["before"]["pending_id_hashes"]).issubset(
            cutover_pending_survivors
        ):
            raise CutoverEvidenceBlocked(
                "V2 pending jobs were not imported/claimed by the active V3 owner"
            )
        rollback_pending_survivors = set(final["pending_id_hashes"]) | set(
            final["terminal_id_hashes"]
        )
        if not set(rollback["before"]["pending_id_hashes"]).issubset(
            rollback_pending_survivors
        ):
            raise CutoverEvidenceBlocked(
                "V3 pending jobs were not returned/settled for the restored V2 owner"
            )
        missing_committed = baseline_committed - set(final["committed_id_hashes"])
        missing_outbox = baseline_outbox - set(final["sent_outbox_id_hashes"])
        duplicate_count = max(
            (
                len(row["committed_id_hash_occurrences"])
                - len(row["committed_id_hashes"])
                + len(row["sent_outbox_id_hash_occurrences"])
                - len(row["sent_outbox_id_hashes"])
            )
            for row in (*snapshots, final)
        )
        analyzed.append(
            {
                "transaction_id": cutover["transaction_id"],
                "cutover_plan_sha256": pair.cutover_plan_sha256,
                "rollback_plan_sha256": pair.rollback_plan_sha256,
                "cutover_report_id": cutover["report_id"],
                "rollback_report_id": rollback["report_id"],
                "cutover_report_sha256": cutover["report_sha256"],
                "rollback_report_sha256": rollback["report_sha256"],
                "rto": rollback["rto"],
                "lost": len(missing_committed) + len(missing_outbox),
                "duplicates": duplicate_count,
                "started_at": cutover["started_at"],
                "finished_at": rollback["finished_at"],
            }
        )
        release_hashes.add(cutover_plan.release_manifest.sha256)
        deploy_hashes.add(cutover_plan.deploy_prepared_marker.sha256)
        v2_application_set_hashes.add(cutover_plan.v2_application_set_sha256)
        v2_initial_loaded_set_hashes.add(cutover_plan.v2_initial_loaded_set_sha256)
        v2_keepalive_set_hashes.add(cutover_plan.v2_keepalive_set_sha256)
    for field in (
        "transaction_id",
        "cutover_plan_sha256",
        "rollback_plan_sha256",
        "cutover_report_id",
        "rollback_report_id",
        "cutover_report_sha256",
        "rollback_report_sha256",
    ):
        values = [row[field] for row in analyzed]
        if len(set(values)) != 3:
            raise CutoverEvidenceBlocked(f"three drills do not have distinct {field}")
    if (
        len(release_hashes) != 1
        or len(deploy_hashes) != 1
        or len(v2_application_set_hashes) != 1
        or len(v2_initial_loaded_set_hashes) != 1
        or len(v2_keepalive_set_hashes) != 1
    ):
        raise CutoverEvidenceBlocked(
            "three drills do not share one certified candidate/deploy/V2 launchd sets"
        )
    if (
        restart_binding.get("release_manifest_sha256") not in release_hashes
        or restart_completed > min(row["started_at"] for row in analyzed)
    ):
        raise CutoverEvidenceBlocked(
            "controlled restart is not candidate-bound and completed before all drills"
        )
    return {
        "controlled_cold_restart_verified": True,
        "atomic_switch_verified": True,
        "cold_rollback_verified": True,
        "rollback_rto_seconds": max(row["rto"] for row in analyzed),
        "lost_committed_jobs": sum(row["lost"] for row in analyzed),
        "duplicate_committed_jobs": max(row["duplicates"] for row in analyzed),
    }


def compile_cutover_evidence(
    *,
    output: Path,
    pairs: Sequence[RawPair],
    controlled_restart_plan: Path,
    controlled_restart_plan_sha256: str,
    controlled_restart_report: Path,
    controlled_restart_sentinel: Path,
    campaign_id: str,
    release_sha: str,
    hardware_id: str,
    gate_config_sha256: str,
    gate_config: Path,
) -> str:
    from scripts.v3_evidence_compiler import CompileContext, SourceArtifact, _emit

    context = CompileContext(campaign_id, release_sha, hardware_id, gate_config_sha256)
    context.validate()
    if _sha256(gate_config) != gate_config_sha256:
        raise CutoverEvidenceBlocked("cutover gate config SHA-256 mismatch")
    config = _json(gate_config, "cutover gate config")
    metrics = derive_cutover_metrics(
        pairs=pairs,
        controlled_restart_plan_path=controlled_restart_plan,
        controlled_restart_plan_sha256=controlled_restart_plan_sha256,
        controlled_restart_report_path=controlled_restart_report,
        controlled_restart_sentinel_path=controlled_restart_sentinel,
        expected_context=context.as_dict(),
        gate_config=config,
    )
    starts = [
        _time(_json(pair.cutover_report_path, "cutover report").get("started_at"), "started_at")
        for pair in pairs
    ]
    finishes = [
        _time(_json(pair.rollback_report_path, "rollback report").get("finished_at"), "finished_at")
        for pair in pairs
    ]
    sources: list[SourceArtifact] = [
        SourceArtifact("upstream_controlled_restart_plan", controlled_restart_plan),
        SourceArtifact("upstream_controlled_restart_report", controlled_restart_report),
        SourceArtifact("upstream_controlled_restart_sentinel", controlled_restart_sentinel),
    ]
    for index, pair in enumerate(pairs, start=1):
        sources.extend(
            (
                SourceArtifact(f"upstream_cutover_plan_{index}", pair.cutover_plan_path),
                SourceArtifact(f"upstream_cutover_report_{index}", pair.cutover_report_path),
                SourceArtifact(f"upstream_rollback_plan_{index}", pair.rollback_plan_path),
                SourceArtifact(f"upstream_rollback_report_{index}", pair.rollback_report_path),
            )
        )
    return _emit(
        output=output,
        evidence_id=EVIDENCE_ID,
        context=context,
        config=config,
        metrics=metrics,
        sources=sources,
        started_at=min(starts),
        completed_at=max(finishes),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--controlled-restart-plan", type=Path, required=True)
    parser.add_argument("--controlled-restart-plan-sha256", required=True)
    parser.add_argument("--controlled-restart-report", type=Path, required=True)
    parser.add_argument("--controlled-restart-sentinel", type=Path, required=True)
    parser.add_argument(
        "--pair",
        action="append",
        nargs=6,
        metavar=(
            "CUTOVER_PLAN",
            "CUTOVER_PLAN_SHA256",
            "CUTOVER_REPORT",
            "ROLLBACK_PLAN",
            "ROLLBACK_PLAN_SHA256",
            "ROLLBACK_REPORT",
        ),
        required=True,
    )
    parser.add_argument("--campaign-id", required=True)
    parser.add_argument("--release-sha", required=True)
    parser.add_argument("--hardware-id", required=True)
    parser.add_argument("--gate-config", type=Path, required=True)
    parser.add_argument("--gate-config-sha256", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    pairs = tuple(
        RawPair(Path(cutover), cutover_sha, Path(cutover_report), Path(rollback), rollback_sha, Path(rollback_report))
        for cutover, cutover_sha, cutover_report, rollback, rollback_sha, rollback_report in args.pair
    )
    try:
        status = compile_cutover_evidence(
            output=args.output,
            pairs=pairs,
            controlled_restart_plan=args.controlled_restart_plan,
            controlled_restart_plan_sha256=args.controlled_restart_plan_sha256,
            controlled_restart_report=args.controlled_restart_report,
            controlled_restart_sentinel=args.controlled_restart_sentinel,
            campaign_id=args.campaign_id,
            release_sha=args.release_sha,
            hardware_id=args.hardware_id,
            gate_config_sha256=args.gate_config_sha256,
            gate_config=args.gate_config,
        )
    except (CutoverEvidenceBlocked, CutoverError) as exc:
        raise SystemExit(f"cutover evidence blocked: {exc}") from exc
    print(json.dumps({"status": status, "evidence_id": EVIDENCE_ID}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "EVIDENCE_ID",
    "CutoverEvidenceBlocked",
    "RawPair",
    "compile_cutover_evidence",
    "derive_cutover_metrics",
]
