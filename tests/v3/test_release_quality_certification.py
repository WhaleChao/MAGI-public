from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from scripts.v3_evidence_compiler import CompileContext, compile_campaign_evidence
from scripts.v3_release_gate import evaluate_evidence
from scripts.v3_validation import release_quality_certification as certification
from scripts.v3_validation.release_quality_evidence import (
    ReleaseQualityEvidenceError,
    sha256_json,
    summarize_report,
)
from tests.v3 import test_campaign_runner as campaign_fixtures


ROOT = Path(__file__).resolve().parents[2]
QUALITY_GATE_IDS = (
    "v3_unit_contract_integration_e2e_passed",
    "interaction_agent_kernel_memory_quality_contracts_passed",
    "context_memory_tool_plan_answer_golden_sets_passed",
    "golden_side_effect_diff_approved",
)


def _canonical(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def _rehash_report(report: dict[str, object]) -> None:
    report.pop("evidence_sha256", None)
    report["evidence_sha256"] = sha256_json(report)


def _quality_inputs(
    release: Path, profile: dict[str, object]
) -> tuple[dict[str, object], dict[str, object], dict[str, str], str]:
    report = campaign_fixtures._passing_release_quality_certification(release, profile)
    inner = report["report"]
    assert isinstance(inner, dict)
    suite_manifest = json.loads(
        (release / "config/v3_release_quality_suites.json").read_text(encoding="utf-8")
    )
    manifest = json.loads((release / "release-manifest.json").read_text(encoding="utf-8"))
    release_files = {str(row["path"]): str(row["sha256"]) for row in manifest["files"]}
    runtime_sha = str(inner["release_binding"]["python_runtime_sha256"])
    return inner, suite_manifest, release_files, runtime_sha


def _bind_test_website_admin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Provide the same hash-bound external input required by production."""
    source_root = tmp_path / "test-external" / "website"
    source = source_root / "admin" / "admin_server.py"
    source.parent.mkdir(parents=True)
    source.write_text("class AdminHandler: pass\n", encoding="utf-8")
    monkeypatch.setenv("MAGI_WEBSITE_ROOT", str(source_root))
    monkeypatch.setenv(
        "MAGI_WEBSITE_ADMIN_SHA256",
        hashlib.sha256(source.read_bytes()).hexdigest(),
    )


def test_v3_website_admin_is_hash_bound_and_staged_inside_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_root = tmp_path / "live-external" / "website"
    source = source_root / "admin" / "admin_server.py"
    source.parent.mkdir(parents=True)
    (source_root / "data").mkdir()
    (source_root / "assets").mkdir()
    source.write_text("class AdminHandler: pass\n", encoding="utf-8")
    expected_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    monkeypatch.setenv("MAGI_WEBSITE_ROOT", str(source_root))
    monkeypatch.setenv("MAGI_WEBSITE_ADMIN_SHA256", expected_sha)

    workspace = tmp_path / "quality-work"
    workspace.mkdir()
    staged, receipt = certification._stage_v3_website_admin(workspace)

    assert staged.is_relative_to(workspace)
    assert staged != source_root
    assert hashlib.sha256((staged / "admin/admin_server.py").read_bytes()).hexdigest() == expected_sha
    assert (staged / "data").is_dir()
    assert (staged / "assets").is_dir()
    assert receipt == {
        "website_admin_sha256": expected_sha,
        "staged_inside_workspace": True,
        "live_mutable_source_read_by_pytest": False,
    }


def test_v3_website_admin_staging_rejects_source_hash_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_root = tmp_path / "live-external" / "website"
    source = source_root / "admin" / "admin_server.py"
    source.parent.mkdir(parents=True)
    source.write_text("class AdminHandler: pass\n", encoding="utf-8")
    monkeypatch.setenv("MAGI_WEBSITE_ROOT", str(source_root))
    monkeypatch.setenv("MAGI_WEBSITE_ADMIN_SHA256", "0" * 64)

    workspace = tmp_path / "quality-work"
    workspace.mkdir()
    with pytest.raises(
        certification.ReleaseQualityCertificationError,
        match="not hash-bound",
    ):
        certification._stage_v3_website_admin(workspace)


@pytest.mark.parametrize("mutation", ["skipped", "xfail", "missing_teardown", "runtime"])
def test_release_quality_transcript_tampering_fails_closed(
    tmp_path: Path, mutation: str
) -> None:
    release, _release_sha = campaign_fixtures.create_release(tmp_path)
    profile = {
        "profile_id": "ordinary_week",
        "replay_start_local": "2026-07-13T00:00:00+08:00",
        "fault_seed": 1101,
    }
    report, suites, release_files, runtime_sha = _quality_inputs(release, profile)
    transcript = report["pytest_runs"]["v3_suites"]
    assert isinstance(transcript, dict)
    phase_reports = transcript["phase_reports"]
    assert isinstance(phase_reports, list)
    if mutation == "skipped":
        phase_reports[1]["outcome"] = "skipped"
        transcript["pytest_exitstatus"] = 1
    elif mutation == "xfail":
        phase_reports[1]["wasxfail"] = True
        transcript["pytest_exitstatus"] = 1
    elif mutation == "missing_teardown":
        nodeid = phase_reports[0]["nodeid"]
        phase_reports[:] = [
            row
            for row in phase_reports
            if not (row["nodeid"] == nodeid and row["when"] == "teardown")
        ]
    else:
        transcript["python_runtime_sha256"] = "0" * 64
    _rehash_report(report)

    with pytest.raises(ReleaseQualityEvidenceError):
        summarize_report(
            report,
            manifest=suites,
            release_files=release_files,
            python_runtime_sha256=runtime_sha,
            expected_profile=profile,
            expected_release_id=str(
                json.loads((release / "release-manifest.json").read_text())["release_id"]
            ),
            expected_release_manifest_sha256=hashlib.sha256(
                (release / "release-manifest.json").read_bytes()
            ).hexdigest(),
        )


def test_truthful_pytest_skip_reaches_strict_no_skip_policy(
    tmp_path: Path,
) -> None:
    release, _release_sha = campaign_fixtures.create_release(tmp_path)
    profile = {
        "profile_id": "ordinary_week",
        "replay_start_local": "2026-07-13T00:00:00+08:00",
        "fault_seed": 1101,
    }
    report, suites, release_files, runtime_sha = _quality_inputs(release, profile)
    transcript = report["pytest_runs"]["v3_suites"]
    assert isinstance(transcript, dict)
    phase_reports = transcript["phase_reports"]
    assert isinstance(phase_reports, list)
    phase_reports[1]["outcome"] = "skipped"
    transcript["pytest_exitstatus"] = 0
    _rehash_report(report)

    with pytest.raises(ReleaseQualityEvidenceError, match="strictly passing") as error:
        summarize_report(
            report,
            manifest=suites,
            release_files=release_files,
            python_runtime_sha256=runtime_sha,
            expected_profile=profile,
            expected_release_id=str(
                json.loads((release / "release-manifest.json").read_text())["release_id"]
            ),
            expected_release_manifest_sha256=hashlib.sha256(
                (release / "release-manifest.json").read_bytes()
            ).hexdigest(),
        )
    assert str(transcript["collected_nodeids"][0]) in str(error.value)


@pytest.mark.parametrize("mutation", ["golden_dependency", "sandbox_safety", "release_id"])
def test_release_quality_source_and_safety_tampering_fails_closed(
    tmp_path: Path, mutation: str
) -> None:
    release, _release_sha = campaign_fixtures.create_release(tmp_path)
    profile = {
        "profile_id": "ordinary_week",
        "replay_start_local": "2026-07-13T00:00:00+08:00",
        "fault_seed": 1101,
    }
    report, suites, release_files, runtime_sha = _quality_inputs(release, profile)
    manifest_path = release / "release-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    if mutation == "golden_dependency":
        report["golden_dependency_sha256"][
            "tests/v3/compat/behavior_fixtures/osc-file-content.json"
        ] = "0" * 64
    elif mutation == "sandbox_safety":
        report["safety"]["network_denied_by_seatbelt"] = False
    else:
        report["release_binding"]["release_id"] = "different-release"
    _rehash_report(report)

    with pytest.raises(ReleaseQualityEvidenceError):
        summarize_report(
            report,
            manifest=suites,
            release_files=release_files,
            python_runtime_sha256=runtime_sha,
            expected_profile=profile,
            expected_release_id=str(manifest["release_id"]),
            expected_release_manifest_sha256=hashlib.sha256(
                manifest_path.read_bytes()
            ).hexdigest(),
        )


@pytest.mark.skipif(os.uname().sysname != "Darwin", reason="Seatbelt is a macOS control")
def test_release_pytest_transcript_runs_inside_write_and_network_seatbelt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "isolated-home"
    temporary = tmp_path / "isolated-tmp"
    workspace = temporary / "quality-work"
    home.mkdir()
    workspace.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("TMPDIR", str(temporary))

    transcript = certification._transcript_run(
        ["tests/v3/test_core_health.py"], workspace
    )

    runtime_sha = hashlib.sha256(Path(os.sys.executable).read_bytes()).hexdigest()
    assert transcript["pytest_exitstatus"] == 0
    assert transcript["python_runtime_sha256"] == runtime_sha
    assert transcript["python_runtime_realpath_sha256"] == runtime_sha
    profile = certification._seatbelt_profile(workspace)
    outside = tmp_path / "outside.txt"
    write = subprocess.run(
        ["/usr/bin/sandbox-exec", "-p", profile, "--", "/usr/bin/touch", str(outside)],
        capture_output=True,
        text=True,
        check=False,
    )
    network = subprocess.run(
        [
            "/usr/bin/sandbox-exec",
            "-p",
            profile,
            "--",
            os.sys.executable,
            "-c",
            (
                "import errno,socket; s=socket.socket();\n"
                "try: s.connect(('127.0.0.1',9))\n"
                "except OSError as e: raise SystemExit(0 if e.errno in (errno.EPERM,errno.EACCES) else 3)\n"
                "raise SystemExit(4)"
            ),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert write.returncode != 0
    assert not outside.exists()
    assert network.returncode == 0, network.stderr


@pytest.mark.skipif(os.uname().sysname != "Darwin", reason="Seatbelt is a macOS control")
def test_outer_seatbelt_uses_explicit_producer_home_not_ambient_live_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    producer_home = tmp_path / "producer-home"
    producer_temporary = tmp_path / "producer-tmp"
    workspace = producer_temporary / "quality-work"
    producer_home.mkdir()
    workspace.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(Path.home()))

    profile = certification._seatbelt_profile(
        workspace,
        isolated_home=producer_home,
        temporary_root=producer_temporary,
    )

    assert str(producer_home) in profile
    assert str(workspace) in profile
    assert "(deny network*)" in profile


def test_v2_compat_environment_removes_sealed_release_bindings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "isolated-home"
    temporary = tmp_path / "isolated-tmp"
    workspace = temporary / "quality-work"
    home.mkdir()
    workspace.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("TMPDIR", str(temporary))
    observed: dict[str, object] = {}

    def capture(command, *, cwd, env, **_kwargs):
        observed.update(command=list(command), cwd=cwd, env=dict(env))
        transcript = Path(env["MAGI_V3_PYTEST_TRANSCRIPT"])
        transcript.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "pytest_exitstatus": 0,
                    "python_runtime_sha256": "0" * 64,
                    "python_runtime_realpath_sha256": "0" * 64,
                    "collected_nodeids": ["tests/test_probe.py::test_probe"],
                    "phase_reports": [],
                }
            ),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setenv("MAGI_AGENT_DIR", "must-not-leak")
    monkeypatch.setenv("MAGI_V3_RELEASE_ID", "must-not-leak")
    monkeypatch.setenv("MAGI_CRON_JOBS_FILE", "must-not-leak")
    monkeypatch.setenv("MAGI_CRON_JOBS_SHA256", "b" * 64)
    monkeypatch.setenv("MAGI_CRON_JOBS_SOURCE_SHA256", "c" * 64)
    monkeypatch.setenv("MAGI_V3_OFFLINE_CERTIFICATION", "1")
    monkeypatch.setenv(certification.SEATBELT_CHILD_ENV, "1")
    monkeypatch.setattr(certification.subprocess, "run", capture)
    v2_root = workspace / "v2-root"
    v2_root.mkdir()

    certification._transcript_run(
        ["tests/test_probe.py"],
        workspace,
        cwd=v2_root,
        v2_compat=True,
        v2_compat_inputs={
            "cron_jobs_sha256": "b" * 64,
            "cron_jobs_source_sha256": "c" * 64,
        },
    )

    environment = observed["env"]
    assert isinstance(environment, dict)
    assert observed["cwd"] == v2_root
    assert set(environment) == {
        name
        for name in (
            *certification.V2_COMPAT_ENV_ALLOWLIST,
            *certification.V2_COMPAT_SAFE_CRON_ENV.values(),
            "MAGI_V3_PYTEST_TRANSCRIPT",
        )
        if name in environment
    }
    assert "MAGI_AGENT_DIR" not in environment
    assert "MAGI_V3_RELEASE_ID" not in environment
    assert "MAGI_CRON_JOBS_FILE" not in environment
    assert "MAGI_CRON_JOBS_SHA256" not in environment
    assert "MAGI_CRON_JOBS_SOURCE_SHA256" not in environment
    assert environment["MAGI_V2_COMPAT_CRON_SNAPSHOT_SHA256"] == "b" * 64
    assert environment["MAGI_V2_COMPAT_CRON_SOURCE_SHA256"] == "c" * 64
    assert environment["MAGI_V3_OFFLINE_CERTIFICATION"] == "1"
    assert environment[certification.SEATBELT_CHILD_ENV] == "1"
    assert environment["MAGI_V3_PYTHON_RUNTIME"] == str(Path(sys.executable).resolve())


def test_manifest_requires_v2_disabled_and_selects_only_v3_paths() -> None:
    manifest = {
        "legacy_v2_validation": {
            "mode": "disabled",
        },
        "v3_suites": {
            "unit": ["tests/v3/test_core_health.py"],
        },
        "quality_contract_groups": {
            "quality": ["tests/v3/test_core_health.py"],
        },
        "golden_sets": {
            "answer": ["tests/v3/test_core_health.py"],
        },
    }
    release_files = {
        "tests/v3/test_core_health.py": "b" * 64,
    }

    v3_paths = certification._paths_from_manifest(manifest, release_files)

    assert v3_paths == ["tests/v3/test_core_health.py"]


def test_v3_formal_pytest_routes_all_named_state_to_its_isolated_shared_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "quality-work"
    workspace.mkdir()
    observed: dict[str, object] = {}

    def capture(command, *, env, **_kwargs):
        observed["env"] = dict(env)
        transcript = Path(env["MAGI_V3_PYTEST_TRANSCRIPT"])
        transcript.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "pytest_exitstatus": 0,
                    "python_runtime_sha256": "0" * 64,
                    "python_runtime_realpath_sha256": "0" * 64,
                    "collected_nodeids": ["tests/test_probe.py::test_probe"],
                    "phase_reports": [],
                }
            ),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setenv(certification.SEATBELT_CHILD_ENV, "1")
    monkeypatch.setenv("MAGI_PDF_NAMER_CASE_INDEX", "/production/must-not-leak.json")
    monkeypatch.setenv(
        "MAGI_PAYMENT_PROOF_REGISTRY_PATH",
        "/production/payment-proof-must-not-leak.json",
    )
    monkeypatch.setattr(certification.subprocess, "run", capture)
    binding = certification.RuntimeBinding(
        True,
        "formal_manifest_bound",
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        (certification.ROOT,),
    )

    certification._transcript_run(
        ["tests/test_probe.py"],
        workspace,
        v3_runtime_binding=binding,
    )

    environment = observed["env"]
    assert isinstance(environment, dict)
    shared = workspace / "v3-test-state" / "shared"
    assert environment["MAGI_V3_SHARED_STATE_DIR"] == str(shared)
    for env_name, (_binding_name, relative) in (
        certification.NAMED_MUTABLE_STATE_BINDINGS.items()
    ):
        assert environment[env_name] == str(shared / relative)
        assert "/production/" not in environment[env_name]
    assert Path(environment["MAGI_PDF_NAMER_CASE_INDEX"]).parent.is_dir()


def test_v2_compat_stages_complete_hash_bound_cron_without_leaking_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = tmp_path / "release"
    config = release / "config"
    config.mkdir(parents=True)
    cron = tmp_path / "cron-jobs.json"
    cron.write_text("[]\n", encoding="utf-8")
    cron_sha = hashlib.sha256(cron.read_bytes()).hexdigest()
    node = tmp_path / "node"
    node.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    node.chmod(0o755)
    source_sha = "a" * 64
    (config / "v3_schedule_dispatch_policy.json").write_text(
        json.dumps({"cron_jobs_sha256": source_sha}), encoding="utf-8"
    )
    home = tmp_path / "home"
    temporary = tmp_path / "tmp"
    workspace = temporary / "quality-work"
    home.mkdir()
    workspace.mkdir(parents=True)
    monkeypatch.setattr(certification, "ROOT", release)
    monkeypatch.setattr(certification, "V2_COMPAT_NODE_CANDIDATES", (node,))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("TMPDIR", str(temporary))
    monkeypatch.setenv(certification.SEATBELT_CHILD_ENV, "1")
    monkeypatch.setenv("MAGI_CRON_JOBS_FILE", str(cron))
    monkeypatch.setenv("MAGI_CRON_JOBS_SHA256", cron_sha)
    monkeypatch.setenv("MAGI_CRON_JOBS_SOURCE_SHA256", source_sha)
    v2_root = workspace / "v2-root"
    v2_root.mkdir()

    evidence = certification._stage_v2_compat_cron(v2_root)

    staged = v2_root / "cron_jobs.json"
    assert staged.read_bytes() == cron.read_bytes()
    assert staged.stat().st_mode & 0o777 == 0o444
    assert evidence == {
        "cron_jobs_sha256": cron_sha,
        "cron_jobs_source_sha256": source_sha,
        "node_runtime_sha256": hashlib.sha256(node.read_bytes()).hexdigest(),
        "node_runtime_realpath_sha256": hashlib.sha256(str(node).encode()).hexdigest(),
    }


def test_v2_compat_mirror_is_manifest_bound_and_detects_source_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = tmp_path / "release"
    workspace = tmp_path / "workspace"
    source = release / "tests" / "test_probe.py"
    source.parent.mkdir(parents=True)
    source.write_text("def test_probe():\n    assert True\n", encoding="utf-8")
    workspace.mkdir()
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    monkeypatch.setattr(certification, "ROOT", release)

    mirror = certification._verified_v2_compat_mirror(
        workspace,
        {"tests/test_probe.py": digest},
    )

    assert (mirror / "tests/test_probe.py").read_bytes() == source.read_bytes()
    assert not (mirror / "release-manifest.json").exists()
    (mirror / "tests/test_probe.py").write_text("tampered\n", encoding="utf-8")
    with pytest.raises(
        certification.ReleaseQualityCertificationError,
        match="mirror source drifted",
    ):
        certification._verify_v2_compat_mirror(
            mirror,
            {"tests/test_probe.py": digest},
        )


def test_campaign_entrypoint_reexecs_complete_producer_under_seatbelt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = tmp_path / "isolated-home"
    temporary = tmp_path / "isolated-tmp"
    home.mkdir()
    temporary.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("TMPDIR", str(temporary))
    _bind_test_website_admin(tmp_path, monkeypatch)
    monkeypatch.delenv(certification.SEATBELT_CHILD_ENV, raising=False)
    observed: dict[str, object] = {}

    def capture(argv, *, cwd, env, **_kwargs):
        observed.update(argv=list(argv), cwd=cwd, env=dict(env))
        return subprocess.CompletedProcess(
            list(argv), 0, certification.EVIDENCE_PREFIX + '{"status":"passed"}\n', ""
        )

    monkeypatch.setattr(certification.subprocess, "run", capture)

    assert certification.main(["--campaign-evidence"]) == 0

    argv = observed["argv"]
    env = observed["env"]
    assert isinstance(argv, list) and argv[:2] == ["/usr/bin/sandbox-exec", "-p"]
    assert "(deny network*)" in argv[2]
    assert "(deny file-write*)" in argv[2]
    assert isinstance(env, dict) and env[certification.SEATBELT_CHILD_ENV] == "1"
    producer_state = Path(str(env["MAGI_RUNTIME_DIR"])).parent
    assert producer_state.parent == temporary
    assert producer_state.name.startswith("magi-v3-producer-state-")
    assert all(
        Path(str(env[name])).is_relative_to(producer_state)
        for name in certification.FORMAL_PRODUCER_STATE_PATHS
    )
    shared = producer_state / "shared"
    assert all(
        env[env_name] == str(shared / relative)
        for env_name, (_binding_name, relative) in
        certification.NAMED_MUTABLE_STATE_BINDINGS.items()
    )
    assert not producer_state.exists()
    assert capsys.readouterr().out.strip().startswith(certification.EVIDENCE_PREFIX)


def test_campaign_entrypoint_uses_fresh_producer_state_per_pass(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = tmp_path / "isolated-home"
    temporary = tmp_path / "isolated-tmp"
    home.mkdir()
    temporary.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("TMPDIR", str(temporary))
    _bind_test_website_admin(tmp_path, monkeypatch)
    monkeypatch.delenv(certification.SEATBELT_CHILD_ENV, raising=False)
    states: list[Path] = []

    def capture(argv, *, env, **_kwargs):
        states.append(Path(str(env["MAGI_RUNTIME_DIR"])).parent)
        return subprocess.CompletedProcess(
            list(argv), 0, certification.EVIDENCE_PREFIX + '{"status":"passed"}\n', ""
        )

    monkeypatch.setattr(certification.subprocess, "run", capture)

    assert certification.main(["--campaign-evidence"]) == 0
    assert certification.main(["--campaign-evidence"]) == 0
    assert len(states) == 2 and states[0] != states[1]
    assert all(not state.exists() for state in states)
    capsys.readouterr()


def test_campaign_entrypoint_reports_sanitized_child_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = tmp_path / "isolated-home"
    temporary = tmp_path / "isolated-tmp"
    home.mkdir()
    temporary.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("TMPDIR", str(temporary))
    _bind_test_website_admin(tmp_path, monkeypatch)
    monkeypatch.delenv(certification.SEATBELT_CHILD_ENV, raising=False)

    def capture(argv, **_kwargs):
        return subprocess.CompletedProcess(
            list(argv),
            2,
            '{"ok":false,"error":"ReleaseQualityCertificationError: exact failure"}\n',
            "private diagnostic details",
        )

    monkeypatch.setattr(certification.subprocess, "run", capture)

    assert certification.main(["--campaign-evidence"]) == 2
    report = json.loads(capsys.readouterr().out)
    assert report["error"] == "seatbelt release quality child failed"
    assert report["child_error"] == (
        "ReleaseQualityCertificationError: exact failure"
    )
    assert "private diagnostic details" not in json.dumps(report)


def test_release_quality_reports_compile_and_gate_all_five_quality_chains(
    tmp_path: Path,
) -> None:
    release, release_sha = campaign_fixtures.create_release(tmp_path, armed=True)
    campaign_context = campaign_fixtures.release_context(release, release_sha)
    runner = campaign_fixtures.make_runner(
        tmp_path,
        campaign_fixtures.Clock(datetime(2026, 7, 16, 1, 0, tzinfo=timezone.utc)),
        [],
        release=release,
        release_sha=release_sha,
        context=campaign_context,
        certifiable_backend=True,
    )
    report = runner.run_today()
    report_path = runner.state_dir / "campaign-report.json"
    # Keep this chain test narrowly scoped to gates 3-7. Other producer chains
    # have their own integration tests and deliberately remain absent here.
    day_path = runner.state_dir / str(report["artifacts"][0]["path"])
    day = json.loads(day_path.read_text())
    day["workloads"] = [
        row for row in day["workloads"] if row["workload"] == "golden_business_flows"
    ]
    day_path.write_bytes(_canonical(day))
    report["artifacts"][0]["sha256"] = hashlib.sha256(day_path.read_bytes()).hexdigest()
    report_path.write_bytes(_canonical(report))
    context = CompileContext(
        campaign_context.campaign_id,
        campaign_context.release_sha,
        campaign_context.hardware_id,
        campaign_context.gate_config_sha256,
    )
    gate_config = json.loads(
        (release / "config/v3_cutover_gates.json").read_text(encoding="utf-8")
    )
    output = tmp_path / "normalized-evidence"

    statuses = compile_campaign_evidence(
        report_path=report_path,
        release_root=release,
        output=output,
        context=context,
        config=gate_config,
    )
    decision = evaluate_evidence(
        gate_config,
        output,
        expected_context=context.as_dict(),
        now=datetime.fromisoformat(str(report["generated_at"]))
        + timedelta(minutes=1),
    )

    assert {evidence_id: statuses[evidence_id] for evidence_id in QUALITY_GATE_IDS} == {
        evidence_id: "passed" for evidence_id in QUALITY_GATE_IDS
    }
    assert all(
        evidence_id in decision["passed"] for evidence_id in QUALITY_GATE_IDS
    ), decision["invalid"]
    expected_reports = int(report["required_independent_passes"])
    for evidence_id in QUALITY_GATE_IDS:
        producer = json.loads((output / f"reports/{evidence_id}.json").read_text())
        assert sum(
            row["role"] == "upstream_release_quality_report"
            for row in producer["source_artifacts"]
        ) == expected_reports
        assert producer["campaign_id"] == context.campaign_id
        assert producer["release_sha"] == context.release_sha
        assert producer["hardware_id"] == context.hardware_id
        assert producer["gate_config_sha256"] == context.gate_config_sha256

    # Even after an attacker updates the envelope/report digests, a normalized
    # assertion cannot override the gate's recomputation from seven inner reports.
    evidence_id = QUALITY_GATE_IDS[0]
    producer_path = output / f"reports/{evidence_id}.json"
    producer = json.loads(producer_path.read_text())
    producer["metrics"]["failed"] = 1
    producer["metrics_sha256"] = hashlib.sha256(
        _canonical(producer["metrics"])
    ).hexdigest()
    producer_path.write_bytes(_canonical(producer))
    envelope_path = output / f"{evidence_id}.json"
    envelope = json.loads(envelope_path.read_text())
    envelope["metrics_sha256"] = producer["metrics_sha256"]
    next(
        row for row in envelope["artifacts"] if row["role"] == "producer_report"
    )["sha256"] = hashlib.sha256(producer_path.read_bytes()).hexdigest()
    envelope_path.write_bytes(_canonical(envelope))
    tampered = evaluate_evidence(
        gate_config,
        output,
        expected_context=context.as_dict(),
        now=datetime.fromisoformat(str(report["generated_at"]))
        + timedelta(minutes=1),
    )
    assert any(
        "authoritative recomputation" in error
        for error in tampered["invalid"][evidence_id]
    )
