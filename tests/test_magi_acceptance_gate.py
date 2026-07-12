from __future__ import annotations

import json
from types import SimpleNamespace

from scripts.ops import magi_acceptance_gate as gate


def test_run_command_uses_safe_process_timeout_sec(monkeypatch):
    captured = {}

    def fake_run(command, *, cwd, timeout_sec, env_extra):
        captured.update(command=command, cwd=cwd, timeout_sec=timeout_sec, env_extra=env_extra)
        return SimpleNamespace(
            returncode=0,
            stdout='{"ok":true}',
            stderr="",
            timed_out=False,
        )

    monkeypatch.setattr(gate.safe_process, "run", fake_run)

    result = gate._run_command(["python3", "probe.py"], timeout_sec=17)

    assert result.returncode == 0
    assert captured["timeout_sec"] == 17


def test_doctor_gate_targets_live_runtime_state(monkeypatch, tmp_path):
    captured = {}
    monkeypatch.setenv("MAGI_LIVE_RUNTIME_ROOT", str(tmp_path))

    def fake_command_gate(*args, **kwargs):
        captured.update(kwargs)
        return gate.GateResult("doctor", "doctor", True, "pass")

    monkeypatch.setattr(gate, "_command_gate", fake_command_gate)

    gate._gate_command_factory("doctor", dry_run=False)

    assert captured["env"]["MAGI_RUNTIME_DIR"] == str(tmp_path / ".runtime")


def test_parse_json_from_output_prefers_largest_report_object():
    raw = 'log\n{"ok": true, "summary": {"warn": 0, "fail": 0}, "checks": [{"name": "x"}]}\n'

    payload = gate.parse_json_from_output(raw)

    assert payload is not None
    assert payload["ok"] is True
    assert "checks" in payload
    assert payload != {"name": "x"}


def test_doctor_evaluator_requires_zero_warn_and_pass_status():
    result = gate.CommandResult(
        command=["doctor"],
        returncode=0,
        elapsed_sec=0.1,
        payload={"ok": True, "status": "pass", "summary": {"pass": 10, "warn": 0, "fail": 0}},
    )

    assert gate.evaluate_doctor(result) == (True, "pass", "status=pass pass=10 warn=0 fail=0")

    result.payload = {"ok": True, "status": "warn", "summary": {"pass": 10, "warn": 1, "fail": 0}}
    ok, status, detail = gate.evaluate_doctor(result)
    assert ok is False
    assert status == "fail"
    assert "warn=1" in detail


def test_business_live_evaluator_rejects_nested_failed_result():
    result = gate.CommandResult(
        command=["business"],
        returncode=0,
        elapsed_sec=0.1,
        payload={"ok": True, "results": [{"name": "token", "ok": True}, {"name": "drive", "ok": False}]},
    )

    ok, status, detail = gate.evaluate_business_live(result)

    assert ok is False
    assert status == "fail"
    assert "drive" in detail


def test_business_live_evaluator_fails_closed_for_missing_or_non_boolean_contract():
    result = gate.CommandResult(command=["business"], returncode=0, elapsed_sec=0.1, payload=None)
    assert gate.evaluate_business_live(result)[0] is False

    result.payload = {"ok": "true", "results": [{"name": "drive", "ok": True}]}
    assert gate.evaluate_business_live(result)[0] is False

    result.payload = {"ok": True, "results": [{"name": "drive", "ok": "true"}]}
    assert gate.evaluate_business_live(result)[0] is False


def test_generic_and_guardian_evaluators_fail_closed_for_empty_or_invalid_json():
    result = gate.CommandResult(command=["generic"], returncode=0, elapsed_sec=0.1, payload={})
    assert gate.evaluate_generic_json_ok(result) == (False, "fail", "exit=0 missing_json_object_contract")

    result.payload = {"ok": "yes"}
    assert gate.evaluate_generic_json_ok(result)[0] is False

    result.payload = None
    assert gate.evaluate_self_repair_guardian(result)[0] is False

    result.payload = {"success": True, "requires_human": [], "summary": {}}
    assert gate.evaluate_self_repair_guardian(result)[0] is True


def test_command_gate_surfaces_safe_process_cleanup_failure(monkeypatch):
    class CleanupFailure(RuntimeError):
        safe_process_cleanup_failed = True

    monkeypatch.setattr(gate.safe_process, "run", lambda *_args, **_kwargs: (_ for _ in ()).throw(CleanupFailure("unreaped")))

    result = gate._command_gate(
        "cleanup",
        "Cleanup",
        ["python3", "-c", "pass"],
        timeout_sec=1,
        evaluator=gate.evaluate_generic_json_ok,
    )

    assert result.ok is False
    assert result.detail == "process cleanup failed after timeout"


def test_conflict_audit_evaluator_rejects_warnings_even_with_zero_errors():
    result = gate.CommandResult(
        command=["conflict"],
        returncode=0,
        elapsed_sec=0.1,
        payload={"ok": False, "error_count": 0, "warning_count": 1},
    )

    ok, status, detail = gate.evaluate_conflict_audit(result)

    assert ok is False
    assert status == "fail"
    assert detail == "errors=0 warnings=1"


def test_verdict_red_beats_warning():
    gates = [
        gate.GateResult("dirty", "Dirty", True, "warn", blocking=False),
        gate.GateResult("doctor", "Doctor", False, "fail", blocking=True),
    ]

    assert gate.verdict(gates) == "RED"


def test_full_profile_is_commit_ready_only_when_green_and_not_dirty():
    gates = [
        gate.GateResult("repo_clean", "Git", True, "pass"),
        gate.GateResult("doctor", "Doctor", True, "pass"),
    ]

    report = gate.build_report("full", gates, allow_dirty=False, dry_run=False)
    assert report["status"] == "GREEN"
    assert report["commit_ready"] is True

    dirty_report = gate.build_report("full", gates, allow_dirty=True, dry_run=False)
    assert dirty_report["status"] == "GREEN"
    assert dirty_report["commit_ready"] is False


def test_acceptance_profiles_have_expected_boundaries():
    assert gate.PROFILE_GATES["quick"] == [
        "repo_clean",
        "residue_audit",
        "runtime_fingerprint",
        "doctor",
    ]
    assert gate.PROFILE_GATES["full"] == [
        "repo_clean",
        "residue_audit",
        "runtime_fingerprint",
        "doctor",
        "live_conflict_audit",
        "function_health",
        "cross_surface_pytest",
    ]
    assert "business_modules_live" in gate.PROFILE_GATES["live"]
    assert "production_live_suite" in gate.PROFILE_GATES["weekly-deep"]


def test_function_health_matrix_excludes_acceptance_outputs():
    matrix_path = gate._write_function_health_matrix()
    payload = json.loads(matrix_path.read_text(encoding="utf-8"))

    suites = payload.get("suites") or {}
    assert "acceptance-quick" not in suites
    assert "acceptance-full" not in suites
    assert "acceptance-live" not in suites
    assert "acceptance-weekly-deep" not in suites
    assert "ci" in suites


def test_live_runtime_artifact_prefers_live_runtime_root(tmp_path, monkeypatch):
    live_root = tmp_path / "runtime" / "MAGI_v2"
    live_root.mkdir(parents=True)
    monkeypatch.setenv("MAGI_LIVE_RUNTIME_ROOT", str(live_root))

    artifact = gate._live_runtime_artifact("live_conflict_audit_ci_latest.json")

    assert artifact == live_root / ".runtime" / "live_conflict_audit_ci_latest.json"


def test_cross_surface_pytest_clears_external_calendar_token_env(monkeypatch):
    captured = {}

    def fake_command_gate(gate_id, name, command, **kwargs):
        captured.update(kwargs)
        return gate.GateResult(gate_id, name, True, "pass", command=command)

    monkeypatch.setattr(gate, "_command_gate", fake_command_gate)

    gate._gate_command_factory("cross_surface_pytest", dry_run=False)

    assert captured["env"]["MAGI_GOOGLE_CALENDAR_TOKEN_PATH"] == ""


def test_live_acceptance_blocks_external_probes_when_runtime_preflight_fails(monkeypatch):
    called = []

    def fake_run_gate(gate_id, **_kwargs):
        called.append(gate_id)
        return gate.GateResult(
            gate_id,
            gate_id,
            gate_id != "runtime_fingerprint",
            "pass" if gate_id != "runtime_fingerprint" else "fail",
        )

    monkeypatch.setattr(gate, "run_gate", fake_run_gate)

    report = gate.run_acceptance("live")

    assert called == ["repo_clean", "residue_audit", "runtime_fingerprint"]
    blocked = {item["id"]: item for item in report["gates"]}
    assert blocked["business_modules_live"]["detail"] == "blocked_by_drift:runtime_fingerprint"
    assert report["status"] == "RED"
