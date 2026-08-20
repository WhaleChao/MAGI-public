from __future__ import annotations

import json
import plistlib
import platform
import zipfile
from pathlib import Path

import pytest

from magi_v3.selfhost import (
    SCHEMA,
    SelfHostError,
    activate_release,
    active_release,
    build_service_plan,
    bootstrap_mysql_databases,
    build_distribution_archive,
    certify_active_release,
    default_config,
    default_layout,
    doctor,
    initialise_instance,
    install_commands,
    layout_from_config,
    render_environment,
    rollback_release,
    stage_release,
    validate_config,
    venv_python,
    write_config,
    write_launcher,
)


def test_windows_required_runtime_modules_have_portable_imports() -> None:
    from magi_v3 import control, fcntl_compat, process_compat, service_runtime, supervisor_service

    assert control is not None
    assert service_runtime is not None
    assert supervisor_service is not None
    assert callable(fcntl_compat.flock)
    assert callable(process_compat.process_group)


def test_portable_file_lock_is_exclusive_and_releasable(tmp_path: Path) -> None:
    from magi_v3 import fcntl_compat as fcntl

    path = tmp_path / "portable.lock"
    with path.open("a+b") as first, path.open("a+b") as second:
        fcntl.flock(first.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(BlockingIOError):
            fcntl.flock(second.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(first.fileno(), fcntl.LOCK_UN)
        fcntl.flock(second.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(second.fileno(), fcntl.LOCK_UN)


def _config(tmp_path: Path, system: str):
    env = {"LOCALAPPDATA": str(tmp_path / "LocalAppData"), "USERPROFILE": str(tmp_path / "User")}
    layout = default_layout(system=system, home=tmp_path / "User", environ=env)
    return layout, default_config(layout=layout, source_root=tmp_path / "source")


def _source(tmp_path: Path, label: str) -> Path:
    source = tmp_path / label
    (source / "magi_v3").mkdir(parents=True)
    (source / "magi_v3" / "__init__.py").write_text("", encoding="utf-8")
    (source / "magi_v3" / "selfhost.py").write_text("# fixture\n", encoding="utf-8")
    (source / "daemon.py").write_text(f"print({label!r})\n", encoding="utf-8")
    (source / "requirements.txt").write_text("", encoding="utf-8")
    (source / "requirements-selfhost.txt").write_text("", encoding="utf-8")
    return source


def test_windows_layout_is_native_and_machine_independent(tmp_path: Path) -> None:
    layout, config = _config(tmp_path, "Windows")
    assert layout.instance_root == tmp_path / "LocalAppData" / "MAGI" / "selfhost"
    assert venv_python(layout).name == "python.exe"
    assert config["models"]["local_backend"] == "disabled"
    assert config["models"]["translation_backend"] == "api"
    assert config["features"]["desktop_status"] == "web"
    assert validate_config(config) == []
    workstation_home = "/" + "Users" + "/" + "ai"
    assert workstation_home not in json.dumps(config)


def test_cross_platform_windows_plan_uses_windows_python_launcher(tmp_path: Path) -> None:
    layout, _config_value = _config(tmp_path, "Windows")
    commands = install_commands(tmp_path / "source", layout, include_optional=False)
    assert commands[0][:3] == ["py", "-3.12", "-m"]
    assert commands[2][-1].endswith("requirements-selfhost.txt")


def test_macos_layout_selects_apple_local_backends(tmp_path: Path) -> None:
    layout = default_layout(system="Darwin", home=tmp_path / "person", environ={})
    config = default_config(layout=layout)
    assert layout.instance_root == tmp_path / "person" / "Library" / "Application Support" / "MAGI" / "selfhost"
    assert config["models"]["local_backend"] == "mlx"
    assert config["models"]["transcription_backend"] == "mlx_whisper"
    assert config["models"]["translation_backend"] == "apple"
    assert config["features"]["desktop_status"] == "menubar"
    assert config["features"]["local_models"] is False


def test_render_environment_disables_apple_only_paths_on_windows(tmp_path: Path) -> None:
    _layout, config = _config(tmp_path, "Windows")
    env = render_environment(config, secret_values={"MAGI_API_KEY": "test-only"})
    assert env["MAGI_TRANSLATE_LOCAL_FIRST"] == "0"
    assert env["MAGI_TRANSLATOR_APE"] == "0"
    assert env["MAGI_SKIP_IMPORT_PROBES"] == "1"
    assert env["MAGI_TOOLS_HEALTH_PROBE_MODEL"] == "0"
    assert env["MAGI_MENUBAR_ENABLED"] == "0"
    assert env["MAGI_INTERNAL_CRON_ENABLED"] == "1"
    assert env["MAGI_AGENT_DIR"].endswith("data/agent")
    assert env["MAGI_MUTABLE_STATIC_DIR"].endswith("data/static")
    assert env["MAGI_BACKGROUND_LOCK_DIR"].endswith("runtime/locks")
    assert env["MAGI_CLOUDFLARED_LOG_PATH"].endswith("logs/cloudflared.log")
    assert env["MAGI_JUDGMENTS_JSON_PATH"].endswith("data/knowledge/judgments.json")
    assert env["MAGI_PDF_NAMER_CASE_INDEX"].endswith("data/knowledge/pdf_namer_case_index.json")
    assert env["MAGI_PORT"] == "5002"
    assert env["MAGI_TOOLS_PORT"] == "5003"
    assert env["MAGI_API_KEY"] == "test-only"


def test_windows_service_plan_uses_current_user_task_without_admin(tmp_path: Path) -> None:
    layout, config = _config(tmp_path, "Windows")
    plan = build_service_plan(config, python_executable=venv_python(layout), launcher_path=layout.launcher_path)
    command = list(plan.install[0])
    assert command[:4] == ["schtasks", "/create", "/tn", "MAGI-V3-SelfHost"]
    assert "/rl" in command and command[command.index("/rl") + 1] == "limited"
    assert "onlogon" in command
    assert str(layout.launcher_path) in command[command.index("/tr") + 1]
    assert len(plan.install) == 2
    assert plan.install[1][:4] == ("powershell.exe", "-NoProfile", "-NonInteractive", "-Command")
    assert "RestartCount 3" in plan.install[1][-1]
    assert "StartWhenAvailable" in plan.install[1][-1]
    assert "Get-ScheduledTask" in plan.stop[0][-1]
    assert "Unregister-ScheduledTask" in plan.uninstall[0][-1]
    assert plan.artifact_path is None


def test_macos_service_plan_uses_argument_array_and_private_logs(tmp_path: Path) -> None:
    layout, config = _config(tmp_path, "Darwin")
    plan = build_service_plan(config, python_executable=venv_python(layout), launcher_path=layout.launcher_path, uid=123)
    assert plan.artifact_bytes is not None
    payload = plistlib.loads(plan.artifact_bytes)
    assert payload["ProgramArguments"] == [
        str(venv_python(layout)),
        str(layout.launcher_path),
        "--config",
        str(layout.config_path),
    ]
    assert payload["StandardOutPath"].startswith(str(layout.logs_dir))
    assert plan.install[0][:3] == ("launchctl", "bootstrap", "gui/123")


def test_config_write_is_fail_closed_without_overwrite(tmp_path: Path) -> None:
    layout, config = _config(tmp_path, "Windows")
    write_config(config, layout.config_path)
    assert json.loads(layout.config_path.read_text(encoding="utf-8"))["schema"] == SCHEMA
    with pytest.raises(SelfHostError, match="already exists"):
        write_config(config, layout.config_path)


def test_configured_layout_remains_authoritative_across_shell_environments(tmp_path: Path) -> None:
    original, config = _config(tmp_path / "original", "Windows")
    restored = layout_from_config(config)
    assert restored == original
    assert venv_python(restored) == original.venv_dir / "Scripts" / "python.exe"


def test_stage_activate_upgrade_and_rollback_are_atomic(tmp_path: Path) -> None:
    layout, config = _config(tmp_path, "Windows")
    initialise_instance(config)
    first = stage_release(_source(tmp_path, "source-one"), config, release_id="r1")
    assert first["file_count"] >= 3
    one = activate_release(config, "r1")
    assert one["release_id"] == "r1"
    second = stage_release(_source(tmp_path, "source-two"), config, release_id="r2")
    assert second["tree_sha256"] != first["tree_sha256"]
    two = activate_release(config, "r2")
    assert two["previous_release_id"] == "r1"
    back = rollback_release(config)
    assert back["release_id"] == "r1"
    assert active_release(config)["release_id"] == "r1"


def test_active_release_certification_binds_marker_manifest_and_payload(tmp_path: Path) -> None:
    _layout, config = _config(tmp_path, "Windows")
    initialise_instance(config)
    staged = stage_release(_source(tmp_path, "source"), config, release_id="r1")
    activate_release(config, "r1")

    assert certify_active_release(config)["ok"] is True

    payload_file = Path(staged["root"]) / "daemon.py"
    payload_file.write_text("tampered\n", encoding="utf-8")
    certified = certify_active_release(config)
    assert certified["ok"] is False
    assert certified["detail"] == "release payload digest mismatch"


def test_active_release_certification_rejects_marker_path_replacement(tmp_path: Path) -> None:
    layout, config = _config(tmp_path, "Windows")
    initialise_instance(config)
    stage_release(_source(tmp_path, "source"), config, release_id="r1")
    activate_release(config, "r1")
    marker_path = layout.active_marker
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    marker["release_root"] = str(tmp_path / "unbound")
    marker_path.write_text(json.dumps(marker), encoding="utf-8")

    certified = certify_active_release(config)
    assert certified["ok"] is False
    assert certified["detail"] == "active release root is unavailable"


def test_doctor_reports_actionable_missing_state_then_passes_core(tmp_path: Path) -> None:
    layout, config = _config(tmp_path, "Windows")
    before = doctor(config)
    assert before["ok"] is False
    assert any(item["key"] == "active_release" and item["action"] for item in before["checks"])
    initialise_instance(config)
    stage_release(_source(tmp_path, "source"), config, release_id="r1")
    activate_release(config, "r1")
    write_launcher(config)
    after = doctor(config)
    assert after["ok"] is True
    assert after["ready"] is False  # generated local secrets are still required


def test_doctor_requires_complete_feature_credentials_and_real_google_file(tmp_path: Path) -> None:
    layout, config = _config(tmp_path, "Windows")
    config["features"].update({
        "legal_aid": True,
        "court_portal": True,
        "google_calendar": True,
        "google_drive": True,
        "messaging": True,
    })
    initialise_instance(config)
    stage_release(_source(tmp_path, "source"), config, release_id="r1")
    activate_release(config, "r1")
    write_launcher(config)
    secret_path = Path(config["secrets"]["env_file"])
    secret_path.write_text(
        "FLASK_SECRET_KEY=" + "a" * 64 + "\n"
        "MAGI_API_KEY=" + "b" * 64 + "\n"
        "DB_HOST=127.0.0.1\nDB_PORT=3306\nDB_USER=magi\nDB_NAME=magi\n"
        "MAGI_LAF_USERNAME=user\n"
        "MAGI_JUDICIAL_EEFILE_USERNAME=user\nMAGI_JUDICIAL_EEFILE_PASSWORD=pass\n"
        "GOOGLE_APPLICATION_CREDENTIALS=/missing/google.json\n"
        "DISCORD_BOT_TOKEN=token\nNVIDIA_API_KEY=key\n",
        encoding="utf-8",
    )
    report = doctor(config)
    by_key = {item["key"]: item for item in report["checks"]}
    assert by_key["feature:legal_aid"]["status"] == "warn"
    assert "MAGI_LAF_PASSWORD" in by_key["feature:legal_aid"]["detail"]
    assert by_key["feature:court_portal"]["status"] == "pass"
    assert by_key["feature:google_calendar"]["status"] == "warn"
    assert by_key["feature:google_drive"]["status"] == "warn"
    assert by_key["feature:messaging"]["status"] == "pass"
    assert by_key["model:nvidia_api"]["status"] == "pass"
    assert report["ok"] is True
    assert report["ready"] is False


def test_doctor_strict_returns_nonzero_for_commissioning_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts import magi_selfhost as cli

    layout, config = _config(tmp_path, "Windows")
    write_config(config, layout.config_path)
    monkeypatch.setattr(
        cli,
        "_doctor_with_instance_python",
        lambda *_args, **_kwargs: {"ok": True, "ready": False, "summary": {"pass": 1, "warn": 1, "fail": 0}, "checks": []},
    )
    args = cli.parser().parse_args(["--config", str(layout.config_path), "doctor", "--strict"])
    assert cli.command_doctor(args) == 1


def test_release_id_and_duplicate_release_fail_closed(tmp_path: Path) -> None:
    _layout, config = _config(tmp_path, "Windows")
    initialise_instance(config)
    source = _source(tmp_path, "source")
    with pytest.raises(SelfHostError, match="unsupported characters"):
        stage_release(source, config, release_id="../escape")
    stage_release(source, config, release_id="safe")
    with pytest.raises(SelfHostError, match="already exists"):
        stage_release(source, config, release_id="safe")


def test_distribution_archive_is_portable_and_secret_free(tmp_path: Path) -> None:
    source = _source(tmp_path, "source")
    (source / "install-magi.ps1").write_text("# fixture\n", encoding="utf-8")
    (source / ".env").write_text("SECRET=must-not-ship\n", encoding="utf-8")
    output = tmp_path / "MAGI-selfhost.zip"
    result = build_distribution_archive(source, output)
    assert result["contains_secrets"] is False
    assert len(result["archive_sha256"]) == 64
    with zipfile.ZipFile(output) as archive:
        names = archive.namelist()
        assert "MAGI-DISTRIBUTION.json" in names
        assert "requirements-selfhost.txt" in names
        assert ".env" not in names
        assert not any(name.startswith("tests/") for name in names)
        manifest = json.loads(archive.read("MAGI-DISTRIBUTION.json"))
    assert manifest["platforms"] == ["macOS", "Windows"]
    assert "/Users/" not in json.dumps(manifest)


def test_distribution_cron_placeholders_rebase_after_extracting_to_new_host(tmp_path: Path) -> None:
    source = _source(tmp_path, "builder-checkout")
    private_root = "/" + "Users" + "/builder/Desktop/MAGI"
    (source / "cron_jobs.json").write_text(
        json.dumps([
            {
                "id": "portable-core",
                "enabled": True,
                "command": (
                    f"'{private_root}/venv/bin/python3' "
                    f"'{private_root}/scripts/core.py' "
                    f"--state '{private_root}/.runtime/core.json'"
                ),
            },
        ]),
        encoding="utf-8",
    )
    archive_path = tmp_path / "MAGI-selfhost.zip"
    build_distribution_archive(source, archive_path)
    extracted = tmp_path / "friend-machine" / "downloaded-MAGI"
    with zipfile.ZipFile(archive_path) as archive:
        archive.extractall(extracted)

    layout, config = _config(tmp_path / "installed", "Windows")
    initialise_instance(config)
    manifest = stage_release(extracted, config, release_id="portable-from-zip")
    staged_cron = json.loads(
        (Path(manifest["root"]) / "cron_jobs.json").read_text(encoding="utf-8")
    )
    rendered = json.dumps(staged_cron, ensure_ascii=False)
    assert "__MAGI_" not in rendered
    assert private_root not in rendered
    assert str(extracted) not in rendered
    assert str(venv_python(layout)) in rendered
    assert str(Path(manifest["root"]) / "scripts" / "core.py") in rendered
    assert str(Path(config["paths"]["runtime_dir"]) / "core.json") in rendered


def test_distribution_rebases_canonical_v3_shared_runtime_path(tmp_path: Path) -> None:
    source = _source(tmp_path, "builder-checkout")
    private_home = "/" + "Users" + "/example"
    live_shared_runtime = (
        private_home + "/Library/Application Support/MAGI/runtime/"
        "MAGI_v3/shared/runtime/reprocess_insights_latest.json"
    )
    (source / "cron_jobs.json").write_text(
        json.dumps(
            [
                {
                    "id": "portable-v3-runtime-state",
                    "enabled": True,
                    "command": (
                        "'__MAGI_PYTHON__' '__MAGI_ROOT__/scripts/core.py' "
                        f"--state '{live_shared_runtime}'"
                    ),
                }
            ]
        ),
        encoding="utf-8",
    )
    archive_path = tmp_path / "MAGI-selfhost.zip"
    build_distribution_archive(source, archive_path)
    extracted = tmp_path / "friend-machine" / "downloaded-MAGI"
    with zipfile.ZipFile(archive_path) as archive:
        archive.extractall(extracted)

    layout, config = _config(tmp_path / "installed", "Windows")
    initialise_instance(config)
    manifest = stage_release(extracted, config, release_id="portable-v3-runtime")
    jobs = json.loads(
        (Path(manifest["root"]) / "cron_jobs.json").read_text(encoding="utf-8")
    )
    rendered = jobs[0]["command"]
    assert live_shared_runtime not in rendered
    assert private_home not in rendered
    assert str(
        Path(config["paths"]["runtime_dir"]) / "reprocess_insights_latest.json"
    ) in rendered


def test_portable_deployment_layer_contains_no_private_machine_path() -> None:
    root = Path(__file__).resolve().parents[1]
    paths = [root / "magi_v3" / "selfhost.py", root / "scripts" / "magi_selfhost.py"]
    text = "\n".join(path.read_text(encoding="utf-8") for path in paths)
    private_markers = (
        "/" + "Users" + "/ai",
        "/" + "Volumes" + "/homes",
        "/" + "Volumes" + "/lumi",
        "lumi" + "63181107",
    )
    for marker in private_markers:
        assert marker not in text


def test_windows_rejects_apple_only_backends(tmp_path: Path) -> None:
    _layout, config = _config(tmp_path, "Windows")
    config["models"]["translation_backend"] = "apple"
    config["models"]["transcription_backend"] = "mlx_whisper"
    config["features"]["desktop_status"] = "menubar"
    errors = validate_config(config)
    assert any("translation_backend=apple" in error for error in errors)
    assert any("transcription_backend=mlx_whisper" in error for error in errors)
    assert any("desktop_status=menubar" in error for error in errors)


def test_config_rejects_missing_or_non_boolean_feature_flags(tmp_path: Path) -> None:
    _layout, config = _config(tmp_path, "Windows")
    del config["features"]["court_portal"]
    config["features"]["messaging"] = "false"
    errors = validate_config(config)
    assert "features.court_portal is required" in errors
    assert "features.messaging must be a boolean" in errors


def test_launcher_exports_the_complete_portable_environment(tmp_path: Path) -> None:
    layout, config = _config(tmp_path, "Windows")
    initialise_instance(config)
    stage_release(_source(tmp_path, "source"), config, release_id="r1")
    activate_release(config, "r1")
    launcher = write_launcher(config)
    text = launcher.read_text(encoding="utf-8")
    assert "render_environment" in text
    assert "MAGI_ROOT_DIR" in text
    assert "PYTHONPATH" in text
    assert "PYTHONDONTWRITEBYTECODE" in text


def test_release_rebuilds_cron_without_private_checkout_paths(tmp_path: Path) -> None:
    layout, config = _config(tmp_path, "Windows")
    source = _source(tmp_path, "source")
    private_root = "/" + "Users" + "/example/Desktop/MAGI"
    (source / "cron_jobs.json").write_text(
        json.dumps([
            {
                "id": "core",
                "enabled": True,
                "command": f"'{private_root}/venv/bin/python3' '{private_root}/scripts/core.py'",
            },
            {
                "id": "job_laf_probe",
                "enabled": True,
                "command": f"'{source}/venv/bin/python3' '{source}/scripts/laf_probe.py'",
            },
        ]),
        encoding="utf-8",
    )
    initialise_instance(config)
    manifest = stage_release(source, config, release_id="portable")
    jobs = json.loads((Path(manifest["root"]) / "cron_jobs.json").read_text(encoding="utf-8"))
    rendered = json.dumps(jobs, ensure_ascii=False)
    assert str(source) not in rendered
    assert str(venv_python(layout)) in rendered
    assert jobs[1]["enabled"] is False
    assert jobs[1]["disabled_reason"].endswith("legal_aid")


def test_retired_machine_specific_cron_is_omitted_without_blocking_release(tmp_path: Path) -> None:
    _layout, config = _config(tmp_path, "Windows")
    source = _source(tmp_path, "source")
    (source / "cron_jobs.json").write_text(
        json.dumps([{
            "id": "retired_manual_archive",
            "enabled": False,
            "manual_enable": True,
            "command": "/usr/bin/open /" + "Volumes" + "/old-private-archive",
        }]),
        encoding="utf-8",
    )
    initialise_instance(config)
    manifest = stage_release(source, config, release_id="portable")
    jobs = json.loads((Path(manifest["root"]) / "cron_jobs.json").read_text(encoding="utf-8"))
    assert jobs[0]["enabled"] is False
    assert jobs[0]["command"] == ""
    assert "machine-specific" in jobs[0]["disabled_reason"]


def test_non_mysql_database_is_rejected_until_a_complete_adapter_exists(tmp_path: Path) -> None:
    _layout, config = _config(tmp_path, "Windows")
    config["database"]["engine"] = "sqlite"
    assert any("database.engine=mysql" in item for item in validate_config(config))


def test_cli_refuses_cross_target_mutation_before_writing(tmp_path: Path, monkeypatch) -> None:
    from scripts import magi_selfhost as cli

    other = "Windows" if platform.system() != "Windows" else "Darwin"
    target_arg = "windows" if other == "Windows" else "macos"
    config_path = tmp_path / "must-not-exist.json"
    args = cli.parser().parse_args([
        "--target",
        target_arg,
        "--config",
        str(config_path),
        "--source",
        str(tmp_path),
        "init",
        "--apply",
    ])
    with pytest.raises(SelfHostError, match="refusing to mutate"):
        args.handler(args)
    assert not config_path.exists()


def test_secrets_command_is_dry_run_by_default(tmp_path: Path, capsys) -> None:
    from scripts import magi_selfhost as cli

    system = platform.system()
    layout = default_layout(
        system=system,
        home=tmp_path / "User",
        environ={"MAGI_SELFHOST_HOME": str(tmp_path / "instance")},
    )
    config = default_config(layout=layout, source_root=tmp_path / "source")
    write_config(config, layout.config_path)
    args = cli.parser().parse_args(["--config", str(layout.config_path), "secrets", "--generate"])
    assert args.handler(args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["dry_run"] is True
    assert not Path(config["secrets"]["env_file"]).exists()


def test_first_install_is_successfully_prepared_when_database_is_not_configured(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    from scripts import magi_selfhost as cli

    layout = default_layout(
        system=platform.system(),
        home=tmp_path / "User",
        environ={"MAGI_SELFHOST_HOME": str(tmp_path / "instance")},
    )
    source = _source(tmp_path, "source")
    config = default_config(layout=layout, source_root=source)
    write_config(config, layout.config_path)
    initialise_instance(config)
    instance_python = venv_python(layout)
    instance_python.parent.mkdir(parents=True, exist_ok=True)
    instance_python.write_text("", encoding="utf-8")
    monkeypatch.setattr(
        cli,
        "_doctor_with_instance_python",
        lambda *_args, **_kwargs: {"ok": True, "ready": False, "checks": []},
    )
    args = cli.parser().parse_args([
        "--config", str(layout.config_path),
        "--source", str(source),
        "install", "--apply", "--skip-dependencies", "--release-id", "prepared",
    ])
    assert args.handler(args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["service_deferred"] is True
    assert payload["service"][0]["status"] == "deferred"
    assert "configure --interactive --apply" in payload["next_action"]


def test_live_wait_retries_until_both_required_services_pass(monkeypatch) -> None:
    from scripts import magi_selfhost as cli

    reports = iter([
        {
            "checks": [
                {"key": "live:main", "status": "pass"},
                {"key": "live:tools", "status": "fail"},
            ]
        },
        {
            "checks": [
                {"key": "live:main", "status": "pass"},
                {"key": "live:tools", "status": "pass"},
            ]
        },
    ])
    monkeypatch.setattr(cli, "_doctor_with_instance_python", lambda *_args, **_kwargs: next(reports))
    monkeypatch.setattr(cli.time, "sleep", lambda _seconds: None)
    result = cli._wait_for_live({}, Path("unused.json"), timeout=1)
    assert result["ok"] is True
    assert result["attempts"] == 2


def test_interactive_database_configuration_redacts_password(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    from scripts import magi_selfhost as cli

    layout = default_layout(
        system=platform.system(),
        home=tmp_path / "User",
        environ={"MAGI_SELFHOST_HOME": str(tmp_path / "instance")},
    )
    config = default_config(layout=layout, source_root=tmp_path / "source")
    write_config(config, layout.config_path)
    initialise_instance(config)
    answers = iter(["127.0.0.1", "3306", "magi_user", "magi", "magi_brain"])
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    monkeypatch.setattr(cli.getpass, "getpass", lambda _prompt: "top-secret-test-password")
    monkeypatch.setattr(
        cli,
        "_doctor_with_instance_python",
        lambda *_args, **_kwargs: {"ok": True, "ready": True, "checks": []},
    )
    args = cli.parser().parse_args([
        "--config", str(layout.config_path),
        "configure", "--interactive", "--apply",
    ])
    assert args.handler(args) == 0
    output = capsys.readouterr().out
    assert "top-secret-test-password" not in output
    payload = json.loads(output)
    assert payload["secret_values_redacted"] is True
    assert "DB_PASSWORD" in payload["updated_keys"]
    assert "top-secret-test-password" in Path(config["secrets"]["env_file"]).read_text(encoding="utf-8")


def test_configuration_cli_has_no_password_argument() -> None:
    from scripts import magi_selfhost as cli

    with pytest.raises(SystemExit):
        cli.parser().parse_args(["configure", "--db-password", "must-not-enter-history"])


def test_upgrade_no_restart_stages_without_changing_active_release(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    from scripts import magi_selfhost as cli

    system = platform.system()
    layout = default_layout(
        system=system,
        home=tmp_path / "User",
        environ={"MAGI_SELFHOST_HOME": str(tmp_path / "instance")},
    )
    source_one = _source(tmp_path, "source-one")
    source_two = _source(tmp_path, "source-two")
    config = default_config(layout=layout, source_root=source_one)
    write_config(config, layout.config_path)
    initialise_instance(config)
    stage_release(source_one, config, release_id="r1")
    activate_release(config, "r1")
    args = cli.parser().parse_args([
        "--config", str(layout.config_path),
        "--source", str(source_two),
        "upgrade", "--apply", "--release-id", "r2", "--no-restart",
    ])
    monkeypatch.setattr(cli, "_ensure_native", lambda _config: None)
    assert args.handler(args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["staged_only"] is True
    assert payload["active"]["release_id"] == "r1"
    assert active_release(config)["release_id"] == "r1"


def test_failed_upgrade_restores_and_live_verifies_previous_release(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    from scripts import magi_selfhost as cli

    system = platform.system()
    layout = default_layout(
        system=system,
        home=tmp_path / "User",
        environ={"MAGI_SELFHOST_HOME": str(tmp_path / "instance")},
    )
    source_one = _source(tmp_path, "source-one")
    source_two = _source(tmp_path, "source-two")
    config = default_config(layout=layout, source_root=source_one)
    write_config(config, layout.config_path)
    initialise_instance(config)
    stage_release(source_one, config, release_id="r1")
    activate_release(config, "r1")
    args = cli.parser().parse_args([
        "--config", str(layout.config_path),
        "--source", str(source_two),
        "upgrade", "--apply", "--release-id", "r2",
    ])
    monkeypatch.setattr(cli, "_ensure_native", lambda _config: None)
    monkeypatch.setattr(
        cli,
        "execute_service_plan",
        lambda _plan, *, action: [{"ok": True, "action": action}],
    )
    live_results = iter([{"ok": False}, {"ok": True}])
    monkeypatch.setattr(cli, "_wait_for_live", lambda *_args, **_kwargs: next(live_results))
    assert args.handler(args) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["automatic_recovery"]["ok"] is True
    assert active_release(config)["release_id"] == "r1"


def test_database_bootstrap_rejects_unsafe_schema_names_before_connecting() -> None:
    with pytest.raises(SelfHostError, match="DB_NAME"):
        bootstrap_mysql_databases({
            "DB_HOST": "127.0.0.1",
            "DB_PORT": "3306",
            "DB_USER": "magi",
            "DB_PASSWORD": "not-a-real-secret",
            "DB_NAME": "magi; DROP DATABASE magi",
        })
