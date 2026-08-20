from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import scripts.v3_validation.cutover_evidence as evidence
import scripts.v3_release_gate as release_gate
from scripts.v3_release_gate import BoundArtifact
from scripts.v3_cutover.mutation import (
    FORMAL_STATE_DATABASES,
    REQUIRED_V2_APPLICATION_LABELS,
    v2_application_set_sha256,
    v2_initial_loaded_set_sha256,
    v2_keepalive_set_sha256,
)


START = datetime(2026, 7, 17, 2, 0, tzinfo=timezone(timedelta(hours=8)))


def _reconciliation(owner: str, *, pending: list[str] | None = None) -> dict:
    pending = pending if pending is not None else ["1" * 64]
    inventory = [
        {
            "relative_path": relative,
            "tables": sorted(tables),
            "database_file_sha256": "2" * 64,
            "wal_present": False,
            "wal_sha256": "",
            "database_snapshot_sha256": "3" * 64,
        }
        for relative, tables in FORMAL_STATE_DATABASES
    ]
    sources = [f"formal:{relative}" for relative, _tables in FORMAL_STATE_DATABASES]
    sources.extend(
        [
            f"{owner}_compat_job_queue",
            f"{owner}_compat_delivery_receipts",
            "v3_control_ledger",
            "v3_gateway_ledger",
            "v3_supervisor_ledger",
        ]
    )
    committed = ["4" * 64]
    outbox = ["5" * 64]
    return {
        "schema_version": 1,
        "certifiable": True,
        "active_owner": owner,
        "active_job_store": f"/runtime/{owner}/job_queue.db",
        "pending_ownership_certified": True,
        "sources_probed": sorted(sources),
        "delivery_receipts_state": "present_verified",
        "native_ledger_roles": ["control", "gateway", "supervisor"],
        "pending_id_hashes": pending,
        "pending_id_hash_occurrences": pending,
        "native_pending_id_hashes": [],
        "orphaned_pending_id_hashes": [],
        "terminal_id_hashes": ["6" * 64],
        "committed_id_hashes": committed,
        "committed_id_hash_occurrences": committed,
        "sent_outbox_id_hashes": outbox,
        "sent_outbox_id_hash_occurrences": outbox,
        "duplicate_committed_jobs": 0,
        "duplicate_sent_outbox": 0,
        "source_inventory": inventory,
        "source_inventory_sha256": hashlib.sha256(
            json.dumps(inventory, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
    }


def _plan(tmp_path: Path) -> SimpleNamespace:
    release = tmp_path / "release" / "release-manifest.json"
    release.parent.mkdir(parents=True, exist_ok=True)
    release.write_text("{}\n", encoding="utf-8")
    agents = tuple(
        SimpleNamespace(
            label=label,
            plist=SimpleNamespace(
                path=tmp_path / "LaunchAgents" / f"{label}.plist",
                sha256="7" * 64,
            ),
            initial_loaded=True,
            initial_state="running",
            keepalive_required_running=False,
            launchctl_receipt_sha256="a" * 64,
        )
        for label in REQUIRED_V2_APPLICATION_LABELS
    )
    return SimpleNamespace(
        source=SimpleNamespace(sha256="8" * 64),
        release_manifest=SimpleNamespace(path=release, sha256="9" * 64),
        deploy_prepared_marker=SimpleNamespace(sha256="a" * 64),
        v2_launchagents=agents,
        v2_application_set_sha256=v2_application_set_sha256(agents),
        v2_initial_loaded_set_sha256=v2_initial_loaded_set_sha256(agents),
        v2_keepalive_set_sha256=v2_keepalive_set_sha256(agents),
    )


def _chain(
    *,
    transaction_id: str,
    phases: tuple[str, ...],
    first_sequence: int,
    previous: str,
    plan: SimpleNamespace,
    marker_release_id: str = "candidate-1",
) -> list[dict]:
    receipts: list[dict] = []
    for offset, phase in enumerate(phases):
        sequence = first_sequence + offset
        at = (START + timedelta(seconds=sequence)).isoformat()
        phase_evidence: dict = {}
        if phase in {"v3_committed", "v2_committed"}:
            if phase == "v3_committed":
                release, release_id = "v3", marker_release_id
                root = str(plan.release_manifest.path.parent)
                manifest_sha = plan.release_manifest.sha256
            else:
                release, release_id = "v2", "v2-cold-rollback"
                root = str(
                    (
                        Path.home()
                        / "Library"
                        / "Application Support"
                        / "MAGI"
                        / "runtime"
                        / "MAGI_v2"
                    ).resolve(strict=False)
                )
                rows = [
                    {"label": agent.label, "sha256": agent.plist.sha256}
                    for agent in sorted(
                        plan.v2_launchagents, key=lambda item: item.label
                    )
                ]
                manifest_sha = hashlib.sha256(
                    json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest()
            marker = {
                "schema": "magi.v3.active-release/v1",
                "schema_version": 1,
                "transaction_id": transaction_id,
                "release": release,
                "release_id": release_id,
                "release_root": root,
                "release_manifest_sha256": manifest_sha,
                "committed_at": at,
            }
            phase_evidence = {
                "active_release_marker": marker,
                "active_release_marker_sha256": hashlib.sha256(
                    evidence._canonical(marker)
                ).hexdigest(),
            }
        chain = {
            "transaction_id": transaction_id,
            "sequence": sequence,
            "phase": phase,
            "at": at,
            "previous_entry_sha256": previous,
            "evidence": phase_evidence,
        }
        entry_sha = hashlib.sha256(evidence._canonical(chain)).hexdigest()
        receipts.append({**chain, "entry_sha256": entry_sha, "journal_sha256": "b" * 64})
        previous = entry_sha
    return receipts


def _raw_report(
    tmp_path: Path,
    *,
    rollback: bool,
    marker_release_id: str = "candidate-1",
    omit_phase: str = "",
    corrupt_hash: bool = False,
    rto: float = 10.0,
) -> tuple[Path, SimpleNamespace]:
    plan = _plan(tmp_path)
    transaction_id = "c" * 32
    cutover_receipts = _chain(
        transaction_id=transaction_id,
        phases=evidence.CUTOVER_PHASES,
        first_sequence=1,
        previous="0" * 64,
        plan=plan,
        marker_release_id=marker_release_id,
    )
    if rollback:
        receipts = _chain(
            transaction_id=transaction_id,
            phases=evidence.ROLLBACK_PHASES,
            first_sequence=7,
            previous=cutover_receipts[-1]["entry_sha256"],
            plan=plan,
        )
    else:
        receipts = cutover_receipts
    receipts = [row for row in receipts if row["phase"] != omit_phase]
    if corrupt_hash:
        receipts[-1]["entry_sha256"] = "d" * 64
    events = [
        {
            "sequence": index,
            "at": row["at"],
            "action": "activation_transaction",
            "receipt": row,
        }
        for index, row in enumerate(receipts, start=1)
    ]
    started = START
    finished = START + timedelta(seconds=30)
    payload = {
        "schema_version": 1,
        "report_id": "rollback-report" if rollback else "cutover-report",
        "report_kind": (
            "v3_to_v2_rollback_execution" if rollback else "v2_to_v3_cutover_execution"
        ),
        "status": "rollback_complete" if rollback else "cutover_complete",
        "ok": True,
        "mutation_performed": True,
        "rollback_performed": rollback,
        "started_at": started.isoformat(),
        "finished_at": finished.isoformat(),
        "rollback_rto_seconds": rto if rollback else None,
        "hash_context": evidence._hash_context(plan),
        "activation_transaction_id": transaction_id,
        "reconciliation_before": _reconciliation("v3" if rollback else "v2"),
        "reconciliation_after": _reconciliation("v2" if rollback else "v3"),
        "events": events,
    }
    path = tmp_path / ("rollback-report.json" if rollback else "cutover-report.json")
    path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    return path, plan


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"marker_release_id": "wrong"}, "marker"),
        ({"omit_phase": "v3_commit_intent"}, "phase"),
        ({"corrupt_hash": True}, "hash"),
    ],
)
def test_raw_atomic_report_rejects_marker_journal_and_hash_faults(
    tmp_path: Path, kwargs: dict, match: str
) -> None:
    path, plan = _raw_report(tmp_path, rollback=False, **kwargs)
    with pytest.raises(evidence.CutoverEvidenceBlocked, match=match):
        evidence._report(
            path,
            plan=plan,
            release={"release_id": "candidate-1"},
            rollback=False,
        )


def test_raw_rollback_report_rejects_rto_larger_than_observed_duration(tmp_path: Path) -> None:
    path, plan = _raw_report(tmp_path, rollback=True, rto=31.0)
    with pytest.raises(evidence.CutoverEvidenceBlocked, match="RTO"):
        evidence._report(
            path,
            plan=plan,
            release={"release_id": "candidate-1"},
            rollback=True,
        )


def _pairs(tmp_path: Path) -> tuple[evidence.RawPair, ...]:
    rows = []
    for index in range(3):
        root = tmp_path / str(index)
        root.mkdir()
        paths = [root / name for name in ("cutover-plan", "cutover-report", "rollback-plan", "rollback-report")]
        for path in paths:
            path.write_text(str(index), encoding="utf-8")
        rows.append(
            evidence.RawPair(
                paths[0], f"{index + 1:x}" * 64, paths[1], paths[2], f"{index + 4:x}" * 64, paths[3]
            )
        )
    return tuple(rows)


def _restart_inputs(
    tmp_path: Path,
    *,
    release_manifest_sha256: str = "e" * 64,
    generated_at: datetime = START - timedelta(minutes=1),
) -> tuple[Path, str, Path, Path]:
    plan = tmp_path / "restart-plan.json"
    report = tmp_path / "restart-report.json"
    sentinel = tmp_path / "restart-sentinel.sqlite3"
    sentinel.write_bytes(b"sentinel")
    context = {
        "campaign_id": "campaign",
        "release_sha": "b" * 64,
        "hardware_id": "mac",
        "gate_config_sha256": "a" * 64,
    }
    plan.write_text(json.dumps(context) + "\n", encoding="utf-8")
    report.write_text(
        json.dumps(
            {
                **context,
                "generated_at": generated_at.isoformat(),
                "release_binding": {
                    "release_manifest_sha256": release_manifest_sha256,
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return plan, hashlib.sha256(plan.read_bytes()).hexdigest(), report, sentinel


def test_three_pair_metrics_reject_unclaimed_v2_pending_jobs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pairs = _pairs(tmp_path)
    plan = _plan(tmp_path)
    plan.release_manifest.sha256 = "e" * 64
    plan.deploy_prepared_marker.sha256 = "f" * 64
    plan.gate_config = SimpleNamespace(sha256="a" * 64)
    release = {"release_id": "candidate", "source_snapshot_sha256": "b" * 64}
    monkeypatch.setattr(evidence, "_load_plan", lambda *_args: (plan, release))

    def fake_report(path, *, rollback, **_kwargs):
        index = int(path.parent.name)
        before = _reconciliation("v3" if rollback else "v2")
        after = _reconciliation("v2" if rollback else "v3", pending=[])
        return {
            "report_id": f"{'r' if rollback else 'c'}-{index}",
            "report_sha256": f"{index + (7 if rollback else 1):x}" * 64,
            "transaction_id": f"{index + 1:x}" * 32,
            "started_at": START + timedelta(minutes=index * 2 + int(rollback)),
            "finished_at": START + timedelta(minutes=index * 2 + int(rollback), seconds=30),
            "rto": 10.0 if rollback else None,
            "before": before,
            "after": after,
            "first_previous_entry_sha256": "0" * 64 if not rollback else f"{index + 1:x}" * 64,
            "last_entry_sha256": f"{index + 1:x}" * 64,
        }

    monkeypatch.setattr(evidence, "_report", fake_report)
    monkeypatch.setattr(evidence, "verify_controlled_restart_plan", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(evidence, "verify_controlled_restart_report", lambda *_args, **_kwargs: None)
    restart_plan, restart_sha, restart_report, restart_sentinel = _restart_inputs(tmp_path)
    config = {
        "promotion_thresholds": {},
        "source_contract": {
            "database_relatives": [relative for relative, _tables in FORMAL_STATE_DATABASES]
        },
    }
    with pytest.raises(evidence.CutoverEvidenceBlocked, match="pending jobs"):
        evidence.derive_cutover_metrics(
            pairs=pairs,
            controlled_restart_plan_path=restart_plan,
            controlled_restart_plan_sha256=restart_sha,
            controlled_restart_report_path=restart_report,
            controlled_restart_sentinel_path=restart_sentinel,
            expected_context={
                "campaign_id": "campaign",
                "release_sha": "b" * 64,
                "hardware_id": "mac",
                "gate_config_sha256": "a" * 64,
            },
            gate_config=config,
        )


@pytest.mark.parametrize("drift_kind", ("application", "initial_state"))
def test_three_pair_metrics_reject_cross_run_v2_launchd_set_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, drift_kind: str
) -> None:
    pairs = _pairs(tmp_path)
    plans: dict[int, SimpleNamespace] = {}
    for index in range(3):
        plan = _plan(tmp_path / f"plan-{index}")
        plan.release_manifest.sha256 = "e" * 64
        plan.deploy_prepared_marker.sha256 = "f" * 64
        plan.gate_config = SimpleNamespace(sha256="a" * 64)
        if index == 2:
            if drift_kind == "application":
                plan.v2_launchagents[0].plist.sha256 = "8" * 64
                plan.v2_application_set_sha256 = v2_application_set_sha256(
                    plan.v2_launchagents
                )
                plan.v2_keepalive_set_sha256 = v2_keepalive_set_sha256(
                    plan.v2_launchagents
                )
            else:
                plan.v2_launchagents[0].initial_loaded = False
                plan.v2_launchagents[0].initial_state = ""
                plan.v2_launchagents[0].launchctl_receipt_sha256 = "b" * 64
                plan.v2_initial_loaded_set_sha256 = v2_initial_loaded_set_sha256(
                    plan.v2_launchagents
                )
        plans[index] = plan
    release = {"release_id": "candidate", "source_snapshot_sha256": "b" * 64}

    def fake_load(path, *_args):
        return plans[int(path.parent.name)], release

    def fake_report(path, *, rollback, **_kwargs):
        index = int(path.parent.name)
        first = f"{index + 1:x}" * 64
        return {
            "report_id": f"{'r' if rollback else 'c'}-{index}",
            "report_sha256": f"{index + (7 if rollback else 1):x}" * 64,
            "transaction_id": f"{index + 1:x}" * 32,
            "started_at": START + timedelta(minutes=index * 2 + int(rollback)),
            "finished_at": START + timedelta(
                minutes=index * 2 + int(rollback), seconds=30
            ),
            "rto": 10.0 if rollback else None,
            "before": _reconciliation("v3" if rollback else "v2"),
            "after": _reconciliation("v2" if rollback else "v3"),
            "first_previous_entry_sha256": first if rollback else "0" * 64,
            "last_entry_sha256": first,
        }

    monkeypatch.setattr(evidence, "_load_plan", fake_load)
    monkeypatch.setattr(evidence, "_report", fake_report)
    monkeypatch.setattr(evidence, "verify_controlled_restart_plan", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(evidence, "verify_controlled_restart_report", lambda *_args, **_kwargs: None)
    restart_plan, restart_sha, restart_report, restart_sentinel = _restart_inputs(tmp_path)
    with pytest.raises(evidence.CutoverEvidenceBlocked, match="V2 launchd sets"):
        evidence.derive_cutover_metrics(
            pairs=pairs,
            controlled_restart_plan_path=restart_plan,
            controlled_restart_plan_sha256=restart_sha,
            controlled_restart_report_path=restart_report,
            controlled_restart_sentinel_path=restart_sentinel,
            expected_context={
                "campaign_id": "campaign",
                "release_sha": "b" * 64,
                "hardware_id": "mac",
                "gate_config_sha256": "a" * 64,
            },
            gate_config={
                "promotion_thresholds": {},
                "source_contract": {
                    "database_relatives": [
                        relative for relative, _tables in FORMAL_STATE_DATABASES
                    ]
                },
            },
        )


def test_reconciliation_recomputes_duplicates_and_rejects_empty_sources() -> None:
    value = _reconciliation("v3")
    value["sources_probed"] = []
    with pytest.raises(evidence.CutoverEvidenceBlocked, match="malformed"):
        evidence._reconciliation(
            value,
            "test",
            expected_owner="v3",
            require_v3_ledgers=True,
        )

    value = _reconciliation("v3")
    value["committed_id_hash_occurrences"] = ["4" * 64, "4" * 64]
    with pytest.raises(evidence.CutoverEvidenceBlocked, match="malformed"):
        evidence._reconciliation(
            value,
            "test",
            expected_owner="v3",
            require_v3_ledgers=True,
        )


def test_release_gate_authoritatively_recomputes_exact_g27_raw_pairs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifacts: list[BoundArtifact] = []
    for name in (
        "controlled_restart_plan",
        "controlled_restart_report",
        "controlled_restart_sentinel",
    ):
        data = name.encode()
        artifacts.append(
            BoundArtifact(
                role=f"upstream_{name}",
                media_type="application/json",
                path=f"sources/{name}.json",
                sha256=hashlib.sha256(data).hexdigest(),
                data=data,
            )
        )
    for index in range(1, 4):
        for name in ("cutover_plan", "cutover_report", "rollback_plan", "rollback_report"):
            data = f"{name}-{index}".encode()
            artifacts.append(
                BoundArtifact(
                    role=f"upstream_{name}_{index}",
                    media_type="application/json",
                    path=f"sources/{name}-{index}.json",
                    sha256=hashlib.sha256(data).hexdigest(),
                    data=data,
                )
            )
    expected = {
        "controlled_cold_restart_verified": True,
        "atomic_switch_verified": True,
        "cold_rollback_verified": True,
        "rollback_rto_seconds": 10.0,
        "lost_committed_jobs": 0,
        "duplicate_committed_jobs": 0,
    }

    def fake_derive(
        *,
        pairs,
        controlled_restart_plan_path,
        controlled_restart_plan_sha256,
        controlled_restart_report_path,
        controlled_restart_sentinel_path,
        expected_context,
        gate_config,
    ):
        assert len(pairs) == 3
        assert controlled_restart_plan_path.read_bytes() == b"controlled_restart_plan"
        assert controlled_restart_report_path.read_bytes() == b"controlled_restart_report"
        assert controlled_restart_sentinel_path.read_bytes() == b"controlled_restart_sentinel"
        assert len(controlled_restart_plan_sha256) == 64
        assert expected_context["campaign_id"] == "campaign"
        assert gate_config == {"promotion_thresholds": {}}
        assert all(pair.cutover_plan_path.read_bytes().startswith(b"cutover_plan") for pair in pairs)
        return expected

    monkeypatch.setattr(evidence, "derive_cutover_metrics", fake_derive)
    context = {
        "campaign_id": "campaign",
        "release_sha": "1" * 64,
        "hardware_id": "mac",
        "gate_config_sha256": "2" * 64,
    }
    assert release_gate._authoritative_normalized_metrics(
        evidence.EVIDENCE_ID,
        artifacts,
        context,
        {"promotion_thresholds": {}},
        {},
    ) == expected

    with pytest.raises(ValueError, match="source roles"):
        release_gate._authoritative_normalized_metrics(
            evidence.EVIDENCE_ID,
            artifacts[:-1],
            context,
            {"promotion_thresholds": {}},
            {},
        )
