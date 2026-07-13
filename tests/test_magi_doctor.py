from scripts import magi_doctor
import json
import plistlib
from datetime import datetime, timedelta, timezone


def test_package_available_falls_back_to_project_venv(monkeypatch):
    calls = []

    monkeypatch.setattr(magi_doctor.importlib.util, "find_spec", lambda name: None)
    monkeypatch.setattr(magi_doctor, "_project_python", lambda: magi_doctor.Path("/tmp/project-python"))
    monkeypatch.setattr(magi_doctor.sys, "executable", "/tmp/current-python")

    class Result:
        returncode = 0

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return Result()

    monkeypatch.setattr(magi_doctor.subprocess, "run", fake_run)

    assert magi_doctor._package_available("fastapi") is True
    assert calls and calls[0][0] == "/tmp/project-python"


def test_cron_state_checks_report_failures_and_freshness(tmp_path):
    runtime = tmp_path / ".runtime"
    runtime.mkdir()
    (runtime / "cron_state.json").write_text(
        json.dumps(
            {
                "ok_job": {
                    "last_success": True,
                    "returncode": 0,
                    "last_success_at": "2026-07-07T00:00:00+00:00",
                },
                "bad_job": {
                    "last_success": False,
                    "returncode": 2,
                    "timed_out": False,
                    "last_result_at": "2026-07-07T00:01:00+00:00",
                },
            }
        ),
        encoding="utf-8",
    )

    checks = magi_doctor._cron_state_checks(
        runtime_dir=runtime,
        now=datetime(2026, 7, 7, 1, 0, tzinfo=timezone.utc),
    )

    by_name = {check.name: check for check in checks}
    assert by_name["cron_state_failures"].status == "fail"
    assert "bad_job" in by_name["cron_state_failures"].detail
    assert by_name["cron_state_freshness"].status == "pass"


def test_cron_state_checks_treat_validation_gate_as_safe_block(tmp_path):
    runtime = tmp_path / ".runtime"
    runtime.mkdir()
    (runtime / "cron_state.json").write_text(
        json.dumps(
            {
                "job_distill_train_gemma": {
                    "last_success": False,
                    "returncode": 1,
                    "last_error": "Validation gate failed: channel_marker_leak insufficient_traditional_chinese",
                    "last_result_at": "2026-07-07T00:01:00+00:00",
                },
                "ok_job": {
                    "last_success": True,
                    "returncode": 0,
                    "last_success_at": "2026-07-07T00:02:00+00:00",
                },
            }
        ),
        encoding="utf-8",
    )

    checks = magi_doctor._cron_state_checks(
        runtime_dir=runtime,
        now=datetime(2026, 7, 7, 1, 0, tzinfo=timezone.utc),
    )

    by_name = {check.name: check for check in checks}
    assert by_name["cron_state_failures"].status == "pass"
    assert "validation-gated=job_distill_train_gemma" in by_name["cron_state_failures"].detail


def test_cron_state_checks_ignores_disabled_job_failure(tmp_path):
    runtime = tmp_path / ".runtime"
    runtime.mkdir()
    (runtime / "cron_state.json").write_text(
        json.dumps({"retired_job": {"last_success": False, "returncode": 1}}),
        encoding="utf-8",
    )
    cron_jobs = tmp_path / "cron_jobs.json"
    cron_jobs.write_text(json.dumps([{"id": "retired_job", "enabled": False}]), encoding="utf-8")

    checks = magi_doctor._cron_state_checks(runtime_dir=runtime, cron_jobs_path=cron_jobs)

    by_name = {check.name: check for check in checks}
    assert by_name["cron_state_failures"].status == "pass"
    assert "disabled=retired_job" in by_name["cron_state_failures"].detail


def test_cron_state_checks_recovers_operational_audit_after_newer_green_artifact(tmp_path):
    runtime = tmp_path / ".runtime"
    runtime.mkdir()
    (runtime / "cron_state.json").write_text(
        json.dumps(
            {
                "job_operational_hardening_audit": {
                    "last_success": False,
                    "returncode": 1,
                    "last_failure_at": "2020-01-01T00:00:00+00:00",
                }
            }
        ),
        encoding="utf-8",
    )
    (runtime / "operational_hardening_audit_latest.json").write_text(
        json.dumps(
            {
                "cron": {"parse_failure_count": 0, "collision_count": 0},
                "gmail_monitor": {"ok": True},
            }
        ),
        encoding="utf-8",
    )
    cron_jobs = tmp_path / "cron_jobs.json"
    cron_jobs.write_text(json.dumps([{"id": "job_operational_hardening_audit", "enabled": True}]), encoding="utf-8")

    checks = magi_doctor._cron_state_checks(runtime_dir=runtime, cron_jobs_path=cron_jobs)

    by_name = {check.name: check for check in checks}
    assert by_name["cron_state_failures"].status == "pass"
    assert "recovered=job_operational_hardening_audit" in by_name["cron_state_failures"].detail


def test_parse_dt_interprets_naive_scheduler_timestamp_as_local_time(monkeypatch):
    local_tz = timezone(timedelta(hours=8))

    class LocalNow(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 7, 10, 8, 0, tzinfo=local_tz)

    monkeypatch.setattr(magi_doctor, "datetime", LocalNow)

    assert magi_doctor._parse_dt("2026-07-10T08:50:00") == datetime(2026, 7, 10, 0, 50, tzinfo=timezone.utc)


def test_mtp_sidecar_check_retries_transient_health_timeouts(monkeypatch):
    responses = iter([(False, "timed out"), (False, "timed out"), (True, '{"ok":true}')])
    monkeypatch.setattr(magi_doctor, "_http_json", lambda *args, **kwargs: next(responses))
    monkeypatch.setattr(magi_doctor.time, "sleep", lambda _seconds: None)

    ok, detail = magi_doctor._mtp_sidecar_check()

    assert ok is True
    assert "retry=2" in detail


def test_launchctl_check_accepts_running_direct_menubar_fallback(monkeypatch):
    monkeypatch.setattr(
        magi_doctor,
        "_launchctl_print_status",
        lambda _label: {"checked": True, "loaded": False, "detail": "not loaded"},
    )
    monkeypatch.setattr(
        magi_doctor,
        "_direct_menubar_process",
        lambda _payload: {"pid": 123, "count": 1, "script": "/runtime/gui/magi_menubar.py"},
    )

    check = magi_doctor._launchctl_check(
        "com.magi.menubar",
        {"KeepAlive": True, "WorkingDirectory": "/runtime"},
    )

    assert check is not None
    assert check.status == "pass"
    assert "direct GUI fallback running" in check.detail


def test_launchagent_checks_flag_missing_program_arguments(tmp_path):
    home = tmp_path / "home"
    launch_dir = home / "Library" / "LaunchAgents"
    launch_dir.mkdir(parents=True)
    plist_payload = {
        "Label": "com.magi.server",
        "WorkingDirectory": str(tmp_path / "missing-root"),
        "ProgramArguments": ["/usr/bin/python3", str(tmp_path / "missing-root" / "api" / "server.py")],
    }
    (launch_dir / "com.magi.server.plist").write_bytes(plistlib.dumps(plist_payload))

    checks = magi_doctor._launchagent_checks(home=home, repo_root=tmp_path / "repo", live_root=tmp_path / "runtime")

    assert checks[0].status == "fail"
    assert "missing path" in checks[0].detail


def test_launchagent_checks_tokenizes_shell_program_arguments(tmp_path):
    home = tmp_path / "home"
    launch_dir = home / "Library" / "LaunchAgents"
    launch_dir.mkdir(parents=True)
    runtime = tmp_path / "runtime" / "MAGI_v2"
    script = runtime / "scripts" / "ops" / "run_with_env.py"
    switcher = runtime / "config" / "bin" / "omlx_switch_model.sh"
    script.parent.mkdir(parents=True)
    switcher.parent.mkdir(parents=True)
    script.write_text("print('ok')\n", encoding="utf-8")
    switcher.write_text("#!/bin/sh\n", encoding="utf-8")
    plist_payload = {
        "Label": "com.magi.omlx-restore",
        "ProgramArguments": [
            "/bin/zsh",
            "-lc",
            f'sleep 90 && exec "{script}" -- /bin/bash "{switcher}" auto',
        ],
    }
    (launch_dir / "com.magi.omlx-restore.plist").write_bytes(plistlib.dumps(plist_payload))

    checks = magi_doctor._launchagent_checks(home=home, repo_root=tmp_path / "repo", live_root=runtime)

    assert checks[0].status == "pass"


def test_launchagent_checks_preserves_direct_paths_with_spaces(tmp_path):
    home = tmp_path / "home"
    launch_dir = home / "Library" / "LaunchAgents"
    launch_dir.mkdir(parents=True)
    runtime = tmp_path / "Application Support" / "MAGI" / "runtime" / "MAGI_v2"
    script = runtime / "daemon.py"
    script.parent.mkdir(parents=True)
    script.write_text("print('ok')\n", encoding="utf-8")
    plist_payload = {
        "Label": "com.magi.daemon",
        "ProgramArguments": [str(script)],
    }
    (launch_dir / "com.magi.daemon.plist").write_bytes(plistlib.dumps(plist_payload))

    checks = magi_doctor._launchagent_checks(home=home, repo_root=tmp_path / "repo", live_root=runtime)

    assert checks[0].status == "pass"


def test_launchagent_checks_allows_runtime_venv_python_symlink(tmp_path):
    home = tmp_path / "home"
    launch_dir = home / "Library" / "LaunchAgents"
    launch_dir.mkdir(parents=True)
    runtime = tmp_path / "Application Support" / "MAGI" / "runtime" / "MAGI_v2"
    py = runtime / "venv" / "bin" / "python3"
    daemon = runtime / "daemon.py"
    target = tmp_path / "homebrew" / "bin" / "python3.14"
    py.parent.mkdir(parents=True)
    daemon.parent.mkdir(parents=True, exist_ok=True)
    target.parent.mkdir(parents=True)
    target.write_text("#!/bin/sh\n", encoding="utf-8")
    py.symlink_to(target)
    daemon.write_text("print('ok')\n", encoding="utf-8")
    plist_payload = {
        "Label": "com.magi.casper",
        "ProgramArguments": [str(py), str(daemon)],
    }
    (launch_dir / "com.magi.casper.plist").write_bytes(plistlib.dumps(plist_payload))

    checks = magi_doctor._launchagent_checks(home=home, repo_root=tmp_path / "repo", live_root=runtime)

    assert checks[0].status == "pass"


def test_launchagent_checks_allows_install_support_scripts_and_home_workdir(tmp_path):
    home = tmp_path / "home"
    launch_dir = home / "Library" / "LaunchAgents"
    launch_dir.mkdir(parents=True)
    runtime = tmp_path / "Application Support" / "MAGI" / "runtime" / "MAGI_v2"
    install_root = runtime.parent.parent
    script = install_root / "bin" / "smb_reconnect.sh"
    rpc = install_root / "rpc-bin" / "rpc-server"
    script.parent.mkdir(parents=True)
    rpc.parent.mkdir(parents=True)
    runtime.mkdir(parents=True)
    script.write_text("#!/bin/sh\n", encoding="utf-8")
    rpc.write_text("#!/bin/sh\n", encoding="utf-8")
    for name, payload in {
        "com.magi.smb-reconnect.plist": {
            "Label": "com.magi.smb-reconnect",
            "ProgramArguments": ["/bin/bash", str(script)],
        },
        "com.magi.rpc.plist": {
            "Label": "com.magi.rpc",
            "WorkingDirectory": str(rpc.parent),
            "ProgramArguments": [str(rpc), "-H", "127.0.0.1"],
        },
        "com.magi.omlx.plist": {
            "Label": "com.magi.omlx",
            "WorkingDirectory": str(home),
            "ProgramArguments": ["/opt/homebrew/bin/omlx-magi-start-text"],
        },
    }.items():
        (launch_dir / name).write_bytes(plistlib.dumps(payload))

    checks = magi_doctor._launchagent_checks(home=home, repo_root=tmp_path / "repo", live_root=runtime)

    assert checks
    assert {check.status for check in checks} == {"pass"}


def test_launchagent_checks_warns_when_env_points_to_source_root(tmp_path):
    home = tmp_path / "home"
    launch_dir = home / "Library" / "LaunchAgents"
    launch_dir.mkdir(parents=True)
    source = tmp_path / "source" / "MAGI_v2"
    runtime = tmp_path / "Application Support" / "MAGI" / "runtime" / "MAGI_v2"
    script = runtime / "scripts" / "ops" / "run_daemon_no_site.py"
    source.mkdir(parents=True)
    script.parent.mkdir(parents=True)
    script.write_text("print('ok')\n", encoding="utf-8")
    plist_payload = {
        "Label": "com.magi.daemon",
        "WorkingDirectory": str(runtime),
        "ProgramArguments": [str(script)],
        "EnvironmentVariables": {
            "MAGI_ROOT": str(source),
            "MAGI_ROOT_DIR": str(source),
            "PYTHONPATH": str(source),
        },
    }
    (launch_dir / "com.magi.daemon.plist").write_bytes(plistlib.dumps(plist_payload))

    checks = magi_doctor._launchagent_checks(home=home, repo_root=source, live_root=runtime)
    by_name = {check.name: check for check in checks}

    assert by_name["launchagent:com.magi.daemon"].status == "pass"
    assert by_name["launchagent:com.magi.daemon:env"].status == "warn"
    assert "MAGI_ROOT points to source root" in by_name["launchagent:com.magi.daemon:env"].detail


def test_launchagent_checks_warns_when_keepalive_service_is_not_loaded(tmp_path, monkeypatch):
    home = tmp_path / "home"
    launch_dir = home / "Library" / "LaunchAgents"
    launch_dir.mkdir(parents=True)
    runtime = tmp_path / "Application Support" / "MAGI" / "runtime" / "MAGI_v2"
    script = runtime / "daemon.py"
    script.parent.mkdir(parents=True)
    script.write_text("print('ok')\n", encoding="utf-8")
    plist_payload = {
        "Label": "com.magi.daemon",
        "ProgramArguments": [str(script)],
        "KeepAlive": True,
        "RunAtLoad": True,
    }
    (launch_dir / "com.magi.daemon.plist").write_bytes(plistlib.dumps(plist_payload))

    class Result:
        returncode = 113
        stdout = ""
        stderr = 'Could not find service "com.magi.daemon"'

    monkeypatch.setattr(magi_doctor.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(magi_doctor.shutil, "which", lambda name: "/bin/launchctl" if name == "launchctl" else None)
    monkeypatch.setattr(magi_doctor.subprocess, "run", lambda *_args, **_kwargs: Result())

    checks = magi_doctor._launchagent_checks(
        home=home,
        repo_root=tmp_path / "repo",
        live_root=runtime,
        check_launchctl=True,
    )
    by_name = {check.name: check for check in checks}

    assert by_name["launchctl:com.magi.daemon"].status == "warn"
    assert "not loaded" in by_name["launchctl:com.magi.daemon"].detail


def test_runtime_root_drift_warns_when_live_copy_differs(tmp_path):
    source = tmp_path / "source"
    live = tmp_path / "runtime" / "MAGI_v2"
    source.mkdir()
    live.mkdir(parents=True)

    checks = magi_doctor._runtime_root_checks(repo_root=source, live_root=live)

    assert checks[0].name == "runtime_root_drift"
    assert checks[0].status == "warn"


def test_runtime_root_fingerprint_covers_acceptance_gate():
    assert "scripts/ops/magi_acceptance_gate.py" in magi_doctor._RUNTIME_ROOT_FINGERPRINT_FILES


def test_runtime_root_drift_passes_when_live_copy_matches_fingerprint(tmp_path, monkeypatch):
    source = tmp_path / "source"
    live = tmp_path / "runtime" / "MAGI_v2"
    source.mkdir()
    live.mkdir(parents=True)
    monkeypatch.setattr(magi_doctor, "_RUNTIME_ROOT_FINGERPRINT_FILES", ("api/server.py", "gui/magi_menubar.py"))
    monkeypatch.setattr(magi_doctor, "_RUNTIME_ROOT_GOOGLE_CRON_JOBS", set())

    for root in (source, live):
        for rel in ("api/server.py", "gui/magi_menubar.py"):
            path = root / rel
            path.parent.mkdir(parents=True)
            path.write_text("print('same runtime payload')\n", encoding="utf-8")

    checks = magi_doctor._runtime_root_checks(repo_root=source, live_root=live)

    assert checks[0].name == "runtime_root_drift"
    assert checks[0].status == "pass"
    assert "fingerprint=matched" in checks[0].detail
