from __future__ import annotations

import os
import sys
from pathlib import Path


_MAGI_ROOT_DEFAULT = Path(__file__).resolve().parent.parent
_LEGACY_CODE_ROOT_SENTINEL = _MAGI_ROOT_DEFAULT / ".legacy_code_disabled"
_ORCH_RELATIVE = Path("casper_ecosystem") / "law_firm_orchestrators"


def _env_path(*names: str) -> Optional[Path]:
    for name in names:
        raw = (os.environ.get(name) or "").strip()
        if raw:
            return Path(raw).expanduser()
    return None


def _env_flag(name: str, default: str = "0") -> bool:
    return (os.environ.get(name) or default).strip().lower() in {"1", "true", "yes", "on"}


def _unique_paths(items: list[Path]) -> list[Path]:
    out: list[Path] = []
    seen: set[str] = set()
    for item in items:
        try:
            key = str(item.resolve())
        except Exception:
            key = str(item)
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def ensure_path_on_sys_path(path: Path | str) -> Path:
    p = Path(path).expanduser()
    try:
        s = str(p.resolve())
    except Exception:
        s = str(p)
    if s not in sys.path:
        sys.path.insert(0, s)
    return Path(s)


def ensure_magi_root_on_sys_path() -> Path:
    return ensure_path_on_sys_path(get_magi_root_dir())


def ensure_orch_on_sys_path() -> Path:
    return ensure_path_on_sys_path(get_orch_dir())


def get_magi_root_dir() -> Path:
    env = _env_path("MAGI_ROOT_DIR", "MAGI_ROOT")
    if env:
        return env.resolve()
    return _MAGI_ROOT_DEFAULT


def get_legacy_code_root() -> Path:
    env = _env_path("MAGI_LEGACY_CODE_DIR")
    if env:
        return env.resolve()
    # Keep old call sites self-contained: when legacy mode is not enabled,
    # treat the MAGI root itself as the effective compatibility root.
    if not _env_flag("MAGI_ENABLE_LEGACY_CODE_ROOT", "0"):
        return get_magi_root_dir()
    return _LEGACY_CODE_ROOT_SENTINEL


def legacy_code_enabled() -> bool:
    if _env_path("MAGI_LEGACY_CODE_DIR") is not None:
        return True
    return _env_flag("MAGI_ENABLE_LEGACY_CODE_ROOT", "0")


def get_orch_dir() -> Path:
    env = _env_path("MAGI_ORCH_DIR")
    if env and env.is_dir():
        return env.resolve()

    compat = _env_path("MAGI_CODE_DIR")
    if compat and (compat / "laf_orchestrator.py").exists():
        return compat.resolve()

    magi_default = get_magi_root_dir() / _ORCH_RELATIVE
    if magi_default.is_dir():
        return magi_default.resolve()

    legacy = get_legacy_code_root()
    if legacy_code_enabled() and legacy.is_dir():
        return legacy.resolve()
    return magi_default.resolve()


def get_json_dir() -> Path:
    env = _env_path("MAGI_JSON_DIR")
    if env:
        return env.resolve()

    magi_json = get_magi_root_dir() / "json"
    orch_json = get_orch_dir() / "json"
    paths = [magi_json, orch_json]
    if legacy_code_enabled():
        paths.append(get_legacy_code_root() / "json")
    for path in paths:
        if path.is_dir():
            return path.resolve()
    return magi_json.resolve()


def get_metrics_dir() -> Path:
    env = _env_path("MAGI_METRICS_DIR")
    if env:
        return env.resolve()
    return (get_magi_root_dir() / "_metrics").resolve()


def get_autopilot_runs_dir() -> Path:
    env = _env_path("MAGI_AUTOPILOT_RUNS_DIR")
    if env:
        return env.resolve()
    return (get_magi_root_dir() / "_autopilot_runs").resolve()


def config_candidates(name: str = "config.json") -> list[Path]:
    env_specific = _env_path("MAGI_CONFIG_PATH") if name == "config.json" else None
    items: list[Optional[Path]] = [
        env_specific,
        get_json_dir() / name,
        get_orch_dir() / "json" / name,
        get_orch_dir() / name,
    ]
    if legacy_code_enabled():
        items.extend(
            [
                get_legacy_code_root() / "json" / name,
                get_legacy_code_root() / name,
            ]
        )
    return _unique_paths([p for p in items if p is not None])


def get_config_path(name: str = "config.json") -> Path:
    for path in config_candidates(name):
        if path.exists():
            return path.resolve()
    return config_candidates(name)[0]


def get_module_path(filename: str) -> Path:
    candidates = [
        get_orch_dir() / filename,
    ]
    if legacy_code_enabled():
        candidates.append(get_legacy_code_root() / filename)
    for path in _unique_paths(candidates):
        if path.exists():
            return path.resolve()
    return candidates[0]


def get_laf_script() -> Path:
    return get_module_path("laf_orchestrator.py")


def get_skill_python() -> Path:
    env = _env_path("MAGI_SKILL_PYTHON")
    if env and env.exists():
        return env

    root = get_magi_root_dir()
    _IS_WIN = sys.platform == "win32"

    # Check venv candidates (cross-platform)
    if _IS_WIN:
        candidates = [
            root / "venv" / "Scripts" / "python.exe",
            root / ".venv" / "Scripts" / "python.exe",
            get_orch_dir() / ".venv" / "Scripts" / "python.exe",
        ]
    else:
        candidates = [
            root / "venv" / "bin" / "python3",
            root / "venv" / "bin" / "python",
            root / ".venv" / "bin" / "python3",
            get_orch_dir() / ".venv" / "bin" / "python",
        ]

    if legacy_code_enabled():
        if _IS_WIN:
            candidates.append(get_legacy_code_root() / ".venv" / "Scripts" / "python.exe")
        else:
            candidates.append(get_legacy_code_root() / ".venv" / "bin" / "python")

    for p in candidates:
        if p.exists():
            return p

    return Path(sys.executable or "python3")
