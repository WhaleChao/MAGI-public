"""Portable, single-tenant self-host deployment primitives for MAGI.

This module deliberately contains no imports from the running MAGI services.
It must be usable on a clean macOS, Windows, or Linux host before MAGI's
optional dependencies have been installed.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import plistlib
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence


SCHEMA = "magi.selfhost/v1"
MARKER_SCHEMA = "magi.selfhost.active-release/v1"
SERVICE_ID = "magi-v3-selfhost"
SUPPORTED_SYSTEMS = {"Darwin", "Windows", "Linux"}
REQUIRED_FEATURES = {
    "web",
    "scheduler",
    "desktop_status",
    "case_management",
    "legal_aid",
    "court_portal",
    "google_calendar",
    "google_drive",
    "messaging",
    "accounting",
    "judgment_library",
    "knowledge",
    "documents",
    "market",
    "research",
    "local_models",
    "translation",
    "development",
    "remote_access",
}
_CODE_ROOT_ANCHORS = {"api", "casper_ecosystem", "config", "gui", "magi_v3", "scripts", "skills"}
IGNORED_RELEASE_PARTS = {
    ".git",
    ".agent",
    ".runtime",
    ".pytest_cache",
    ".venv",
    "venv",
    "__pycache__",
}
UNTRACKED_RELEASE_ALLOWLIST = {
    ".env.example",
    "README.md",
    "requirements-selfhost.txt",
    "install-magi.cmd",
    "install-magi.command",
    "install-magi.ps1",
    "config/selfhost.example.json",
    "config/selfhost.schema.json",
    "docs/SELFHOST_DEPLOYMENT.md",
    "magi_v3/fcntl_compat.py",
    "magi_v3/process_compat.py",
    "magi_v3/selfhost.py",
    "scripts/magi_selfhost.py",
    "scripts/ops/selfhost_minimal_import_check.py",
    "scripts/ops/selfhost_portability_audit.py",
}


def _release_secret_path(relative: Path) -> bool:
    name = relative.name.lower()
    if name == ".env.example":
        return False
    return (
        name == ".env"
        or name.startswith(".env.")
        or name == "magi.env"
        or name in {"credentials.json", "token.json"}
        or (name.startswith("client_secret") and name.endswith(".json"))
    )


def _release_excluded_path(relative: Path) -> bool:
    if relative.parts and relative.parts[0] in {"tests", ".github"}:
        return True
    if relative.parts and relative.parts[0] == "docs":
        return relative.as_posix() != "docs/SELFHOST_DEPLOYMENT.md"
    if relative.as_posix().startswith("resources/osc/photo/"):
        return True
    if relative.as_posix() == "skills/laf-portal-automation/references/snapshot_training.json":
        return True
    return False


class SelfHostError(RuntimeError):
    """Raised when a self-host operation cannot be completed safely."""


@dataclass(frozen=True)
class HostLayout:
    platform: str
    home: Path
    instance_root: Path
    config_dir: Path
    data_dir: Path
    runtime_dir: Path
    logs_dir: Path
    releases_dir: Path
    secrets_dir: Path
    backups_dir: Path
    launcher_path: Path
    active_marker: Path
    config_path: Path
    venv_dir: Path

    def as_dict(self) -> dict[str, str]:
        return {key: str(value) for key, value in self.__dict__.items()}


@dataclass(frozen=True)
class Check:
    key: str
    status: str
    detail: str
    action: str = ""

    def as_dict(self) -> dict[str, str]:
        return {
            "key": self.key,
            "status": self.status,
            "detail": self.detail,
            "action": self.action,
        }


@dataclass(frozen=True)
class ServicePlan:
    platform: str
    service_id: str
    artifact_path: Path | None
    artifact_bytes: bytes | None
    install: tuple[tuple[str, ...], ...]
    start: tuple[tuple[str, ...], ...]
    stop: tuple[tuple[str, ...], ...]
    uninstall: tuple[tuple[str, ...], ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "service_id": self.service_id,
            "artifact_path": str(self.artifact_path) if self.artifact_path else "",
            "install": [list(command) for command in self.install],
            "start": [list(command) for command in self.start],
            "stop": [list(command) for command in self.stop],
            "uninstall": [list(command) for command in self.uninstall],
        }


def _system_name(system: str | None = None) -> str:
    value = system or platform.system()
    if value not in SUPPORTED_SYSTEMS:
        raise SelfHostError(f"unsupported operating system: {value or 'unknown'}")
    return value


def default_layout(
    *,
    system: str | None = None,
    home: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> HostLayout:
    """Return an OS-native per-user layout without machine-specific literals."""

    system_name = _system_name(system)
    env = dict(os.environ if environ is None else environ)
    home_path = Path(home or env.get("USERPROFILE") or Path.home()).expanduser()
    explicit = str(env.get("MAGI_SELFHOST_HOME") or "").strip()
    if explicit:
        instance_root = Path(explicit).expanduser()
    elif system_name == "Darwin":
        instance_root = home_path / "Library" / "Application Support" / "MAGI" / "selfhost"
    elif system_name == "Windows":
        local_app_data = Path(env.get("LOCALAPPDATA") or (home_path / "AppData" / "Local"))
        instance_root = local_app_data / "MAGI" / "selfhost"
    else:
        instance_root = Path(env.get("XDG_DATA_HOME") or (home_path / ".local" / "share")) / "MAGI" / "selfhost"

    config_dir = instance_root / "config"
    runtime_dir = instance_root / "runtime"
    return HostLayout(
        platform=system_name,
        home=home_path,
        instance_root=instance_root,
        config_dir=config_dir,
        data_dir=instance_root / "data",
        runtime_dir=runtime_dir,
        logs_dir=instance_root / "logs",
        releases_dir=instance_root / "releases",
        secrets_dir=instance_root / "secrets",
        backups_dir=instance_root / "backups",
        launcher_path=instance_root / "bin" / "magi-selfhost-launcher.py",
        active_marker=runtime_dir / "active-release.json",
        config_path=config_dir / "selfhost.json",
        venv_dir=runtime_dir / "venv",
    )


def default_config(
    *,
    layout: HostLayout,
    instance_name: str = "MAGI",
    source_root: Path | None = None,
) -> dict[str, Any]:
    """Build a secret-free configuration suitable for first boot."""

    windows = layout.platform == "Windows"
    apple = layout.platform == "Darwin"
    case_root = layout.data_dir / "cases" / "active"
    archive_root = layout.data_dir / "cases" / "archive"
    return {
        "schema": SCHEMA,
        "instance": {
            "name": instance_name,
            "profile": "single_tenant_selfhost",
            "platform": layout.platform,
        },
        "paths": {
            **layout.as_dict(),
            "source_root": str(source_root.resolve()) if source_root else "",
            "case_root": str(case_root),
            "archive_root": str(archive_root),
            "exports_dir": str(layout.data_dir / "exports"),
            "knowledge_dir": str(layout.data_dir / "knowledge"),
            "database_path": str(layout.data_dir / "magi.sqlite3"),
        },
        "network": {
            "bind_host": "127.0.0.1",
            "public_base_url": "",
            "ports": {"main": 5002, "tools": 5003},
            "remote_access": False,
        },
        "models": {
            "local_backend": "mlx" if apple else "disabled",
            "heavy_backend": "nvidia_api",
            "embedding_backend": "mlx" if apple else "api",
            "transcription_backend": "mlx_whisper" if apple else "api",
            "translation_backend": "apple" if apple else "api",
        },
        "database": {"engine": "mysql", "dsn_env": "MAGI_DATABASE_URL"},
        "features": {
            "web": True,
            "scheduler": True,
            "desktop_status": "web" if windows else "menubar",
            "case_management": True,
            "legal_aid": False,
            "court_portal": False,
            "google_calendar": False,
            "google_drive": False,
            "messaging": False,
            "accounting": False,
            "judgment_library": False,
            "knowledge": False,
            "documents": False,
            "market": False,
            "research": False,
            "local_models": False,
            "translation": False,
            "development": False,
            "remote_access": False,
        },
        "secrets": {
            "env_file": str(layout.secrets_dir / "magi.env"),
            "required_names": [
                "FLASK_SECRET_KEY",
                "MAGI_API_KEY",
                "DB_HOST",
                "DB_PORT",
                "DB_USER",
                "DB_PASSWORD",
                "DB_NAME",
            ],
            "optional_names": [
                "NVIDIA_API_KEY",
                "GOOGLE_APPLICATION_CREDENTIALS",
                "MAGI_LAF_USERNAME",
                "MAGI_LAF_PASSWORD",
                "MAGI_JUDICIAL_EEFILE_USERNAME",
                "MAGI_JUDICIAL_EEFILE_PASSWORD",
                "DISCORD_BOT_TOKEN",
                "TELEGRAM_BOT_TOKEN",
                "OPENCLAW_TELEGRAM_BOT_TOKEN",
                "MAGI_LINE_CHANNEL_ACCESS_TOKEN",
                "MAGI_BRAIN_DB_NAME",
            ],
        },
        "service": {
            "id": SERVICE_ID,
            "auto_start": True,
            "python": str(venv_python(layout)),
            "launcher": str(layout.launcher_path),
        },
        "release": {
            "active_marker": str(layout.active_marker),
            "releases_dir": str(layout.releases_dir),
            "retain": 3,
        },
    }


def layout_from_config(config: Mapping[str, Any]) -> HostLayout:
    """Reconstruct the authoritative host layout recorded by an instance.

    Commands must not silently fall back to the current shell's HOME or
    LOCALAPPDATA after a configuration has been created.  This matters for
    portable installs, custom roots, service accounts, and restored systems.
    """

    paths = dict(config.get("paths") or {})
    instance = dict(config.get("instance") or {})
    platform_name = _system_name(str(instance.get("platform") or ""))
    instance_root_value = str(paths.get("instance_root") or "").strip()
    if not instance_root_value:
        raise SelfHostError("paths.instance_root is required to reconstruct the host layout")
    instance_root = Path(instance_root_value).expanduser()

    def configured(name: str, fallback: Path) -> Path:
        value = str(paths.get(name) or "").strip()
        return Path(value).expanduser() if value else fallback

    config_dir = configured("config_dir", instance_root / "config")
    runtime_dir = configured("runtime_dir", instance_root / "runtime")
    return HostLayout(
        platform=platform_name,
        home=configured("home", Path.home()),
        instance_root=instance_root,
        config_dir=config_dir,
        data_dir=configured("data_dir", instance_root / "data"),
        runtime_dir=runtime_dir,
        logs_dir=configured("logs_dir", instance_root / "logs"),
        releases_dir=configured("releases_dir", instance_root / "releases"),
        secrets_dir=configured("secrets_dir", instance_root / "secrets"),
        backups_dir=configured("backups_dir", instance_root / "backups"),
        launcher_path=configured("launcher_path", instance_root / "bin" / "magi-selfhost-launcher.py"),
        active_marker=configured("active_marker", runtime_dir / "active-release.json"),
        config_path=configured("config_path", config_dir / "selfhost.json"),
        venv_dir=configured("venv_dir", runtime_dir / "venv"),
    )


def venv_python(layout: HostLayout) -> Path:
    if layout.platform == "Windows":
        return layout.venv_dir / "Scripts" / "python.exe"
    return layout.venv_dir / "bin" / "python"


def required_directories(config: Mapping[str, Any]) -> tuple[Path, ...]:
    paths = dict(config.get("paths") or {})
    names = (
        "instance_root",
        "config_dir",
        "data_dir",
        "runtime_dir",
        "logs_dir",
        "releases_dir",
        "secrets_dir",
        "backups_dir",
        "case_root",
        "archive_root",
        "exports_dir",
        "knowledge_dir",
    )
    return tuple(Path(str(paths[name])).expanduser() for name in names if str(paths.get(name) or "").strip())


def validate_config(config: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    if config.get("schema") != SCHEMA:
        errors.append(f"schema must be {SCHEMA}")
    instance = dict(config.get("instance") or {})
    if instance.get("platform") not in SUPPORTED_SYSTEMS:
        errors.append("instance.platform is invalid")
    paths = dict(config.get("paths") or {})
    for key in ("instance_root", "runtime_dir", "releases_dir", "secrets_dir", "case_root", "archive_root"):
        if not str(paths.get(key) or "").strip():
            errors.append(f"paths.{key} is required")
    network = dict(config.get("network") or {})
    ports = dict(network.get("ports") or {})
    values: list[int] = []
    port_keys = ["main", "tools"] + [key for key in ("file_review", "admin") if key in ports]
    for key in port_keys:
        try:
            value = int(ports.get(key))
        except (TypeError, ValueError):
            errors.append(f"network.ports.{key} must be an integer")
            continue
        if not 1 <= value <= 65535:
            errors.append(f"network.ports.{key} is outside 1..65535")
        values.append(value)
    if len(values) != len(set(values)):
        errors.append("network ports must be unique")
    if network.get("remote_access") and network.get("bind_host") in {"127.0.0.1", "localhost"}:
        errors.append("remote_access requires a non-loopback bind_host or a configured reverse proxy")
    secrets = dict(config.get("secrets") or {})
    if not str(secrets.get("env_file") or "").strip():
        errors.append("secrets.env_file is required")
    database = dict(config.get("database") or {})
    if database.get("engine") != "mysql":
        errors.append("database.engine=mysql is required by the current self-host web and tools services")
    features = dict(config.get("features") or {})
    for key in sorted(REQUIRED_FEATURES - set(features)):
        errors.append(f"features.{key} is required")
    for key in sorted(REQUIRED_FEATURES - {"desktop_status"}):
        if key in features and not isinstance(features[key], bool):
            errors.append(f"features.{key} must be a boolean")
    if features.get("desktop_status") not in {"menubar", "web", "disabled"}:
        errors.append("features.desktop_status must be menubar, web, or disabled")
    if instance.get("platform") == "Windows":
        models = dict(config.get("models") or {})
        if models.get("local_backend") == "mlx":
            errors.append("models.local_backend=mlx is only supported on macOS")
        if models.get("embedding_backend") == "mlx":
            errors.append("models.embedding_backend=mlx is only supported on macOS")
        if models.get("transcription_backend") == "mlx_whisper":
            errors.append("models.transcription_backend=mlx_whisper is only supported on macOS")
        if models.get("translation_backend") == "apple":
            errors.append("models.translation_backend=apple is only supported on macOS")
        if features.get("desktop_status") == "menubar":
            errors.append("features.desktop_status=menubar is only supported on macOS")
    return errors


def _atomic_write(path: Path, data: bytes, *, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", delete=False) as handle:
        tmp = Path(handle.name)
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    if mode is not None:
        try:
            tmp.chmod(mode)
        except OSError:
            pass
    os.replace(tmp, path)


def write_config(config: Mapping[str, Any], path: Path, *, overwrite: bool = False) -> None:
    errors = validate_config(config)
    if errors:
        raise SelfHostError("invalid self-host config: " + "; ".join(errors))
    if path.exists() and not overwrite:
        raise SelfHostError(f"configuration already exists: {path}")
    _atomic_write(path, (json.dumps(config, ensure_ascii=False, indent=2) + "\n").encode(), mode=0o600)


def load_config(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SelfHostError(f"configuration not found: {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise SelfHostError(f"configuration is unreadable: {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise SelfHostError("configuration root must be an object")
    errors = validate_config(payload)
    if errors:
        raise SelfHostError("invalid self-host config: " + "; ".join(errors))
    return payload


def parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def write_secret_template(config: Mapping[str, Any], *, overwrite: bool = False) -> Path:
    secrets = dict(config.get("secrets") or {})
    path = Path(str(secrets["env_file"])).expanduser()
    if path.exists() and not overwrite:
        return path
    required = [str(item) for item in secrets.get("required_names") or []]
    optional = [str(item) for item in secrets.get("optional_names") or []]
    lines = [
        "# MAGI self-host secrets. Never commit or share this file.",
        "# Generate FLASK_SECRET_KEY and MAGI_API_KEY with the init command.",
    ]
    for key in required + optional:
        lines.append(f"{key}=")
    _atomic_write(path, ("\n".join(lines) + "\n").encode(), mode=0o600)
    return path


def render_environment(config: Mapping[str, Any], *, secret_values: Mapping[str, str] | None = None) -> dict[str, str]:
    paths = dict(config.get("paths") or {})
    network = dict(config.get("network") or {})
    ports = dict(network.get("ports") or {})
    models = dict(config.get("models") or {})
    features = dict(config.get("features") or {})
    database = dict(config.get("database") or {})
    data_dir = Path(str(paths["data_dir"]))
    runtime_dir = Path(str(paths["runtime_dir"]))
    logs_dir = Path(str(paths["logs_dir"]))
    agent_dir = data_dir / "agent"
    mutable_static = data_dir / "static"
    file_review_dir = data_dir / "file-review"
    laf_state_dir = agent_dir / "laf-orchestrator"
    env = {
        "MAGI_DEPLOYMENT_MODE": "selfhost",
        "MAGI_SELFHOST_CONFIG": str(Path(str(paths["config_path"]))),
        "MAGI_ROOT_DIR": str(paths.get("source_root") or ""),
        "MAGI_SHARED_STATE_DIR": str(paths["data_dir"]),
        "MAGI_V3_SHARED_STATE_DIR": str(paths["data_dir"]),
        "MAGI_RUNTIME_DIR": str(paths["runtime_dir"]),
        "MAGI_LOG_DIR": str(paths["logs_dir"]),
        "MAGI_V3_LOG_DIR": str(paths["logs_dir"]),
        "MAGI_DAEMON_LOG_PATH": str(logs_dir / "daemon.log"),
        "MAGI_CLOUDFLARED_LOG_PATH": str(logs_dir / "cloudflared.log"),
        "MAGI_AGENT_DIR": str(agent_dir),
        "MAGI_DATA_DIR": str(data_dir),
        "MAGI_MUTABLE_STATIC_DIR": str(mutable_static),
        "MAGI_METRICS_DIR": str(runtime_dir / "metrics"),
        "MAGI_BACKGROUND_LOCK_DIR": str(runtime_dir / "locks"),
        "MAGI_ENV_FILE": str(dict(config["secrets"])["env_file"]),
        "MAGI_EXPORTS_DIR": str(paths["exports_dir"]),
        "MAGI_V3_CASE_ROOT": str(paths["case_root"]),
        "MAGI_V3_ARCHIVE_ROOT": str(paths["archive_root"]),
        "MAGI_NAS_CASE_ROOT": str(paths["case_root"]),
        "MAGI_ACTIVE_CASE_ROOT": str(paths["case_root"]),
        "MAGI_DRIVE_SYNC_ACTIVE_CASE_ROOT": str(paths["case_root"]),
        "MAGI_CLOSED_CASE_ROOT": str(paths["archive_root"]),
        "MAGI_DRIVE_SYNC_CLOSED_CASE_ROOT": str(paths["archive_root"]),
        "PAPERCLIP_CASE_ROOT": str(paths["case_root"]),
        "MAGI_TRANSCRIPT_DOWNLOAD_DIR": str(data_dir / "transcript-downloads"),
        "MAGI_FILE_REVIEW_STATE_DIR": str(file_review_dir),
        "MAGI_FILE_REVIEW_BG_JOB_DIR": str(file_review_dir / "bg-jobs"),
        "MAGI_EEFILE_DOWNLOAD_FOLDER": str(file_review_dir / "downloads"),
        "MAGI_PAYMENT_REGISTRY_PATH": str(file_review_dir / "downloads" / "payment_registry.json"),
        "MAGI_PAYMENT_PROOF_REGISTRY_PATH": str(file_review_dir / "downloads" / "payment_proof_registry.json"),
        "MAGI_FILE_REVIEW_EMAIL_MONITOR_STATE": str(mutable_static / "file_review_email_monitor_state.json"),
        "MAGI_LAF_ORCHESTRATOR_STATE_DIR": str(laf_state_dir),
        "MAGI_LAF_PROCESSED_EMAILS_PATH": str(laf_state_dir / "processed_laf_emails.json"),
        "MAGI_LAF_GMAIL_STATE_PATH": str(mutable_static / "laf_gmail_monitor_state.json"),
        "MAGI_LAF_GMAIL_MONITOR_STATE": str(mutable_static / "laf_gmail_monitor_state.json"),
        "MAGI_LAF_GMAIL_PENDING_PATH": str(runtime_dir / "laf_gmail_dispatch_pending.json"),
        "MAGI_CORTEX_SYNC_STATE_PATH": str(runtime_dir / "cortex_sync_state.json"),
        "MAGI_JUDGMENTS_JSON_PATH": str(data_dir / "knowledge" / "judgments.json"),
        "MAGI_PDF_NAMER_CASE_INDEX": str(data_dir / "knowledge" / "pdf_namer_case_index.json"),
        "MAGI_DEBT_ADDRESS_BOOK_DIR": str(data_dir / "debt" / "address-book"),
        "MAGI_BRAIN_SQLITE_PATH": str(paths["database_path"]),
        "MAGI_DATABASE_ENGINE": str(database.get("engine") or "mysql"),
        "MAGI_SERVER_HOST": str(network.get("bind_host") or "127.0.0.1"),
        "MAGI_SERVER_PORT": str(ports["main"]),
        "MAGI_HOST": str(network.get("bind_host") or "127.0.0.1"),
        "MAGI_PORT": str(ports["main"]),
        "MAGI_TOOLS_HOST": str(network.get("bind_host") or "127.0.0.1"),
        "MAGI_TOOLS_PORT": str(ports["tools"]),
        "MAGI_LOCAL_MODEL_BACKEND": str(models.get("local_backend") or "disabled"),
        "MAGI_SKIP_IMPORT_PROBES": "0" if features.get("local_models") else "1",
        "MAGI_TOOLS_HEALTH_PROBE_MODEL": "1" if features.get("local_models") else "0",
        "MAGI_HEAVY_MODEL_PROVIDER": str(models.get("heavy_backend") or "nvidia_api"),
        "MAGI_TRANSCRIPTION_BACKEND": str(models.get("transcription_backend") or "api"),
        "MAGI_TRANSLATION_BACKEND": str(models.get("translation_backend") or "api"),
        "MAGI_TRANSLATE_LOCAL_FIRST": "1" if models.get("translation_backend") == "apple" else "0",
        "MAGI_TRANSLATOR_APE": "1" if models.get("translation_backend") == "apple" else "0",
        "MAGI_MENUBAR_ENABLED": "1" if features.get("desktop_status") == "menubar" else "0",
        "MAGI_FEATURE_LEGAL_AID": "1" if features.get("legal_aid") else "0",
        "MAGI_FEATURE_COURT_PORTAL": "1" if features.get("court_portal") else "0",
        "MAGI_FEATURE_GOOGLE_CALENDAR": "1" if features.get("google_calendar") else "0",
        "MAGI_FEATURE_GOOGLE_DRIVE": "1" if features.get("google_drive") else "0",
        "MAGI_FEATURE_MESSAGING": "1" if features.get("messaging") else "0",
        "MAGI_FEATURE_ACCOUNTING": "1" if features.get("accounting") else "0",
        "MAGI_FEATURE_CASE_MANAGEMENT": "1" if features.get("case_management") else "0",
        "MAGI_FEATURE_JUDGMENT_LIBRARY": "1" if features.get("judgment_library") else "0",
        "MAGI_FEATURE_KNOWLEDGE": "1" if features.get("knowledge") else "0",
        "MAGI_FEATURE_DOCUMENTS": "1" if features.get("documents") else "0",
        "MAGI_FEATURE_MARKET": "1" if features.get("market") else "0",
        "MAGI_FEATURE_RESEARCH": "1" if features.get("research") else "0",
        "MAGI_FEATURE_LOCAL_MODELS": "1" if features.get("local_models") else "0",
        "MAGI_FEATURE_TRANSLATION": "1" if features.get("translation") else "0",
        "MAGI_FEATURE_DEVELOPMENT": "1" if features.get("development") else "0",
        "MAGI_FEATURE_REMOTE_ACCESS": "1" if features.get("remote_access") else "0",
        "MAGI_FEATURE_WEB": "1" if features.get("web") else "0",
        "MAGI_FEATURE_SCHEDULER": "1" if features.get("scheduler") else "0",
        "MAGI_INTERNAL_CRON_ENABLED": "1" if features.get("scheduler") else "0",
    }
    if ports.get("file_review"):
        env["MAGI_FILE_REVIEW_PORT"] = str(ports["file_review"])
    if ports.get("admin"):
        env["MAGI_ADMIN_PORT"] = str(ports["admin"])
    for key, value in (secret_values or {}).items():
        if value:
            env[str(key)] = str(value)
    database_aliases = {
        "OSC_DB_HOST": "DB_HOST",
        "OSC_DB_PORT": "DB_PORT",
        "OSC_DB_USER": "DB_USER",
        "OSC_DB_PASSWORD": "DB_PASSWORD",
        "OSC_DB_NAME": "DB_NAME",
    }
    for alias, source_name in database_aliases.items():
        if env.get(source_name):
            env[alias] = env[source_name]
    return env


def initialise_instance(config: Mapping[str, Any], *, create_secrets: bool = True) -> list[str]:
    created: list[str] = []
    for directory in required_directories(config):
        directory.mkdir(parents=True, exist_ok=True)
        created.append(str(directory))
    if create_secrets:
        path = write_secret_template(config)
        created.append(str(path))
    return created


def _quote_systemd(parts: Sequence[str]) -> str:
    def quote(part: str) -> str:
        return '"' + part.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return " ".join(quote(str(part)) for part in parts)


def build_service_plan(
    config: Mapping[str, Any],
    *,
    python_executable: Path,
    launcher_path: Path,
    uid: int | None = None,
) -> ServicePlan:
    """Render an injectable, testable service plan for the configured OS."""

    system_name = str(dict(config.get("instance") or {}).get("platform"))
    paths = dict(config.get("paths") or {})
    logs_dir = Path(str(paths["logs_dir"]))
    config_path = Path(str(paths["config_path"]))
    argv = (str(python_executable), str(launcher_path), "--config", str(config_path))
    if system_name == "Darwin":
        label = "com.magi.v3.selfhost"
        artifact = Path(str(paths.get("home") or Path.home())) / "Library" / "LaunchAgents" / f"{label}.plist"
        plist = {
            "Label": label,
            "ProgramArguments": list(argv),
            "RunAtLoad": True,
            "KeepAlive": {"SuccessfulExit": False},
            "ProcessType": "Background",
            "WorkingDirectory": str(paths["instance_root"]),
            "StandardOutPath": str(logs_dir / "service.stdout.log"),
            "StandardErrorPath": str(logs_dir / "service.stderr.log"),
            "EnvironmentVariables": {"MAGI_SELFHOST_CONFIG": str(config_path)},
        }
        domain = f"gui/{uid if uid is not None else getattr(os, 'getuid', lambda: 0)()}"
        return ServicePlan(
            system_name,
            label,
            artifact,
            plistlib.dumps(plist, sort_keys=True),
            (("launchctl", "bootstrap", domain, str(artifact)),),
            (("launchctl", "kickstart", "-k", f"{domain}/{label}"),),
            (("launchctl", "kill", "SIGTERM", f"{domain}/{label}"),),
            (("launchctl", "bootout", domain, str(artifact)),),
        )
    if system_name == "Windows":
        task = "MAGI-V3-SelfHost"
        command_line = subprocess.list2cmdline(argv)
        settings_script = (
            f"$s=New-ScheduledTaskSettingsSet -RestartCount 3 "
            "-RestartInterval (New-TimeSpan -Minutes 1) -StartWhenAvailable "
            "-ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries "
            "-DontStopIfGoingOnBatteries; "
            f"Set-ScheduledTask -TaskName '{task}' -Settings $s | Out-Null"
        )
        stop_script = (
            f"$t=Get-ScheduledTask -TaskName '{task}' -ErrorAction SilentlyContinue; "
            "if($null -ne $t -and $t.State -eq 'Running'){"
            f"Stop-ScheduledTask -TaskName '{task}'"
            "}"
        )
        uninstall_script = (
            f"$t=Get-ScheduledTask -TaskName '{task}' -ErrorAction SilentlyContinue; "
            "if($null -ne $t){"
            f"Unregister-ScheduledTask -TaskName '{task}' -Confirm:$false"
            "}"
        )
        return ServicePlan(
            system_name,
            task,
            None,
            None,
            (
                ("schtasks", "/create", "/tn", task, "/tr", command_line, "/sc", "onlogon", "/rl", "limited", "/f"),
                ("powershell.exe", "-NoProfile", "-NonInteractive", "-Command", settings_script),
            ),
            (("schtasks", "/run", "/tn", task),),
            (("powershell.exe", "-NoProfile", "-NonInteractive", "-Command", stop_script),),
            (("powershell.exe", "-NoProfile", "-NonInteractive", "-Command", uninstall_script),),
        )
    unit = "magi-v3-selfhost.service"
    artifact = Path.home() / ".config" / "systemd" / "user" / unit
    body = (
        "[Unit]\nDescription=MAGI V3 self-host\nAfter=network-online.target\n\n"
        "[Service]\nType=simple\n"
        f"ExecStart={_quote_systemd(argv)}\n"
        f"WorkingDirectory={paths['instance_root']}\n"
        "Restart=on-failure\nRestartSec=5\n\n"
        "[Install]\nWantedBy=default.target\n"
    ).encode()
    return ServicePlan(
        system_name,
        unit,
        artifact,
        body,
        (("systemctl", "--user", "daemon-reload"), ("systemctl", "--user", "enable", unit)),
        (("systemctl", "--user", "start", unit),),
        (("systemctl", "--user", "stop", unit),),
        (("systemctl", "--user", "disable", unit), ("systemctl", "--user", "daemon-reload")),
    )


def execute_service_plan(
    plan: ServicePlan,
    *,
    action: str,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    dry_run: bool = False,
) -> list[dict[str, Any]]:
    if action not in {"install", "start", "stop", "uninstall"}:
        raise SelfHostError(f"unsupported service action: {action}")
    if action == "install" and plan.artifact_path and plan.artifact_bytes is not None and not dry_run:
        _atomic_write(plan.artifact_path, plan.artifact_bytes, mode=0o600)
    results: list[dict[str, Any]] = []
    for command in getattr(plan, action):
        if dry_run:
            results.append({"command": list(command), "ok": True, "dry_run": True})
            continue
        proc = runner(list(command), text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=60)
        results.append({"command": list(command), "ok": proc.returncode == 0, "returncode": proc.returncode, "output": (proc.stdout or "")[-2000:]})
        if proc.returncode != 0:
            break
    if action == "uninstall" and plan.artifact_path and not dry_run and all(item["ok"] for item in results):
        plan.artifact_path.unlink(missing_ok=True)
    return results


def _iter_release_files(source: Path) -> Iterable[tuple[Path, Path]]:
    emitted: set[str] = set()
    git = shutil.which("git")
    if git and (source / ".git").exists():
        proc = subprocess.run([git, "ls-files", "-z"], cwd=source, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if proc.returncode == 0:
            for raw in proc.stdout.split(b"\0"):
                if not raw:
                    continue
                rel = Path(os.fsdecode(raw))
                if not any(part in IGNORED_RELEASE_PARTS for part in rel.parts):
                    if _release_secret_path(rel) or _release_excluded_path(rel):
                        continue
                    if (source / rel).is_symlink():
                        raise SelfHostError(f"release contains a symbolic link: {rel}")
                    emitted.add(rel.as_posix())
                    yield source / rel, rel
    for path in source.rglob("*"):
        if not path.is_file() or any(part in IGNORED_RELEASE_PARTS for part in path.relative_to(source).parts):
            continue
        rel = path.relative_to(source)
        if _release_secret_path(rel) or _release_excluded_path(rel):
            continue
        rel_key = rel.as_posix()
        if rel_key in emitted:
            continue
        if (source / ".git").exists() and rel_key not in UNTRACKED_RELEASE_ALLOWLIST:
            continue
        if path.is_symlink():
            raise SelfHostError(f"release contains a symbolic link: {rel}")
        yield path, rel


_CRON_FEATURE_MARKERS: dict[str, tuple[str, ...]] = {
    "legal_aid": ("laf_", "laf-", "legal_aid", "legal-aid", "法扶"),
    "court_portal": (
        "file_review",
        "file-review",
        "transcript",
        "court_portal",
        "court-portal",
        "閱卷",
        "筆錄",
    ),
    "google_calendar": ("gcal", "google_calendar", "google-calendar"),
    "google_drive": ("drive_case_sync", "google_drive", "google-drive"),
    "accounting": ("accounting", "billing", "bonus", "帳務", "獎金"),
    "local_models": ("job_omlx_", "omlx_switch_model", "model_live_gate"),
    "translation": ("translator_ape", "translation_quality", "heavy_translation"),
    "judgment_library": (
        "judgment",
        "judicial_api",
        "legal_crawl",
        "reprocess_insights",
        "insight_sync",
        "weekend_resummary",
    ),
    "knowledge": ("obsidian", "wiki_synthesizer", "knowledge_lint", "cortex_vector", "case_index_sync"),
    "documents": ("pdf_", "pdfnamer", "bookmark", "ocr_worker", "docling_layout"),
    "market": ("market_", "worldmonitor"),
    "research": ("research_brief",),
    "remote_access": ("tailscale", "funnel"),
    "development": (
        "nightly_regression",
        "code_auto_cycle",
        "auto-skill",
        "auto_skill",
        "nightly_autopilot",
        "distill_train",
        "operational_hardening_audit",
    ),
    "case_management": (
        "osc_index_cases",
        "osc_events_refresh",
        "osc_todo",
        "osc_overdue",
        "slow_archive_closed_cases",
        "empty_case_shell_cleanup",
        "business_readiness_snapshot",
    ),
}


def _job_required_feature(job: Mapping[str, Any]) -> str:
    searchable = " ".join(
        str(job.get(key) or "") for key in ("id", "name", "desc", "command")
    ).lower()
    for feature, markers in _CRON_FEATURE_MARKERS.items():
        if any(marker.lower() in searchable for marker in markers):
            return feature
    return ""


def _job_platform_requirement(job: Mapping[str, Any]) -> str:
    """Return the only supported platform for native-only scheduled jobs."""

    searchable = " ".join(
        str(job.get(key) or "") for key in ("id", "name", "desc", "command")
    ).lower()
    darwin_markers = (
        "job_omlx_",
        "omlx_switch_model.sh",
        "job_translator_ape_regression",
        "apple translation",
    )
    if any(marker in searchable for marker in darwin_markers):
        return "Darwin"
    return ""


def _rebase_cron_token(
    token: str,
    *,
    source: Path,
    release_root: Path,
    python_executable: Path,
    config: Mapping[str, Any],
) -> str:
    """Map one legacy checkout token to release/runtime-owned storage."""

    key = ""
    value = token
    if "=" in token and not token.startswith(("/", "\\")):
        maybe_key, maybe_value = token.split("=", 1)
        if maybe_key.replace("_", "").isalnum() and maybe_key.upper() == maybe_key:
            key, value = maybe_key, maybe_value

    source_text = str(source)
    normalized = value.replace("\\", "/")
    python_suffix = re.search(r"/(?:\.venv|venv)/(?:bin|Scripts)/python(?:3(?:\.\d+)?)?(?:\.exe)?$", normalized, re.IGNORECASE)
    placeholder_roots = {
        "__MAGI_ROOT__": release_root,
        "__MAGI_RUNTIME__": Path(str(dict(config["paths"])["runtime_dir"])),
        "__MAGI_DATA__": Path(str(dict(config["paths"])["data_dir"])),
        "__MAGI_EXPORTS__": Path(str(dict(config["paths"])["exports_dir"])),
    }
    if normalized == "__MAGI_PYTHON__":
        mapped = str(python_executable)
    elif any(normalized == marker or normalized.startswith(marker + "/") for marker in placeholder_roots):
        marker = next(
            item for item in placeholder_roots
            if normalized == item or normalized.startswith(item + "/")
        )
        relative = normalized[len(marker):].lstrip("/")
        mapped = str(placeholder_roots[marker].joinpath(*[part for part in relative.split("/") if part]))
    elif value in {
        str(source / "venv" / "bin" / "python3"),
        str(source / "venv" / "bin" / "python"),
        str(source / ".venv" / "bin" / "python3"),
        str(source / ".venv" / "bin" / "python"),
        str(source / "venv" / "Scripts" / "python.exe"),
        str(source / ".venv" / "Scripts" / "python.exe"),
    } or python_suffix:
        mapped = str(python_executable)
    elif value == source_text or value.startswith(source_text + os.sep):
        relative = value[len(source_text):].lstrip("/\\")
        parts = [part for part in relative.replace("\\", "/").split("/") if part]
        paths = dict(config.get("paths") or {})
        if parts and parts[0] == ".runtime":
            mapped = str(Path(str(paths["runtime_dir"])).joinpath(*parts[1:]))
        elif parts and parts[0] == ".agent":
            mapped = str(Path(str(paths["data_dir"])) / "agent" / Path(*parts[1:]))
        elif parts and parts[0] == "exports":
            mapped = str(Path(str(paths["exports_dir"])).joinpath(*parts[1:]))
        elif parts and parts[0] in {"_metrics", "_autopilot_runs"}:
            mapped = str(Path(str(paths["runtime_dir"])).joinpath(*parts))
        elif parts and parts[0] == "static" and len(parts) > 1:
            mapped = str(Path(str(paths["data_dir"])) / "static" / Path(*parts[1:]))
        else:
            mapped = str(release_root.joinpath(*parts))
    elif key == "JUDGMENT_CACHE_ROOT_NAS_FALLBACK" and value.startswith(("/Users/", "/Volumes/")):
        mapped = str(Path(str(dict(config["paths"])["data_dir"])) / "cache" / "judgment_collector")
    elif normalized.startswith("/") or re.match(r"^[A-Za-z]:/", normalized):
        paths = dict(config.get("paths") or {})
        target_platform = str(dict(config.get("instance") or {}).get("platform") or "")
        native_executables = {
            "Darwin": {"/bin/bash", "/usr/bin/open"},
            "Linux": {"/bin/bash", "/usr/bin/bash"},
            "Windows": set(),
        }
        if normalized in native_executables.get(target_platform, set()):
            mapped = value
        else:
            mapped = ""
            canonical_shared_destinations: tuple[tuple[str, Path], ...] = (
                (
                    "/MAGI/runtime/MAGI_v3/shared/runtime",
                    Path(str(paths["runtime_dir"])),
                ),
                (
                    "/MAGI/runtime/MAGI_v3/shared/agent",
                    Path(str(paths["data_dir"])) / "agent",
                ),
            )
            for marker, destination_root in canonical_shared_destinations:
                marker_index = normalized.rfind(marker)
                if marker_index < 0:
                    continue
                relative = normalized[marker_index + len(marker):]
                if relative and not relative.startswith("/"):
                    continue
                mapped = str(
                    destination_root.joinpath(
                        *[part for part in relative.split("/") if part]
                    )
                )
                break
            marker_destinations: tuple[tuple[str, Path], ...] = (
                ("/.runtime/", Path(str(paths["runtime_dir"]))),
                ("/.agent/", Path(str(paths["data_dir"])) / "agent"),
                ("/exports/", Path(str(paths["exports_dir"]))),
                ("/_metrics/", Path(str(paths["runtime_dir"])) / "_metrics"),
                ("/_autopilot_runs/", Path(str(paths["runtime_dir"])) / "_autopilot_runs"),
                ("/static/", Path(str(paths["data_dir"])) / "static"),
            )
        if not mapped:
            for marker, destination_root in marker_destinations:
                if marker in normalized:
                    relative = normalized.rsplit(marker, 1)[1]
                    mapped = str(destination_root.joinpath(*[part for part in relative.split("/") if part]))
                    break
        if not mapped:
            for anchor in sorted(_CODE_ROOT_ANCHORS):
                marker = f"/{anchor}/"
                if marker in normalized:
                    relative = normalized.rsplit(marker, 1)[1]
                    mapped = str(release_root / anchor / Path(*[part for part in relative.split("/") if part]))
                    break
        if not mapped and normalized.endswith("/.agent"):
            mapped = str(Path(str(paths["data_dir"])) / "agent")
        if not mapped and normalized.endswith("/.runtime"):
            mapped = str(Path(str(paths["runtime_dir"])))
        if not mapped:
            raise SelfHostError(f"cron token contains an unportable absolute path: {value}")
    else:
        mapped = value
    return f"{key}={mapped}" if key else mapped


def build_portable_cron_jobs(
    source_path: Path,
    *,
    source_root: Path,
    release_root: Path,
    python_executable: Path,
    config: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Rebuild cron definitions without checkout paths or disabled features."""

    try:
        raw_jobs = json.loads(source_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SelfHostError(f"cron definition is unreadable: {source_path}: {exc}") from exc
    if not isinstance(raw_jobs, list):
        raise SelfHostError("cron definition root must be an array")
    features = dict(config.get("features") or {})
    output: list[dict[str, Any]] = []
    disabled_by_profile = 0
    disabled_by_platform = 0
    for raw in raw_jobs:
        if not isinstance(raw, dict):
            continue
        job = {
            str(key): value
            for key, value in raw.items()
            if not str(key).startswith("last_")
            and str(key) not in {
                "command_sha256",
                "result",
                "result_evidence",
                "stdout",
                "stderr",
                "returncode",
                "timed_out",
                "duration_sec",
                "v3_pending_occurrence",
                "v3_retry",
            }
        }
        target_platform = str(dict(config.get("instance") or {}).get("platform") or "")
        required_platform = _job_platform_requirement(job)
        platform_unsupported = bool(required_platform and required_platform != target_platform)
        command = str(job.get("command") or "").strip()
        if command and platform_unsupported:
            if job.get("enabled", True):
                disabled_by_platform += 1
            job["enabled"] = False
            job["command"] = ""
            job["disabled_reason"] = (
                f"selfhost platform unsupported: requires {required_platform}"
            )
        elif command:
            try:
                tokens = shlex.split(command, posix=True)
            except ValueError as exc:
                raise SelfHostError(f"cron job {job.get('id')} is not parseable: {exc}") from exc
            if len(tokens) >= 3 and tokens[0] == "cd" and tokens[2] == "&&":
                tokens = tokens[3:]
            try:
                mapped = [
                    _rebase_cron_token(
                        token,
                        source=source_root,
                        release_root=release_root,
                        python_executable=python_executable,
                        config=config,
                    )
                    for token in tokens
                ]
                job["command"] = shlex.join(mapped)
            except SelfHostError:
                # Retired, manually enabled jobs may legitimately point at an
                # operator's old archive.  They must not block a clean install,
                # but the private command must not leak into the new instance.
                if job.get("enabled", True):
                    raise
                job["command"] = ""
                job["disabled_reason"] = "selfhost omitted retired machine-specific command"
        required_feature = _job_required_feature(job)
        if required_feature and not bool(features.get(required_feature)):
            if job.get("enabled", True):
                disabled_by_profile += 1
            job["enabled"] = False
            job["disabled_reason"] = f"selfhost feature disabled: {required_feature}"
        output.append(job)
    return output, {
        "total": len(output),
        "disabled_by_profile": disabled_by_profile,
        "disabled_by_platform": disabled_by_platform,
    }


def tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted((p for p in root.rglob("*") if p.is_file()), key=lambda p: p.as_posix()):
        rel = path.relative_to(root).as_posix().encode()
        digest.update(len(rel).to_bytes(4, "big"))
        digest.update(rel)
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()


def _release_payload_digest(root: Path) -> str:
    """Hash the staged payload using the same scope as :func:`stage_release`.

    ``selfhost-release.json`` is written after the payload digest is calculated,
    so it must not recursively attest itself.
    """

    digest = hashlib.sha256()
    for path in sorted(
        (p for p in root.rglob("*") if p.is_file() and p.name != "selfhost-release.json"),
        key=lambda p: p.as_posix(),
    ):
        rel = path.relative_to(root).as_posix().encode()
        digest.update(len(rel).to_bytes(4, "big"))
        digest.update(rel)
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()


def stage_release(source: Path, config: Mapping[str, Any], *, release_id: str) -> dict[str, Any]:
    if not release_id or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-" for char in release_id):
        raise SelfHostError("release_id contains unsupported characters")
    source = source.resolve()
    if not (source / "magi_v3").is_dir():
        raise SelfHostError(f"source does not look like MAGI: {source}")
    releases_dir = Path(str(dict(config["release"])["releases_dir"])).expanduser()
    destination = releases_dir / release_id
    if destination.exists():
        raise SelfHostError(f"release already exists: {destination}")
    staging = releases_dir / f".{release_id}.staging-{os.getpid()}"
    staging.mkdir(parents=True, exist_ok=False)
    count = 0
    try:
        for src, rel in _iter_release_files(source):
            if rel.as_posix() == "cron_jobs.json":
                continue
            target = staging / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, target)
            count += 1
        cron_stats = {"total": 0, "disabled_by_profile": 0}
        cron_source = source / "cron_jobs.json"
        if cron_source.is_file():
            jobs, cron_stats = build_portable_cron_jobs(
                cron_source,
                source_root=source,
                release_root=destination,
                python_executable=Path(str(dict(config["service"])["python"])),
                config=config,
            )
            _atomic_write(
                staging / "cron_jobs.json",
                (json.dumps(jobs, ensure_ascii=False, indent=2) + "\n").encode(),
                mode=0o444,
            )
            count += 1
        required_release_files = (
            staging / "daemon.py",
            staging / "magi_v3" / "selfhost.py",
            staging / "requirements-selfhost.txt",
        )
        missing_release_files = [str(path.relative_to(staging)) for path in required_release_files if not path.is_file()]
        if missing_release_files:
            raise SelfHostError("release is missing required files: " + ", ".join(missing_release_files))
        digest = tree_digest(staging)
        manifest = {
            "schema": "magi.selfhost.release/v1",
            "release_id": release_id,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "source": "portable-source",
            "file_count": count,
            "tree_sha256": digest,
            "cron": cron_stats,
        }
        _atomic_write(staging / "selfhost-release.json", (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode(), mode=0o444)
        os.replace(staging, destination)
        manifest["root"] = str(destination)
        return manifest
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def build_distribution_archive(source: Path, output: Path) -> dict[str, Any]:
    """Create a secret-free universal source archive for macOS and Windows."""

    source = source.expanduser().resolve()
    output = output.expanduser().resolve()
    if output.suffix.lower() != ".zip":
        raise SelfHostError("distribution output must use the .zip extension")
    if output.exists():
        raise SelfHostError(f"distribution already exists: {output}")
    files = sorted(_iter_release_files(source), key=lambda item: item[1].as_posix())
    distribution_cron: bytes | None = None
    cron_source = source / "cron_jobs.json"
    if cron_source.is_file():
        placeholder_features = {
            key: True for key in REQUIRED_FEATURES if key != "desktop_status"
        }
        placeholder_features["desktop_status"] = "menubar"
        placeholder_config = {
            "instance": {"platform": "Darwin"},
            "features": placeholder_features,
            "paths": {
                "runtime_dir": "__MAGI_RUNTIME__",
                "data_dir": "__MAGI_DATA__",
                "exports_dir": "__MAGI_EXPORTS__",
            },
        }
        jobs, _stats = build_portable_cron_jobs(
            cron_source,
            source_root=source,
            release_root=Path("__MAGI_ROOT__"),
            python_executable=Path("__MAGI_PYTHON__"),
            config=placeholder_config,
        )
        distribution_cron = (json.dumps(jobs, ensure_ascii=False, indent=2) + "\n").encode()
    digest = hashlib.sha256()
    for path, relative in files:
        rel_bytes = relative.as_posix().encode("utf-8")
        digest.update(len(rel_bytes).to_bytes(4, "big"))
        digest.update(rel_bytes)
        if relative.as_posix() == "cron_jobs.json" and distribution_cron is not None:
            digest.update(distribution_cron)
        else:
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
    manifest = {
        "schema": "magi.selfhost.distribution/v1",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "platforms": ["macOS", "Windows"],
        "file_count": len(files),
        "content_sha256": digest.hexdigest(),
        "contains_secrets": False,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.{os.getpid()}.tmp")
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for path, relative in files:
                if relative.as_posix() == "cron_jobs.json" and distribution_cron is not None:
                    archive.writestr(relative.as_posix(), distribution_cron)
                else:
                    archive.write(path, arcname=relative.as_posix())
            archive.writestr(
                "MAGI-DISTRIBUTION.json",
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            )
        os.replace(temporary, output)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return {**manifest, "output": str(output), "archive_sha256": _file_sha256(output)}


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def active_release(config: Mapping[str, Any]) -> dict[str, Any] | None:
    marker = Path(str(dict(config["release"])["active_marker"])).expanduser()
    if not marker.exists():
        return None
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SelfHostError(f"active release marker is invalid: {exc}") from exc
    if payload.get("schema") != MARKER_SCHEMA:
        raise SelfHostError("active release marker has an unsupported schema")
    return payload


def certify_active_release(config: Mapping[str, Any]) -> dict[str, Any]:
    """Read-only attestation for the release selected by the active marker.

    The marker is mutable user state, so merely checking that its target is a
    directory is not sufficient for a self-host health check.  Keep this
    deliberately small and local: bind the marker to the configured releases
    directory, then re-hash the staged payload against its immutable manifest.
    """

    try:
        marker = active_release(config)
    except SelfHostError as exc:
        return {"ok": False, "detail": str(exc), "release_id": ""}
    if not marker:
        return {"ok": False, "detail": "no active release marker", "release_id": ""}

    release_id = str(marker.get("release_id") or "")
    release = dict(config.get("release") or {})
    releases_dir = Path(str(release.get("releases_dir") or "")).expanduser()
    if not release_id or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-" for char in release_id):
        return {"ok": False, "detail": "active release id is invalid", "release_id": release_id}
    expected_root = (releases_dir / release_id).resolve()
    declared_root = Path(str(marker.get("release_root") or "")).expanduser()
    try:
        declared_root = declared_root.resolve(strict=True)
    except OSError:
        return {"ok": False, "detail": "active release root is unavailable", "release_id": release_id}
    if declared_root != expected_root or not declared_root.is_dir():
        return {"ok": False, "detail": "active release root is not bound to configured releases", "release_id": release_id}

    manifest_path = declared_root / "selfhost-release.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"ok": False, "detail": f"release manifest is invalid: {exc}", "release_id": release_id}
    if not isinstance(manifest, dict) or manifest.get("schema") != "magi.selfhost.release/v1":
        return {"ok": False, "detail": "release manifest schema is invalid", "release_id": release_id}
    if manifest.get("release_id") != release_id:
        return {"ok": False, "detail": "release manifest id does not match active marker", "release_id": release_id}
    expected_digest = str(manifest.get("tree_sha256") or "")
    if not re.fullmatch(r"[0-9a-f]{64}", expected_digest):
        return {"ok": False, "detail": "release manifest digest is invalid", "release_id": release_id}
    actual_digest = _release_payload_digest(declared_root)
    if actual_digest != expected_digest:
        return {"ok": False, "detail": "release payload digest mismatch", "release_id": release_id}
    return {"ok": True, "detail": "active release marker and payload are hash-bound", "release_id": release_id}


def activate_release(config: Mapping[str, Any], release_id: str) -> dict[str, Any]:
    release = dict(config["release"])
    root = Path(str(release["releases_dir"])).expanduser() / release_id
    manifest_path = root / "selfhost-release.json"
    if not manifest_path.exists():
        raise SelfHostError(f"release is incomplete: {root}")
    previous = active_release(config)
    payload = {
        "schema": MARKER_SCHEMA,
        "release_id": release_id,
        "release_root": str(root),
        "activated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "previous_release_id": str((previous or {}).get("release_id") or ""),
    }
    marker = Path(str(release["active_marker"])).expanduser()
    _atomic_write(marker, (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode(), mode=0o600)
    return payload


def rollback_release(config: Mapping[str, Any]) -> dict[str, Any]:
    current = active_release(config)
    if not current:
        raise SelfHostError("no active release")
    previous = str(current.get("previous_release_id") or "")
    if not previous:
        raise SelfHostError("no previous release is recorded")
    return activate_release(config, previous)


LAUNCHER_SOURCE = '''#!/usr/bin/env python3
from __future__ import annotations
import argparse, json, os, runpy, sys
from pathlib import Path

def parse_env(path: Path):
    values = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    return values

p = argparse.ArgumentParser()
p.add_argument("--config", required=True)
p.add_argument("--check", action="store_true")
a = p.parse_args()
config_path = Path(a.config).expanduser()
config = json.loads(config_path.read_text(encoding="utf-8"))
marker_path = Path(config["release"]["active_marker"]).expanduser()
marker = json.loads(marker_path.read_text(encoding="utf-8"))
root = Path(marker["release_root"]).resolve()
if not (root / "daemon.py").is_file():
    raise SystemExit("active MAGI release is incomplete")
sys.path.insert(0, str(root))
from magi_v3.selfhost import render_environment
environment = render_environment(
    config,
    secret_values=parse_env(Path(config["secrets"]["env_file"])),
)
environment["MAGI_ROOT_DIR"] = str(root)
environment["MAGI_ROOT"] = str(root)
environment["MAGI_ORCH_DIR"] = str(root / "casper_ecosystem" / "law_firm_orchestrators")
environment["MAGI_CODE_DIR"] = environment["MAGI_ORCH_DIR"]
environment["MAGI_V3_RELEASE_ID"] = str(marker["release_id"])
environment["MAGI_V3_PYTHON_RUNTIME"] = str(Path(sys.executable).resolve())
environment["PYTHONDONTWRITEBYTECODE"] = "1"
environment["PYTHONNOUSERSITE"] = "1"
environment["PYTHONPATH"] = str(root) + (
    os.pathsep + os.environ["PYTHONPATH"] if os.environ.get("PYTHONPATH") else ""
)
if (root / "cron_jobs.json").is_file():
    environment["MAGI_CRON_JOBS_FILE"] = str(root / "cron_jobs.json")
    environment["MAGI_CRON_DEFINITIONS_IMMUTABLE"] = "1"
    environment["MAGI_USE_RUNTIME_DIR"] = "1"
for key, value in environment.items():
    if value:
        os.environ[key] = value
if a.check:
    print(json.dumps({"ok": True, "release_id": marker["release_id"], "root": str(root)}, ensure_ascii=False))
    raise SystemExit(0)
os.chdir(root)
runpy.run_path(str(root / "daemon.py"), run_name="__main__")
'''


def write_launcher(config: Mapping[str, Any], *, overwrite: bool = True) -> Path:
    path = Path(str(dict(config["service"])["launcher"])).expanduser()
    if path.exists() and not overwrite:
        raise SelfHostError(f"launcher already exists: {path}")
    _atomic_write(path, LAUNCHER_SOURCE.encode(), mode=0o700)
    return path


def _probe(url: str, *, timeout: float = 2.0) -> tuple[bool, str]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return 200 <= response.status < 400, f"HTTP {response.status}"
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return False, str(exc)


_MYSQL_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_]{1,64}$")
_MYSQL_REQUIRED_VALUES = ("DB_HOST", "DB_PORT", "DB_USER", "DB_PASSWORD", "DB_NAME")


def _mysql_parameters(values: Mapping[str, str]) -> dict[str, Any]:
    missing = [name for name in _MYSQL_REQUIRED_VALUES if not str(values.get(name) or "").strip()]
    if missing:
        raise SelfHostError("missing database settings: " + ", ".join(missing))
    database = str(values["DB_NAME"]).strip()
    brain_database = str(values.get("MAGI_BRAIN_DB_NAME") or "magi_brain").strip()
    for name, value in (("DB_NAME", database), ("MAGI_BRAIN_DB_NAME", brain_database)):
        if not _MYSQL_IDENTIFIER_RE.fullmatch(value):
            raise SelfHostError(f"{name} must contain only letters, numbers, and underscores")
    try:
        port = int(str(values["DB_PORT"]).strip())
    except (TypeError, ValueError) as exc:
        raise SelfHostError("DB_PORT must be an integer") from exc
    if not 1 <= port <= 65535:
        raise SelfHostError("DB_PORT is outside 1..65535")
    return {
        "host": str(values["DB_HOST"]).strip(),
        "port": port,
        "user": str(values["DB_USER"]).strip(),
        "password": str(values["DB_PASSWORD"]),
        "business_database": database,
        "brain_database": brain_database,
    }


_BRAIN_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS users (
      id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
      username VARCHAR(255) NOT NULL,
      password_hash VARCHAR(255) NOT NULL,
      role VARCHAR(32) NOT NULL DEFAULT 'user',
      line_user_id VARCHAR(100) NULL,
      tenant_id VARCHAR(64) NOT NULL DEFAULT 'default',
      tenant_role VARCHAR(32) NOT NULL DEFAULT 'member',
      created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
      UNIQUE KEY uq_users_username (username),
      UNIQUE KEY uq_users_line_user_id (line_user_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS pending_registrations (
      token VARCHAR(64) NOT NULL PRIMARY KEY,
      role VARCHAR(32) NOT NULL DEFAULT 'admin',
      created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS audit_log (
      id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
      agent_name VARCHAR(120) NOT NULL DEFAULT '',
      target_db VARCHAR(64) NOT NULL DEFAULT '',
      table_name VARCHAR(120) NOT NULL DEFAULT '',
      record_id VARCHAR(255) NOT NULL DEFAULT '',
      operation VARCHAR(64) NOT NULL DEFAULT '',
      old_value LONGTEXT NULL,
      new_value LONGTEXT NULL,
      reason TEXT NULL,
      action VARCHAR(255) NOT NULL DEFAULT '',
      details LONGTEXT NULL,
      timestamp DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS documents (
      id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
      content LONGTEXT NULL,
      source TEXT NULL,
      synced TINYINT(1) NOT NULL DEFAULT 0,
      created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS vectors (
      doc_id BIGINT NOT NULL,
      embedding JSON NULL,
      KEY idx_vectors_doc_id (doc_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
)

_BUSINESS_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS cases (
      id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
      case_number VARCHAR(50) NOT NULL,
      client_name VARCHAR(255) NOT NULL DEFAULT '',
      case_type VARCHAR(100) NOT NULL DEFAULT '',
      case_reason TEXT NULL,
      case_category VARCHAR(50) NOT NULL DEFAULT '',
      legal_aid_number VARCHAR(100) NOT NULL DEFAULT '',
      laf_case_no VARCHAR(120) NOT NULL DEFAULT '',
      application_no VARCHAR(120) NOT NULL DEFAULT '',
      court_case_number VARCHAR(255) NOT NULL DEFAULT '',
      court_case_no VARCHAR(255) NOT NULL DEFAULT '',
      court_name VARCHAR(255) NOT NULL DEFAULT '',
      status VARCHAR(50) NOT NULL DEFAULT '',
      notes TEXT NULL,
      folder_path TEXT NULL,
      created_date DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
      updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
      KEY idx_cases_case_number (case_number),
      KEY idx_cases_client_name (client_name),
      KEY idx_cases_court_case_number (court_case_number(100))
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS case_todos (
      id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
      case_number VARCHAR(255) NOT NULL,
      client_name TEXT NULL,
      todo_type VARCHAR(120) NOT NULL,
      todo_date DATE NULL,
      todo_time TIME NULL,
      description TEXT NULL,
      status VARCHAR(50) NOT NULL DEFAULT 'pending',
      source_file TEXT NULL,
      google_calendar_id TEXT NULL,
      created_date DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
      completed_date DATETIME NULL,
      KEY idx_case_todos_case_number (case_number),
      KEY idx_case_todos_date (todo_date)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS todo_keywords (
      id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
      todo_type VARCHAR(120) NOT NULL,
      pattern TEXT NOT NULL,
      pattern_type VARCHAR(50) NOT NULL,
      days INT NULL,
      is_active TINYINT(1) NOT NULL DEFAULT 1,
      created_date DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
      UNIQUE KEY uq_todo_keyword (todo_type, pattern_type, pattern(128))
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
)


def mysql_database_status(values: Mapping[str, str]) -> dict[str, Any]:
    """Verify connection, required schemas, and first-boot tables."""

    params = _mysql_parameters(values)
    try:
        import mysql.connector  # type: ignore
    except Exception as exc:
        return {"ok": False, "error": "mysql_connector_missing", "detail": str(exc)}
    connection = None
    try:
        connection = mysql.connector.connect(
            host=params["host"],
            port=params["port"],
            user=params["user"],
            password=params["password"],
            connection_timeout=5,
        )
        cursor = connection.cursor()
        required = {
            params["brain_database"]: {"users", "pending_registrations", "audit_log", "documents", "vectors"},
            params["business_database"]: {"cases", "case_todos", "todo_keywords"},
        }
        missing: dict[str, list[str]] = {}
        for database, tables in required.items():
            cursor.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema=%s",
                (database,),
            )
            present = {str(row[0]) for row in cursor.fetchall()}
            absent = sorted(tables - present)
            if absent:
                missing[database] = absent
        cursor.close()
        return {"ok": not missing, "schemas": list(required), "missing_tables": missing}
    except Exception as exc:
        return {"ok": False, "error": "database_probe_failed", "detail": f"{type(exc).__name__}: {exc}"}
    finally:
        try:
            if connection is not None:
                connection.close()
        except Exception:
            pass


def bootstrap_mysql_databases(values: Mapping[str, str]) -> dict[str, Any]:
    """Create the two required databases and idempotent first-boot tables."""

    params = _mysql_parameters(values)
    try:
        import mysql.connector  # type: ignore
    except Exception as exc:
        raise SelfHostError(f"mysql connector is not installed: {exc}") from exc
    connection = None
    try:
        connection = mysql.connector.connect(
            host=params["host"],
            port=params["port"],
            user=params["user"],
            password=params["password"],
            connection_timeout=8,
        )
        cursor = connection.cursor()
        for database in (params["brain_database"], params["business_database"]):
            cursor.execute(
                f"CREATE DATABASE IF NOT EXISTS `{database}` "
                "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
            )
        cursor.close()
        connection.close()
        connection = None
        created: dict[str, int] = {}
        for database, statements in (
            (params["brain_database"], _BRAIN_SCHEMA),
            (params["business_database"], _BUSINESS_SCHEMA),
        ):
            db_connection = mysql.connector.connect(
                host=params["host"],
                port=params["port"],
                user=params["user"],
                password=params["password"],
                database=database,
                connection_timeout=8,
            )
            db_cursor = db_connection.cursor()
            for statement in statements:
                db_cursor.execute(statement)
            db_connection.commit()
            db_cursor.close()
            db_connection.close()
            created[database] = len(statements)
        status = mysql_database_status(values)
        if not status.get("ok"):
            raise SelfHostError("database bootstrap completed but verification failed")
        return {"ok": True, "created_or_verified": created, "verification": status}
    except SelfHostError:
        raise
    except Exception as exc:
        raise SelfHostError(f"database bootstrap failed: {type(exc).__name__}: {exc}") from exc
    finally:
        try:
            if connection is not None:
                connection.close()
        except Exception:
            pass


def doctor(config: Mapping[str, Any], *, live: bool = False) -> dict[str, Any]:
    checks: list[Check] = []
    errors = validate_config(config)
    checks.append(Check("config", "fail" if errors else "pass", "; ".join(errors) if errors else "configuration schema is valid", "修正 selfhost.json" if errors else ""))
    version_ok = sys.version_info >= (3, 12)
    checks.append(Check("python", "pass" if version_ok else "fail", platform.python_version(), "安裝 Python 3.12 以上版本" if not version_ok else ""))
    for directory in required_directories(config):
        checks.append(Check(f"path:{directory.name}", "pass" if directory.is_dir() else "fail", str(directory), "執行 init 或 install 建立目錄" if not directory.is_dir() else ""))
    secrets = dict(config.get("secrets") or {})
    secret_path = Path(str(secrets.get("env_file") or "")).expanduser()
    values = parse_env_file(secret_path)
    missing = [name for name in secrets.get("required_names") or [] if not values.get(str(name), "").strip()]
    checks.append(Check("secrets", "warn" if missing else "pass", "缺少：" + ", ".join(missing) if missing else "必要本機密鑰已設定", "執行 secrets --generate 後再驗收" if missing else ""))
    database = dict(config.get("database") or {})
    db_required_missing = [name for name in _MYSQL_REQUIRED_VALUES if not values.get(name, "").strip()]
    if database.get("engine") == "mysql" and not db_required_missing:
        db_status = mysql_database_status(values)
        checks.append(Check(
            "database",
            "pass" if db_status.get("ok") else "fail",
            "MySQL/MariaDB schemas and core tables are ready" if db_status.get("ok") else str(db_status.get("error") or db_status.get("missing_tables") or "not ready"),
            "確認連線資料後執行 database --apply，再重跑 doctor" if not db_status.get("ok") else "",
        ))
    elif database.get("engine") == "mysql":
        checks.append(Check(
            "database",
            "warn",
            "缺少連線設定：" + ", ".join(db_required_missing),
            "將資料庫設定寫入 secrets/magi.env",
        ))
    features = dict(config.get("features") or {})
    feature_credentials = {
        "legal_aid": {
            "mode": "all",
            "names": ("MAGI_LAF_USERNAME", "MAGI_LAF_PASSWORD"),
        },
        "court_portal": {
            "mode": "all",
            "names": (
                "MAGI_JUDICIAL_EEFILE_USERNAME",
                "MAGI_JUDICIAL_EEFILE_PASSWORD",
            ),
        },
        "google_calendar": {
            "mode": "all",
            "names": ("GOOGLE_APPLICATION_CREDENTIALS",),
        },
        "google_drive": {
            "mode": "all",
            "names": ("GOOGLE_APPLICATION_CREDENTIALS",),
        },
        "messaging": {
            "mode": "any",
            "names": (
                "DISCORD_BOT_TOKEN",
                "TELEGRAM_BOT_TOKEN",
                "OPENCLAW_TELEGRAM_BOT_TOKEN",
                "LINE_CHANNEL_ACCESS_TOKEN",
                "MAGI_LINE_CHANNEL_ACCESS_TOKEN",
            ),
        },
    }
    for feature, requirement in feature_credentials.items():
        if not features.get(feature):
            continue
        credential_names = tuple(requirement["names"])
        found = [name for name in credential_names if values.get(name, "").strip()]
        complete = bool(found) if requirement["mode"] == "any" else len(found) == len(credential_names)
        detail = (
            "已設定：" + ", ".join(found)
            if complete
            else "缺少：" + ", ".join(name for name in credential_names if name not in found)
        )
        if complete and feature in {"google_calendar", "google_drive"}:
            credential_path = Path(values["GOOGLE_APPLICATION_CREDENTIALS"]).expanduser()
            complete = credential_path.is_file() and not credential_path.is_symlink()
            detail = (
                f"憑證檔可讀：{credential_path.name}"
                if complete
                else "Google 憑證路徑不是可讀的正規檔案"
            )
        checks.append(Check(
            f"feature:{feature}",
            "pass" if complete else "warn",
            detail,
            "先將所需憑證寫入 secrets/magi.env，否則關閉這項功能" if not complete else "",
        ))
    models = dict(config.get("models") or {})
    if models.get("heavy_backend") == "nvidia_api":
        nvidia_ready = bool(values.get("NVIDIA_API_KEY", "").strip())
        checks.append(Check(
            "model:nvidia_api",
            "pass" if nvidia_ready else "warn",
            "NVIDIA API 憑證已設定" if nvidia_ready else "重型模型設為 NVIDIA API，但尚未設定 NVIDIA_API_KEY",
            "將 NVIDIA_API_KEY 寫入 secrets/magi.env，或將 heavy_backend 改為 disabled" if not nvidia_ready else "",
        ))
    certification = certify_active_release(config)
    checks.append(Check(
        "active_release",
        "pass" if certification["ok"] else "fail",
        str(certification.get("detail") or certification.get("release_id") or "none"),
        "先 stage 並 activate release，或重新 stage 未受竄改的 release" if not certification["ok"] else "",
    ))
    if live:
        ports = dict(dict(config["network"])["ports"])
        host = str(dict(config["network"]).get("bind_host") or "127.0.0.1")
        probe_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
        probes = [("main", "/readyz"), ("tools", "/health")]
        if ports.get("file_review"):
            probes.append(("file_review", "/health"))
        if ports.get("admin"):
            probes.append(("admin", "/health"))
        for name, suffix in probes:
            url = f"http://{probe_host}:{int(ports[name])}{suffix}"
            ok, detail = _probe(url)
            checks.append(Check(f"live:{name}", "pass" if ok else "fail", f"{url} {detail}", "檢查服務日誌或重新啟動服務" if not ok else ""))
    summary = {state: sum(item.status == state for item in checks) for state in ("pass", "warn", "fail")}
    return {
        "schema": "magi.selfhost.doctor/v1",
        "ok": summary["fail"] == 0,
        "ready": summary["fail"] == 0 and summary["warn"] == 0,
        "platform": str(dict(config.get("instance") or {}).get("platform") or ""),
        "summary": summary,
        "checks": [item.as_dict() for item in checks],
    }


def install_commands(source: Path, layout: HostLayout, *, include_optional: bool) -> list[list[str]]:
    if layout.platform == "Windows" and platform.system() != "Windows":
        bootstrap = ["py", "-3.12"]
    else:
        bootstrap = [sys.executable or ("python" if layout.platform == "Windows" else "python3")]
    python = str(venv_python(layout))
    commands = [
        [*bootstrap, "-m", "venv", str(layout.venv_dir)],
        [python, "-m", "pip", "install", "--upgrade", "pip", "wheel"],
        [python, "-m", "pip", "install", "-r", str(source / "requirements-selfhost.txt")],
    ]
    if include_optional:
        commands.append([python, "-m", "pip", "install", "-r", str(source / "requirements-optional.txt")])
        commands.append([python, "-m", "playwright", "install", "chromium"])
    return commands


def safe_remove_program(config: Mapping[str, Any], *, remove_data: bool = False) -> list[str]:
    """Remove program/runtime files; user data is preserved unless explicit."""

    paths = dict(config["paths"])
    removed: list[str] = []
    for key in ("releases_dir", "runtime_dir"):
        path = Path(str(paths[key])).expanduser()
        if path.exists():
            shutil.rmtree(path)
            removed.append(str(path))
    launcher = Path(str(dict(config["service"])["launcher"])).expanduser()
    if launcher.exists():
        launcher.unlink()
        removed.append(str(launcher))
    if remove_data:
        for key in ("data_dir", "backups_dir"):
            path = Path(str(paths[key])).expanduser()
            if path.exists():
                shutil.rmtree(path)
                removed.append(str(path))
    return removed
