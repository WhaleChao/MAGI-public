#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MAGI Platform Abstraction Layer
================================
Provides cross-platform utilities for macOS and Windows compatibility.

Usage:
    from skills.ops.platform_utils import (
        IS_MACOS, IS_WINDOWS, IS_LINUX,
        file_lock, file_unlock,
        get_temp_dir, get_data_dir, get_config_dir,
        open_file, find_executable,
        get_service_manager, get_venv_python,
    )
"""
from __future__ import annotations

import logging
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Generator

logger = logging.getLogger("PlatformUtils")

# ---------------------------------------------------------------------------
# Platform Detection
# ---------------------------------------------------------------------------

SYSTEM = platform.system()          # "Darwin", "Windows", "Linux"
IS_MACOS = SYSTEM == "Darwin"
IS_WINDOWS = SYSTEM == "Windows"
IS_LINUX = SYSTEM == "Linux"
IS_APPLE_SILICON = IS_MACOS and platform.machine() == "arm64"
ARCH = platform.machine()           # "arm64", "x86_64", "AMD64"

# ---------------------------------------------------------------------------
# File Locking (fcntl on Unix, msvcrt on Windows)
# ---------------------------------------------------------------------------

if IS_WINDOWS:
    import msvcrt

    def file_lock(fh, exclusive: bool = True, blocking: bool = True) -> None:
        """Acquire a lock on file handle *fh*."""
        mode = msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK
        # Lock first byte
        msvcrt.locking(fh.fileno(), mode, 1)

    def file_unlock(fh) -> None:
        """Release lock on file handle *fh*."""
        try:
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 61, exc_info=True)

else:
    import fcntl

    def file_lock(fh, exclusive: bool = True, blocking: bool = True) -> None:
        """Acquire a lock on file handle *fh*."""
        flags = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        if not blocking:
            flags |= fcntl.LOCK_NB
        fcntl.flock(fh.fileno(), flags)

    def file_unlock(fh) -> None:
        """Release lock on file handle *fh*."""
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 78, exc_info=True)


@contextmanager
def locked_file(
    path: str | Path, mode: str = "w", exclusive: bool = True, blocking: bool = True
) -> Generator:
    """Context manager that opens a file with an advisory lock.

    >>> with locked_file("/tmp/my.lock") as fh:
    ...     fh.write(str(os.getpid()))
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, mode, encoding="utf-8")
    try:
        file_lock(fh, exclusive=exclusive, blocking=blocking)
        yield fh
    finally:
        file_unlock(fh)
        fh.close()


# ---------------------------------------------------------------------------
# Path Utilities
# ---------------------------------------------------------------------------

def get_magi_root() -> Path:
    """Resolve MAGI root directory."""
    env = os.environ.get("MAGI_ROOT_DIR")
    if env:
        return Path(env)
    # Walk up from this file
    return Path(__file__).resolve().parent.parent.parent


def get_venv_python() -> str:
    """Get the path to the venv Python interpreter."""
    root = get_magi_root()
    if IS_WINDOWS:
        candidates = [
            root / "venv" / "Scripts" / "python.exe",
            root / ".venv" / "Scripts" / "python.exe",
        ]
    else:
        candidates = [
            root / "venv" / "bin" / "python3",
            root / ".venv" / "bin" / "python3",
        ]
    for p in candidates:
        if p.exists():
            return str(p)
    return sys.executable


def get_temp_dir() -> Path:
    """Platform-safe temporary directory."""
    return Path(tempfile.gettempdir())


def get_data_dir(app_name: str = "MAGI") -> Path:
    """Platform-specific data directory.

    macOS:  ~/Library/Application Support/MAGI
    Windows: %LOCALAPPDATA%/MAGI
    Linux:  ~/.local/share/MAGI
    """
    if IS_MACOS:
        base = Path.home() / "Library" / "Application Support"
    elif IS_WINDOWS:
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return base / app_name


def get_config_dir(app_name: str = "MAGI") -> Path:
    """Platform-specific configuration directory.

    macOS:  ~/Library/Application Support/MAGI
    Windows: %LOCALAPPDATA%/MAGI
    Linux:  ~/.config/MAGI
    """
    if IS_MACOS:
        return Path.home() / "Library" / "Application Support" / app_name
    elif IS_WINDOWS:
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / app_name
    else:
        return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / app_name


def normalize_path(path: str | Path) -> Path:
    """Normalize path separators for the current platform."""
    return Path(os.path.normpath(str(path)))


# ---------------------------------------------------------------------------
# Executable Discovery
# ---------------------------------------------------------------------------

# Common tool name → platform-specific search hints
_TOOL_HINTS: dict[str, dict[str, list[str]]] = {
    "cloudflared": {
        "Darwin": ["/opt/homebrew/bin/cloudflared", "/usr/local/bin/cloudflared"],
        "Windows": [r"C:\Program Files\cloudflared\cloudflared.exe"],
        "Linux": ["/usr/local/bin/cloudflared", "/usr/bin/cloudflared"],
    },
    "pdftotext": {
        "Darwin": ["/opt/homebrew/bin/pdftotext", "/usr/local/bin/pdftotext"],
        "Windows": [r"C:\Program Files\poppler\bin\pdftotext.exe",
                     r"C:\Program Files\xpdf\pdftotext.exe"],
        "Linux": ["/usr/bin/pdftotext"],
    },
    "pdfinfo": {
        "Darwin": ["/opt/homebrew/bin/pdfinfo", "/usr/local/bin/pdfinfo"],
        "Windows": [r"C:\Program Files\poppler\bin\pdfinfo.exe"],
        "Linux": ["/usr/bin/pdfinfo"],
    },
    "soffice": {
        "Darwin": ["/opt/homebrew/bin/soffice",
                    "/Applications/LibreOffice.app/Contents/MacOS/soffice"],
        "Windows": [r"C:\Program Files\LibreOffice\program\soffice.exe",
                     r"C:\Program Files (x86)\LibreOffice\program\soffice.exe"],
        "Linux": ["/usr/bin/soffice", "/usr/local/bin/soffice"],
    },
    "chromium": {
        "Darwin": ["/opt/homebrew/bin/chromium",
                    "/Applications/Chromium.app/Contents/MacOS/Chromium"],
        "Windows": [r"C:\Program Files\Chromium\Application\chrome.exe",
                     r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"],
        "Linux": ["/usr/bin/chromium", "/usr/bin/chromium-browser"],
    },
    "tailscale": {
        "Darwin": ["/opt/homebrew/bin/tailscale", "/usr/local/bin/tailscale"],
        "Windows": [r"C:\Program Files\Tailscale\tailscale.exe"],
        "Linux": ["/usr/bin/tailscale"],
    },
    "whisper": {
        "Darwin": ["/opt/homebrew/bin/whisper"],
        "Windows": [],
        "Linux": ["/usr/local/bin/whisper"],
    },
    "mariadb": {
        "Darwin": ["/opt/homebrew/bin/mariadb", "/usr/local/bin/mariadb"],
        "Windows": [r"C:\Program Files\MariaDB\bin\mariadb.exe",
                     r"C:\Program Files\MariaDB\bin\mysql.exe"],
        "Linux": ["/usr/bin/mariadb", "/usr/bin/mysql"],
    },
}


def find_executable(name: str) -> str | None:
    """Find an executable by name, checking platform-specific paths first."""
    # 1. Check PATH via shutil.which
    found = shutil.which(name)
    if found:
        return found

    # 2. Check platform hints
    hints = _TOOL_HINTS.get(name, {}).get(SYSTEM, [])
    for p in hints:
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p

    # 3. On Windows, try .exe suffix
    if IS_WINDOWS and not name.endswith(".exe"):
        return find_executable(name + ".exe")

    return None


# ---------------------------------------------------------------------------
# File / URL Opening
# ---------------------------------------------------------------------------

def open_file(path: str) -> bool:
    """Open a file or URL with the system default handler."""
    try:
        if IS_MACOS:
            subprocess.Popen(["open", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif IS_WINDOWS:
            os.startfile(path)  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["xdg-open", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception as e:
        logger.warning("Failed to open %s: %s", path, e)
        return False


# ---------------------------------------------------------------------------
# Service Management
# ---------------------------------------------------------------------------

class ServiceManager:
    """Abstract interface for OS-level service management."""

    def install(self, name: str, command: str, description: str = "") -> bool:
        raise NotImplementedError

    def uninstall(self, name: str) -> bool:
        raise NotImplementedError

    def start(self, name: str) -> bool:
        raise NotImplementedError

    def stop(self, name: str) -> bool:
        raise NotImplementedError

    def is_running(self, name: str) -> bool:
        raise NotImplementedError


class LaunchAgentManager(ServiceManager):
    """macOS LaunchAgent-based service management."""

    def _plist_path(self, name: str) -> Path:
        return Path.home() / "Library" / "LaunchAgents" / f"{name}.plist"

    def install(self, name: str, command: str, description: str = "") -> bool:
        import plistlib
        plist_path = self._plist_path(name)
        plist_path.parent.mkdir(parents=True, exist_ok=True)

        parts = command.split()
        plist_data = {
            "Label": name,
            "ProgramArguments": parts,
            "RunAtLoad": True,
            "KeepAlive": True,
            "StandardOutPath": f"/tmp/{name}.stdout.log",
            "StandardErrorPath": f"/tmp/{name}.stderr.log",
        }
        with open(plist_path, "wb") as f:
            plistlib.dump(plist_data, f)

        subprocess.run(["launchctl", "load", str(plist_path)], check=False)
        return True

    def uninstall(self, name: str) -> bool:
        plist_path = self._plist_path(name)
        if plist_path.exists():
            subprocess.run(["launchctl", "unload", str(plist_path)], check=False)
            plist_path.unlink(missing_ok=True)
        return True

    def start(self, name: str) -> bool:
        try:
            subprocess.run(["launchctl", "start", name], check=True)
            return True
        except Exception:
            return False

    def stop(self, name: str) -> bool:
        try:
            subprocess.run(["launchctl", "stop", name], check=True)
            return True
        except Exception:
            return False

    def is_running(self, name: str) -> bool:
        try:
            r = subprocess.run(
                ["launchctl", "print", f"gui/{os.getuid()}/{name}"],
                capture_output=True, text=True,
            )
            return r.returncode == 0
        except Exception:
            return False


class WindowsServiceManager(ServiceManager):
    """Windows Task Scheduler-based service management."""

    def install(self, name: str, command: str, description: str = "") -> bool:
        try:
            # Use schtasks to create a task that runs at logon
            subprocess.run([
                "schtasks", "/create",
                "/tn", name,
                "/tr", command,
                "/sc", "onlogon",
                "/rl", "highest",
                "/f",
            ], check=True, capture_output=True)
            return True
        except Exception as e:
            logger.warning("Failed to install Windows task %s: %s", name, e)
            return False

    def uninstall(self, name: str) -> bool:
        try:
            subprocess.run(
                ["schtasks", "/delete", "/tn", name, "/f"],
                check=True, capture_output=True,
            )
            return True
        except Exception:
            return False

    def start(self, name: str) -> bool:
        try:
            subprocess.run(
                ["schtasks", "/run", "/tn", name],
                check=True, capture_output=True,
            )
            return True
        except Exception:
            return False

    def stop(self, name: str) -> bool:
        try:
            subprocess.run(
                ["schtasks", "/end", "/tn", name],
                check=True, capture_output=True,
            )
            return True
        except Exception:
            return False

    def is_running(self, name: str) -> bool:
        try:
            r = subprocess.run(
                ["schtasks", "/query", "/tn", name, "/fo", "csv"],
                capture_output=True, text=True,
            )
            return "Running" in r.stdout
        except Exception:
            return False


class SystemdManager(ServiceManager):
    """Linux systemd user service management."""

    def _service_path(self, name: str) -> Path:
        return Path.home() / ".config" / "systemd" / "user" / f"{name}.service"

    def install(self, name: str, command: str, description: str = "") -> bool:
        svc_path = self._service_path(name)
        svc_path.parent.mkdir(parents=True, exist_ok=True)
        svc_path.write_text(
            f"[Unit]\nDescription={description or name}\n\n"
            f"[Service]\nExecStart={command}\nRestart=on-failure\n\n"
            f"[Install]\nWantedBy=default.target\n",
            encoding="utf-8",
        )
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
        subprocess.run(["systemctl", "--user", "enable", name], check=False)
        return True

    def uninstall(self, name: str) -> bool:
        subprocess.run(["systemctl", "--user", "disable", name], check=False)
        self._service_path(name).unlink(missing_ok=True)
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
        return True

    def start(self, name: str) -> bool:
        try:
            subprocess.run(["systemctl", "--user", "start", name], check=True)
            return True
        except Exception:
            return False

    def stop(self, name: str) -> bool:
        try:
            subprocess.run(["systemctl", "--user", "stop", name], check=True)
            return True
        except Exception:
            return False

    def is_running(self, name: str) -> bool:
        try:
            r = subprocess.run(
                ["systemctl", "--user", "is-active", name],
                capture_output=True, text=True,
            )
            return r.stdout.strip() == "active"
        except Exception:
            return False


def get_service_manager() -> ServiceManager:
    """Get the platform-appropriate service manager."""
    if IS_MACOS:
        return LaunchAgentManager()
    elif IS_WINDOWS:
        return WindowsServiceManager()
    else:
        return SystemdManager()


# ---------------------------------------------------------------------------
# Hardware Detection Helpers
# ---------------------------------------------------------------------------

def get_cpu_name() -> str:
    """Get CPU name/model string."""
    try:
        if IS_MACOS:
            name = subprocess.check_output(
                ["sysctl", "-n", "machdep.cpu.brand_string"],
                text=True, stderr=subprocess.DEVNULL,
            ).strip()
            if not name:
                # Apple Silicon
                name = subprocess.check_output(
                    ["sysctl", "-n", "hw.chip"],
                    text=True, stderr=subprocess.DEVNULL,
                ).strip()
            return name or "Apple Silicon"
        elif IS_WINDOWS:
            return platform.processor() or "Unknown"
        else:
            with open("/proc/cpuinfo") as f:
                for line in f:
                    if "model name" in line:
                        return line.split(":")[1].strip()
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 496, exc_info=True)
    return platform.processor() or "Unknown"


def get_total_ram_gb() -> float:
    """Get total system RAM in GB."""
    try:
        import psutil
        return round(psutil.virtual_memory().total / (1024 ** 3), 1)
    except ImportError:
        pass
    try:
        if IS_MACOS:
            raw = subprocess.check_output(
                ["sysctl", "-n", "hw.memsize"], text=True
            ).strip()
            return round(int(raw) / (1024 ** 3), 1)
        elif IS_WINDOWS:
            raw = subprocess.check_output(
                ["wmic", "computersystem", "get", "TotalPhysicalMemory"],
                text=True,
            )
            for line in raw.strip().split("\n"):
                line = line.strip()
                if line.isdigit():
                    return round(int(line) / (1024 ** 3), 1)
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 523, exc_info=True)
    return 0.0


def get_gpu_info() -> dict[str, Any]:
    """Detect GPU type and capabilities.

    Returns dict with keys: type, name, vram_gb, metal, cuda
    """
    info: dict[str, Any] = {
        "type": "none", "name": "", "vram_gb": 0.0,
        "metal": False, "cuda": False,
    }

    if IS_APPLE_SILICON:
        info["type"] = "apple_silicon"
        info["metal"] = True
        info["name"] = get_cpu_name()
        info["vram_gb"] = get_total_ram_gb()  # unified memory
        return info

    # NVIDIA
    try:
        nv = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=name,memory.total",
             "--format=csv,noheader,nounits"],
            text=True, stderr=subprocess.DEVNULL,
        ).strip()
        if nv:
            parts = nv.split(",")
            info["type"] = "nvidia"
            info["name"] = parts[0].strip()
            info["vram_gb"] = round(float(parts[1].strip()) / 1024, 1)
            info["cuda"] = True
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 558, exc_info=True)

    return info


# ---------------------------------------------------------------------------
# Inference Engine Detection
# ---------------------------------------------------------------------------

def get_inference_engine() -> str:
    """Detect the best available inference engine.

    Returns: 'omlx', 'ollama', or 'none'
    """
    if IS_APPLE_SILICON and shutil.which("omlx"):
        return "omlx"
    if shutil.which("ollama"):
        return "ollama"
    return "none"


def get_inference_port() -> int:
    """Get the default port for the detected inference engine."""
    engine = os.environ.get("MAGI_OMLX_ENABLED", "0")
    if engine == "1":
        return int(os.environ.get("MAGI_OMLX_PORT", "8080"))
    return 11434  # Ollama default


# ---------------------------------------------------------------------------
# Calendar Integration (platform-specific)
# ---------------------------------------------------------------------------

def query_calendar_events(days_ahead: int = 14) -> list[dict[str, str]]:
    """Query system calendar for upcoming events.

    macOS: uses osascript / Calendar.app
    Windows: uses COM / Outlook (if available)
    Returns list of dicts with keys: title, start, end, location, notes
    """
    if IS_MACOS:
        return _calendar_macos(days_ahead)
    elif IS_WINDOWS:
        return _calendar_windows(days_ahead)
    return []


def _calendar_macos(days_ahead: int) -> list[dict[str, str]]:
    """Query Apple Calendar via osascript."""
    import json as _json
    script = f'''
    set output to ""
    set today to current date
    set endDate to today + ({days_ahead} * days)
    tell application "Calendar"
        repeat with c in calendars
            set evts to (every event of c whose start date >= today and start date <= endDate)
            repeat with e in evts
                set t to summary of e
                set s to start date of e as string
                set ed to end date of e as string
                set loc to ""
                try
                    set loc to location of e
                end try
                set output to output & t & "|||" & s & "|||" & ed & "|||" & loc & "\\n"
            end repeat
        end repeat
    end tell
    return output
    '''
    try:
        raw = subprocess.check_output(
            ["osascript", "-e", script],
            text=True, stderr=subprocess.DEVNULL, timeout=15,
        ).strip()
        events = []
        for line in raw.split("\n"):
            parts = line.split("|||")
            if len(parts) >= 3:
                events.append({
                    "title": parts[0].strip(),
                    "start": parts[1].strip(),
                    "end": parts[2].strip(),
                    "location": parts[3].strip() if len(parts) > 3 else "",
                })
        return events
    except Exception as e:
        logger.debug("Calendar query failed: %s", e)
        return []


def _calendar_windows(days_ahead: int) -> list[dict[str, str]]:
    """Query Outlook calendar via COM automation (Windows only)."""
    try:
        import win32com.client  # type: ignore[import-not-found]
        from datetime import datetime, timedelta

        outlook = win32com.client.Dispatch("Outlook.Application").GetNamespace("MAPI")
        calendar_folder = outlook.GetDefaultFolder(9)  # olFolderCalendar
        items = calendar_folder.Items
        items.Sort("[Start]")
        items.IncludeRecurrences = True

        now = datetime.now()
        end = now + timedelta(days=days_ahead)
        items = items.Restrict(
            f"[Start] >= '{now.strftime('%m/%d/%Y')}' AND "
            f"[Start] <= '{end.strftime('%m/%d/%Y')}'"
        )

        events = []
        for item in items:
            events.append({
                "title": str(item.Subject),
                "start": str(item.Start),
                "end": str(item.End),
                "location": str(getattr(item, "Location", "")),
            })
        return events
    except Exception as e:
        logger.debug("Outlook calendar query failed: %s", e)
        return []


# ---------------------------------------------------------------------------
# PATH Environment Helpers
# ---------------------------------------------------------------------------

def extend_path() -> None:
    """Add platform-specific tool directories to PATH."""
    extra_dirs: list[str] = []
    if IS_MACOS:
        extra_dirs = ["/opt/homebrew/bin", "/usr/local/bin"]
    elif IS_WINDOWS:
        # Common Windows tool directories
        extra_dirs = [
            r"C:\Program Files\poppler\bin",
            r"C:\Program Files\Tailscale",
            r"C:\Program Files\cloudflared",
        ]
    else:
        extra_dirs = ["/usr/local/bin", "/snap/bin"]

    current = os.environ.get("PATH", "")
    for d in extra_dirs:
        if d not in current and os.path.isdir(d):
            current = d + os.pathsep + current
    os.environ["PATH"] = current
