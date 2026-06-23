from __future__ import annotations

import os
import sys
from pathlib import Path


def _candidate_root(path: Path) -> Path | None:
    current = path if path.is_dir() else path.parent
    for base in (current, *current.parents):
        if (base / "daemon.py").exists() and (base / "skills").is_dir() and (base / "api").is_dir():
            return base.resolve()
    return None


def resolve_release_root() -> Path | None:
    for env_name in ("MAGI_ROOT", "MAGI_ROOT_DIR"):
        raw = (os.environ.get(env_name) or "").strip()
        if raw:
            root = _candidate_root(Path(raw).expanduser().resolve())
            if root is not None:
                return root

    root = _candidate_root(Path.cwd().resolve())
    if root is not None:
        return root

    package_root = Path(__file__).resolve().parents[1]
    return _candidate_root(package_root)


def runtime_env(root: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["MAGI_ROOT"] = str(root)
    env["MAGI_ROOT_DIR"] = str(root)

    pythonpath = env.get("PYTHONPATH", "").strip()
    env["PYTHONPATH"] = str(root) if not pythonpath else f"{root}{os.pathsep}{pythonpath}"
    return env


def resolve_python(root: Path) -> Path:
    if os.name == "nt":
        candidates = [
            root / "venv" / "Scripts" / "python.exe",
            root / ".venv" / "Scripts" / "python.exe",
        ]
    else:
        candidates = [
            root / "venv" / "bin" / "python3",
            root / "venv" / "bin" / "python",
            root / ".venv" / "bin" / "python3",
            root / ".venv" / "bin" / "python",
        ]

    for candidate in candidates:
        if candidate.exists():
            return candidate
    return Path(sys.executable or "python3")


def root_error_message() -> str:
    return (
        "MAGI release root not found. Run this command from the MAGI project directory "
        "or set MAGI_ROOT to the unpacked release path."
    )
