#!/usr/bin/env python3
"""Single entry point for a portable MAGI self-host installation."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import platform
import secrets
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from magi_v3.selfhost import (  # noqa: E402
    SelfHostError,
    activate_release,
    active_release,
    bootstrap_mysql_databases,
    build_distribution_archive,
    build_service_plan,
    default_config,
    default_layout,
    doctor,
    execute_service_plan,
    initialise_instance,
    install_commands,
    layout_from_config,
    load_config,
    parse_env_file,
    rollback_release,
    safe_remove_program,
    stage_release,
    validate_config,
    venv_python,
    write_config,
    write_launcher,
    write_secret_template,
)


def _target_system(value: str) -> str:
    lowered = value.strip().lower()
    mapping = {"": "", "auto": "", "mac": "Darwin", "macos": "Darwin", "darwin": "Darwin", "windows": "Windows", "win": "Windows", "linux": "Linux"}
    if lowered not in mapping:
        raise argparse.ArgumentTypeError("target must be auto, macos, windows, or linux")
    return mapping[lowered]


def _json(payload: Mapping[str, Any]) -> None:
    print(json.dumps(dict(payload), ensure_ascii=False, indent=2))


def _command_display(command: list[str] | tuple[str, ...]) -> str:
    return subprocess.list2cmdline([str(part) for part in command])


def _write_env(path: Path, values: Mapping[str, str]) -> None:
    existing = parse_env_file(path)
    existing.update({str(key): str(value) for key, value in values.items()})
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        "# MAGI self-host secrets. Never commit or share this file.\n"
        + "\n".join(f"{key}={value}" for key, value in sorted(existing.items()))
        + "\n",
        encoding="utf-8",
    )
    try:
        temporary.chmod(0o600)
    except OSError:
        pass
    os.replace(temporary, path)


def _generate_local_secrets(config: Mapping[str, Any]) -> dict[str, str]:
    path = Path(str(dict(config["secrets"])["env_file"])).expanduser()
    values = parse_env_file(path)
    generated: dict[str, str] = {}
    for key in ("FLASK_SECRET_KEY", "MAGI_API_KEY"):
        if not str(values.get(key) or "").strip():
            generated[key] = secrets.token_hex(32)
    if generated:
        _write_env(path, generated)
    return {"path": str(path), "generated": sorted(generated), "ok": True}


def _resolve(args: argparse.Namespace, *, create: bool = False) -> tuple[Any, Path, dict[str, Any]]:
    system = args.target or None
    layout = default_layout(system=system)
    path = Path(args.config).expanduser() if args.config else layout.config_path
    if path.exists():
        config = load_config(path)
        return layout_from_config(config), path, config
    if not create:
        raise SelfHostError(f"configuration not found: {path}; run init first")
    config = default_config(layout=layout, instance_name=args.name, source_root=Path(args.source).expanduser())
    if path != layout.config_path:
        config["paths"]["config_path"] = str(path)
    return layout, path, config


def command_plan(args: argparse.Namespace) -> int:
    layout, path, config = _resolve(args, create=True)
    commands = install_commands(Path(args.source).expanduser().resolve(), layout, include_optional=args.full)
    service = build_service_plan(config, python_executable=venv_python(layout), launcher_path=layout.launcher_path)
    _json({
        "schema": "magi.selfhost.plan/v1",
        "ok": not validate_config(config),
        "target": config["instance"]["platform"],
        "config_path": str(path),
        "layout": layout.as_dict(),
        "dependency_commands": commands,
        "service": service.as_dict(),
        "mutates_machine": False,
    })
    return 0


def command_init(args: argparse.Namespace) -> int:
    layout, path, config = _resolve(args, create=True)
    if not args.apply:
        _json({"ok": True, "dry_run": True, "config_path": str(path), "config": config})
        return 0
    _ensure_native(config)
    if not path.exists():
        write_config(config, path)
    created = initialise_instance(config)
    secret_result = _generate_local_secrets(config) if args.generate_secrets else {"ok": True, "generated": []}
    _json({"ok": True, "dry_run": False, "config_path": str(path), "created": created, "secrets": secret_result})
    return 0


def _run(command: list[str], *, timeout: int) -> dict[str, Any]:
    started = time.monotonic()
    try:
        proc = subprocess.run(command, cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
        return {
            "command": command,
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "elapsed_sec": round(time.monotonic() - started, 3),
            "output_tail": (proc.stdout or "")[-4000:],
        }
    except subprocess.TimeoutExpired as exc:
        output = exc.stdout if isinstance(exc.stdout, str) else ""
        return {"command": command, "ok": False, "timeout": True, "elapsed_sec": round(time.monotonic() - started, 3), "output_tail": output[-4000:]}


def _wait_for_live(
    config: Mapping[str, Any],
    config_path: Path,
    *,
    timeout: float = 90.0,
) -> dict[str, Any]:
    """Wait for both required HTTP services and retain the final evidence."""

    started = time.monotonic()
    attempts = 0
    final: dict[str, Any] = {}
    while time.monotonic() - started < timeout:
        attempts += 1
        final = _doctor_with_instance_python(config, config_path, live=True)
        live_checks = [
            item for item in final.get("checks", [])
            if str(item.get("key") or "").startswith("live:")
        ]
        if live_checks and all(item.get("status") == "pass" for item in live_checks):
            return {
                "ok": True,
                "attempts": attempts,
                "elapsed_sec": round(time.monotonic() - started, 3),
                "doctor": final,
            }
        time.sleep(2)
    return {
        "ok": False,
        "attempts": attempts,
        "elapsed_sec": round(time.monotonic() - started, 3),
        "doctor": final,
        "action": "查看服務日誌；修復後重試，不要繞過 LIVE 驗收",
    }


def _release_id(args: argparse.Namespace) -> str:
    return args.release_id or time.strftime("v3-selfhost-%Y%m%d-%H%M%S")


def _ensure_native(config: Mapping[str, Any]) -> None:
    """Refuse cross-target writes while still allowing safe plan generation."""

    target = str(dict(config.get("instance") or {}).get("platform") or "")
    actual = platform.system()
    if target != actual:
        raise SelfHostError(
            f"refusing to mutate {target} deployment from {actual}; "
            "use plan for cross-platform preview"
        )


def _ensure_python_supported() -> None:
    if sys.version_info < (3, 12):
        raise SelfHostError(
            f"Python 3.12 or newer is required; current interpreter is {sys.version.split()[0]}"
        )


def _doctor_with_instance_python(
    config: Mapping[str, Any],
    config_path: Path,
    *,
    live: bool,
) -> dict[str, Any]:
    """Run dependency-aware checks with the installed instance interpreter."""

    target_python = Path(str(dict(config["service"])["python"])).expanduser()
    try:
        using_target_python = target_python.resolve() == Path(sys.executable).resolve()
    except OSError:
        using_target_python = False
    if (
        target_python.is_file()
        and not using_target_python
        and os.environ.get("MAGI_SELFHOST_DOCTOR_CHILD") != "1"
    ):
        command = [
            str(target_python),
            str(ROOT / "scripts" / "magi_selfhost.py"),
            "--config",
            str(config_path),
            "doctor",
        ]
        if live:
            command.append("--live")
        completed = subprocess.run(
            command,
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=180,
            env={**os.environ, "MAGI_SELFHOST_DOCTOR_CHILD": "1"},
        )
        try:
            payload = json.loads(completed.stdout or "")
        except json.JSONDecodeError:
            return {
                "schema": "magi.selfhost.doctor/v1",
                "ok": False,
                "ready": False,
                "platform": str(dict(config.get("instance") or {}).get("platform") or ""),
                "summary": {"pass": 0, "warn": 0, "fail": 1},
                "checks": [{
                    "key": "doctor_runtime",
                    "status": "fail",
                    "detail": (completed.stdout or "instance doctor produced no JSON")[-2000:],
                    "action": "重新建立 self-host 虛擬環境後再執行 doctor",
                }],
            }
        return dict(payload)
    return doctor(config, live=live)


def command_install(args: argparse.Namespace) -> int:
    layout, path, config = _resolve(args, create=True)
    source = Path(args.source).expanduser().resolve()
    commands = install_commands(source, layout, include_optional=args.full)
    release_id = _release_id(args)
    service_plan = build_service_plan(config, python_executable=venv_python(layout), launcher_path=layout.launcher_path)
    if not args.apply:
        _json({
            "schema": "magi.selfhost.install/v1",
            "ok": True,
            "dry_run": True,
            "target": config["instance"]["platform"],
            "config_path": str(path),
            "release_id": release_id,
            "steps": [
                {"key": "init", "detail": "create platform-native instance directories and secret file"},
                *({"key": "dependency", "command": command} for command in commands),
                {"key": "release", "detail": "stage immutable release and atomically activate it"},
                {"key": "service", "detail": service_plan.as_dict()},
                {"key": "doctor", "detail": "run local acceptance checks"},
            ],
        })
        return 0

    _ensure_native(config)
    _ensure_python_supported()
    if path.exists():
        config = load_config(path)
    else:
        write_config(config, path)
    initialise_instance(config)
    secret_result = _generate_local_secrets(config)
    dependency_results: list[dict[str, Any]] = []
    if not args.skip_dependencies:
        for index, command in enumerate(commands):
            result = _run(command, timeout=3600 if index >= 2 else 600)
            dependency_results.append(result)
            if not result["ok"]:
                _json({"ok": False, "stage": "dependencies", "results": dependency_results, "next_action": "修正失敗項目後以同一設定重新執行 install --apply"})
                return 1
    elif not venv_python(layout).exists():
        raise SelfHostError("--skip-dependencies requires an existing self-host virtual environment")

    database_result: dict[str, Any] = {"ok": True, "skipped": True}
    if args.bootstrap_database:
        database_result = _run([
            str(venv_python(layout)),
            str(ROOT / "scripts" / "magi_selfhost.py"),
            "--config",
            str(path),
            "database",
            "--apply",
        ], timeout=180)
        if not database_result.get("ok"):
            _json({
                "ok": False,
                "stage": "database",
                "database": database_result,
                "next_action": "確認資料庫帳號具有連線及初始化權限後重試",
            })
            return 1

    release = stage_release(source, config, release_id=release_id)
    active = activate_release(config, release_id)
    launcher = write_launcher(config)
    report = _doctor_with_instance_python(config, path, live=False)
    service_results: list[dict[str, Any]] = []
    live_acceptance: dict[str, Any] = {"ok": True, "skipped": True}
    service_deferred = False
    if not args.no_service:
        if not report["ready"]:
            service_deferred = True
            service_results.append({
                "ok": True,
                "status": "deferred",
                "stage": "configuration",
                "detail": "program installation completed; service activation is waiting for required configuration",
                "action": "run configure --interactive --apply, database --apply, doctor, then service install/start --apply",
            })
        else:
            service_results.extend(execute_service_plan(service_plan, action="install"))
            if all(item.get("ok") for item in service_results):
                service_results.extend(execute_service_plan(service_plan, action="start"))
                if all(item.get("ok") for item in service_results):
                    live_acceptance = _wait_for_live(config, path)
                    if not live_acceptance["ok"]:
                        live_acceptance["service_stop"] = execute_service_plan(service_plan, action="stop")
    ok = (
        report["ok"]
        and live_acceptance["ok"]
        and all(item.get("ok") for item in dependency_results + service_results)
    )
    _json({
        "schema": "magi.selfhost.install/v1",
        "ok": ok,
        "dry_run": False,
        "config_path": str(path),
        "secrets": secret_result,
        "dependencies": dependency_results,
        "database": database_result,
        "release": release,
        "active": active,
        "launcher": str(launcher),
        "service": service_results,
        "service_deferred": service_deferred,
        "live_acceptance": live_acceptance,
        "doctor": report,
        "next_action": (
            "run configure --interactive --apply, then database --apply and service install/start --apply"
            if ok and service_deferred
            else "於瀏覽器開啟 http://127.0.0.1:5002"
            if ok
            else "依失敗項目的 action 修復後重試"
        ),
    })
    return 0 if ok else 1


def command_doctor(args: argparse.Namespace) -> int:
    _layout, path, config = _resolve(args)
    payload = _doctor_with_instance_python(config, path, live=args.live)
    payload["strict"] = bool(args.strict)
    payload["accepted"] = bool(payload["ready"] if args.strict else payload["ok"])
    _json(payload)
    return 0 if payload["accepted"] else 1


def command_database(args: argparse.Namespace) -> int:
    layout, path, config = _resolve(args)
    secret_path = Path(str(dict(config["secrets"])["env_file"])).expanduser()
    if not args.apply:
        _json({
            "ok": True,
            "dry_run": True,
            "engine": str(dict(config.get("database") or {}).get("engine") or ""),
            "secret_path": str(secret_path),
            "steps": [
                "connect without printing credentials",
                "create magi_brain and the configured business database if permitted",
                "create or verify idempotent first-boot tables",
                "run an independent schema verification",
            ],
        })
        return 0
    _ensure_native(config)
    target_python = Path(str(dict(config["service"])["python"])).expanduser()
    try:
        using_target_python = target_python.resolve() == Path(sys.executable).resolve()
    except OSError:
        using_target_python = False
    if target_python.is_file() and not using_target_python and os.environ.get("MAGI_SELFHOST_DB_CHILD") != "1":
        completed = subprocess.run(
            [
                str(target_python),
                str(ROOT / "scripts" / "magi_selfhost.py"),
                "--config",
                str(path),
                "database",
                "--apply",
            ],
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=180,
            env={**os.environ, "MAGI_SELFHOST_DB_CHILD": "1"},
        )
        print(completed.stdout or "", end="")
        return int(completed.returncode)
    result = bootstrap_mysql_databases(parse_env_file(secret_path))
    _json(result)
    return 0 if result.get("ok") else 1


def command_service(args: argparse.Namespace) -> int:
    layout, path, config = _resolve(args)
    if args.apply:
        _ensure_native(config)
        if args.action in {"install", "start"}:
            report = _doctor_with_instance_python(config, path, live=False)
            if not report["ready"]:
                _json({
                    "ok": False,
                    "stage": "configuration",
                    "doctor": report,
                    "next_action": "先補齊必要設定，確認 doctor ready=true 後再啟動服務",
                })
                return 1
    plan = build_service_plan(config, python_executable=venv_python(layout), launcher_path=layout.launcher_path)
    results = execute_service_plan(plan, action=args.action, dry_run=not args.apply)
    live_acceptance: dict[str, Any] = {"ok": True, "skipped": True}
    if args.apply and args.action == "start" and all(item.get("ok") for item in results):
        live_acceptance = _wait_for_live(config, path)
        if not live_acceptance["ok"]:
            live_acceptance["service_stop"] = execute_service_plan(plan, action="stop")
    payload = {
        "ok": all(item.get("ok") for item in results) and live_acceptance["ok"],
        "dry_run": not args.apply,
        "action": args.action,
        "plan": plan.as_dict(),
        "results": results,
        "live_acceptance": live_acceptance,
    }
    _json(payload)
    return 0 if payload["ok"] else 1


def command_upgrade(args: argparse.Namespace) -> int:
    layout, path, config = _resolve(args)
    source = Path(args.source).expanduser().resolve()
    release_id = _release_id(args)
    plan = build_service_plan(config, python_executable=venv_python(layout), launcher_path=layout.launcher_path)
    if not args.apply:
        _json({"ok": True, "dry_run": True, "release_id": release_id, "source": str(source), "current": active_release(config), "service_restart": not args.no_restart})
        return 0
    _ensure_native(config)
    staged = stage_release(source, config, release_id=release_id)
    if args.no_restart:
        _json({
            "ok": True,
            "dry_run": False,
            "staged_only": True,
            "release": staged,
            "active": active_release(config),
            "next_action": "候選版已封存但未啟用；移除 --no-restart 後再執行才會切換",
        })
        return 0

    previous = active_release(config)
    stopped: list[dict[str, Any]] = []
    started: list[dict[str, Any]] = []
    stopped = execute_service_plan(plan, action="stop")
    if stopped and not all(item.get("ok") for item in stopped):
        _json({"ok": False, "stage": "stop", "results": stopped, "release": staged})
        return 1
    marker = activate_release(config, release_id)
    started = execute_service_plan(plan, action="start")
    live_acceptance = (
        _wait_for_live(config, path)
        if all(item.get("ok") for item in started)
        else {"ok": False, "skipped": True, "reason": "service start command failed"}
    )
    ok = all(item.get("ok") for item in stopped + started) and live_acceptance["ok"]
    automatic_recovery: dict[str, Any] = {"attempted": False, "ok": True}
    if not ok:
        execute_service_plan(plan, action="stop")
        previous_id = str((previous or {}).get("release_id") or "")
        if previous_id:
            recovered_marker = activate_release(config, previous_id)
            recovered_start = execute_service_plan(plan, action="start")
            recovered_live = (
                _wait_for_live(config, path)
                if all(item.get("ok") for item in recovered_start)
                else {"ok": False, "skipped": True, "reason": "previous service failed to start"}
            )
            automatic_recovery = {
                "attempted": True,
                "ok": all(item.get("ok") for item in recovered_start) and recovered_live["ok"],
                "active": recovered_marker,
                "start": recovered_start,
                "live_acceptance": recovered_live,
            }
        else:
            automatic_recovery = {
                "attempted": True,
                "ok": False,
                "reason": "no previous release was available",
            }
    _json({
        "ok": ok,
        "dry_run": False,
        "release": staged,
        "active": marker,
        "stop": stopped,
        "start": started,
        "live_acceptance": live_acceptance,
        "automatic_recovery": automatic_recovery,
    })
    return 0 if ok else 1


def command_rollback(args: argparse.Namespace) -> int:
    layout, path, config = _resolve(args)
    plan = build_service_plan(config, python_executable=venv_python(layout), launcher_path=layout.launcher_path)
    if not args.apply:
        current = active_release(config)
        _json({"ok": bool(current and current.get("previous_release_id")), "dry_run": True, "current": current})
        return 0 if current and current.get("previous_release_id") else 1
    _ensure_native(config)
    stopped = execute_service_plan(plan, action="stop")
    if stopped and not all(item.get("ok") for item in stopped):
        _json({"ok": False, "stage": "stop", "results": stopped})
        return 1
    marker = rollback_release(config)
    started = execute_service_plan(plan, action="start")
    live_acceptance = (
        _wait_for_live(config, path)
        if all(item.get("ok") for item in started)
        else {"ok": False, "skipped": True, "reason": "service start command failed"}
    )
    ok = all(item.get("ok") for item in started) and live_acceptance["ok"]
    if not ok:
        execute_service_plan(plan, action="stop")
    _json({
        "ok": ok,
        "active": marker,
        "stop": stopped,
        "start": started,
        "live_acceptance": live_acceptance,
    })
    return 0 if ok else 1


def command_uninstall(args: argparse.Namespace) -> int:
    layout, path, config = _resolve(args)
    if args.remove_data and not args.confirm_remove_data:
        raise SelfHostError("--remove-data requires --confirm-remove-data")
    plan = build_service_plan(config, python_executable=venv_python(layout), launcher_path=layout.launcher_path)
    if not args.apply:
        _json({"ok": True, "dry_run": True, "preserve_data": not args.remove_data, "config_path": str(path), "service": plan.as_dict()})
        return 0
    _ensure_native(config)
    stopped = execute_service_plan(plan, action="stop")
    removed_service = execute_service_plan(plan, action="uninstall")
    removed = safe_remove_program(config, remove_data=args.remove_data)
    _json({"ok": all(item.get("ok") for item in stopped + removed_service), "service_stop": stopped, "service_uninstall": removed_service, "removed": removed, "preserved_config": str(path), "preserved_data": not args.remove_data})
    return 0


def command_secrets(args: argparse.Namespace) -> int:
    _layout, _path, config = _resolve(args)
    secret_path = Path(str(dict(config.get("secrets") or {})["env_file"])).expanduser()
    if not args.apply:
        _json({
            "ok": True,
            "dry_run": True,
            "path": str(secret_path),
            "generate": bool(args.generate),
            "mutates_machine": False,
        })
        return 0
    _ensure_native(config)
    path = write_secret_template(config)
    result = _generate_local_secrets(config) if args.generate else {"ok": True, "path": str(path), "generated": []}
    _json(result)
    return 0


def _prompt_value(label: str, current: str, default: str) -> str:
    shown = current or default
    entered = input(f"{label} [{shown}]: ").strip()
    return entered or shown


def command_configure(args: argparse.Namespace) -> int:
    """Configure database values without exposing the password in argv/history."""

    _layout, path, config = _resolve(args)
    secret_path = Path(str(dict(config["secrets"])["env_file"])).expanduser()
    existing = parse_env_file(secret_path)
    requested = {
        "DB_HOST": str(args.db_host or "").strip(),
        "DB_PORT": str(args.db_port or "").strip(),
        "DB_USER": str(args.db_user or "").strip(),
        "DB_NAME": str(args.db_name or "").strip(),
        "MAGI_BRAIN_DB_NAME": str(args.brain_db_name or "").strip(),
    }
    if not args.apply:
        _json({
            "ok": True,
            "dry_run": True,
            "path": str(secret_path),
            "interactive": bool(args.interactive),
            "requested_keys": sorted(key for key, value in requested.items() if value),
            "missing_database_keys": [
                key for key in ("DB_HOST", "DB_PORT", "DB_USER", "DB_PASSWORD", "DB_NAME")
                if not existing.get(key, "").strip()
            ],
            "password_transport": "hidden terminal prompt only; never a command-line argument",
        })
        return 0

    _ensure_native(config)
    if args.interactive:
        if not sys.stdin.isatty():
            raise SelfHostError("interactive configuration requires a terminal")
        requested.update({
            "DB_HOST": _prompt_value("資料庫主機", existing.get("DB_HOST", ""), "127.0.0.1"),
            "DB_PORT": _prompt_value("資料庫連接埠", existing.get("DB_PORT", ""), "3306"),
            "DB_USER": _prompt_value("資料庫帳號", existing.get("DB_USER", ""), "magi"),
            "DB_NAME": _prompt_value("事業資料庫名稱", existing.get("DB_NAME", ""), "magi"),
            "MAGI_BRAIN_DB_NAME": _prompt_value(
                "MAGI 核心資料庫名稱",
                existing.get("MAGI_BRAIN_DB_NAME", ""),
                "magi_brain",
            ),
        })
        password = getpass.getpass(
            "資料庫密碼（留空可保留現有密碼）: "
        )
        if password:
            requested["DB_PASSWORD"] = password
        elif existing.get("DB_PASSWORD", ""):
            requested["DB_PASSWORD"] = existing["DB_PASSWORD"]
        else:
            raise SelfHostError("資料庫密碼不可為空")

    updates = {key: value for key, value in requested.items() if value}
    if not updates:
        raise SelfHostError("no configuration values were supplied")
    port = updates.get("DB_PORT") or existing.get("DB_PORT") or ""
    try:
        parsed_port = int(port)
    except ValueError as exc:
        raise SelfHostError("DB_PORT must be an integer") from exc
    if not 1 <= parsed_port <= 65535:
        raise SelfHostError("DB_PORT is outside 1..65535")
    for key in ("DB_NAME", "MAGI_BRAIN_DB_NAME"):
        value = updates.get(key) or existing.get(key) or ""
        if value and (len(value) > 64 or not value.replace("_", "").isalnum()):
            raise SelfHostError(f"{key} must contain only letters, numbers, and underscores")
    _write_env(secret_path, updates)
    report = _doctor_with_instance_python(config, path, live=False)
    _json({
        "ok": True,
        "dry_run": False,
        "path": str(secret_path),
        "updated_keys": sorted(updates),
        "secret_values_redacted": True,
        "doctor": report,
        "next_action": "執行 database --apply，然後執行 service install --apply 與 service start --apply",
    })
    return 0


def command_package(args: argparse.Namespace) -> int:
    source = Path(args.source).expanduser().resolve()
    output = (
        Path(args.output).expanduser()
        if args.output
        else source / "dist" / f"MAGI-V3-selfhost-{time.strftime('%Y%m%d-%H%M%S')}.zip"
    )
    if not args.apply:
        _json({
            "ok": True,
            "dry_run": True,
            "source": str(source),
            "output": str(output),
            "contains_secrets": False,
            "platforms": ["macOS", "Windows"],
        })
        return 0
    result = build_distribution_archive(source, output)
    _json({"ok": True, "dry_run": False, **result})
    return 0


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Install and operate a portable MAGI self-host instance on macOS or Windows.")
    p.add_argument("--target", type=_target_system, default="", help="auto, macos, windows, or linux")
    p.add_argument("--config", help="selfhost.json path; defaults to the native per-user directory")
    p.add_argument("--source", default=str(ROOT), help="MAGI source or extracted release directory")
    p.add_argument("--name", default="MAGI", help="instance display name")
    sub = p.add_subparsers(dest="command", required=True)

    plan = sub.add_parser("plan", help="show an immutable, non-mutating installation plan")
    plan.add_argument("--full", action="store_true", help="include optional local models, OCR, and browser dependencies")
    plan.set_defaults(handler=command_plan)

    init = sub.add_parser("init", help="create instance directories and configuration")
    init.add_argument("--apply", action="store_true")
    init.add_argument("--generate-secrets", action="store_true")
    init.set_defaults(handler=command_init)

    install = sub.add_parser("install", help="install dependencies, stage a release, and install the background service")
    install.add_argument("--apply", action="store_true")
    install.add_argument("--full", action="store_true")
    install.add_argument("--skip-dependencies", action="store_true")
    install.add_argument("--no-service", action="store_true")
    install.add_argument("--bootstrap-database", action="store_true")
    install.add_argument("--release-id")
    install.set_defaults(handler=command_install)

    check = sub.add_parser("doctor", help="validate configuration, release, secrets, and optional live endpoints")
    check.add_argument("--live", action="store_true")
    check.add_argument("--strict", action="store_true", help="treat every warning as a commissioning blocker")
    check.set_defaults(handler=command_doctor)

    database = sub.add_parser("database", help="safely create or verify the required MySQL/MariaDB schemas")
    database.add_argument("--apply", action="store_true")
    database.set_defaults(handler=command_database)

    service = sub.add_parser("service", help="install, start, stop, or remove the native user service")
    service.add_argument("action", choices=("install", "start", "stop", "uninstall"))
    service.add_argument("--apply", action="store_true")
    service.set_defaults(handler=command_service)

    upgrade = sub.add_parser("upgrade", help="stage and atomically activate a new immutable release")
    upgrade.add_argument("--apply", action="store_true")
    upgrade.add_argument("--release-id")
    upgrade.add_argument("--no-restart", action="store_true")
    upgrade.set_defaults(handler=command_upgrade)

    rollback = sub.add_parser("rollback", help="atomically return to the previously active release")
    rollback.add_argument("--apply", action="store_true")
    rollback.set_defaults(handler=command_rollback)

    uninstall = sub.add_parser("uninstall", help="remove the program while preserving user data by default")
    uninstall.add_argument("--apply", action="store_true")
    uninstall.add_argument("--remove-data", action="store_true")
    uninstall.add_argument("--confirm-remove-data", action="store_true")
    uninstall.set_defaults(handler=command_uninstall)

    secret_cmd = sub.add_parser("secrets", help="create the private env file and generate local secrets")
    secret_cmd.add_argument("--apply", action="store_true")
    secret_cmd.add_argument("--generate", action="store_true")
    secret_cmd.set_defaults(handler=command_secrets)

    configure = sub.add_parser("configure", help="configure MySQL/MariaDB without exposing its password in shell history")
    configure.add_argument("--apply", action="store_true")
    configure.add_argument("--interactive", action="store_true")
    configure.add_argument("--db-host")
    configure.add_argument("--db-port")
    configure.add_argument("--db-user")
    configure.add_argument("--db-name")
    configure.add_argument("--brain-db-name")
    configure.set_defaults(handler=command_configure)

    package = sub.add_parser("package", help="build a secret-free universal macOS/Windows source archive")
    package.add_argument("--apply", action="store_true")
    package.add_argument("--output")
    package.set_defaults(handler=command_package)
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        return int(args.handler(args))
    except SelfHostError as exc:
        _json({"ok": False, "error": str(exc), "next_action": "依訊息修正設定後，以不帶 --apply 的命令先查看計畫"})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
