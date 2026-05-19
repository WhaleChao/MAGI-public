#!/usr/bin/env python3
"""Detect and prepare the best local MAGI model runtime for a customer host.

The bootstrapper is safe by default.  Without --yes it only writes a plan.
With --yes it can install runtime dependencies and download models.  System
package manager operations require the separate --allow-system-install flag so
the installer never silently changes a customer's machine.
"""

from __future__ import annotations

import argparse
import ctypes
from dataclasses import asdict, dataclass, field
import json
import os
import platform
import re
import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_REPORT = REPO_ROOT / ".runtime" / "runtime_bootstrap_latest.json"
ENV_AUTOGEN_HEADER = "# ── MAGI 安裝精靈自動偵測設定（可手動調整）────────────────"

OMLX_MODEL_SOURCES = {
    "gemma-4-e4b-it-4bit": "mlx-community/gemma-4-e4b-it-4bit",
    "gemma-4-26b-a4b-it-4bit": "mlx-community/gemma-4-26b-a4b-it-4bit",
    "modernbert-embed-4bit": "mlx-community/modernbert-embed-4bit",
    "Phi-4-mini-instruct-4bit": "mlx-community/Phi-4-mini-instruct-4bit",
    "SmolLM3-3B-Instruct-4bit": "mlx-community/SmolLM3-3B-Instruct-4bit",
}

OLLAMA_EMBED_MODEL = "nomic-embed-text"

ENV_OVERWRITE_KEYS = {
    "INFERENCE_LOCAL_CHAT_MODELS",
    "INFERENCE_LOCAL_OLLAMA_BASE",
    "MAGI_DEFAULT_MODEL",
    "MAGI_INFERENCE_PROVIDER",
    "MAGI_INSTALLER_PROFILE_MACHINE",
    "MAGI_INSTALLER_PROFILE_OS",
    "MAGI_INSTALLER_UPDATED_AT",
    "MAGI_MAIN_MODEL",
    "MAGI_MARIADB_BIN",
    "MAGI_OMLX_EMBED_MODEL",
    "MAGI_OMLX_ENABLED",
    "MAGI_OMLX_HOST",
    "MAGI_OMLX_PORT",
    "MAGI_SKILL_PYTHON",
    "MAGI_TAILSCALE_BIN",
    "MAGI_TAILSCALE_ENABLED",
    "MAGI_TEXT_HEAVY_MODEL",
    "MAGI_TEXT_PRIMARY_MODEL",
    "OLLAMA_BASE_URL",
    "OLLAMA_MODEL",
    "OLLAMA_PORT",
    "OMLX_API_KEY",
    "OMLX_BASE_URL",
    "OMLX_MODEL",
    "OMLX_TEXT_MODEL_DIR",
    "OMLX_TEXT_PORT",
    "OMLX_TEXT_START_SCRIPT",
}


@dataclass(frozen=True)
class HardwareProfile:
    os_name: str
    machine: str
    cpu_brand: str
    memory_gb: float
    free_disk_gb: float
    is_apple_silicon: bool


@dataclass(frozen=True)
class ModelDownload:
    role: str
    model: str
    source: str
    local_dir: str = ""
    required: bool = True


@dataclass(frozen=True)
class RuntimePlan:
    provider: str
    runtime: str
    primary_model: str
    embedding_model: str
    heavy_model: str = ""
    reason: str = ""
    downloads: list[ModelDownload] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)


@dataclass
class BootstrapStep:
    key: str
    title: str
    status: str
    detail: str = ""
    required: bool = True
    command: list[str] = field(default_factory=list)
    elapsed_sec: float = 0.0
    output_tail: str = ""
    next_action: str = ""


@dataclass(frozen=True)
class AuxiliaryDependency:
    key: str
    title: str
    executables: tuple[str, ...]
    required: bool
    install_hint: str


UTILITY_DEPENDENCIES = (
    AuxiliaryDependency(
        key="mariadb",
        title="MariaDB 資料庫",
        executables=("mariadb", "mysql"),
        required=True,
        install_hint="MAGI 需要 MariaDB/MySQL 儲存案件、待辦與知識索引。",
    ),
    AuxiliaryDependency(
        key="tailscale",
        title="Tailscale 遠端連線",
        executables=("tailscale",),
        required=False,
        install_hint="Tailscale 用於外網安全連線、NAS fallback 與遠端支援；單機離線可稍後設定。",
    ),
)


def _run(command: list[str], *, cwd: Path | None = None, timeout: int = 900, required: bool = True, env: dict[str, str] | None = None) -> BootstrapStep:
    started = time.monotonic()
    try:
        proc = subprocess.run(
            command,
            cwd=cwd,
            env={**os.environ, **(env or {})},
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
        )
        ok = proc.returncode == 0
        return BootstrapStep(
            key=Path(command[0]).name if command else "command",
            title=" ".join(command),
            status="pass" if ok else ("fail" if required else "warn"),
            detail=f"exit={proc.returncode}",
            required=required,
            command=command,
            elapsed_sec=round(time.monotonic() - started, 3),
            output_tail=(proc.stdout or "")[-4000:],
        )
    except subprocess.TimeoutExpired as exc:
        return BootstrapStep(
            key=Path(command[0]).name if command else "command",
            title=" ".join(command),
            status="fail" if required else "warn",
            detail=f"timeout after {timeout}s",
            required=required,
            command=command,
            elapsed_sec=round(time.monotonic() - started, 3),
            output_tail=((exc.stdout or "") + (exc.stderr or ""))[-4000:] if isinstance(exc.stdout, str) else "",
        )
    except Exception as exc:
        return BootstrapStep(
            key=Path(command[0]).name if command else "command",
            title=" ".join(command),
            status="fail" if required else "warn",
            detail=str(exc),
            required=required,
            command=command,
            elapsed_sec=round(time.monotonic() - started, 3),
        )


def _which(name: str) -> str:
    return shutil.which(name) or ""


def _which_any(names: tuple[str, ...]) -> str:
    for name in names:
        found = _which(name)
        if found:
            return found
    if platform.system() == "Windows":
        candidates = {
            "mariadb": [
                r"C:\Program Files\MariaDB\bin\mariadb.exe",
                r"C:\Program Files\MariaDB\bin\mysql.exe",
            ],
            "mysql": [
                r"C:\Program Files\MariaDB\bin\mysql.exe",
                r"C:\Program Files\MySQL\MySQL Server 8.0\bin\mysql.exe",
            ],
            "tailscale": [
                r"C:\Program Files\Tailscale\tailscale.exe",
            ],
        }
        for name in names:
            for candidate in candidates.get(name, []):
                if Path(candidate).is_file():
                    return candidate
    return ""


def _probe(command: list[str], *, timeout: int = 15) -> bool:
    try:
        proc = subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=timeout, check=False)
    except Exception:
        return False
    return proc.returncode == 0


def _subprocess_text(command: list[str]) -> str:
    try:
        proc = subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=10, check=False)
    except Exception:
        return ""
    return (proc.stdout or "").strip()


def _memory_gb() -> float:
    system = platform.system()
    if system == "Darwin":
        value = _subprocess_text(["sysctl", "-n", "hw.memsize"])
        if value.isdigit():
            return round(int(value) / (1024**3), 1)
    if system == "Windows":
        class MemoryStatus(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        status = MemoryStatus()
        status.dwLength = ctypes.sizeof(MemoryStatus)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):  # type: ignore[attr-defined]
            return round(status.ullTotalPhys / (1024**3), 1)
    try:
        pages = os.sysconf("SC_PHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
        return round((pages * page_size) / (1024**3), 1)
    except Exception:
        return 0.0


def _cpu_brand() -> str:
    if platform.system() == "Darwin":
        return _subprocess_text(["sysctl", "-n", "machdep.cpu.brand_string"]) or platform.processor()
    return platform.processor() or platform.machine()


def detect_hardware() -> HardwareProfile:
    os_name = platform.system()
    machine = platform.machine()
    free_disk = shutil.disk_usage(Path.home()).free / (1024**3)
    is_apple_silicon = os_name == "Darwin" and machine.lower() in {"arm64", "aarch64"}
    return HardwareProfile(
        os_name=os_name,
        machine=machine,
        cpu_brand=_cpu_brand(),
        memory_gb=_memory_gb(),
        free_disk_gb=round(free_disk, 1),
        is_apple_silicon=is_apple_silicon,
    )


def _env_override(name: str, fallback: str) -> str:
    return os.environ.get(name, "").strip() or fallback


def select_runtime_plan(profile: HardwareProfile, *, force_provider: str = "", include_heavy: bool = False) -> RuntimePlan:
    provider = (force_provider or os.environ.get("MAGI_INSTALL_FORCE_PROVIDER", "")).strip().lower()
    if not provider:
        provider = "omlx" if profile.is_apple_silicon and profile.memory_gb >= 16 else "ollama"
    if provider not in {"omlx", "ollama"}:
        raise ValueError(f"unsupported provider: {provider}")

    if provider == "omlx":
        primary = _env_override("MAGI_INSTALL_OMLX_PRIMARY_MODEL", "gemma-4-e4b-it-4bit")
        embedding = _env_override("MAGI_INSTALL_OMLX_EMBED_MODEL", "modernbert-embed-4bit")
        heavy = _env_override("MAGI_INSTALL_OMLX_HEAVY_MODEL", "gemma-4-26b-a4b-it-4bit")
        heavy_enabled = include_heavy or profile.memory_gb >= 48
        downloads = [
            ModelDownload("primary", primary, OMLX_MODEL_SOURCES.get(primary, f"mlx-community/{primary}"), str(Path.home() / ".omlx" / "models" / primary)),
            ModelDownload("embedding", embedding, OMLX_MODEL_SOURCES.get(embedding, f"mlx-community/{embedding}"), str(Path.home() / ".omlx" / "models" / embedding), required=False),
        ]
        if heavy_enabled:
            downloads.append(
                ModelDownload("heavy", heavy, OMLX_MODEL_SOURCES.get(heavy, f"mlx-community/{heavy}"), str(Path.home() / ".omlx" / "models" / heavy), required=False)
            )
        return RuntimePlan(
            provider="omlx",
            runtime="oMLX / MLX on Apple Silicon",
            primary_model=primary,
            embedding_model=embedding,
            heavy_model=heavy if heavy_enabled else "",
            reason="Apple Silicon detected; oMLX is the preferred local runtime.",
            downloads=downloads,
            env={
                "MAGI_INFERENCE_PROVIDER": "omlx",
                "MAGI_TEXT_PRIMARY_MODEL": primary,
                "MAGI_OMLX_EMBED_MODEL": embedding,
            },
        )

    if profile.memory_gb >= 64:
        ollama_model = _env_override("MAGI_INSTALL_OLLAMA_MODEL", "gemma3:27b")
    elif profile.memory_gb >= 32:
        ollama_model = _env_override("MAGI_INSTALL_OLLAMA_MODEL", "gemma3:12b")
    else:
        ollama_model = _env_override("MAGI_INSTALL_OLLAMA_MODEL", "gemma3:4b")
    return RuntimePlan(
        provider="ollama",
        runtime="Ollama",
        primary_model=ollama_model,
        embedding_model=_env_override("MAGI_INSTALL_OLLAMA_EMBED_MODEL", OLLAMA_EMBED_MODEL),
        reason="Windows/Linux or non-Apple-Silicon host; Ollama is the most portable local runtime.",
        downloads=[
            ModelDownload("primary", ollama_model, ollama_model),
            ModelDownload("embedding", _env_override("MAGI_INSTALL_OLLAMA_EMBED_MODEL", OLLAMA_EMBED_MODEL), _env_override("MAGI_INSTALL_OLLAMA_EMBED_MODEL", OLLAMA_EMBED_MODEL), required=False),
        ],
        env={
            "MAGI_INFERENCE_PROVIDER": "ollama",
            "OLLAMA_MODEL": ollama_model,
            "MAGI_MAIN_MODEL": ollama_model,
        },
    )


def venv_python(repo_dir: Path) -> str:
    candidates = [
        os.environ.get("MAGI_RUNTIME_BOOTSTRAP_PYTHON", ""),
        str(repo_dir / ".venv" / ("Scripts/python.exe" if platform.system() == "Windows" else "bin/python")),
        str(repo_dir / "venv" / ("Scripts/python.exe" if platform.system() == "Windows" else "bin/python")),
        sys.executable,
        _which("python3"),
        _which("python"),
    ]
    for candidate in candidates:
        if not candidate:
            continue
        if _probe([candidate, "-c", "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)"]):
            return candidate
    return sys.executable or "python3"


def _step_skipped(key: str, title: str, detail: str, *, required: bool = False, command: list[str] | None = None, next_action: str = "") -> BootstrapStep:
    return BootstrapStep(key, title, "skipped", detail, required=required, command=command or [], next_action=next_action)


def _step_warn(key: str, title: str, detail: str, *, command: list[str] | None = None, next_action: str = "") -> BootstrapStep:
    return BootstrapStep(key, title, "warn", detail, required=False, command=command or [], next_action=next_action)


def _create_omlx_start_script(runtime_root: Path) -> Path:
    bin_dir = runtime_root / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    script = bin_dir / "omlx-magi-start-text"
    script.write_text(
        """#!/bin/zsh
set -euo pipefail
OMLX_BIN="${OMLX_BIN:-$(command -v omlx || true)}"
if [ -z "${OMLX_BIN}" ]; then
  echo "omlx binary not found. Install oMLX first." >&2
  exit 127
fi
BASE_PATH="${OMLX_TEXT_BASE_PATH:-$HOME/.omlx}"
MODEL_DIR="${OMLX_TEXT_MODEL_DIR:-$HOME/.omlx/models-text}"
mkdir -p "$BASE_PATH" "$MODEL_DIR"
exec "$OMLX_BIN" serve \
  --base-path "$BASE_PATH" \
  --model-dir "$MODEL_DIR" \
  --port "${OMLX_TEXT_PORT:-8080}" \
  --max-model-memory "${OMLX_TEXT_MAX_MODEL_MEMORY:-10GB}" \
  --max-process-memory "${OMLX_TEXT_MAX_PROCESS_MEMORY:-12GB}" \
  --max-num-seqs "${OMLX_TEXT_MAX_NUM_SEQS:-1}" \
  --completion-batch-size "${OMLX_TEXT_COMPLETION_BATCH_SIZE:-1}"
""",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


def _download_hf_command(python: str, repo_id: str, local_dir: str) -> list[str]:
    code = (
        "from huggingface_hub import snapshot_download; "
        f"snapshot_download(repo_id={repo_id!r}, local_dir={local_dir!r}, local_dir_use_symlinks=False)"
    )
    return [python, "-c", code]


def _ensure_symlink(target: Path, link: Path) -> None:
    link.parent.mkdir(parents=True, exist_ok=True)
    target = target.expanduser()
    if link.is_symlink() or link.exists():
        if link.resolve() == target.resolve():
            return
        if link.is_dir() and not link.is_symlink():
            return
        link.unlink()
    link.symlink_to(target)


def _run_or_plan(
    steps: list[BootstrapStep],
    command: list[str],
    *,
    key: str,
    title: str,
    execute: bool,
    timeout: int = 900,
    required: bool = True,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    next_action: str = "",
) -> None:
    if not execute:
        steps.append(_step_skipped(key, title, "dry-run; pass --yes to execute", required=required, command=command, next_action=next_action or "Run the installer with --yes."))
        return
    result = _run(command, cwd=cwd, timeout=timeout, required=required, env=env)
    result.key = key
    result.title = title
    result.next_action = next_action if result.status != "pass" else ""
    steps.append(result)


def _utility_install_command(dep: AuxiliaryDependency) -> list[str]:
    system = platform.system()
    if dep.key == "mariadb":
        if system == "Windows":
            return ["winget", "install", "--id", "MariaDB.Server", "-e", "--accept-package-agreements", "--accept-source-agreements"]
        if system == "Darwin":
            brew = _which("brew")
            return [brew, "install", "mariadb"] if brew else ["echo", "請先安裝 Homebrew，再安裝 MariaDB"]
        if _which("apt-get"):
            return ["sudo", "apt-get", "install", "-y", "mariadb-server"]
        if _which("dnf"):
            return ["sudo", "dnf", "install", "-y", "mariadb-server"]
        if _which("yum"):
            return ["sudo", "yum", "install", "-y", "mariadb-server"]
        return ["echo", "請安裝 MariaDB Server 或 MySQL Server"]
    if dep.key == "tailscale":
        if system == "Windows":
            return ["winget", "install", "--id", "Tailscale.Tailscale", "-e", "--accept-package-agreements", "--accept-source-agreements"]
        if system == "Darwin":
            brew = _which("brew")
            return [brew, "install", "--cask", "tailscale"] if brew else ["echo", "請先安裝 Homebrew，再安裝 Tailscale"]
        if _which("curl") and _which("sh"):
            return ["sh", "-c", "curl -fsSL https://tailscale.com/install.sh | sh"]
        return ["echo", "請安裝 Tailscale：https://tailscale.com/download"]
    return ["echo", f"請安裝 {dep.title}"]


def _utility_start_command(dep: AuxiliaryDependency) -> list[str]:
    system = platform.system()
    if dep.key == "mariadb":
        if system == "Darwin" and _which("brew"):
            return [_which("brew"), "services", "start", "mariadb"]
        if system == "Linux" and _which("systemctl"):
            return ["sudo", "systemctl", "enable", "--now", "mariadb"]
        if system == "Windows":
            return ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", "Start-Service MariaDB -ErrorAction SilentlyContinue"]
    return []


def _build_utility_steps(*, execute: bool, allow_system_install: bool) -> list[BootstrapStep]:
    steps: list[BootstrapStep] = []
    for dep in UTILITY_DEPENDENCIES:
        found = _which_any(dep.executables)
        if found:
            steps.append(BootstrapStep(f"utility:{dep.key}", dep.title, "pass", f"found {found}", required=dep.required))
        else:
            install_cmd = _utility_install_command(dep)
            can_execute = allow_system_install and execute and install_cmd and install_cmd[0] != "echo"
            if can_execute:
                result = _run(install_cmd, timeout=2400, required=dep.required)
                result.key = f"utility:{dep.key}"
                result.title = dep.title
                if result.status != "pass":
                    result.next_action = dep.install_hint
                steps.append(result)
            else:
                steps.append(
                    _step_warn(
                        f"utility:{dep.key}",
                        dep.title,
                        f"{dep.title} 尚未安裝。",
                        command=install_cmd,
                        next_action="使用 --yes --allow-system-install 讓安裝精靈協助安裝；或先手動安裝後重跑。",
                    )
                )

        if dep.key == "mariadb" and (found or (steps and steps[-1].status == "pass")):
            start_cmd = _utility_start_command(dep)
            if start_cmd:
                _run_or_plan(
                    steps,
                    start_cmd,
                    key="utility:mariadb_start",
                    title="啟動 MariaDB 服務",
                    execute=execute,
                    timeout=300,
                    required=False,
                    next_action="若未啟動，請手動啟動 MariaDB 服務後再執行資料庫初始化。",
                )
        if dep.key == "tailscale" and (found or (steps and steps[-1].status == "pass")):
            steps.append(
                BootstrapStep(
                    "utility:tailscale_login",
                    "登入 Tailscale",
                    "warn",
                    "Tailscale 安裝後仍需使用者登入授權。",
                    required=False,
                    command=["tailscale", "up"],
                    next_action="啟動 Tailscale 並登入 tailnet；Linux 可執行 sudo tailscale up。",
                )
            )
    return steps


def build_steps(
    plan: RuntimePlan,
    *,
    profile: HardwareProfile,
    repo_dir: Path,
    execute: bool,
    allow_system_install: bool,
    download_models: bool,
    install_services: bool,
    install_utilities: bool = True,
) -> list[BootstrapStep]:
    steps: list[BootstrapStep] = [
        BootstrapStep("hardware", "Detect hardware", "pass", json.dumps(asdict(profile), ensure_ascii=False), required=True),
        BootstrapStep("runtime_plan", "Select runtime and model", "pass", json.dumps(asdict(plan), ensure_ascii=False), required=True),
    ]
    python = venv_python(repo_dir)
    if install_utilities:
        steps.extend(_build_utility_steps(execute=execute, allow_system_install=allow_system_install))
    else:
        steps.append(_step_skipped("utility", "安裝外部輔助套件", "skipped by --skip-utilities", next_action="MariaDB 與 Tailscale 需另行安裝。"))

    if plan.provider == "ollama":
        ollama = _which("ollama")
        if not ollama:
            if platform.system() == "Windows":
                install_cmd = ["winget", "install", "--id", "Ollama.Ollama", "-e", "--accept-package-agreements", "--accept-source-agreements"]
            elif platform.system() == "Darwin" and _which("brew"):
                install_cmd = [_which("brew"), "install", "ollama"]
            else:
                install_cmd = ["echo", "Install Ollama from https://ollama.com/download"]
            if allow_system_install and execute and install_cmd[0] != "echo":
                _run_or_plan(steps, install_cmd, key="install_ollama", title="Install Ollama", execute=True, timeout=1800, required=True)
            else:
                steps.append(_step_warn("install_ollama", "Install Ollama", "Ollama is not installed.", command=install_cmd, next_action="Allow the installer to install Ollama, or install it from https://ollama.com/download."))
        else:
            steps.append(BootstrapStep("install_ollama", "Install Ollama", "pass", f"found {ollama}", required=True))

        if download_models:
            for model in plan.downloads:
                _run_or_plan(
                    steps,
                    ["ollama", "pull", model.source],
                    key=f"ollama_pull:{model.role}",
                    title=f"Download Ollama model for {model.role}",
                    execute=execute,
                    timeout=7200,
                    required=model.required,
                )
        else:
            steps.append(_step_skipped("ollama_pull", "Download Ollama models", "model download disabled", next_action="Rerun with --download-models."))
        return steps

    # oMLX / MLX path
    if not profile.is_apple_silicon:
        steps.append(_step_warn("omlx_arch", "Check oMLX architecture", "oMLX is intended for Apple Silicon; forcing it may fail."))
    omlx_bin = _which("omlx")
    brew = _which("brew") or "/opt/homebrew/bin/brew"
    if not omlx_bin:
        install_cmd = [brew, "install", "omlx"] if _which("brew") else ["echo", "Install Homebrew and oMLX first"]
        if allow_system_install and execute and install_cmd[0] != "echo":
            _run_or_plan(steps, install_cmd, key="install_omlx", title="Install oMLX runtime", execute=True, timeout=1800, required=True)
        else:
            steps.append(_step_warn("install_omlx", "Install oMLX runtime", "oMLX binary is not installed.", command=install_cmd, next_action="Allow system install, or install Homebrew/oMLX manually before starting MAGI."))
    else:
        steps.append(BootstrapStep("install_omlx", "Install oMLX runtime", "pass", f"found {omlx_bin}", required=True))

    if download_models:
        _run_or_plan(
            steps,
            [python, "-m", "pip", "install", "-U", "huggingface_hub"],
            key="install_huggingface_hub",
            title="Install Hugging Face downloader",
            execute=execute,
            timeout=900,
            required=True,
            cwd=repo_dir,
        )
        for model in plan.downloads:
            _run_or_plan(
                steps,
                _download_hf_command(python, model.source, model.local_dir),
                key=f"hf_download:{model.role}",
                title=f"Download oMLX model for {model.role}",
                execute=execute,
                timeout=14400,
                required=model.required,
                cwd=repo_dir,
            )
            if execute:
                try:
                    if model.role == "primary":
                        _ensure_symlink(Path(model.local_dir), Path.home() / ".omlx" / "models-text" / model.model)
                    elif model.role == "embedding":
                        _ensure_symlink(Path(model.local_dir), Path.home() / ".omlx" / "models-embed" / model.model)
                except Exception as exc:
                    steps.append(_step_warn(f"symlink:{model.role}", f"Register {model.role} model", str(exc), next_action="Create the model symlink manually or rerun the installer."))
                else:
                    steps.append(BootstrapStep(f"symlink:{model.role}", f"Register {model.role} model", "pass", model.local_dir, required=False))
            else:
                steps.append(_step_skipped(f"symlink:{model.role}", f"Register {model.role} model", "dry-run; model symlink not changed", required=False))
    else:
        steps.append(_step_skipped("hf_download", "Download oMLX models", "model download disabled", next_action="Rerun with --download-models."))

    runtime_root = Path.home() / "Library" / "Application Support" / "MAGI"
    start_script = _create_omlx_start_script(runtime_root) if execute else runtime_root / "bin" / "omlx-magi-start-text"
    if install_services:
        _run_or_plan(
            steps,
            [python, str(repo_dir / "scripts" / "install_omlx_text.py")],
            key="install_omlx_text_service",
            title="Install oMLX text LaunchAgent",
            execute=execute,
            timeout=240,
            required=False,
            cwd=repo_dir,
            env={"OMLX_TEXT_START_SCRIPT": str(start_script)},
        )
    else:
        steps.append(_step_skipped("install_omlx_text_service", "Install oMLX text LaunchAgent", "service install disabled", next_action="Rerun with --install-services after model download."))

    return steps


def _summary(steps: list[BootstrapStep]) -> dict[str, int]:
    return {
        "pass": sum(1 for step in steps if step.status == "pass"),
        "warn": sum(1 for step in steps if step.status == "warn"),
        "fail": sum(1 for step in steps if step.status == "fail"),
        "skipped": sum(1 for step in steps if step.status == "skipped"),
        "total": len(steps),
    }


def _write_report(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _env_is_placeholder(value: str) -> bool:
    text = str(value or "").strip().strip('"').strip("'")
    if not text:
        return True
    lowered = text.lower()
    return (
        text.startswith("<")
        or text.endswith(">")
        or "replace_with" in lowered
        or "your-" in lowered
        or "remote-db-ip" in lowered
        or text in {"changeme", "replace-me", "random-hex-string"}
    )


def _env_quote(value: str) -> str:
    text = str(value)
    if not text:
        return ""
    if re.search(r"\s|#|'|\"", text):
        return json.dumps(text, ensure_ascii=False)
    return text


def _env_line_value(line: str) -> tuple[str, str] | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or "=" not in stripped:
        return None
    key, value = stripped.split("=", 1)
    key = key.strip()
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
        return None
    return key, value.strip()


def _seed_env_from_example(repo_dir: Path, env_path: Path) -> bool:
    example = repo_dir / ".env.example"
    if not example.is_file():
        return False
    text = example.read_text(encoding="utf-8")
    replacements = {
        "FLASK_SECRET_KEY=<random-hex-string>": f"FLASK_SECRET_KEY={secrets.token_hex(32)}",
        "MAGI_API_KEY=<random-hex-string>": f"MAGI_API_KEY={secrets.token_hex(32)}",
        "MAGI_ROOT_DIR=/path/to/MAGI_v2": f"MAGI_ROOT_DIR={repo_dir}",
        "MAGI_SKILL_PYTHON=/path/to/MAGI_v2/.venv/bin/python": f"MAGI_SKILL_PYTHON={venv_python(repo_dir)}",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    env_path.write_text(text, encoding="utf-8")
    return True


def _default_binary_candidate(name: str) -> str:
    system = platform.system()
    if system == "Darwin":
        for prefix in ("/opt/homebrew/bin", "/usr/local/bin"):
            candidate = Path(prefix) / name
            if candidate.exists():
                return str(candidate)
    if system == "Windows":
        candidates = {
            "mariadb": [
                r"C:\Program Files\MariaDB\bin\mariadb.exe",
                r"C:\Program Files\MariaDB\bin\mysql.exe",
            ],
            "mysql": [
                r"C:\Program Files\MariaDB\bin\mysql.exe",
                r"C:\Program Files\MySQL\MySQL Server 8.0\bin\mysql.exe",
            ],
            "tailscale": [r"C:\Program Files\Tailscale\tailscale.exe"],
            "ollama": [r"C:\Users\Public\AppData\Local\Programs\Ollama\ollama.exe"],
        }
        for candidate in candidates.get(name, []):
            if Path(candidate).exists():
                return candidate
    return ""


def build_env_updates(plan: RuntimePlan, *, profile: HardwareProfile, repo_dir: Path) -> dict[str, str]:
    python = venv_python(repo_dir)
    local_chat_models = [plan.primary_model]
    if plan.heavy_model:
        local_chat_models.append(plan.heavy_model)
    updates: dict[str, str] = {
        "MAGI_ROOT_DIR": str(repo_dir),
        "MAGI_SKILL_PYTHON": python,
        "MAGI_INSTALLER_PROFILE_OS": profile.os_name,
        "MAGI_INSTALLER_PROFILE_MACHINE": profile.machine,
        "MAGI_INSTALLER_UPDATED_AT": time.strftime("%Y-%m-%d %H:%M:%S"),
        "DB_HOST": "127.0.0.1",
        "MAGI_LOCAL_DB_HOST": "127.0.0.1",
        "MAGI_LOCAL_DB_PORT": "3306",
        "OSC_DB_HOST": "127.0.0.1",
        "OSC_DB_PORT": "3306",
        "OSC_DB_NAME": "law_firm_data",
        "INFERENCE_LOCAL_CHAT_MODELS": ",".join(local_chat_models),
        "MAGI_DEFAULT_MODEL": plan.primary_model,
        "MAGI_TEXT_PRIMARY_MODEL": plan.primary_model,
        **plan.env,
    }
    if plan.heavy_model:
        updates["MAGI_TEXT_HEAVY_MODEL"] = plan.heavy_model

    mariadb_bin = _which_any(("mariadb", "mysql")) or _default_binary_candidate("mariadb") or _default_binary_candidate("mysql")
    if mariadb_bin:
        updates["MAGI_MARIADB_BIN"] = mariadb_bin

    tailscale_bin = _which_any(("tailscale",)) or _default_binary_candidate("tailscale")
    if tailscale_bin:
        updates["MAGI_TAILSCALE_BIN"] = tailscale_bin
        updates["MAGI_TAILSCALE_ENABLED"] = "1"
    else:
        updates["MAGI_TAILSCALE_ENABLED"] = "0"

    if plan.provider == "omlx":
        start_script = Path.home() / "Library" / "Application Support" / "MAGI" / "bin" / "omlx-magi-start-text"
        updates.update(
            {
                "MAGI_OMLX_ENABLED": "1",
                "MAGI_OMLX_HOST": "127.0.0.1",
                "MAGI_OMLX_PORT": "8080",
                "OMLX_BASE_URL": "http://127.0.0.1:8080/v1",
                "OMLX_API_KEY": "omlx-local",
                "OMLX_MODEL": plan.primary_model,
                "OMLX_TEXT_MODEL_DIR": str(Path.home() / ".omlx" / "models-text"),
                "OMLX_TEXT_PORT": "8080",
                "OMLX_TEXT_START_SCRIPT": str(start_script),
                "INFERENCE_LOCAL_OLLAMA_BASE": "http://127.0.0.1:8080",
            }
        )
    else:
        updates.update(
            {
                "OLLAMA_BASE_URL": "http://127.0.0.1:11434",
                "OLLAMA_MODEL": plan.primary_model,
                "OLLAMA_PORT": "11434",
                "MAGI_MAIN_MODEL": plan.primary_model,
                "INFERENCE_LOCAL_OLLAMA_BASE": "http://127.0.0.1:11434",
            }
        )
    return {key: value for key, value in updates.items() if value is not None}


def write_env_updates(
    repo_dir: Path,
    updates: dict[str, str],
    *,
    execute: bool,
    no_write_env: bool = False,
) -> dict[str, Any]:
    env_path = repo_dir / ".env"
    result: dict[str, Any] = {
        "path": str(env_path),
        "planned_keys": sorted(updates),
        "updated_keys": [],
        "preserved_keys": [],
        "created": False,
        "written": False,
        "skipped": False,
    }
    if no_write_env:
        result["skipped"] = True
        result["reason"] = "disabled_by_option"
        return result
    if not execute:
        result["skipped"] = True
        result["reason"] = "dry_run"
        return result

    repo_dir.mkdir(parents=True, exist_ok=True)
    if not env_path.exists():
        result["created"] = _seed_env_from_example(repo_dir, env_path)
        if not result["created"]:
            env_path.write_text("", encoding="utf-8")
            result["created"] = True

    lines = env_path.read_text(encoding="utf-8").splitlines()
    seen: set[str] = set()
    next_lines: list[str] = []
    updated_keys: list[str] = []
    preserved_keys: list[str] = []

    for line in lines:
        parsed = _env_line_value(line)
        if parsed is None:
            next_lines.append(line)
            continue
        key, existing = parsed
        if key not in updates:
            next_lines.append(line)
            continue
        seen.add(key)
        if key in ENV_OVERWRITE_KEYS or _env_is_placeholder(existing):
            next_lines.append(f"{key}={_env_quote(updates[key])}")
            updated_keys.append(key)
        else:
            next_lines.append(line)
            preserved_keys.append(key)

    missing = [key for key in updates if key not in seen]
    if missing:
        if next_lines and next_lines[-1].strip():
            next_lines.append("")
        next_lines.append(ENV_AUTOGEN_HEADER)
        for key in sorted(missing):
            next_lines.append(f"{key}={_env_quote(updates[key])}")
            updated_keys.append(key)

    env_path.write_text("\n".join(next_lines).rstrip() + "\n", encoding="utf-8")
    result["updated_keys"] = sorted(set(updated_keys))
    result["preserved_keys"] = sorted(set(preserved_keys))
    result["written"] = True
    return result


def _env_step_from_result(result: dict[str, Any]) -> BootstrapStep:
    if result.get("skipped"):
        reason = str(result.get("reason") or "skipped")
        return _step_skipped(
            "env_import",
            "匯入外部套件設定到 .env",
            reason,
            next_action="正式安裝請不要使用 --dry-run，或移除 --no-write-env。",
        )
    updated = result.get("updated_keys") or []
    preserved = result.get("preserved_keys") or []
    detail = f"updated {len(updated)} keys"
    if result.get("created"):
        detail += "; created .env"
    if preserved:
        detail += f"; preserved {len(preserved)} existing values"
    return BootstrapStep(
        "env_import",
        "匯入外部套件設定到 .env",
        "pass",
        detail,
        required=True,
        next_action="只需補齊資料庫密碼、OAuth/token、NAS 帳密等敏感設定；安裝精靈不會自動猜測或輸出這些值。",
    )


def build_payload(args: argparse.Namespace) -> dict[str, Any]:
    repo_dir = args.repo_dir.resolve()
    profile = detect_hardware()
    plan = select_runtime_plan(profile, force_provider=args.provider, include_heavy=args.include_heavy)
    execute = bool(args.yes and not args.dry_run)
    steps = build_steps(
        plan,
        profile=profile,
        repo_dir=repo_dir,
        execute=execute,
        allow_system_install=bool(args.allow_system_install),
        download_models=bool(args.download_models),
        install_services=bool(args.install_services),
        install_utilities=not bool(getattr(args, "skip_utilities", False)),
    )
    env_updates = build_env_updates(plan, profile=profile, repo_dir=repo_dir)
    env_result = write_env_updates(
        repo_dir,
        env_updates,
        execute=execute,
        no_write_env=bool(getattr(args, "no_write_env", False)),
    )
    steps.append(_env_step_from_result(env_result))
    summary = _summary(steps)
    ok = summary["fail"] == 0
    status = "pass" if ok and summary["warn"] == 0 else ("warn" if ok else "fail")
    return {
        "ok": ok,
        "status": status,
        "mode": "install" if args.yes and not args.dry_run else "dry-run",
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "repo_dir": str(repo_dir),
        "profile": asdict(profile),
        "plan": asdict(plan),
        "env_import": env_result,
        "summary": summary,
        "steps": [asdict(step) for step in steps],
        "next_steps": [
            "Run with --yes --allow-system-install --download-models to let MAGI install MariaDB/Tailscale, model runtime dependencies, and models.",
            "After sensitive .env values are complete, run scripts/magi_doctor.py --json and scripts/ops/commercial_readiness_live.py --strict-public.",
        ],
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Detect and install the best local MAGI model runtime.")
    parser.add_argument("--repo-dir", type=Path, default=REPO_ROOT, help="MAGI checkout or extracted release directory")
    parser.add_argument("--provider", choices=["", "omlx", "ollama"], default="", help="force a runtime instead of auto-detecting")
    parser.add_argument("--yes", action="store_true", help="execute bootstrap steps")
    parser.add_argument("--dry-run", action="store_true", help="plan only")
    parser.add_argument("--allow-system-install", action="store_true", help="allow Homebrew/winget runtime installation")
    parser.add_argument("--download-models", action="store_true", help="download selected local models")
    parser.add_argument("--skip-utilities", action="store_true", help="skip MariaDB/Tailscale helper installation checks")
    parser.add_argument("--include-heavy", action="store_true", help="also download heavy model even if RAM is below the automatic threshold")
    parser.add_argument("--install-services", action="store_true", help="install local runtime services after model download")
    parser.add_argument("--no-write-env", action="store_true", help="do not merge detected runtime and utility settings into .env")
    parser.add_argument("--json", action="store_true", help="print JSON")
    parser.add_argument("--output", "--json-out", type=Path, default=DEFAULT_REPORT, help="write JSON report here")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    payload = build_payload(args)
    _write_report(args.output, payload)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        plan = payload["plan"]
        print(f"MAGI runtime bootstrap: {payload['status'].upper()} ({payload['mode']})")
        print(f"Runtime: {plan['runtime']} | primary: {plan['primary_model']} | embed: {plan['embedding_model']}")
        if plan.get("heavy_model"):
            print(f"Heavy model: {plan['heavy_model']}")
        print(f"Report: {args.output}")
        for step in payload["steps"]:
            print(f"- {step['status'].upper():7} {step['title']}: {step['detail']}")
            if step.get("next_action"):
                print(f"          next: {step['next_action']}")
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
