from __future__ import annotations

import hashlib
import json
import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.v3_validation.isolated_resource_window import (
    IsolatedResourceWindowError,
    verify_report,
)
from scripts.v3_validation.isolated_resource_window_collector import (
    MODEL_RESULT_PREFIX,
    PLAN_SCHEMA,
    CollectorError,
    Handle,
    HostBackend,
    Snapshot,
    collect,
    load_plan,
    parse_agx_bytes,
    parse_powermetrics_process_gpu,
    REQUIRED_STOPPED_LABELS,
)
from scripts.v3_validation.isolated_resource_window_plan_builder import _policy_thresholds


def file_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tree_sha(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix().encode()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(path.stat().st_size.to_bytes(8, "big"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


class Process:
    def __init__(self, *, model: bool) -> None:
        self.model = model
        self.stopped = False
        self.polls = 0


class FakeBackend:
    def __init__(self) -> None:
        self.clock = 1_000_000_000
        self.next_pid = 1000
        self.empty_samples = 0

    def configure_scope(self, _ports, _owner_markers):
        return None

    def isolation_probe(self, _profile, _workdir, _runtime):
        return {
            "network_probe": {"attempted": True, "denied_by_seatbelt": True, "errno": 1},
            "live_state_probe": {"attempted": True, "denied_by_seatbelt": True, "errno": 13},
            "probe_argv_sha256": "7" * 64,
        }

    def now_ns(self) -> int:
        self.clock += 1
        return self.clock

    def sleep(self, seconds: float) -> None:
        self.clock += int(seconds * 1e9)

    def preflight(self, _ports, _owner_markers, _pidfiles, _launch_labels, stopped_labels):
        result = {
            "v2_fully_stopped": True,
            "candidate_not_started_at_baseline": True,
            "production_ingress_quiesced": True,
            "v2_owner_pids": [],
            "v3_owner_pids_before_start": [],
            "production_port_owner_pids": [],
            "noncandidate_user_metal_processes": [],
            "process_inventory_sha256": "8" * 64,
            "per_process_gpu_permission": True,
            "raw_source_coverage": ["ioreg", "lsof", "powermetrics", "ps"],
            "required_stopped_launchd_labels": list(stopped_labels),
            "stopped_launchd_states": [
                {"label": label, "loaded": False, "returncode": 113,
                 "stdout_sha256": "0" * 64, "stderr_sha256": "1" * 64}
                for label in stopped_labels
            ],
        }
        result["raw_sources"] = dict(self.snapshot([]).raw_sources)
        result["process_inventory_sha256"] = result["raw_sources"]["ps"][
            "stdout_sha256"
        ]
        return result

    def start(self, argv, _cwd, _env):
        self.next_pid += 1
        process = Process(model="model" in argv[1])
        return Handle(
            self.next_pid,
            self.next_pid,
            5000 + self.next_pid,
            process,
            tuple(argv),
        )

    def snapshot(self, handles):
        names = [" ".join(handle.argv) for handle in handles]
        if not handles:
            self.empty_samples += 1
            agx = 100_000_000 if self.empty_samples == 1 else 101_000_000
            footprint = 0
            models = 0
        elif any("--arm v3-candidate" in name for name in names):
            agx = 800_000_000
            footprint = 9000
            models = 1
        elif any("--arm v2-reference" in name for name in names):
            agx = 600_000_000
            footprint = 9000
            models = 1
        elif any("--arm v2 --role application" in name for name in names):
            agx = 101_000_000
            footprint = 1000
            models = 0
        else:
            agx = 101_000_000
            footprint = 200
            models = 0
        def raw(name, stdout, rc=0):
            argv = {
                "ps": ["/bin/ps", "-axo", "pid=,uid=,ppid=,pgid=,%cpu=,command="],
                "lsof": ["/usr/sbin/lsof", "-b", "-nP", "-a", "-iTCP", "-sTCP:LISTEN", "-Fpn"],
                "ioreg": ["/usr/sbin/ioreg", "-r", "-c", "AGXAccelerator"],
                "powermetrics": ["/usr/bin/powermetrics", "--format", "plist", "--sample-count", "1", "--sample-rate", "100", "--samplers", "tasks,gpu_power", "--show-process-gpu"],
            }[name]
            row = {"argv": argv, "returncode": rc, "stdout": stdout, "stderr": "",
                   "stdout_sha256": hashlib.sha256(stdout.encode()).hexdigest(),
                   "stderr_sha256": hashlib.sha256(b"").hexdigest()}
            if name == "powermetrics":
                row.update(
                    invoker_argv=["/usr/bin/sudo", "-n", "--", *argv],
                    privilege_receipt={
                        "schema": "magi.v3.fixed-powermetrics-privilege/v1",
                        "collector_euid": 501, "collector_ran_as_root": False,
                        "invoker": "/usr/bin/sudo", "noninteractive": True,
                        "fixed_measurement_argv_sha256": hashlib.sha256(json.dumps(argv, separators=(",", ":")).encode()).hexdigest(),
                    },
                )
            return row
        pids = tuple(handle.pid for handle in handles)
        candidate_gpu = tuple(
            {"pid": pid, "gpu_time_ns": 100, "candidate": True} for pid in pids
        )
        ps_text = "1 501 0 1 0.0 /usr/bin/test\n" + "".join(
            f"{pid} 501 1 {pid} 0.0 /sealed/owned-{pid}\n" for pid in pids
        )
        power_text = plistlib.dumps(
            {"tasks": [{"pid": pid, "gpu_time_ns": 100} for pid in pids] + [{"pid": 1, "gpu_time_ns": 0}]}
        ).decode()
        return Snapshot(
            monotonic_ns=self.clock,
            physical_footprint_mb=footprint,
            cpu_percent=0.5,
            available_mb=9000,
            swapouts_mb=10,
            agx_bytes=agx,
            pids=pids,
            python_processes=min(3, len(handles)),
            model_processes=models,
            nonowned_model_processes=(),
            candidate_gpu_processes=candidate_gpu,
            noncandidate_gpu_processes=(),
            per_process_gpu_permission=True,
            raw_sources={
                "ps": raw("ps", ps_text),
                "lsof": raw("lsof", "", 1),
                "ioreg": raw("ioreg", f'"In use system memory"={agx}'),
                "powermetrics": raw("powermetrics", power_text),
            },
        )

    def poll(self, handle):
        process = handle.process
        if process.stopped:
            return 0
        if not process.model:
            return None
        process.polls += 1
        return 0 if process.polls >= 3 else None

    def finish(self, handle, _timeout):
        handle.process.stopped = True
        is_v3 = "v3-candidate" in handle.argv
        seconds = 9.5 if is_v3 else 10.0
        return (
            0,
            MODEL_RESULT_PREFIX
            + json.dumps({
                "generated_tokens": 200,
                "generation_seconds": seconds,
                "request_sha256": "e" * 64,
                "response_sha256": "f" * 64,
                "arm": "v3-candidate" if is_v3 else "v2-reference",
                "transport": "arm_owned_production_process_http",
                "owned_model_server_pid": handle.pid,
            }),
            "",
        )

    def stop(self, handles, _grace_seconds):
        for handle in handles:
            handle.process.stopped = True

    def group_gone(self, handle):
        return handle.process.stopped


def make_plan(tmp_path: Path, token: str) -> dict:
    release = tmp_path / "release"
    (release / "config").mkdir(parents=True)
    policy = release / "config/v3_resource_policy.json"
    policy_raw = (Path(__file__).resolve().parents[2] / "config/v3_resource_policy.json").read_text()
    policy.write_text(policy_raw)
    sandbox = release / "config/v3_resource_window.sb"
    sandbox.write_text((Path(__file__).resolve().parents[2] / "config/v3_resource_window.sb").read_text())
    (release / "config/v3_service_manifest.json").write_text(
        json.dumps({"schema_version": 1, "release_mode": "single_active_replacement", "deployment_mode": "production", "services": []})
    )
    (release / "config/v3_launchagent_roles.json").write_text(json.dumps({
        "schema_version": 1,
        "roles": [
            {"role": "gateway", "label": "com.magi.v3.gateway", "entrypoint_module": "magi_v3.gateway", "ports": [5002, 5003], "ownership_domains": ["production_ingress", "webhook_consumers", "external_api"]},
            {"role": "control", "label": "com.magi.v3.control", "entrypoint_module": "magi_v3.control", "ports": [8088], "ownership_domains": ["control_plane", "scheduler", "durable_ledger_writer"]},
            {"role": "supervisor", "label": "com.magi.v3.supervisor", "entrypoint_module": "magi_v3.supervisor_service", "ports": [], "ownership_domains": ["worker_processes", "resource_leases"]},
        ],
    }))
    website = tmp_path / "website"
    (website / "admin").mkdir(parents=True)
    website_admin = website / "admin/admin_server.py"
    website_admin.write_text("# admin\n")
    external = tmp_path / "external"
    external.mkdir()
    config = external / "config.json"
    credentials = external / "credentials.json"
    config.write_text("{}\n")
    credentials.write_text("{}\n")
    config.chmod(0o600)
    credentials.chmod(0o600)
    calendar_token = external / "google-calendar-token.json"
    laf_token = external / "laf-gmail-token.pickle"
    file_review_token = external / "filereview-token.pickle"
    for token_path in (calendar_token, laf_token, file_review_token):
        token_path.write_bytes(b"inert-token\n")
    model = tmp_path / "model"
    model.mkdir()
    (model / "weights.bin").write_bytes(b"model")
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("prompt")
    (release / "bin").mkdir()
    runtime = release / "bin/python"
    shutil.copy2(Path(sys.executable).resolve(), runtime)
    runtime.chmod(0o555)
    phase_token = "outer-zero-owner-phase"
    os.environ["MAGI_V3_ISOLATED_LIVE_ZERO_OWNER_PHASE_TOKEN"] = phase_token
    (release / "scripts/v3_validation").mkdir(parents=True)
    core_adapter = release / "scripts/v3_validation/resource_window_core_adapter.py"
    model_adapter = release / "scripts/v3_validation/resource_window_model_adapter.py"
    core_adapter.write_text("# core adapter\n")
    model_adapter.write_text("# model adapter\n")
    scripts = {"core": core_adapter, "model": model_adapter, "runtime": runtime}
    manifest = {
        "schema_version": 1,
        "immutable": True,
        "release_id": "v3-test",
        "source_snapshot_sha256": "2" * 64,
        "release_sha256": "2" * 64,
        "files": [
            {"path": path.relative_to(release).as_posix(), "sha256": file_sha(path)}
            for name, path in scripts.items()
        ],
    }
    manifest_path = release / "release-manifest.json"
    manifest_path.write_text(json.dumps(manifest, sort_keys=True))
    plan = {
        "schema": PLAN_SCHEMA,
        "approval_token_sha256": hashlib.sha256(token.encode()).hexdigest(),
        "workdir": str(tmp_path / "work"),
        "consumption_receipt_path": str(tmp_path / ".work.consumed.json"),
        "production_ports": [5002, 5003, 5014, 8080, 8081, 8088, 18080],
        "v3_owner_markers": [str(release), str(runtime)],
        "v3_pidfiles": [str(tmp_path / "work/v3-control.pid")],
        "v3_launch_labels": [
            "com.magi.v3.control",
            "com.magi.v3.gateway",
            "com.magi.v3.supervisor",
        ],
        "orchestration_binding": {
            "caller": "scripts.v3_validation.isolated_live_execute",
            "phase": "resource_window_after_v2_zero_owner",
            "v2_restore_owner": "outer_isolated_live_executor_finally",
            "collector_may_stop_or_restore_v2": False,
            "zero_owner_phase_token_sha256": hashlib.sha256(
                phase_token.encode()
            ).hexdigest(),
            "outer_plan_sha256": "d" * 64,
            "outer_plan_file_sha256": "d" * 64,
            "outer_plan_semantic_sha256": "e" * 64,
            "provisional_gate_sha256": "f" * 64,
            "provisional_gate_context": {"campaign_id": "test"},
        },
        "release_binding": {
            "release_id": "v3-test",
            "release_root": str(release),
            "release_manifest_sha256": file_sha(manifest_path),
            "release_snapshot_sha256": "2" * 64,
            "python_runtime": str(runtime),
            "python_runtime_sha256": file_sha(runtime),
            "python_runtime_binding": {
                "kind": "release_member",
                "path": str(runtime),
                "launcher_path": str(runtime),
                "sha256": file_sha(runtime),
                "realpath": str(runtime),
                "manifest": "",
                "manifest_sha256": "",
                "tree_sha256": "",
            },
            "resource_policy_sha256": file_sha(policy),
            "model_root": str(model),
            "model_tree_sha256": tree_sha(model),
            "model_backend": str(model_adapter),
            "model_backend_sha256": file_sha(model_adapter),
            "prompt_path": str(prompt),
            "prompt_sha256": file_sha(prompt),
            "sandbox_profile": str(sandbox),
            "sandbox_profile_sha256": file_sha(sandbox),
        },
        "external_inputs": {
            "website_root": str(website.resolve()),
            "website_admin_sha256": file_sha(website_admin),
            "laf_config_file": str(config.resolve()),
            "laf_config_sha256": file_sha(config),
            "laf_config_mode": "0600",
            "google_credentials_file": str(credentials.resolve()),
            "google_credentials_sha256": file_sha(credentials),
            "google_credentials_mode": "0600",
            "google_calendar_token_source_file": str(calendar_token.resolve()),
            "google_calendar_token_source_sha256": file_sha(calendar_token),
            "laf_gmail_token_source_file": str(laf_token.resolve()),
            "laf_gmail_token_source_sha256": file_sha(laf_token),
            "file_review_token_source_file": str(file_review_token.resolve()),
            "file_review_token_source_sha256": file_sha(file_review_token),
        },
        "policy_binding": {},
        "workload_binding": {},
        "outer_owner_contract": {
            "required_stopped_launchd_labels": list(REQUIRED_STOPPED_LABELS),
            "required_absent_process_patterns": ["omlx serve", "mlx_lm.server", "mlx_vlm.server", "whisper", "llama"],
            "zero_owner_snapshot_required_coverage": ["launchd", "ownership", "pidfile", "port", "process"],
            "outer_must_capture_initial_label_state": True,
            "outer_finally_restore_initial_label_state_exactly": True,
            "outer_restore_readiness": {},
            "restore_proof_owner": "outer_isolated_live_executor_finally",
        },
        "commands": {
            "v2_core": [[str(runtime), str(core_adapter), "--arm", "v2", "--role", "application", "--release-root", str(release)]],
            "v3_core": [
                [str(runtime), str(core_adapter), "--arm", "v3", "--role", role, "--release-root", str(release)]
                for role in ("control", "supervisor", "gateway")
            ],
            "v2_model": [str(runtime), str(model_adapter), "--arm", "v2-reference", "--backend", "mlx_lm", "--model", str(model), "--prompt", str(prompt), "--max-tokens", "256", "--model-port", "18080", "--arm-endpoint", "http://127.0.0.1:5003/collab/chat"],
            "v3_model": [str(runtime), str(model_adapter), "--arm", "v3-candidate", "--backend", "mlx_lm", "--model", str(model), "--prompt", str(prompt), "--max-tokens", "256", "--model-port", "18080", "--arm-endpoint", "http://127.0.0.1:5003/collab/chat"],
            "model_repeats": 3,
        },
        "durations": {
            "negative_control_seconds": 30,
            "v2_reference_seconds": 60,
            "v3_deep_idle_seconds": 1800,
            "sample_interval_seconds": 10,
            "model_timeout_seconds": 300,
            "model_sample_interval_seconds": 1,
            "stop_grace_seconds": 1,
        },
        "thresholds": _policy_thresholds(json.loads(policy_raw)),
    }
    plan["policy_binding"] = {
        "policy_raw_json": policy_raw,
        "policy_raw_sha256": file_sha(policy),
        "resolved_thresholds": plan["thresholds"],
        "resolved_thresholds_sha256": hashlib.sha256(json.dumps(plan["thresholds"], ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
    }
    request = {"schema": "magi.v3.resource-window-matched-request/v1", "corpus_sha256": file_sha(prompt), "model_tree_sha256": tree_sha(model), "max_tokens": 256, "temperature": 0, "seed": 63181107, "repeats_per_arm": 3}
    composition = {
        "schema": "magi.v3.resource-window-production-composition/v1",
        "v2": {},
        "v3": {},
        "external_inputs": dict(plan["external_inputs"]),
        "arm_transport": "arm_owned_production_process",
        "shared_direct_backend": False,
    }
    composition["composition_sha256"] = hashlib.sha256(json.dumps(composition, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    plan["workload_binding"] = {
        "request": request,
        "request_sha256": hashlib.sha256(json.dumps(request, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
        "http_request_sha256": "e" * 64,
        "composition": composition,
        "same_corpus_model_request_required": True,
    }
    plan["plan_sha256"] = hashlib.sha256(
        json.dumps(plan, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return plan


def test_collector_executes_full_unshortened_window_and_emits_verifiable_raw_report(
    tmp_path: Path,
) -> None:
    token = "one-time-approval"
    report = collect(make_plan(tmp_path, token), token, backend=FakeBackend())
    metrics = verify_report(report, expected_release_id="v3-test")

    assert report["resource_profiles"]["total_magi_deep_idle"]["observation_seconds"] >= 1800
    assert len(report["model_benchmark"]["arms"]) == 6
    assert metrics["g8"]["model_tokens_per_second_measured"] is True
    assert metrics["g9"]["all_budgets_passed"] is True
    assert metrics["g25"]["per_process_metal_bytes_available"] is False
    assert metrics["g25"]["metal_returned_to_baseline"] is True
    report_plan = make_plan(tmp_path / "consumed", token)
    collect(report_plan, token, backend=FakeBackend())
    # The receipt is consumed before any long observation and cannot be reused.
    with pytest.raises(FileExistsError):
        collect(report_plan, token, backend=FakeBackend())


def test_collector_refuses_shortened_window_and_wrong_token(tmp_path: Path) -> None:
    plan = make_plan(tmp_path, "right")
    plan["durations"]["v3_deep_idle_seconds"] = 1799
    plan.pop("plan_sha256")
    plan["plan_sha256"] = hashlib.sha256(
        json.dumps(plan, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    with pytest.raises(CollectorError, match="exactly 1800"):
        collect(plan, "right", backend=FakeBackend())

    plan = make_plan(tmp_path / "second", "right")
    with pytest.raises(CollectorError, match="token mismatch"):
        collect(plan, "wrong", backend=FakeBackend())


def test_collector_rechecks_external_website_hash_before_consumption(tmp_path: Path) -> None:
    plan = make_plan(tmp_path, "right")
    website_admin = (
        Path(plan["external_inputs"]["website_root"]) / "admin/admin_server.py"
    )
    website_admin.write_text("tampered", encoding="utf-8")

    with pytest.raises(CollectorError, match="source/hash is invalid"):
        collect(plan, "right", backend=FakeBackend())

    assert not Path(plan["consumption_receipt_path"]).exists()


def test_agx_parser_and_read_only_plan_fail_closed(tmp_path: Path) -> None:
    assert parse_agx_bytes('"PerformanceStatistics"={"In use system memory"=123}') == 123
    assert (
        parse_agx_bytes(
            '  |   "PerformanceStatistics" = {"In use system memory (driver)"=0,'
            '"Alloc system memory"=3081469952,"Renderer Utilization %"=24,'
            '"In use system memory"=528760832}'
        )
        == 528760832
    )
    with pytest.raises(CollectorError, match="ambiguous"):
        parse_agx_bytes('"In use system memory"=1 "In use system memory"=2')

    path = tmp_path / "plan.json"
    path.write_text("{}")
    with pytest.raises(CollectorError, match="read-only"):
        load_plan(path)
    path.chmod(0o444)
    assert load_plan(path) == {}


def test_powermetrics_parser_requires_explicit_per_process_gpu_field() -> None:
    raw = plistlib.dumps(
        {"tasks": [{"pid": 123, "gpu_time_ns": 456}, {"pid": 124, "gpu_time_ns": 0}]}
    ) + b"\0"
    assert parse_powermetrics_process_gpu(raw) == (
        {"pid": 123, "gpu_time_ns": 456},
        {"pid": 124, "gpu_time_ns": 0},
    )
    with pytest.raises(IsolatedResourceWindowError, match="omitted per-process GPU"):
        parse_powermetrics_process_gpu(plistlib.dumps({"tasks": [{"pid": 1}]}))


def test_powermetrics_permission_failure_is_no_go_without_running_collector_as_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(os, "geteuid", lambda: 501)
    monkeypatch.setattr(
        "scripts.v3_validation.isolated_resource_window_collector._raw_command",
        lambda argv, timeout=10: {
            "argv": list(argv), "returncode": 1, "stdout": "", "stderr": "sudo denied",
            "stdout_sha256": hashlib.sha256(b"").hexdigest(),
            "stderr_sha256": hashlib.sha256(b"sudo denied").hexdigest(),
            "_stdout_bytes": b"",
        },
    )
    with pytest.raises(CollectorError, match="fixed passwordless read-only"):
        HostBackend._powermetrics_raw()


def test_host_preflight_uses_one_nonblocking_listener_scan_and_real_v3_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = (
        "  123 501 1 123 0.0 /sealed/v3/release/bin/service\n"
        "  321 501 1 321 0.0 /usr/bin/unrelated\n"
    )
    rows = [
        {
            "pid": 123,
            "uid": 501,
            "ppid": 1,
            "pgid": 123,
            "cpu_percent": 0.0,
            "command": "/sealed/v3/release/bin/service",
        },
        {
            "pid": 321,
            "uid": 501,
            "ppid": 1,
            "pgid": 321,
            "cpu_percent": 0.0,
            "command": "/usr/bin/unrelated",
        },
    ]
    def raw_value(argv, stdout=""):
        return {"argv": list(argv), "returncode": 0, "stdout": stdout, "stderr": "",
                "stdout_sha256": hashlib.sha256(stdout.encode()).hexdigest(),
                "stderr_sha256": hashlib.sha256(b"").hexdigest(), "_stdout_bytes": b""}
    monkeypatch.setattr(
        HostBackend,
        "_ps_raw",
        staticmethod(lambda: (raw_value(["/bin/ps"], raw), rows)),
    )
    monkeypatch.setattr(
        HostBackend,
        "_listener_raw",
        staticmethod(lambda: raw_value(["/usr/sbin/lsof"], "p654\nn*:5002\np655\nn*:9999\n")),
    )
    monkeypatch.setattr(
        HostBackend,
        "_ioreg_raw",
        staticmethod(lambda: raw_value(["/usr/sbin/ioreg"], '"In use system memory"=1')),
    )
    monkeypatch.setattr(
        HostBackend,
        "_powermetrics_raw",
        staticmethod(lambda: (raw_value(["/usr/bin/powermetrics"], "plist"), ())),
    )
    pidfile = tmp_path / "v3.pid"
    pidfile.write_text("456")
    calls: list[tuple[str, ...]] = []

    def fake_run(argv, **_kwargs):
        command = tuple(argv)
        calls.append(command)
        if command[:2] == ("/bin/launchctl", "print"):
            return subprocess.CompletedProcess(argv, 0, "    pid = 789\n", "")
        raise AssertionError(command)

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = HostBackend().preflight(
        [5002, 5003],
        ["/sealed/v3/release"],
        [pidfile],
        ["com.magi.v3.control"],
        [],
    )

    assert result["v3_owner_pids_before_start"] == [123, 456, 789]
    assert result["production_port_owner_pids"] == [654]
    assert sum(call[:2] == ("/bin/launchctl", "print") for call in calls) == 1
