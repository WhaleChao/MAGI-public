#!/usr/bin/env python3
"""Cross-platform MAGI installer launcher.

This script is the small program that a macOS .app/.dmg or Windows .exe
starts.  It unpacks a customer-safe MAGI release bundle, finds a usable Python
interpreter, starts the existing customer install wizard, and then bootstraps
the best local model runtime for the customer's machine.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any


APP_NAME = "MAGI"
DEFAULT_REPORT_NAME = "magi_installer_launcher_latest.json"


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def resource_dir() -> Path:
    if hasattr(sys, "_MEIPASS"):
        return Path(getattr(sys, "_MEIPASS"))
    return Path(__file__).resolve().parent


def default_archive() -> Path | None:
    candidates = [
        os.environ.get("MAGI_INSTALL_ARCHIVE", ""),
        resource_dir() / "MAGI-release.zip",
        Path(__file__).resolve().with_name("MAGI-release.zip"),
    ]
    for candidate in candidates:
        if not candidate:
            continue
        path = Path(candidate)
        if path.is_file():
            return path
    return None


def default_install_base() -> Path:
    override = os.environ.get("MAGI_INSTALL_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    if platform.system() == "Windows":
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            return Path(local_app_data) / APP_NAME
        return Path.home() / APP_NAME
    return Path.home() / APP_NAME


def _safe_zip_target(base: Path, member: str) -> Path:
    rel = PurePosixPath(member)
    if rel.is_absolute() or ".." in rel.parts:
        raise ValueError(f"unsafe zip member: {member}")
    target = base / Path(*rel.parts)
    target.resolve().relative_to(base.resolve())
    return target


def _single_top_level(names: list[str]) -> str | None:
    roots = {PurePosixPath(name).parts[0] for name in names if name and not name.endswith("/")}
    if len(roots) == 1:
        return next(iter(roots))
    return None


def extract_release_archive(archive: Path, install_base: Path, *, force: bool = False) -> Path:
    """Extract a MAGI release zip and return the repository directory."""

    install_base = install_base.expanduser().resolve()
    install_base.mkdir(parents=True, exist_ok=True)

    existing_wizard = install_base / "scripts" / "customer_install_wizard.py"
    if existing_wizard.is_file() and not force:
        return install_base

    with zipfile.ZipFile(archive) as zf:
        names = [name for name in zf.namelist() if name and not name.endswith("/")]
        for name in names:
            _safe_zip_target(install_base, name)
        root = _single_top_level(names)

        if root:
            repo_dir = install_base
            if any(repo_dir.iterdir()) and not force and not existing_wizard.exists():
                repo_dir = install_base / root
            if force and repo_dir.exists() and repo_dir != install_base:
                shutil.rmtree(repo_dir)
            repo_dir.mkdir(parents=True, exist_ok=True)
            for info in zf.infolist():
                if info.is_dir():
                    continue
                rel = PurePosixPath(info.filename)
                if root and rel.parts and rel.parts[0] == root:
                    rel = PurePosixPath(*rel.parts[1:])
                if not rel.parts:
                    continue
                target = _safe_zip_target(repo_dir, rel.as_posix())
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, target.open("wb") as dst:
                    shutil.copyfileobj(src, dst)
            return repo_dir

        repo_dir = install_base
        if force and repo_dir.exists():
            shutil.rmtree(repo_dir)
            repo_dir.mkdir(parents=True, exist_ok=True)
        for info in zf.infolist():
            if info.is_dir():
                continue
            target = _safe_zip_target(repo_dir, info.filename)
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, target.open("wb") as dst:
                shutil.copyfileobj(src, dst)
        return repo_dir


def _run_probe(command: list[str]) -> bool:
    try:
        proc = subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15, check=False)
    except Exception:
        return False
    return proc.returncode == 0


def find_python() -> str | None:
    override = os.environ.get("MAGI_INSTALL_BOOTSTRAP_PYTHON", "").strip()
    if override and _run_probe([override, "-c", "import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)"]):
        return override

    if not is_frozen() and sys.executable:
        if _run_probe([sys.executable, "-c", "import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)"]):
            return sys.executable

    candidates: list[list[str]]
    if platform.system() == "Windows":
        candidates = [["py", "-3"], ["python"], ["python3"]]
    else:
        candidates = [["python3"], ["python"]]

    for candidate in candidates:
        if _run_probe([*candidate, "-c", "import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)"]):
            return " ".join(candidate)
    return None


def python_command(python: str, script: Path, args: list[str]) -> list[str]:
    parts = python.split() if " " in python else [python]
    return [*parts, str(script), *args]


def build_wizard_command(
    repo_dir: Path,
    *,
    python: str,
    yes: bool,
    public: bool,
    no_live: bool,
    no_optional: bool,
    install_service: bool,
    report_path: Path,
) -> list[str]:
    args = ["--output", str(report_path)]
    if public:
        args.append("--public")
    if yes:
        args.append("--yes")
    if no_live:
        args.append("--no-live")
    if no_optional:
        args.append("--no-optional")
    if install_service:
        args.append("--install-service")
    return python_command(python, repo_dir / "scripts" / "customer_install_wizard.py", args)


def run_wizard(command: list[str], repo_dir: Path) -> int:
    print("[MAGI] Starting customer install wizard...")
    print("[MAGI] " + " ".join(command))
    proc = subprocess.Popen(command, cwd=repo_dir)
    return proc.wait()


def build_runtime_command(
    repo_dir: Path,
    *,
    python: str,
    yes: bool,
    dry_run: bool,
    allow_system_install: bool,
    download_models: bool,
    install_services: bool,
    provider: str,
    include_heavy: bool,
    report_path: Path,
) -> list[str]:
    args = ["--repo-dir", str(repo_dir), "--output", str(report_path)]
    if provider:
        args.extend(["--provider", provider])
    if yes and not dry_run:
        args.append("--yes")
    if dry_run:
        args.append("--dry-run")
    if allow_system_install:
        args.append("--allow-system-install")
    if download_models:
        args.append("--download-models")
    if install_services:
        args.append("--install-services")
    if include_heavy:
        args.append("--include-heavy")
    return python_command(python, repo_dir / "scripts" / "packaging" / "runtime_bootstrap.py", args)


def run_runtime_bootstrap(command: list[str], repo_dir: Path) -> int:
    print("[MAGI] Detecting runtime and downloading the best local model set...")
    print("[MAGI] " + " ".join(command))
    proc = subprocess.Popen(command, cwd=repo_dir)
    return proc.wait()


def write_launcher_report(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def prompt_yes_no(question: str, default: bool) -> bool:
    suffix = "Y/n" if default else "y/N"
    try:
        answer = input(f"{question} [{suffix}] ").strip().lower()
    except EOFError:
        return default
    if not answer:
        return default
    return answer in {"y", "yes", "是", "好", "1"}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Start the MAGI customer install wizard from a DMG or EXE launcher.")
    parser.add_argument("--archive", type=Path, default=None, help="MAGI release zip embedded in the installer")
    parser.add_argument("--install-dir", type=Path, default=None, help="Where MAGI should be installed")
    parser.add_argument("--public", action="store_true", default=True, help="run public-release install checks")
    parser.add_argument("--private", dest="public", action="store_false", help="do not force public-release mode")
    parser.add_argument("--yes", action="store_true", help="run install steps immediately")
    parser.add_argument("--dry-run", action="store_true", help="preview only")
    parser.add_argument("--force-extract", action="store_true", help="overwrite an existing extracted bundle")
    parser.add_argument("--no-live", action="store_true", default=True, help="skip live probes during first install")
    parser.add_argument("--check-live", dest="no_live", action="store_false", help="include live probes")
    parser.add_argument("--no-optional", action="store_true", default=True, help="skip optional local model acceleration deps")
    parser.add_argument("--with-optional", dest="no_optional", action="store_false", help="include optional deps")
    parser.add_argument("--install-service", action="store_true", help="install background service after setup")
    parser.add_argument("--skip-runtime-bootstrap", action="store_true", help="skip local model runtime detection and installation")
    parser.add_argument("--provider", choices=["", "omlx", "ollama"], default="", help="force local model runtime")
    parser.add_argument("--allow-system-install", action="store_true", help="allow Homebrew/winget runtime installation")
    parser.add_argument("--download-models", action="store_true", help="download selected local models")
    parser.add_argument("--skip-model-download", action="store_true", help="do not download local models")
    parser.add_argument("--install-runtime-services", action="store_true", help="install local runtime services such as oMLX LaunchAgents")
    parser.add_argument("--include-heavy", action="store_true", help="also install the heavy model when supported")
    parser.add_argument("--non-interactive", action="store_true", help="do not prompt")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    archive = args.archive or default_archive()
    install_base = (args.install_dir or default_install_base()).expanduser()
    report_path = install_base / ".runtime" / DEFAULT_REPORT_NAME

    if not archive or not archive.is_file():
        print("[MAGI] 找不到安裝封包 MAGI-release.zip。請重新下載安裝器。")
        return 2

    if not args.non_interactive and not args.yes and not args.dry_run:
        print("MAGI 安裝精靈")
        print(f"安裝位置：{install_base}")
        args.yes = prompt_yes_no("要現在開始安裝 MAGI、MariaDB/Tailscale、模型 runtime 與建議模型嗎？", default=True)
        if args.yes:
            args.allow_system_install = prompt_yes_no("允許 MAGI 使用 Homebrew/winget/系統套件管理器安裝 MariaDB、Tailscale、oMLX 或 Ollama 嗎？", default=True)
            if not args.skip_model_download:
                args.download_models = prompt_yes_no("允許 MAGI 下載最適合此電腦的本地模型嗎？", default=True)

    started = time.strftime("%Y-%m-%d %H:%M:%S")
    repo_dir = extract_release_archive(archive, install_base, force=args.force_extract)
    python = find_python()
    if not python:
        print("[MAGI] 找不到 Python 3.12 以上。請先安裝 Python 3.12+ 後再重新啟動 MAGI 安裝器。")
        write_launcher_report(
            report_path,
            {
                "ok": False,
                "started_at": started,
                "install_base": str(install_base),
                "repo_dir": str(repo_dir),
                "error": "python_3_12_not_found",
            },
        )
        return 3

    execute = bool(args.yes and not args.dry_run)
    wizard_report = repo_dir / ".runtime" / "customer_install_wizard_latest.json"
    command = build_wizard_command(
        repo_dir,
        python=python,
        yes=execute,
        public=bool(args.public),
        no_live=bool(args.no_live),
        no_optional=bool(args.no_optional),
        install_service=bool(args.install_service),
        report_path=wizard_report,
    )
    os.environ["MAGI_INSTALL_BOOTSTRAP_PYTHON"] = python
    rc = run_wizard(command, repo_dir)
    runtime_rc = 0
    runtime_report = repo_dir / ".runtime" / "runtime_bootstrap_latest.json"
    runtime_command: list[str] = []
    if rc == 0 and not args.skip_runtime_bootstrap:
        runtime_command = build_runtime_command(
            repo_dir,
            python=python,
            yes=execute,
            dry_run=bool(args.dry_run or not args.yes),
            allow_system_install=bool(args.allow_system_install),
            download_models=bool(args.download_models and not args.skip_model_download),
            install_services=bool(args.install_runtime_services),
            provider=str(args.provider or ""),
            include_heavy=bool(args.include_heavy),
            report_path=runtime_report,
        )
        runtime_rc = run_runtime_bootstrap(runtime_command, repo_dir)
    payload: dict[str, Any] = {
        "ok": rc == 0 and runtime_rc == 0,
        "started_at": started,
        "finished_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "returncode": rc,
        "runtime_returncode": runtime_rc,
        "archive": str(archive),
        "install_base": str(install_base),
        "repo_dir": str(repo_dir),
        "python": python,
        "wizard_report": str(wizard_report),
        "runtime_report": str(runtime_report) if runtime_command else "",
        "command": command,
        "runtime_command": runtime_command,
    }
    write_launcher_report(report_path, payload)
    print(f"[MAGI] Launcher report: {report_path}")
    print(f"[MAGI] MAGI folder: {repo_dir}")
    if payload["ok"]:
        print("[MAGI] 安裝精靈完成。外部套件與模型設定已匯入 .env；請只補齊帳密、OAuth/token、NAS 帳號等敏感設定後啟動 MAGI。")
    else:
        print("[MAGI] 安裝精靈未完成，請查看上方錯誤與 JSON 報告。")
    if not args.non_interactive:
        try:
            input("按 Enter 關閉視窗...")
        except EOFError:
            pass
    return 0 if payload["ok"] else (rc or runtime_rc or 1)


if __name__ == "__main__":
    raise SystemExit(main())
