from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from scripts.v3_cutover.core import Owner, Snapshot
from scripts.v3_validation.provisional_resource_window_execute import (
    PROVISIONAL_EVIDENCE,
    RESOURCE_EVIDENCE,
    ProvisionalResourceWindowExecutor,
    SCHEMA,
    ResourceWindowCollectorTimeout,
    ZERO_LSOF_ARGV,
    ZERO_PS_ARGV,
    _release_resource_window,
)
from scripts.v3_validation.isolated_resource_window_collector import (
    REQUIRED_MODEL_OWNER_PATTERNS,
    REQUIRED_OBSERVED_PORTS,
    REQUIRED_STOPPED_LABELS,
)
from scripts.v3_validation.isolated_resource_window import sha256_json


NOW = datetime(2026, 7, 17, 2, 30, tzinfo=timezone(timedelta(hours=8)))
COVERAGE = frozenset({"process", "pidfile", "port", "launchd", "ownership"})


def _write_json(path: Path, value: object, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
    path.chmod(mode)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _semantic(value: dict) -> str:
    return hashlib.sha256(
        (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
    ).hexdigest()


def _receipt(operation: str, value: dict) -> dict:
    result = {
        "schema": "magi.v3.resource-window-host-receipt/v1",
        "operation": operation,
        **value,
    }
    result["receipt_sha256"] = _semantic(result)
    return result


def _launch_status(label: str, loaded: bool) -> dict:
    raw = {
        "argv": ["/bin/launchctl", "print", f"gui/501/{label}"],
        "returncode": 0 if loaded else 113,
        "stdout": "state = running\n" if loaded else "",
        "stderr": "" if loaded else "Could not find service",
        "timed_out": False,
    }
    return {
        "label": label,
        "loaded": loaded,
        "pid": 100 if loaded else None,
        "state": "running" if loaded else "",
        "launchctl_receipt": raw,
        "launchctl_receipt_sha256": _semantic(raw),
    }


def _probe_receipt(
    argv: list[str], rc: int = 0, *, stdout: str = "", stderr: str = ""
) -> dict:
    value = {
        "argv": argv,
        "returncode": rc,
        "stdout": stdout,
        "stderr": stderr,
        "timed_out": False,
        "started_at": NOW.isoformat(),
        "finished_at": NOW.isoformat(),
    }
    value["receipt_sha256"] = _semantic(value)
    return value


class FakeMachine:
    def __init__(self) -> None:
        self.actions: list[str] = []
        self.initial_loaded = {
            "com.magi.daemon",
            "com.magi.omlx",
            "com.magi.omlx-embed",
        }
        self.loaded = set(self.initial_loaded)
        self.capture: dict | None = None
        self.restore_drift = False
        self.zero_receipt_missing_label = False
        self.zero_launchd_receipt_tampered = False
        self.blackout_fail_after_mutation = False
        self.restore_v2_failure = False
        self.lsof_permission_error = False
        self.zero_raw_argv_drift = False
        self.zero_hidden_model_process = False

    @property
    def state(self) -> str:
        return "v2" if "com.magi.daemon" in self.loaded else "zero"

    def collect_ownership_snapshot(self) -> Snapshot:
        owners = () if self.state == "zero" else (Owner("v2", "release", "v2", "test", 1),)
        return Snapshot(
            owners=owners,
            coverage=COVERAGE,
            observed_at=NOW.isoformat(),
            metadata={
                "launchd": (
                    {} if self.state == "zero" else {"com.magi.daemon": {"loaded": True, "pid": 1}}
                )
            },
        )

    def activate_maintenance_blackout(self):
        self.actions.append("blackout_on")
        if self.blackout_fail_after_mutation:
            self.loaded.discard("com.magi.daemon")
            raise RuntimeError("injected partial blackout failure")
        return {"ok": True}

    def deactivate_maintenance_blackout(self):
        self.actions.append("blackout_off")
        return {"ok": True}

    def stop_v2(self):
        self.actions.append("stop_v2")
        self.loaded.discard("com.magi.daemon")
        return {"ok": True}

    def restore_v2(self):
        self.actions.append("restore_v2")
        if self.restore_v2_failure:
            raise RuntimeError("injected restore_v2 failure")
        self.loaded.add("com.magi.daemon")
        return {"ok": True}

    def verify_v2_readiness_integrity(self):
        self.actions.append("ready_v2")
        return {"ok": self.state == "v2"}

    def capture_resource_window_host_state(self, labels):
        self.actions.append("capture")
        states = []
        for label in labels:
            row = _launch_status(label, label in self.loaded)
            row.update(
                plist=f"/Users/ai/Library/LaunchAgents/{label}.plist" if label in self.loaded else "",
                plist_sha256="a" * 64 if label in self.loaded else "",
            )
            states.append(row)
        self.capture = _receipt(
            "capture_initial_state",
            {"ok": True, "labels": list(labels), "states": states, "captured_at": NOW.isoformat()},
        )
        return self.capture

    def stop_resource_window_labels(self, capture):
        self.actions.append("stop_labels")
        self.loaded.difference_update(REQUIRED_STOPPED_LABELS)
        return _receipt(
            "stop_required_labels",
            {
                "ok": True,
                "capture_receipt_sha256": capture["receipt_sha256"],
                "labels": list(REQUIRED_STOPPED_LABELS),
                "states": [_launch_status(label, False) for label in REQUIRED_STOPPED_LABELS],
            },
        )

    def collect_resource_window_zero_receipt(self, capture):
        self.actions.append("zero_proof")
        labels = list(REQUIRED_STOPPED_LABELS)
        if self.zero_receipt_missing_label:
            labels.pop()
        launchd = [_launch_status(label, False) for label in REQUIRED_STOPPED_LABELS]
        if self.zero_launchd_receipt_tampered:
            launchd[0]["launchctl_receipt"]["stderr"] = "tampered"
        ps_argv = ["/bin/ps"] if self.zero_raw_argv_drift else list(ZERO_PS_ARGV)
        ps_stdout = (
            "987 501 1 987 /usr/bin/python3 -m mlx_lm.server --port 8080\n"
            if self.zero_hidden_model_process
            else "1 0 0 1 /sbin/launchd\n"
        )
        lsof_stderr = "lsof: permission denied" if self.lsof_permission_error else ""
        return _receipt(
            "prove_zero_ownership",
            {
                "ok": True,
                "capture_receipt_sha256": capture["receipt_sha256"],
                "labels": labels,
                "launchd": launchd,
                "observed_ports": list(REQUIRED_OBSERVED_PORTS),
                "v2_processes": [],
                "model_processes": [],
                "listener_pids": [],
                "unparsed_ps_rows": [],
                "ps_receipt": _probe_receipt(
                    ps_argv, stdout=ps_stdout
                ),
                "lsof_receipt": _probe_receipt(
                    list(ZERO_LSOF_ARGV), 1, stderr=lsof_stderr
                ),
                "coverage": ["launchd", "ownership", "pidfile", "port", "process"],
            },
        )

    def restore_resource_window_labels(self, capture):
        self.actions.append("restore_labels")
        self.loaded = set(self.initial_loaded)
        if self.restore_drift:
            self.loaded.add("com.magi.omlx-phi4")
        return _receipt(
            "restore_initial_state",
            {
                "ok": True,
                "capture_receipt_sha256": capture["receipt_sha256"],
                "labels": list(REQUIRED_STOPPED_LABELS),
                "states": [
                    _launch_status(label, label in self.loaded)
                    for label in REQUIRED_STOPPED_LABELS
                ],
            },
        )

    def verify_resource_window_readiness(self, capture):
        self.actions.append("ready_all")
        urls = [
            "http://127.0.0.1:5002/health",
            "http://127.0.0.1:5003/health",
            "http://127.0.0.1:8088/health",
            "http://127.0.0.1:8080/v1/models",
            "http://127.0.0.1:8081/v1/models",
        ]
        return _receipt(
            "verify_restored_readiness",
            {
                "ok": True,
                "capture_receipt_sha256": capture["receipt_sha256"],
                "labels": list(REQUIRED_STOPPED_LABELS),
                "states": [
                    _launch_status(label, label in self.loaded)
                    for label in REQUIRED_STOPPED_LABELS
                ],
                "readiness": {
                    url: {"ok": True, "status_code": 200, "body_sha256": "b" * 64}
                    for url in urls
                },
                "required_urls": urls,
                "originally_inactive_not_started": sorted(
                    set(REQUIRED_STOPPED_LABELS) - self.initial_loaded
                ),
            },
        )


def _fixture(tmp_path: Path) -> dict[str, Path | str]:
    release = tmp_path / "release"
    release.mkdir()
    gate_config = release / "config" / "v3_cutover_gates.json"
    _write_json(
        gate_config,
        {
            "schema_version": 1,
            "timezone": "Asia/Taipei",
            "window": {"start": "02:00", "end": "04:00"},
        },
        0o400,
    )
    manifest = release / "release-manifest.json"
    _write_json(manifest, {"schema_version": 1, "release_id": "candidate"}, 0o400)
    context = {
        "campaign_id": "campaign",
        "release_sha": "1" * 64,
        "hardware_id": "mac",
        "gate_config_sha256": _sha(gate_config),
    }
    gate = tmp_path / "provisional-gate.json"
    _write_json(
        gate,
        {
            "schema": "magi.v3.provisional-resource-window-gate/v1",
            "status": "provisional_16_of_19_passed",
            "formal_live_eligible": False,
            **context,
            "release_manifest_sha256": _sha(manifest),
            "required_evidence": sorted(PROVISIONAL_EVIDENCE),
            "excluded_resource_evidence": sorted(RESOURCE_EVIDENCE),
            "counts": {"required": 16, "passed": 16, "failed": 0, "missing": 0, "invalid": 0},
        },
        0o400,
    )
    outer_token = tmp_path / "outer.token"
    outer_token.write_text("outer-secret\n", encoding="utf-8")
    outer_token.chmod(0o600)
    unsigned_outer = {
        "schema": SCHEMA,
        "operation": "isolated_resource_window_validation",
        "context": context,
        "release_manifest": {"path": str(manifest.resolve()), "sha256": _sha(manifest)},
        "provisional_gate_report": {"path": str(gate.resolve()), "sha256": _sha(gate)},
        "token_sha256": hashlib.sha256(b"outer-secret").hexdigest(),
    }
    outer = {**unsigned_outer, "plan_sha256": _semantic(unsigned_outer)}
    outer_plan = tmp_path / "outer-plan.json"
    _write_json(outer_plan, outer, 0o400)
    approval, phase = "approval-secret", "phase-secret"
    unsigned_inner = {
        "schema": "magi.v3.isolated-resource-window-plan/v1",
        "approval_token_sha256": hashlib.sha256(approval.encode()).hexdigest(),
        "release_binding": {
            "release_root": str(release.resolve()),
            "release_manifest_sha256": _sha(manifest),
            "gate_config_sha256": _sha(gate_config),
        },
        "orchestration_binding": {
            "outer_plan_sha256": _sha(outer_plan),
            "phase": "resource_window_after_v2_zero_owner",
            "v2_restore_owner": "outer_isolated_live_executor_finally",
            "zero_owner_phase_token_sha256": hashlib.sha256(phase.encode()).hexdigest(),
        },
        "outer_owner_contract": {
            "required_stopped_launchd_labels": list(REQUIRED_STOPPED_LABELS),
            "required_absent_process_patterns": list(REQUIRED_MODEL_OWNER_PATTERNS),
            "zero_owner_snapshot_required_coverage": [
                "launchd", "ownership", "pidfile", "port", "process"
            ],
            "outer_must_capture_initial_label_state": True,
            "outer_finally_restore_initial_label_state_exactly": True,
            "outer_restore_readiness": {
                "v2": [
                    "http://127.0.0.1:5002/health",
                    "http://127.0.0.1:5003/health",
                    "http://127.0.0.1:5014/health",
                    "http://127.0.0.1:8088/health",
                ],
                "model_hosts_if_initially_active": [
                    "http://127.0.0.1:8080/v1/models",
                    "http://127.0.0.1:8081/v1/models",
                ],
            },
            "restore_proof_owner": "outer_isolated_live_executor_finally",
        },
    }
    inner = {**unsigned_inner, "plan_sha256": sha256_json(unsigned_inner)}
    inner_plan = tmp_path / "inner-plan.json"
    _write_json(inner_plan, inner, 0o400)
    inner_token = tmp_path / "inner.token"
    _write_json(
        inner_token,
        {
            "approval_token": approval,
            "zero_owner_phase_token": phase,
            "outer_plan_sha256": _sha(outer_plan),
            "release_manifest_sha256": _sha(manifest),
        },
        0o600,
    )
    return {
        "outer_plan": outer_plan,
        "outer_sha": _sha(outer_plan),
        "outer_token": outer_token,
        "inner_plan": inner_plan,
        "inner_sha": _sha(inner_plan),
        "inner_token": inner_token,
        "collector_output": (tmp_path / "collector.json").resolve(),
    }


def test_resource_window_uses_hash_bound_conditional_daytime_window(tmp_path: Path) -> None:
    release = tmp_path / "release"
    manifest = release / "release-manifest.json"
    gate = release / "config" / "v3_cutover_gates.json"
    _write_json(manifest, {"schema_version": 1}, 0o400)
    _write_json(
        gate,
        {
            "schema_version": 1,
            "timezone": "Asia/Taipei",
            "window": {"start": "02:00", "end": "04:00"},
            "conditional_daytime_window": {
                "timezone": "Asia/Taipei",
                "starts_at": "2026-07-17T09:00:00+08:00",
                "ends_at": "2026-07-17T18:00:00+08:00",
            },
        },
        0o400,
    )
    digest = _sha(gate)
    outer = {
        "release_manifest": {"path": str(manifest.resolve()), "sha256": _sha(manifest)},
        "context": {"gate_config_sha256": digest},
    }
    inner = {"release_binding": {"gate_config_sha256": digest}}
    result = _release_resource_window(
        outer,
        inner,
        now=datetime(2026, 7, 17, 10, 0, tzinfo=timezone(timedelta(hours=8))),
    )
    assert result["kind"] == "conditional_daytime"
    assert result["within_window"] is True

    inner["release_binding"]["gate_config_sha256"] = "0" * 64
    with pytest.raises(Exception, match="configuration SHA drifted"):
        _release_resource_window(
            outer,
            inner,
            now=datetime(2026, 7, 17, 10, 0, tzinfo=timezone(timedelta(hours=8))),
        )


def _executor(tmp_path: Path, machine: FakeMachine, collector):
    fixture = _fixture(tmp_path)
    return ProvisionalResourceWindowExecutor(
        outer_plan_path=fixture["outer_plan"],
        outer_plan_sha256=fixture["outer_sha"],
        inner_plan_path=fixture["inner_plan"],
        inner_plan_sha256=fixture["inner_sha"],
        outer_token_file=fixture["outer_token"],
        inner_token_file=fixture["inner_token"],
        collector_output=fixture["collector_output"],
        machine=machine,
        collector=collector,
        clock=lambda: NOW,
    ), fixture


def test_provisional_window_stops_zero_collects_and_always_restores_v2(tmp_path: Path) -> None:
    machine = FakeMachine()
    observed: list[tuple[str, str]] = []

    def collector(_plan, approval, phase):
        assert machine.state == "zero"
        observed.append((approval, phase))
        return {"schema": "magi.v3.isolated-resource-window/v1", "status": "passed"}

    executor, fixture = _executor(tmp_path, machine, collector)
    report = executor.execute()

    assert report["ok"] is True
    assert report["formal_live_eligible"] is False
    assert report["v3_production_started"] is False
    assert machine.actions == [
        "capture",
        "blackout_on",
        "stop_v2",
        "stop_labels",
        "zero_proof",
        "zero_proof",
        "restore_labels",
        "restore_v2",
        "ready_v2",
        "ready_all",
        "blackout_off",
    ]
    assert machine.state == "v2"
    assert observed == [("approval-secret", "phase-secret")]
    event_actions = [event["action"] for event in report["events"]]
    assert event_actions.index("restore_initial_state") < event_actions.index("restore_v2")
    restore_event = next(
        event for event in report["events"] if event["action"] == "restore_initial_state"
    )
    assert restore_event["receipt"]["capture_receipt_sha256"] == report[
        "initial_host_capture_sha256"
    ]
    assert Path(fixture["collector_output"]).stat().st_mode & 0o777 == 0o400
    assert not Path(fixture["outer_token"]).exists()
    assert not Path(fixture["inner_token"]).exists()


def test_provisional_window_collector_failure_still_restores_v2(tmp_path: Path) -> None:
    machine = FakeMachine()

    def collector(*_args):
        raise RuntimeError("injected collector failure")

    executor, _fixture_paths = _executor(tmp_path, machine, collector)
    report = executor.execute()

    assert report["ok"] is False
    assert report["v2_restored"] is True
    assert machine.state == "v2"
    assert "collector failure" in report["error"]


def test_provisional_gate_drift_blocks_before_v2_stop(tmp_path: Path) -> None:
    machine = FakeMachine()
    executor, fixture = _executor(
        tmp_path,
        machine,
        lambda *_args: {"schema": "magi.v3.isolated-resource-window/v1", "status": "passed"},
    )
    outer = json.loads(Path(fixture["outer_plan"]).read_text(encoding="utf-8"))
    gate_path = Path(outer["provisional_gate_report"]["path"])
    gate = json.loads(gate_path.read_text(encoding="utf-8"))
    gate["counts"]["passed"] = 15
    gate_path.chmod(0o600)
    _write_json(gate_path, gate, 0o400)

    report = executor.execute()

    assert report["ok"] is False
    assert report["mutation_performed"] is False
    assert machine.actions == []


def test_missing_required_model_label_blocks_before_any_host_mutation(tmp_path: Path) -> None:
    machine = FakeMachine()
    executor, fixture = _executor(
        tmp_path,
        machine,
        lambda *_args: {"schema": "magi.v3.isolated-resource-window/v1", "status": "passed"},
    )
    inner_path = Path(fixture["inner_plan"])
    inner = json.loads(inner_path.read_text())
    inner["outer_owner_contract"]["required_stopped_launchd_labels"].pop()
    inner.pop("plan_sha256")
    inner["plan_sha256"] = sha256_json(inner)
    inner_path.chmod(0o600)
    _write_json(inner_path, inner, 0o400)
    executor.inner_plan_sha256 = _sha(inner_path)

    report = executor.execute()

    assert report["ok"] is False
    assert report["mutation_performed"] is False
    assert machine.actions == []


def test_missing_share_gateway_restore_endpoint_blocks_before_any_host_mutation(
    tmp_path: Path,
) -> None:
    machine = FakeMachine()
    executor, fixture = _executor(
        tmp_path,
        machine,
        lambda *_args: {"schema": "magi.v3.isolated-resource-window/v1", "status": "passed"},
    )
    inner_path = Path(fixture["inner_plan"])
    inner = json.loads(inner_path.read_text())
    inner["outer_owner_contract"]["outer_restore_readiness"]["v2"].remove(
        "http://127.0.0.1:5014/health"
    )
    inner.pop("plan_sha256")
    inner["plan_sha256"] = sha256_json(inner)
    inner_path.chmod(0o600)
    _write_json(inner_path, inner, 0o400)
    executor.inner_plan_sha256 = _sha(inner_path)

    report = executor.execute()

    assert report["ok"] is False
    assert report["mutation_performed"] is False
    assert machine.actions == []


def test_restore_drift_is_blocked_and_never_reported_as_restored(tmp_path: Path) -> None:
    machine = FakeMachine()
    machine.restore_drift = True
    executor, _fixture_paths = _executor(
        tmp_path,
        machine,
        lambda *_args: {"schema": "magi.v3.isolated-resource-window/v1", "status": "passed"},
    )

    report = executor.execute()

    assert report["ok"] is False
    assert report["v2_restored"] is False
    assert report["initial_host_state_restored"] is False
    assert "restored launchd label state drifted" in report["error"]


def test_v2_reappearing_after_collector_fails_zero_proof_and_restores(tmp_path: Path) -> None:
    machine = FakeMachine()

    def collector(*_args):
        machine.loaded.add("com.magi.daemon")
        return {"schema": "magi.v3.isolated-resource-window/v1", "status": "passed"}

    executor, _fixture_paths = _executor(tmp_path, machine, collector)
    report = executor.execute()

    assert report["ok"] is False
    assert report["v2_restored"] is True
    assert machine.loaded == machine.initial_loaded
    assert "did not prove zero ownership" in report["error"]


def test_collector_timeout_always_restores_initial_labels(tmp_path: Path) -> None:
    machine = FakeMachine()

    def collector(*_args):
        raise ResourceWindowCollectorTimeout("injected timeout")

    executor, _fixture_paths = _executor(tmp_path, machine, collector)
    report = executor.execute()

    assert report["ok"] is False
    assert report["v2_restored"] is True
    assert machine.loaded == machine.initial_loaded
    assert "injected timeout" in report["error"]


def test_invalid_token_performs_no_mutation_and_proves_initial_state_preserved(
    tmp_path: Path,
) -> None:
    machine = FakeMachine()
    executor, fixture = _executor(
        tmp_path,
        machine,
        lambda *_args: {"schema": "magi.v3.isolated-resource-window/v1", "status": "passed"},
    )
    Path(fixture["outer_token"]).write_text("wrong\n")
    Path(fixture["outer_token"]).chmod(0o600)

    report = executor.execute()

    assert report["ok"] is False
    assert report["mutation_performed"] is False
    assert report["initial_host_state_restored"] is True
    assert machine.actions == ["capture", "ready_all"]
    assert machine.loaded == machine.initial_loaded


def test_incomplete_raw_zero_receipt_restores_and_blocks_collector(tmp_path: Path) -> None:
    machine = FakeMachine()
    machine.zero_receipt_missing_label = True
    called = False

    def collector(*_args):
        nonlocal called
        called = True
        return {"schema": "magi.v3.isolated-resource-window/v1", "status": "passed"}

    executor, _fixture_paths = _executor(tmp_path, machine, collector)
    report = executor.execute()

    assert report["ok"] is False
    assert report["v2_restored"] is True
    assert called is False
    assert machine.loaded == machine.initial_loaded


def test_tampered_launchctl_zero_receipt_restores_and_blocks_collector(
    tmp_path: Path,
) -> None:
    machine = FakeMachine()
    machine.zero_launchd_receipt_tampered = True
    called = False

    def collector(*_args):
        nonlocal called
        called = True
        return {"schema": "magi.v3.isolated-resource-window/v1", "status": "passed"}

    executor, _fixture_paths = _executor(tmp_path, machine, collector)
    report = executor.execute()

    assert report["ok"] is False
    assert report["v2_restored"] is True
    assert called is False
    assert machine.loaded == machine.initial_loaded
    assert "zero-ownership raw receipt is incomplete" in report["error"]


def test_partial_blackout_failure_is_treated_as_mutation_and_restored(
    tmp_path: Path,
) -> None:
    machine = FakeMachine()
    machine.blackout_fail_after_mutation = True
    executor, _fixture_paths = _executor(
        tmp_path,
        machine,
        lambda *_args: {"schema": "magi.v3.isolated-resource-window/v1", "status": "passed"},
    )

    report = executor.execute()

    assert report["ok"] is False
    assert report["mutation_performed"] is True
    assert report["v2_restored"] is True
    assert machine.loaded == machine.initial_loaded
    assert machine.actions == [
        "capture",
        "blackout_on",
        "restore_labels",
        "restore_v2",
        "ready_v2",
        "ready_all",
        "blackout_off",
    ]


def test_restore_v2_failure_does_not_skip_remaining_restore_steps(
    tmp_path: Path,
) -> None:
    machine = FakeMachine()
    machine.restore_v2_failure = True
    executor, _fixture_paths = _executor(
        tmp_path,
        machine,
        lambda *_args: {"schema": "magi.v3.isolated-resource-window/v1", "status": "passed"},
    )

    report = executor.execute()

    assert report["ok"] is False
    assert report["v2_restored"] is False
    assert "restore_v2: RuntimeError" in report["error"]
    assert "restore_labels" in machine.actions
    assert "ready_all" in machine.actions
    assert machine.loaded == machine.initial_loaded


@pytest.mark.parametrize("failure", ["permission", "argv", "hidden_model"])
def test_zero_raw_probe_failure_blocks_collector_and_restores(
    tmp_path: Path, failure: str
) -> None:
    machine = FakeMachine()
    machine.lsof_permission_error = failure == "permission"
    machine.zero_raw_argv_drift = failure == "argv"
    machine.zero_hidden_model_process = failure == "hidden_model"
    called = False

    def collector(*_args):
        nonlocal called
        called = True
        return {"schema": "magi.v3.isolated-resource-window/v1", "status": "passed"}

    executor, _fixture_paths = _executor(tmp_path, machine, collector)
    report = executor.execute()

    assert report["ok"] is False
    assert report["v2_restored"] is True
    assert called is False
    assert "zero-ownership raw receipt is incomplete" in report["error"]
