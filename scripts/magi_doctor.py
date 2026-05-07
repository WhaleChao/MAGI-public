#!/usr/bin/env python3
"""Beginner-friendly MAGI detection wizard."""

from __future__ import annotations

import argparse
import importlib.util
import json
import platform
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    detail: str
    fix: str = ""


def _project_python() -> Path | None:
    candidates = [
        REPO_ROOT / ".venv" / ("Scripts/python.exe" if platform.system() == "Windows" else "bin/python"),
        REPO_ROOT / "venv" / ("Scripts/python.exe" if platform.system() == "Windows" else "bin/python"),
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def _package_available(module_name: str) -> bool:
    if importlib.util.find_spec(module_name) is not None:
        return True

    project_python = _project_python()
    if not project_python or Path(sys.executable).absolute() == project_python.absolute():
        return False

    probe = (
        "import importlib.util, sys; "
        f"sys.exit(0 if importlib.util.find_spec({module_name!r}) else 1)"
    )
    try:
        return subprocess.run(
            [str(project_python), "-c", probe],
            cwd=REPO_ROOT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        ).returncode == 0
    except Exception:
        return False


def _disk_free_gb(path: Path) -> float:
    usage = shutil.disk_usage(path)
    return round(usage.free / (1024 ** 3), 1)


def _ram_gb() -> float:
    try:
        import psutil

        return round(psutil.virtual_memory().total / (1024 ** 3), 1)
    except Exception:
        if platform.system() == "Darwin":
            try:
                import subprocess

                raw = subprocess.check_output(["sysctl", "-n", "hw.memsize"], text=True).strip()
                return round(int(raw) / (1024 ** 3), 1)
            except Exception:
                return 0.0
        return 0.0


def _http_json(url: str, timeout: float = 2.0) -> tuple[bool, str]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            body = resp.read(4096).decode("utf-8", errors="replace")
            return 200 <= resp.status < 300, body
    except urllib.error.URLError as exc:
        return False, str(exc)
    except Exception as exc:
        return False, str(exc)


def collect_report(*, live: bool = True) -> dict[str, Any]:
    ram = _ram_gb()
    system = platform.system()
    machine = platform.machine()
    checks: list[Check] = []

    checks.append(Check("python", "pass" if sys.version_info >= (3, 10) else "fail", platform.python_version(), "Install Python 3.10 or newer."))
    checks.append(Check("disk", "pass" if _disk_free_gb(REPO_ROOT) >= 20 else "warn", f"{_disk_free_gb(REPO_ROOT)} GB free", "Free at least 20 GB for models and logs."))
    checks.append(Check("memory", "pass" if ram >= 16 else "warn", f"{ram} GB RAM", "16 GB+ is recommended; 32 GB+ is better for local models."))
    checks.append(Check("git", "pass" if shutil.which("git") else "fail", shutil.which("git") or "missing", "Install Git."))

    project_python = _project_python()
    venv_path = REPO_ROOT / ".venv"
    legacy_venv = REPO_ROOT / "venv"
    checks.append(Check("virtualenv", "pass" if project_python else "warn", str(project_python or venv_path if venv_path.exists() else legacy_venv), "Run scripts/install_magi.py --yes."))

    for module, pip_name in (
        ("flask", "flask"),
        ("fastapi", "fastapi"),
        ("uvicorn", "uvicorn"),
        ("pytest", "pytest"),
        ("requests", "requests"),
    ):
        checks.append(Check(f"python:{module}", "pass" if _package_available(module) else "warn", pip_name, f"Install with pip install {pip_name}."))

    apple_silicon = system == "Darwin" and machine == "arm64"
    checks.append(Check("apple_silicon", "pass" if apple_silicon else "warn", f"{system} {machine}", "MLX acceleration is best on Apple Silicon."))
    checks.append(Check("mlx", "pass" if _package_available("mlx") else "warn", "installed" if _package_available("mlx") else "missing", "Install optional MLX dependencies."))
    checks.append(Check("mlx_vlm", "pass" if _package_available("mlx_vlm") else "warn", "installed" if _package_available("mlx_vlm") else "missing", "pip install mlx-vlm"))

    model_dir = Path.home() / ".omlx" / "models" / "gemma-4-E4B-it-assistant-bf16"
    checks.append(Check("gemma4_e4b_model", "pass" if model_dir.exists() else "warn", str(model_dir), "Download the Gemma 4 E4B assistant draft model."))

    if live:
        ok, detail = _http_json("http://127.0.0.1:8090/health")
        checks.append(Check("mlx_mtp_sidecar", "pass" if ok else "warn", detail[:240], "Start com.magi.mlx-mtp or run scripts/serve_mlx_mtp.py."))

    failed = sum(1 for c in checks if c.status == "fail")
    warned = sum(1 for c in checks if c.status == "warn")
    return {
        "ok": failed == 0,
        "status": "pass" if failed == 0 and warned == 0 else ("warn" if failed == 0 else "fail"),
        "system": {
            "os": system,
            "release": platform.release(),
            "machine": machine,
            "python": platform.python_version(),
            "python_executable": sys.executable,
            "project_python": str(project_python) if project_python else None,
            "repo": str(REPO_ROOT),
        },
        "summary": {"pass": sum(1 for c in checks if c.status == "pass"), "warn": warned, "fail": failed},
        "checks": [asdict(c) for c in checks],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Detect whether this computer can run MAGI.")
    parser.add_argument("--json", action="store_true", help="print JSON")
    parser.add_argument("--no-live", action="store_true", help="skip localhost service probes")
    parser.add_argument("--output", type=Path, help="write JSON report to a file")
    args = parser.parse_args(argv)

    report = collect_report(live=not args.no_live)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"MAGI Doctor: {report['status'].upper()} {report['summary']}")
        for item in report["checks"]:
            print(f"{item['status'].upper():4} {item['name']}: {item['detail']}")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
