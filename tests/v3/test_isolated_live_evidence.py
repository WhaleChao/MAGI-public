from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from pathlib import Path

import pytest

from scripts.v3_release_gate import freeze_artifacts, validate_evidence_semantics
from scripts.v3_validation.isolated_live_evidence import (
    LIVE_EVIDENCE_IDS,
    IsolatedLiveEvidenceBlocked,
    RawRun,
    compile_isolated_live_evidence,
)
from tests.v3.test_isolated_live_execute import (
    INSIDE_WINDOW,
    FakeMachine,
    _execute,
    _json,
    _prepared,
)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _artifacts(tmp_path: Path):
    prepared = _prepared(tmp_path)
    base = _execute(prepared, FakeMachine(prepared.fixture_sha256), now=INSIDE_WINDOW)
    plan_payload = json.loads(prepared.plan.read_text(encoding="utf-8"))
    runs: list[RawRun] = []
    for index in range(1, 4):
        plan = (tmp_path / "campaign-inputs" / f"plan-{index}.json").resolve()
        plan_row = json.loads(json.dumps(plan_payload))
        plan_row["plan_id"] = f"isolated-live-campaign-{index}"
        plan_sha = _json(plan, plan_row)
        report = (tmp_path / "campaign-inputs" / f"report-{index}.json").resolve()
        report_row = json.loads(json.dumps(base))
        shift = timedelta(minutes=15 * (index - 1))
        started = INSIDE_WINDOW + shift
        finished = started + timedelta(minutes=1)
        report_row["report_id"] = f"isolated-live-report-{index}"
        report_row["plan_id"] = plan_row["plan_id"]
        report_row["hash_context"]["plan_sha256"] = plan_sha
        report_row["started_at"] = started.isoformat()
        report_row["finished_at"] = finished.isoformat()
        for event in report_row["events"]:
            event["at"] = started.isoformat()
            if event.get("action") == "native_ime_candidate_window_probe":
                command = event["detail"]["receipt"]["command_receipt"]
                command["started_at"] = started.isoformat()
                command["finished_at"] = started.isoformat()
        _json(report, report_row)
        runs.append(RawRun(plan, plan_sha, report))

    release_root = prepared.release_manifest.parent
    campaign_config = release_root / "config" / "v3_validation_campaign.json"
    gate_config = release_root / "config" / "v3_cutover_gates.json"
    offline = json.loads(prepared.offline.read_text(encoding="utf-8"))
    context = {name: offline[name] for name in ("campaign_id", "release_sha", "hardware_id", "gate_config_sha256")}
    day = (tmp_path / "campaign-inputs" / "offline-day.json").resolve()
    _json(
        day,
        {
            "schema_version": 1,
            **context,
            "status": "offline_passed",
            "evidence_class": "immutable_release_offline_campaign",
            "execution_backend": "release_launcher",
            "certifying": True,
            "release_gate_eligible": True,
            "live_execution_performed": False,
            "campaign_config_sha256": _sha(campaign_config),
            "release_manifest_sha256": _sha(prepared.release_manifest),
            "required_independent_passes": 7,
            "completed_independent_passes": 7,
            "python_runtime_sha256": "2" * 64,
            "python_runtime_manifest_sha256": "3" * 64,
            "python_runtime_tree_sha256": "4" * 64,
            "started_at": (INSIDE_WINDOW - timedelta(hours=1)).isoformat(),
            "completed_at": (INSIDE_WINDOW - timedelta(minutes=10)).isoformat(),
            "workloads": [
                {
                    "status": "offline_passed",
                    "returncode": 0,
                    "validation_pass": index,
                    "validation_profile": {"profile_id": f"profile-{index}"},
                }
                for index in range(1, 8)
            ],
        },
    )
    return prepared, runs, day, campaign_config, gate_config, context


def test_three_independent_runs_emit_four_authoritatively_recomputed_envelopes(
    tmp_path: Path,
) -> None:
    _prepared_fixture, runs, day, campaign_config, gate_config, context = _artifacts(tmp_path)
    output = (tmp_path / "normalized").resolve()
    emitted = compile_isolated_live_evidence(
        output=output,
        offline_campaign_day=day,
        campaign_config=campaign_config,
        runs=runs,
        campaign_id=context["campaign_id"],
        release_sha=context["release_sha"],
        hardware_id=context["hardware_id"],
        gate_config_sha256=context["gate_config_sha256"],
        gate_config=gate_config,
    )

    assert set(emitted) == set(LIVE_EVIDENCE_IDS)
    config = json.loads(gate_config.read_text(encoding="utf-8"))
    for evidence_id in LIVE_EVIDENCE_IDS:
        envelope = json.loads((output / f"{evidence_id}.json").read_text(encoding="utf-8"))
        frozen, errors = freeze_artifacts(envelope, output)
        assert errors == []
        assert validate_evidence_semantics(
            envelope,
            evidence_id,
            config=config,
            bound_artifacts=frozen,
            expected_context=context,
        ) == []


def test_duplicate_run_and_tampered_trace_fail_closed(tmp_path: Path) -> None:
    _prepared_fixture, runs, day, campaign_config, gate_config, context = _artifacts(tmp_path)
    with pytest.raises(IsolatedLiveEvidenceBlocked, match="independent"):
        compile_isolated_live_evidence(
            output=(tmp_path / "duplicate").resolve(),
            offline_campaign_day=day,
            campaign_config=campaign_config,
            runs=(runs[0], runs[0], runs[0]),
            campaign_id=context["campaign_id"],
            release_sha=context["release_sha"],
            hardware_id=context["hardware_id"],
            gate_config_sha256=context["gate_config_sha256"],
            gate_config=gate_config,
        )

    report = json.loads(runs[1].report_path.read_text(encoding="utf-8"))
    report["events"] = [
        event for event in report["events"] if event["action"] != "ownership_snapshot"
    ]
    for sequence, event in enumerate(report["events"], start=1):
        event["sequence"] = sequence
    _json(runs[1].report_path, report)
    with pytest.raises(IsolatedLiveEvidenceBlocked, match="handoff sequence|ownership"):
        compile_isolated_live_evidence(
            output=(tmp_path / "tampered").resolve(),
            offline_campaign_day=day,
            campaign_config=campaign_config,
            runs=runs,
            campaign_id=context["campaign_id"],
            release_sha=context["release_sha"],
            hardware_id=context["hardware_id"],
            gate_config_sha256=context["gate_config_sha256"],
            gate_config=gate_config,
        )
