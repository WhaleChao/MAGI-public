"""
SKILL GENESIS MODULE (技能進化引擎)
====================================
Enables CASPER to create, research, and safely integrate new Skills.
Falls back to MELCHIOR for complex generation tasks.

Safety: All skills are validated against MAGI Codex Article 5 (Prohibited Actions).
"""

import os
import json
import requests
import re
import subprocess
import shutil
import time
import random
import hashlib
import sys
import ast
import importlib.util
import threading
from typing import Optional
from datetime import datetime

# Ensure we import MAGI's local `skills.*` package (not an unrelated installed package).
# `__file__` = .../MAGI/skills/evolution/skill_genesis.py
_MAGI_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _MAGI_ROOT not in sys.path:
    sys.path.insert(0, _MAGI_ROOT)

from api.runtime_paths import get_legacy_code_root, get_magi_root_dir, get_skill_python, legacy_code_enabled

# =============================================================================
# Configuration
# =============================================================================
SKILLS_DIR = f"{_MAGI_ROOT}/skills"
DEFINITIONS_PATH = os.path.join(SKILLS_DIR, "definitions.json")
MAX_DEBUG_ROUNDS = int(os.environ.get("MAGI_SKILL_DEBUG_ROUNDS", "2"))
MAX_RUNTIME_REPAIR_ROUNDS = int(os.environ.get("MAGI_RUNTIME_REPAIR_ROUNDS", "2"))
SKILL_VERSIONS_DIR = os.path.join(SKILLS_DIR, ".versions")
SKILL_EVENTS_FILE = os.environ.get("MAGI_SKILL_EVENTS_FILE", f"{_MAGI_ROOT}/logs/skill_runtime_events.jsonl")
SKILL_USAGE_TRACKER_FILE = os.environ.get("MAGI_SKILL_USAGE_TRACKER_FILE", f"{_MAGI_ROOT}/logs/skill_usage_events.jsonl")
SKILL_EXEC_TIMEOUT_SEC = int(os.environ.get("MAGI_SKILL_EXEC_TIMEOUT_SEC", "30"))
SKILL_EXEC_MEM_MB = int(os.environ.get("MAGI_SKILL_EXEC_MEM_MB", "1024"))
SKILL_EXEC_CPU_SEC = int(os.environ.get("MAGI_SKILL_EXEC_CPU_SEC", "20"))
SKILL_ENABLE_PREEXEC = os.environ.get("MAGI_SKILL_ENABLE_PREEXEC", "0").strip().lower() in {"1", "true", "yes", "on"}
SKILL_PYTHON = str(get_skill_python())
SKILL_RUNTIME_SITE_PACKAGES = os.environ.get("MAGI_SKILL_RUNTIME_SITE_PACKAGES", f"{_MAGI_ROOT}/.runtime_site_packages")
SKILL_AUTO_PIP_ENABLED = os.environ.get("MAGI_SKILL_AUTO_PIP", "1").strip().lower() not in {"0", "false", "no", "off"}
SKILL_AUTO_PIP_TIMEOUT_SEC = int(os.environ.get("MAGI_SKILL_AUTO_PIP_TIMEOUT_SEC", "120"))
SKILL_AUTO_PIP_MAX_PACKAGES = int(os.environ.get("MAGI_SKILL_AUTO_PIP_MAX_PACKAGES", "6"))
SKILL_AUTO_PIP_ALLOW_ANY = os.environ.get("MAGI_SKILL_AUTO_PIP_ALLOW_ANY", "1").strip().lower() not in {"0", "false", "no", "off"}
SKILL_AUTO_PIP_ALLOWLIST = {
    x.strip().lower()
    for x in os.environ.get(
        "MAGI_SKILL_AUTO_PIP_ALLOWLIST",
        "requests,httpx,aiohttp,beautifulsoup4,lxml,pillow,python-dateutil,pytz,numpy,pandas,matplotlib,openpyxl,xlsxwriter,pyyaml,python-dotenv,feedparser,markdown,jinja2,scikit-learn",
    ).split(",")
    if x.strip()
}
SKILL_AUTO_PIP_BLOCKLIST = {
    x.strip().lower()
    for x in os.environ.get(
        "MAGI_SKILL_AUTO_PIP_BLOCKLIST",
        "",
    ).split(",")
    if x.strip()
}
SKILL_IMPORT_PACKAGE_MAP = {
    "bs4": "beautifulsoup4",
    "cv2": "opencv-python-headless",
    "dateutil": "python-dateutil",
    "dotenv": "python-dotenv",
    "PIL": "pillow",
    "yaml": "pyyaml",
    "sklearn": "scikit-learn",
}
_SKILL_AUTO_PIP_CACHE = set()
_STD_LIB_MODULES = set(getattr(sys, "stdlib_module_names", set()))
# Local MAGI modules that should NEVER be pip-installed (they exist in the project tree)
_LOCAL_MAGI_MODULES = {
    "calculators", "open_case_vision", "magi_eventlog", "laf_orchestrator",
    "casper_bridge", "melchior_bridge", "balthasar_bridge", "watcher_bridge",
    "iron_dome", "api", "skills", "casper_ecosystem",
}
if SKILL_RUNTIME_SITE_PACKAGES not in sys.path:
    sys.path.insert(0, SKILL_RUNTIME_SITE_PACKAGES)
SKILL_ALLOWED_ENV_KEYS = {
    "PATH",
    "LANG",
    "LC_ALL",
    "TZ",
    "PYTHONIOENCODING",
    "PYTHONUNBUFFERED",
    "MAGI_ROOT_DIR",
    "MAGI_ORCH_DIR",
    "MAGI_JSON_DIR",
    "MAGI_SKILL_PYTHON",
    "MAGI_CONFIG_PATH",
    "MAGI_AUTOPILOT_RUNS_DIR",
    "MAGI_METRICS_DIR",
    "MAGI_ENV_PATH",
    "MAGI_LEGACY_CODE_DIR",
    "MAGI_ENABLE_LEGACY_CODE_ROOT",
}
# Prefix-based pass-through for skill-specific env vars.
_SKILL_ALLOWED_ENV_PREFIXES = (
    "JUDICIAL_",
    "MAGI_",
    "DB_",
    "OSC_DB_",
    "INFERENCE_",
    "DISCORD_",
    "LINE_",
)
MAGI_ALLOW_INTERNET = os.environ.get("MAGI_ALLOW_INTERNET", "0").strip().lower() in {"1", "true", "yes", "on"}
SKILL_TEMPLATE = """---
name: {name}
description: {description}
author: {author}
created: {created}
---

# {title}

{instructions}

## Examples
{examples}

## Guidelines
{guidelines}

## Safety Constraints
Per MAGI Codex Article 5, this skill SHALL NOT execute destructive operations.
"""
# =============================================================================
# Iron Dome Integration (Delegated to skills.iron_dome)
# =============================================================================

try:
    from skills.iron_dome import core as iron_dome
    from skills.iron_dome import protocol_override as dome_override
except ImportError:
    # Fallback if iron_dome skill is missing/broken
    import logging
    logging.getLogger("SkillGenesis").warning("Iron Dome skill not found! Security Disabled.")
    class MockID:
        def list_patterns(self, *args, **kwargs): return {"success": False, "error": "Iron Dome Missing"}
        def add_pattern(self, *args, **kwargs): return {"success": False, "error": "Iron Dome Missing"}
        def auto_harden_scope(self, *args, **kwargs): return {"success": False, "message": "Iron Dome Missing"}
        def sanitize_input(self, text): return text
        def is_safe(self, text): return True, ""
        def get_all_patterns(self): return []
    class MockOverride:
        def request_override(self, skill_name, files, reason=""): return {"blocked": False, "message": ""}
    iron_dome = MockID()
    dome_override = MockOverride()


def list_iron_dome_patterns(include_static: bool = False, include_disabled: bool = False, limit: int = 500) -> dict:
    return iron_dome.list_patterns(include_static=include_static, include_disabled=include_disabled, limit=limit)


def add_iron_dome_pattern(pattern: str, reason: str = "", source: str = "manual", enabled: bool = True) -> dict:
    return iron_dome.add_pattern(pattern, reason=reason, source=source, enabled=enabled)


def auto_harden_iron_dome_scope(incident_text: str, source: str = "auto", max_new: int = 3) -> dict:
    return iron_dome.auto_harden_scope(incident_text, source=source, max_new=max_new)


# =============================================================================
# Safety Validator
# =============================================================================

def validate_skill_safety(content: str) -> tuple[bool, list[str]]:
    """
    Validates skill content against MAGI Codex Article 5 (via Iron Dome).
    """
    safe, msg = iron_dome.is_safe(content)
    if not safe:
        return False, [msg]
    return True, []


def _safe_write_skill_file(skill_folder: str, filename: str, content: str, reason: str = "auto_update") -> dict:
    """
    Core function for all skill file writes.
    Routes existing skill modifications through Iron Dome Protocol Override.
    Returns: {"success": bool, "error": str, "blocked": bool}
    """
    skill_dir = os.path.join(SKILLS_DIR, skill_folder)
    file_path = os.path.join(skill_dir, filename)
    
    # If the skill exists and we are rewriting it, we must trigger Protocol Override
    if os.path.exists(skill_dir) and os.path.exists(file_path):
        # Exemption: Initial generation or stubs should not trigger override, even if rewriting aborted files
        if reason not in ("generate_skill", "generate_skill_stub"):
            override_res = dome_override.request_override(
                skill_name=skill_folder,
                files={filename: content},
                reason=reason
            )
            if override_res.get("blocked"):
                return {
                    "success": False, 
                    "error": override_res.get("message", "PROTOCOL OVERRIDE BLOCKED"),
                    "blocked": True
                }
            
    # Regular write if it's a new skill or not blocked
    os.makedirs(skill_dir, exist_ok=True)
    try:
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(content)
        return {"success": True, "error": None, "blocked": False}
    except Exception as e:
        return {"success": False, "error": str(e), "blocked": False}


def _build_skill_slug(text: str, prefix: str = "generated") -> str:
    slug = re.sub(r"[^a-z0-9_-]+", "-", (text or "").lower()).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)
    if not slug:
        slug = datetime.now().strftime("%Y%m%d%H%M%S")
    return f"{prefix}-{slug[:40]}"


def _extract_python_block(text: str) -> str:
    if not text:
        return ""
    block = re.search(r"```python\s*(.*?)```", text, re.DOTALL | re.IGNORECASE)
    if block:
        return block.group(1).strip()
    block = re.search(r"```(.*?)```", text, re.DOTALL)
    if block:
        return block.group(1).strip()
    return text.strip()


def _ensure_skill_instructions(skill_content: str) -> str:
    if not skill_content:
        return skill_content
    if "python3 action.py" in skill_content.lower():
        return skill_content
    addon = (
        "\n\n## Runtime Contract\n"
        "- Execute with `python3 action.py --task \"<user request>\"`.\n"
        "- Fallback invoke: `python3 action.py \"<user request>\"`.\n"
    )
    return skill_content.rstrip() + addon


def _safe_skill_dir(skill_folder: str) -> Optional[str]:
    name = (skill_folder or "").strip()
    if not name:
        return None
    candidate = os.path.abspath(os.path.join(SKILLS_DIR, name))
    skills_root = os.path.abspath(SKILLS_DIR)
    if not candidate.startswith(skills_root + os.sep):
        return None
    return candidate


def _record_skill_event(event_type: str, skill: str = "", status: str = "info", detail: str = "", extra: Optional[dict] = None) -> None:
    try:
        os.makedirs(os.path.dirname(SKILL_EVENTS_FILE), exist_ok=True)
        payload = {
            "ts": datetime.now().isoformat(),
            "event": event_type,
            "skill": skill,
            "status": status,
            "detail": detail[:500],
            "extra": extra or {},
        }
        with open(SKILL_EVENTS_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
        # Auto-prune: keep last 50K lines when file exceeds 10MB
        try:
            if os.path.getsize(SKILL_EVENTS_FILE) > 10 * 1024 * 1024:
                with open(SKILL_EVENTS_FILE, "r", encoding="utf-8") as f:
                    lines = f.readlines()
                if len(lines) > 50000:
                    with open(SKILL_EVENTS_FILE, "w", encoding="utf-8") as f:
                        f.writelines(lines[-50000:])
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 284, exc_info=True)
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 286, exc_info=True)


def get_skill_runtime_stats(limit: int = 200) -> dict:
    if not os.path.exists(SKILL_EVENTS_FILE):
        return {"success": True, "total": 0, "by_event": {}, "by_status": {}, "recent": []}

    try:
        with open(SKILL_EVENTS_FILE, "r", encoding="utf-8") as f:
            lines = [ln.strip() for ln in f.readlines() if ln.strip()]
    except Exception as e:
        return {"success": False, "error": str(e)}

    by_event = {}
    by_status = {}
    recent = []
    for raw in lines[-limit:]:
        try:
            obj = json.loads(raw)
        except Exception:
            continue
        ev = obj.get("event", "unknown")
        st = obj.get("status", "unknown")
        by_event[ev] = by_event.get(ev, 0) + 1
        by_status[st] = by_status.get(st, 0) + 1
        recent.append(obj)

    return {
        "success": True,
        "total": len(lines),
        "window": len(recent),
        "by_event": by_event,
        "by_status": by_status,
        "recent": recent[-20:],
    }


def _track_skill_usage(skill: str, result: dict, task: str = "") -> dict:
    try:
        from skills.evolution.usage_tracker import UsageTracker
        from skills.evolution.skill_scorer import score_skill_run
        from skills.evolution.skill_improver import build_improvement_plan

        tracker = UsageTracker(SKILL_USAGE_TRACKER_FILE)
        success = bool(result.get("success"))
        failure_reason = str(result.get("error") or result.get("stderr") or "").strip()
        trace = result.get("trace") or []
        latency_ms = 0
        if isinstance(trace, list):
            for item in reversed(trace):
                if isinstance(item, dict) and item.get("duration_ms") is not None:
                    try:
                        latency_ms = int(item.get("duration_ms") or 0)
                    except Exception:
                        latency_ms = 0
                    break

        event = tracker.record(
            skill=skill,
            success=success,
            latency_ms=latency_ms,
            intent=str(task or "").strip()[:120],
            failure_reason=failure_reason[:160],
            auto_repaired=bool(result.get("auto_repaired")),
        )
        scored = score_skill_run(
            {
                "skill": skill,
                "success": success,
                "latency_ms": latency_ms,
                "auto_repaired": bool(result.get("auto_repaired")),
            }
        )
        summary = tracker.summarize(days=7)
        plan = build_improvement_plan(skill, summary)
        return {
            "event": event,
            "score": scored,
            "summary_7d": summary,
            "improvement_plan": plan,
        }
    except Exception as exc:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 327, exc_info=True)
        return {"error": str(exc)[:160]}


def _build_skill_exec_env() -> dict:
    env = {}
    for k in SKILL_ALLOWED_ENV_KEYS:
        if k in os.environ:
            env[k] = os.environ[k]

    # Prefix-based pass-through (e.g. JUDICIAL_*, MAGI_*, DB_*)
    for k, v in os.environ.items():
        if k not in env and any(k.startswith(p) for p in _SKILL_ALLOWED_ENV_PREFIXES):
            env[k] = v

    # Allow explicit pass-through for needed external APIs.
    allow = os.environ.get("MAGI_SKILL_ALLOW_ENV", "")
    for key in [x.strip() for x in allow.split(",") if x.strip()]:
        if key in os.environ:
            env[key] = os.environ[key]

    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUNBUFFERED", "1")
    existing_pythonpath = os.environ.get("PYTHONPATH", "")
    # Ensure skills can import MAGI + code modules without auto_pip mis-detecting them as missing deps.
    # This is important for /skills/run and OpenClaw cron execution stability.
    try:
        magi_root = os.path.realpath(str(get_magi_root_dir()))
    except Exception:
        magi_root = str(_MAGI_ROOT)

    pythonpath_parts = [SKILL_RUNTIME_SITE_PACKAGES]
    for p in (magi_root,):
        if p and os.path.exists(p):
            pythonpath_parts.append(p)
    if legacy_code_enabled():
        code_root = str(get_legacy_code_root())
        if code_root and os.path.exists(code_root):
            pythonpath_parts.append(code_root)
    if existing_pythonpath:
        pythonpath_parts.append(existing_pythonpath)
    env["PYTHONPATH"] = ":".join(pythonpath_parts)
    return env


def _skill_preexec():
    if os.name == "nt":
        return None
    if not SKILL_ENABLE_PREEXEC:
        return None
    if threading.active_count() > 1:
        # preexec_fn is unsafe in multithreaded runtimes (for example Flask/Tools API)
        # and can fail with "Exception occurred in preexec_fn" before exec.
        return None
    try:
        import resource

        def _fn():
            try:
                resource.setrlimit(resource.RLIMIT_CPU, (SKILL_EXEC_CPU_SEC, SKILL_EXEC_CPU_SEC + 2))
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 374, exc_info=True)
            try:
                mem_bytes = SKILL_EXEC_MEM_MB * 1024 * 1024
                resource.setrlimit(resource.RLIMIT_AS, (mem_bytes, mem_bytes))
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 379, exc_info=True)
        return _fn
    except Exception:
        return None


def _isolated_run(cmd: list[str], cwd: str, timeout_sec: int):
    start = time.time()
    proc = subprocess.run(
        cmd,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout_sec,
        env=_build_skill_exec_env(),
        preexec_fn=_skill_preexec(),
    )
    return {
        "rc": proc.returncode,
        "stdout": (proc.stdout or "").strip(),
        "stderr": (proc.stderr or "").strip(),
        "duration_ms": int((time.time() - start) * 1000),
    }


def _skill_cmd(*args: str) -> list[str]:
    python_bin = SKILL_PYTHON if (SKILL_PYTHON and os.path.exists(SKILL_PYTHON)) else (sys.executable or "python3")
    return [python_bin, "action.py", *args]


def _extract_missing_modules(error_text: str) -> list[str]:
    text = error_text or ""
    found = []
    patterns = [
        r"No module named ['\"]([a-zA-Z0-9_.-]+)['\"]",
        r"ModuleNotFoundError:\s*No module named ['\"]([a-zA-Z0-9_.-]+)['\"]",
        r"ImportError:\s*No module named ['\"]?([a-zA-Z0-9_.-]+)['\"]?",
    ]
    for pattern in patterns:
        for module_name in re.findall(pattern, text):
            if module_name:
                found.append(module_name.split(".")[0])
    dedup = []
    seen = set()
    for item in found:
        key = item.strip()
        if key and key not in seen:
            dedup.append(key)
            seen.add(key)
    return dedup


def _parse_action_imports(action_path: str) -> list[str]:
    if not action_path or not os.path.exists(action_path):
        return []
    try:
        with open(action_path, "r", encoding="utf-8") as f:
            source = f.read()
        root = ast.parse(source, filename=action_path)
    except Exception:
        return []

    imports = []
    for node in ast.walk(root):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias and alias.name:
                    imports.append(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imports.append(node.module.split(".")[0])

    dedup = []
    seen = set()
    for item in imports:
        if item and item not in seen:
            dedup.append(item)
            seen.add(item)
    return dedup


def _module_to_package(module_name: str) -> str:
    key = (module_name or "").strip()
    if not key:
        return ""
    pkg = SKILL_IMPORT_PACKAGE_MAP.get(key, SKILL_IMPORT_PACKAGE_MAP.get(key.lower(), key.lower()))
    return pkg.strip()


def _module_available(module_name: str) -> bool:
    name = (module_name or "").strip()
    if not name:
        return True
    try:
        return importlib.util.find_spec(name) is not None
    except Exception:
        return False


def _auto_pip_allowed(package_name: str) -> bool:
    name = (package_name or "").strip().lower()
    if not name:
        return False
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", name):
        return False
    if name in SKILL_AUTO_PIP_BLOCKLIST:
        return False
    if SKILL_AUTO_PIP_ALLOW_ANY:
        return True
    return name in SKILL_AUTO_PIP_ALLOWLIST


def _pip_install_package(package_name: str) -> dict:
    pkg = (package_name or "").strip()
    if not pkg:
        return {"success": False, "error": "empty package name"}

    base_cmd = [
        sys.executable or "python3",
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "--no-input",
        pkg,
    ]
    os.makedirs(SKILL_RUNTIME_SITE_PACKAGES, exist_ok=True)
    attempts = [
        ("default", base_cmd),
        ("break_system", base_cmd + ["--break-system-packages"]),
        ("user", base_cmd + ["--user"]),
        ("target", base_cmd + ["--target", SKILL_RUNTIME_SITE_PACKAGES]),
    ]
    last_result = None
    for mode, cmd in attempts:
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=SKILL_AUTO_PIP_TIMEOUT_SEC,
                env=os.environ.copy(),
            )
            payload = {
                "success": result.returncode == 0,
                "mode": mode,
                "cmd": " ".join(cmd),
                "rc": result.returncode,
                "stdout": (result.stdout or "").strip()[:1200],
                "stderr": (result.stderr or "").strip()[:1000],
            }
            if payload["success"]:
                return payload
            last_result = payload
        except Exception as e:
            last_result = {"success": False, "mode": mode, "error": str(e), "cmd": " ".join(cmd)}
    return last_result or {"success": False, "error": "unknown pip install failure"}


def _ensure_skill_runtime_dependencies(
    skill_dir: str,
    stderr_text: str = "",
    force_scan: bool = False,
    max_packages: int = SKILL_AUTO_PIP_MAX_PACKAGES,
) -> dict:
    if not SKILL_AUTO_PIP_ENABLED:
        return {"success": True, "installed": [], "skipped": [], "errors": [], "reason": "auto_pip_disabled"}

    modules = _extract_missing_modules(stderr_text)
    action_path = os.path.join(skill_dir, "action.py")
    if force_scan or not modules:
        modules.extend(_parse_action_imports(action_path))

    candidates = []
    seen = set()
    for mod in modules:
        root = (mod or "").split(".")[0].strip()
        if not root:
            continue
        if root in _STD_LIB_MODULES:
            continue
        if root in _LOCAL_MAGI_MODULES:
            continue
        if root in seen:
            continue
        seen.add(root)
        candidates.append(root)

    installed = []
    skipped = []
    errors = []
    installs_done = 0
    for module_name in candidates:
        package_name = _module_to_package(module_name)
        if not package_name:
            skipped.append({"module": module_name, "reason": "empty_package"})
            continue
        cache_key = package_name.lower()
        if cache_key in _SKILL_AUTO_PIP_CACHE:
            continue
        if _module_available(module_name):
            _SKILL_AUTO_PIP_CACHE.add(cache_key)
            continue
        if not _auto_pip_allowed(package_name):
            skipped.append({"module": module_name, "package": package_name, "reason": "not_allowed"})
            continue
        if installs_done >= int(max(1, max_packages)):
            skipped.append({"module": module_name, "package": package_name, "reason": "max_packages_reached"})
            continue

        install_result = _pip_install_package(package_name)
        if install_result.get("success"):
            installs_done += 1
            _SKILL_AUTO_PIP_CACHE.add(cache_key)
            installed.append({"module": module_name, "package": package_name})
            _record_skill_event("dependency_install", os.path.basename(skill_dir.rstrip(os.sep)), "ok", package_name)
        else:
            errors.append(
                {
                    "module": module_name,
                    "package": package_name,
                    "error": install_result.get("error") or install_result.get("stderr", "install failed"),
                }
            )
            _record_skill_event(
                "dependency_install",
                os.path.basename(skill_dir.rstrip(os.sep)),
                "error",
                f"{package_name}: {install_result.get('error') or install_result.get('stderr', 'install failed')}",
            )

    return {
        "success": len(errors) == 0,
        "installed": installed,
        "skipped": skipped,
        "errors": errors,
    }


def _snapshot_skill_version(skill_dir: str, reason: str = "") -> dict:
    """
    Save a restore point for SKILL.md/action.py before mutation.
    """
    try:
        skill_folder = os.path.basename(skill_dir.rstrip(os.sep))
        version_id = datetime.now().strftime("%Y%m%d%H%M%S%f")
        version_dir = os.path.join(SKILL_VERSIONS_DIR, skill_folder, version_id)
        os.makedirs(version_dir, exist_ok=True)

        copied = []
        for file_name in ("SKILL.md", "action.py"):
            src = os.path.join(skill_dir, file_name)
            if os.path.exists(src):
                shutil.copy2(src, os.path.join(version_dir, file_name))
                copied.append(file_name)

        meta = {
            "skill": skill_folder,
            "version_id": version_id,
            "timestamp": datetime.now().isoformat(),
            "reason": reason or "snapshot",
            "files": copied,
        }
        with open(os.path.join(version_dir, "meta.json"), "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)

        result = {"success": True, "version_id": version_id, "path": version_dir, "files": copied}
        _record_skill_event("snapshot", skill_folder, "ok", reason or "snapshot", {"version_id": version_id, "files": copied})
        return result
    except Exception as e:
        _record_skill_event("snapshot", os.path.basename(skill_dir.rstrip(os.sep)), "error", str(e))
        return {"success": False, "error": str(e)}


def list_skill_versions(skill_folder: str) -> dict:
    root = os.path.join(SKILL_VERSIONS_DIR, (skill_folder or "").strip())
    if not os.path.isdir(root):
        return {"success": False, "versions": [], "error": "No versions found"}
    versions = []
    for item in sorted(os.listdir(root), reverse=True):
        version_dir = os.path.join(root, item)
        if not os.path.isdir(version_dir):
            continue
        meta_path = os.path.join(version_dir, "meta.json")
        meta = {"version_id": item}
        if os.path.exists(meta_path):
            try:
                with open(meta_path, "r", encoding="utf-8") as f:
                    obj = json.load(f)
                    if isinstance(obj, dict):
                        meta.update(obj)
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 666, exc_info=True)
        versions.append(meta)
    return {"success": True, "versions": versions, "error": None}


def rollback_skill_version(skill_folder: str, version_id: str = "") -> dict:
    """
    Restore SKILL.md/action.py from a saved version snapshot.
    """
    skill_folder = (skill_folder or "").strip()
    if not skill_folder:
        _record_skill_event("rollback", "", "error", "missing skill folder")
        return {"success": False, "error": "Missing skill folder"}

    skill_dir = _safe_skill_dir(skill_folder)
    if not skill_dir:
        _record_skill_event("rollback", skill_folder, "error", "invalid skill folder path")
        return {"success": False, "error": "Invalid skill folder path"}
    os.makedirs(skill_dir, exist_ok=True)

    root = os.path.join(SKILL_VERSIONS_DIR, skill_folder)
    if not os.path.isdir(root):
        _record_skill_event("rollback", skill_folder, "error", "no versions found")
        return {"success": False, "error": "No versions found for this skill"}

    chosen = (version_id or "").strip()
    if not chosen:
        candidates = [x for x in os.listdir(root) if os.path.isdir(os.path.join(root, x))]
        if not candidates:
            return {"success": False, "error": "No versions found for this skill"}
        chosen = sorted(candidates)[-1]

    version_dir = os.path.join(root, chosen)
    if not os.path.isdir(version_dir):
        _record_skill_event("rollback", skill_folder, "error", f"version not found: {chosen}")
        return {"success": False, "error": f"Version '{chosen}' not found"}

    pre = _snapshot_skill_version(skill_dir, reason=f"pre_rollback_to_{chosen}")
    restored = []
    for file_name in ("SKILL.md", "action.py"):
        src = os.path.join(version_dir, file_name)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(skill_dir, file_name))
            restored.append(file_name)

    if not restored:
        _record_skill_event("rollback", skill_folder, "error", f"version has no files: {chosen}")
        return {"success": False, "error": f"Version '{chosen}' has no restorable files"}

    result = {
        "success": True,
        "skill_folder": skill_folder,
        "restored_version": chosen,
        "restored_files": restored,
        "pre_rollback_snapshot": pre if pre.get("success") else None,
    }
    _record_skill_event("rollback", skill_folder, "ok", f"restored {chosen}", {"files": restored})
    return result


def _version_dir(skill_folder: str, version_id: str) -> str:
    return os.path.join(SKILL_VERSIONS_DIR, skill_folder, version_id)


def _release_state_path(skill_folder: str) -> str:
    return os.path.join(SKILL_VERSIONS_DIR, skill_folder, "release_state.json")


def _load_release_state(skill_folder: str) -> dict:
    default_state = {
        "skill": skill_folder,
        "stable_version": "",
        "canary_active": False,
        "canary_version": "",
        "canary_percent": 0,
        "min_runs": 10,
        "fail_threshold": 3,
        "max_failure_rate": 0.5,
        "stats": {
            "runs": 0,
            "success": 0,
            "fail": 0,
            "consecutive_fail": 0,
        },
        "auto_disabled": False,
        "last_update": "",
        "enforce_stable": False,
        "auto_promote": True,
        "promote_min_runs": 10,
        "promote_max_failure_rate": 0.2,
        "auto_promoted": False,
        "last_promoted_version": "",
        "last_promoted_at": "",
    }
    path = _release_state_path(skill_folder)
    if not os.path.exists(path):
        return default_state
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):
                merged = dict(default_state)
                merged.update(data)
                stats = dict(default_state.get("stats", {}))
                raw_stats = data.get("stats", {})
                if isinstance(raw_stats, dict):
                    stats.update(raw_stats)
                merged["stats"] = stats
                return merged
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 776, exc_info=True)
    return default_state


def _save_release_state(skill_folder: str, state: dict) -> dict:
    try:
        root = os.path.join(SKILL_VERSIONS_DIR, skill_folder)
        os.makedirs(root, exist_ok=True)
        state["skill"] = skill_folder
        state["last_update"] = datetime.now().isoformat()
        with open(_release_state_path(skill_folder), "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        return {"success": True, "state": state}
    except Exception as e:
        return {"success": False, "error": str(e)}


def set_stable_skill_version(skill_folder: str, version_id: str = "", enforce: bool = True) -> dict:
    """
    Mark a version as stable. If version_id is empty, snapshot current live skill and mark it stable.
    """
    skill_folder = (skill_folder or "").strip()
    skill_dir = _safe_skill_dir(skill_folder)
    if not skill_dir:
        return {"success": False, "error": "Invalid skill folder"}
    if not os.path.isdir(skill_dir):
        return {"success": False, "error": "Skill folder not found"}

    chosen = (version_id or "").strip()
    if not chosen:
        snap = _snapshot_skill_version(skill_dir, reason="mark_stable")
        if not snap.get("success"):
            return {"success": False, "error": f"Snapshot failed: {snap.get('error')}"}
        chosen = snap.get("version_id", "")

    if not os.path.isdir(_version_dir(skill_folder, chosen)):
        return {"success": False, "error": f"Stable version '{chosen}' not found"}

    state = _load_release_state(skill_folder)
    state["stable_version"] = chosen
    state["enforce_stable"] = bool(enforce)
    save = _save_release_state(skill_folder, state)
    if save.get("success"):
        _record_skill_event("release_stable", skill_folder, "ok", f"stable={chosen}", {"enforce": enforce})
        return {"success": True, "skill": skill_folder, "stable_version": chosen, "state": state}
    return {"success": False, "error": save.get("error", "save failed")}


def start_canary_release(
    skill_folder: str,
    version_id: str,
    canary_percent: int = 10,
    min_runs: int = 10,
    fail_threshold: int = 3,
    max_failure_rate: float = 0.5,
    auto_promote: bool = True,
    promote_min_runs: Optional[int] = None,
    promote_max_failure_rate: Optional[float] = None,
) -> dict:
    skill_folder = (skill_folder or "").strip()
    version_id = (version_id or "").strip()
    if not skill_folder or not version_id:
        return {"success": False, "error": "Missing skill or version_id"}
    if canary_percent < 1 or canary_percent > 100:
        return {"success": False, "error": "canary_percent must be 1..100"}

    skill_dir = _safe_skill_dir(skill_folder)
    if not skill_dir or not os.path.isdir(skill_dir):
        return {"success": False, "error": "Skill folder not found"}

    target_dir = _version_dir(skill_folder, version_id)
    if not os.path.isdir(target_dir):
        return {"success": False, "error": f"Canary version '{version_id}' not found"}
    if not os.path.exists(os.path.join(target_dir, "action.py")):
        return {"success": False, "error": f"Canary version '{version_id}' missing action.py"}

    state = _load_release_state(skill_folder)
    if not state.get("stable_version"):
        stable = set_stable_skill_version(skill_folder, version_id="", enforce=True)
        if not stable.get("success"):
            return {"success": False, "error": f"Failed to set baseline stable version: {stable.get('error')}"}
        state = _load_release_state(skill_folder)

    state["canary_active"] = True
    state["canary_version"] = version_id
    state["canary_percent"] = int(canary_percent)
    state["min_runs"] = int(max(1, min_runs))
    state["fail_threshold"] = int(max(1, fail_threshold))
    state["max_failure_rate"] = float(max(0.0, min(1.0, max_failure_rate)))
    state["auto_promote"] = bool(auto_promote)
    state["promote_min_runs"] = int(max(1, promote_min_runs if promote_min_runs is not None else min_runs))
    promoted_fail_rate = promote_max_failure_rate if promote_max_failure_rate is not None else min(max_failure_rate, 0.2)
    state["promote_max_failure_rate"] = float(max(0.0, min(1.0, promoted_fail_rate)))
    state["stats"] = {"runs": 0, "success": 0, "fail": 0, "consecutive_fail": 0}
    state["auto_disabled"] = False
    state["auto_promoted"] = False
    state["last_promoted_version"] = ""
    state["last_promoted_at"] = ""

    save = _save_release_state(skill_folder, state)
    if save.get("success"):
        _record_skill_event(
            "release_canary_start",
            skill_folder,
            "ok",
            f"canary={version_id}",
            {
                "percent": canary_percent,
                "min_runs": min_runs,
                "fail_threshold": fail_threshold,
                "max_failure_rate": max_failure_rate,
                "auto_promote": auto_promote,
                "promote_min_runs": state["promote_min_runs"],
                "promote_max_failure_rate": state["promote_max_failure_rate"],
            },
        )
        return {"success": True, "state": state}
    return {"success": False, "error": save.get("error", "save failed")}


def stop_canary_release(skill_folder: str, reason: str = "manual_stop") -> dict:
    skill_folder = (skill_folder or "").strip()
    if not skill_folder:
        return {"success": False, "error": "Missing skill folder"}
    state = _load_release_state(skill_folder)
    state["canary_active"] = False
    state["auto_disabled"] = reason == "auto_disable"
    save = _save_release_state(skill_folder, state)
    if save.get("success"):
        _record_skill_event("release_canary_stop", skill_folder, "ok", reason)
        return {"success": True, "state": state}
    return {"success": False, "error": save.get("error", "save failed")}


def get_skill_release_state(skill_folder: str) -> dict:
    skill_folder = (skill_folder or "").strip()
    if not skill_folder:
        return {"success": False, "error": "Missing skill folder"}
    return {"success": True, "state": _load_release_state(skill_folder)}


def _choose_canary_bucket(route_key: str, canary_percent: int) -> bool:
    pct = int(max(0, min(100, canary_percent)))
    if pct <= 0:
        return False
    if pct >= 100:
        return True
    key = (route_key or "").strip()
    if not key:
        return random.random() < (pct / 100.0)
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    bucket = int(digest[:8], 16) % 100
    return bucket < pct


def _resolve_run_target(skill: str, route_key: str = "", force_non_canary: bool = False) -> dict:
    skill_dir = _safe_skill_dir(skill)
    if not skill_dir or not os.path.isdir(skill_dir):
        return {"success": False, "error": "Skill folder not found"}

    state = _load_release_state(skill)
    canary_active = bool(state.get("canary_active"))
    canary_version = state.get("canary_version", "")
    stable_version = state.get("stable_version", "")
    canary_percent = int(state.get("canary_percent", 0) or 0)

    canary_dir = _version_dir(skill, canary_version) if canary_version else ""
    stable_dir = _version_dir(skill, stable_version) if stable_version else ""

    if (not force_non_canary) and canary_active and canary_version and os.path.exists(os.path.join(canary_dir, "action.py")):
        if _choose_canary_bucket(route_key, canary_percent):
            return {
                "success": True,
                "channel": "canary",
                "skill_dir": canary_dir,
                "version_id": canary_version,
                "state": state,
            }
        if stable_version and os.path.exists(os.path.join(stable_dir, "action.py")):
            return {
                "success": True,
                "channel": "stable",
                "skill_dir": stable_dir,
                "version_id": stable_version,
                "state": state,
            }

    if state.get("enforce_stable") and stable_version and os.path.exists(os.path.join(stable_dir, "action.py")):
        return {
            "success": True,
            "channel": "stable",
            "skill_dir": stable_dir,
            "version_id": stable_version,
            "state": state,
        }

    return {
        "success": True,
        "channel": "live",
        "skill_dir": skill_dir,
        "version_id": "",
        "state": state,
    }


def _update_canary_outcome(skill: str, state: dict, success: bool, detail: str = "") -> dict:
    if not state.get("canary_active"):
        return {"state": state, "auto_disabled": False, "auto_promoted": False}
    stats = state.setdefault("stats", {"runs": 0, "success": 0, "fail": 0, "consecutive_fail": 0})
    stats["runs"] = int(stats.get("runs", 0)) + 1
    if success:
        stats["success"] = int(stats.get("success", 0)) + 1
        stats["consecutive_fail"] = 0
    else:
        stats["fail"] = int(stats.get("fail", 0)) + 1
        stats["consecutive_fail"] = int(stats.get("consecutive_fail", 0)) + 1

    runs = max(1, int(stats.get("runs", 0)))
    fail = int(stats.get("fail", 0))
    fail_rate = fail / runs
    min_runs = int(state.get("min_runs", 10) or 10)
    fail_threshold = int(state.get("fail_threshold", 3) or 3)
    max_failure_rate = float(state.get("max_failure_rate", 0.5) or 0.5)

    auto_disabled = False
    auto_promoted = False
    promoted_version = ""
    reason = ""
    if int(stats.get("consecutive_fail", 0)) >= fail_threshold:
        auto_disabled = True
        reason = f"consecutive_fail={stats.get('consecutive_fail')}"
    elif runs >= min_runs and fail_rate > max_failure_rate:
        auto_disabled = True
        reason = f"failure_rate={fail_rate:.2f}>{max_failure_rate:.2f}"

    if auto_disabled:
        state["canary_active"] = False
        state["auto_disabled"] = True
        state["auto_promoted"] = False
        _record_skill_event("release_canary_auto_disabled", skill, "error", reason, {"runs": runs, "fail": fail, "fail_rate": fail_rate, "detail": detail[:240]})
    elif state.get("auto_promote") and state.get("canary_version"):
        promote_min_runs = int(state.get("promote_min_runs", min_runs) or min_runs)
        promote_max_failure_rate = float(state.get("promote_max_failure_rate", min(max_failure_rate, 0.2)) or min(max_failure_rate, 0.2))
        if runs >= promote_min_runs and fail_rate <= promote_max_failure_rate:
            promoted_version = state.get("canary_version", "")
            state["stable_version"] = promoted_version
            state["canary_active"] = False
            state["auto_disabled"] = False
            state["enforce_stable"] = True
            state["auto_promoted"] = True
            state["last_promoted_version"] = promoted_version
            state["last_promoted_at"] = datetime.now().isoformat()
            auto_promoted = True
            _record_skill_event(
                "release_canary_promoted",
                skill,
                "ok",
                f"promoted={promoted_version}",
                {
                    "runs": runs,
                    "success": int(stats.get("success", 0)),
                    "fail": fail,
                    "fail_rate": round(fail_rate, 4),
                    "threshold": promote_max_failure_rate,
                },
            )
        else:
            state["auto_promoted"] = False

    _save_release_state(skill, state)
    return {
        "state": state,
        "auto_disabled": auto_disabled,
        "auto_promoted": auto_promoted,
        "promoted_version": promoted_version,
    }


def _repair_action_code_with_llm(
    current_code: str,
    error_message: str,
    need_description: str,
    round_index: int,
) -> Optional[str]:
    prompt = f"""Fix the Python script so it passes syntax validation and follows this runtime contract:
1) Support `python3 action.py --task "<text>"`.
2) Also support `python3 action.py "<text>"`.
3) Print useful result to stdout.
4) Keep it safe: no destructive operations.

Need:
{need_description}

Error:
{error_message}

Current code:
```python
{current_code}
```

Return ONLY corrected Python code.
"""
    candidates = [
        (
            OMLX_HOST + "/v1/chat/completions",
            {
                "model": os.environ.get("MAGI_MAIN_MODEL", ""),
                "messages": [
                    {"role": "system", "content": "You are a precise Python debugger."},
                    {"role": "user", "content": prompt},
                ],
                "stream": False,
                "temperature": 0.0,
                "max_tokens": 4096,
            },
            ("choices", 0, "message", "content"),
        ),
        (
            f"{MELCHIOR_BASE}/api/generate",
            {
                "model": get_available_melchior_model(),
                "prompt": prompt,
                "stream": False,
                "options": {"temperature": 0.0, "num_ctx": 8192},
            },
            ("response",),
        ),
    ]

    for url, payload, path in candidates:
        try:
            resp = requests.post(url, json=payload, timeout=120)
            if resp.status_code != 200:
                continue
            data = resp.json()
            text = data
            for key in path:
                text = text[key] if isinstance(key, str) else text[key]
            fixed = _extract_python_block(text)
            if not fixed:
                continue
            is_safe, violations = validate_skill_safety(fixed)
            if not is_safe:
                continue
            return fixed
        except Exception:
            continue
    return None


def _validate_and_debug_action_code(action_code: str, need_description: str, max_rounds: int = MAX_DEBUG_ROUNDS) -> dict:
    if not action_code:
        return {"success": True, "code": "", "debug_log": [], "error": None}

    candidate = action_code
    debug_log = []

    safe, violations = validate_skill_safety(candidate)
    if not safe:
        return {"success": False, "code": "", "debug_log": debug_log, "error": f"IRON DOME BLOCKED CODE: {violations}"}

    for idx in range(max_rounds + 1):
        try:
            compile(candidate, "<generated_action>", "exec")
            return {"success": True, "code": candidate, "debug_log": debug_log, "error": None}
        except SyntaxError as e:
            err = f"{e.msg} (line {e.lineno}, col {e.offset})"
            debug_log.append(err)
            if idx >= max_rounds:
                return {"success": False, "code": "", "debug_log": debug_log, "error": f"Syntax check failed: {err}"}
            repaired = _repair_action_code_with_llm(candidate, err, need_description, idx + 1)
            if not repaired:
                return {"success": False, "code": "", "debug_log": debug_log, "error": f"Auto-debug failed: {err}"}
            candidate = repaired
        except Exception as e:
            return {"success": False, "code": "", "debug_log": debug_log, "error": f"Compile check failed: {e}"}

    return {"success": False, "code": "", "debug_log": debug_log, "error": "Unknown validation failure"}


def _auto_runtime_repair_action(skill_dir: str, need_description: str, max_rounds: int = MAX_RUNTIME_REPAIR_ROUNDS) -> dict:
    """
    Runtime self-healing loop:
    1) run smoke test
    2) if failed, use stderr to repair code
    3) validate safety + syntax, repeat
    """
    action_path = os.path.join(skill_dir, "action.py")
    if not os.path.exists(action_path):
        return {"success": True, "repaired": False, "smoke": None, "rounds": 0, "logs": []}

    logs = []
    try:
        with open(action_path, "r", encoding="utf-8") as f:
            current = f.read()
    except Exception as e:
        return {"success": False, "repaired": False, "smoke": None, "rounds": 0, "logs": [str(e)], "error": str(e)}

    repaired = False
    for idx in range(max_rounds + 1):
        smoke = _smoke_test_action(skill_dir)
        if smoke.get("success"):
            return {
                "success": True,
                "repaired": repaired,
                "smoke": smoke,
                "rounds": idx,
                "logs": logs,
            }

        stderr = smoke.get("stderr") or "unknown runtime error"
        logs.append(stderr)
        if idx >= max_rounds:
            return {
                "success": False,
                "repaired": repaired,
                "smoke": smoke,
                "rounds": idx,
                "logs": logs,
                "error": f"Runtime auto-repair exhausted: {stderr}",
            }

        fixed = _repair_action_code_with_llm(
            current_code=current,
            error_message=f"Runtime failure: {stderr}",
            need_description=need_description,
            round_index=idx + 1,
        )
        if not fixed:
            return {
                "success": False,
                "repaired": repaired,
                "smoke": smoke,
                "rounds": idx,
                "logs": logs,
                "error": f"Runtime repair failed to produce code: {stderr}",
            }

        safe, violations = validate_skill_safety(fixed)
        if not safe:
            return {
                "success": False,
                "repaired": repaired,
                "smoke": smoke,
                "rounds": idx,
                "logs": logs,
                "error": f"IRON DOME BLOCKED RUNTIME PATCH: {violations}",
            }

        verify = _validate_and_debug_action_code(fixed, need_description, max_rounds=0)
        if not verify.get("success"):
            return {
                "success": False,
                "repaired": repaired,
                "smoke": smoke,
                "rounds": idx,
                "logs": logs,
                "error": f"Patched code invalid: {verify.get('error')}",
            }

        with open(action_path, "w", encoding="utf-8") as f:
            f.write(verify["code"])
        current = verify["code"]
        repaired = True

    return {"success": False, "repaired": repaired, "smoke": None, "rounds": max_rounds, "logs": logs, "error": "Unknown runtime repair failure"}


def _smoke_test_action(skill_dir: str, timeout_sec: int = 15, auto_install_deps: bool = True) -> dict:
    action_path = os.path.join(skill_dir, "action.py")
    if not os.path.exists(action_path):
        return {"success": False, "command": "", "stdout": "", "stderr": "action.py not found"}

    dep_bootstrap = {"success": True, "installed": []}
    if auto_install_deps:
        dep_bootstrap = _ensure_skill_runtime_dependencies(skill_dir, force_scan=True)

    commands = [
        _skill_cmd("--help"),
        _skill_cmd("--task", "self test"),
        _skill_cmd("self test"),
    ]
    last_err = ""
    for cmd in commands:
        try:
            r = _isolated_run(cmd, skill_dir, timeout_sec)
            if r["rc"] == 0:
                return {
                    "success": True,
                    "command": " ".join(cmd),
                    "stdout": r["stdout"][:800],
                    "stderr": r["stderr"][:800],
                    "dep_bootstrap": dep_bootstrap,
                }
            if auto_install_deps:
                dep_fix = _ensure_skill_runtime_dependencies(skill_dir, stderr_text=f"{r['stderr']}\n{r['stdout']}")
                if dep_fix.get("installed"):
                    rerun = _isolated_run(cmd, skill_dir, timeout_sec)
                    if rerun["rc"] == 0:
                        return {
                            "success": True,
                            "command": " ".join(cmd),
                            "stdout": rerun["stdout"][:800],
                            "stderr": rerun["stderr"][:800],
                            "dep_bootstrap": dep_bootstrap,
                            "dep_fix": dep_fix,
                        }
            last_err = f"{' '.join(cmd)} -> rc={r['rc']}, stderr={r['stderr'][:200]}"
        except Exception as e:
            last_err = f"{' '.join(cmd)} -> {e}"
    return {"success": False, "command": "", "stdout": "", "stderr": last_err, "dep_bootstrap": dep_bootstrap}


def _register_skill_tool_definition(skill_folder: str, description: str) -> dict:
    tool_name = f"run_{re.sub(r'[^a-z0-9_]+', '_', skill_folder.lower())}"
    try:
        payload = {"_meta": {"version": "1.0.0", "description": "MAGI Skill Definitions for OpenClaw Integration"}, "tools": []}
        if os.path.exists(DEFINITIONS_PATH):
            with open(DEFINITIONS_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    payload.update(data)
        tools = payload.setdefault("tools", [])
        target = {
            "name": tool_name,
            "description": description[:220] or f"Run generated skill {skill_folder}",
            "endpoint": "/skills/run",
            "method": "POST",
            "sage": "casper",
            "iron_dome": True,
            "parameters": {
                "type": "object",
                "properties": {
                    "skill": {
                        "type": "string",
                        "default": skill_folder,
                        "enum": [skill_folder],
                        "description": f"Skill folder name (default: {skill_folder})",
                    },
                    "task": {"type": "string", "description": "Task text to pass into action.py"},
                },
                "required": ["task", "skill"],
            },
        }

        updated = False
        replaced = False
        for idx, t in enumerate(tools):
            if isinstance(t, dict) and t.get("name") == tool_name:
                tools[idx] = target
                updated = True
                replaced = True
                break
        if not replaced:
            tools.append(target)
            updated = True

        payload.setdefault("_meta", {})["updated"] = datetime.now().strftime("%Y-%m-%d")
        with open(DEFINITIONS_PATH, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=4)
        return {"success": True, "name": tool_name, "updated": updated}
    except Exception as e:
        return {"success": False, "name": tool_name, "updated": False, "error": str(e)}


# =============================================================================
# Skill Generator (CASPER)
# =============================================================================

def generate_skill(
    name: str,
    description: str,
    instructions: str,
    examples: str = "- Example usage 1\n- Example usage 2",
    guidelines: str = "- Follow best practices\n- Document all changes",
    author: str = "CASPER"
) -> dict:
    """
    Generates a new SKILL.md file.
    
    Args:
        name: Skill identifier (lowercase, hyphens)
        description: What the skill does
        instructions: Main instructions for Claude
        examples: Example usages
        guidelines: Guidelines to follow
        author: Who created it (CASPER or MELCHIOR)
    
    Returns:
        {"success": bool, "path": str, "error": str}
    """
    # Sanitize name
    safe_name = re.sub(r'[^a-z0-9_-]', '-', name.lower())
    
    skill_content = SKILL_TEMPLATE.format(
        name=safe_name,
        description=description,
        author=author,
        created=datetime.now().strftime("%Y-%m-%d"),
        title=name.replace("-", " ").title(),
        instructions=instructions,
        examples=examples,
        guidelines=guidelines
    )
    
    # === IRON DOME CHECK ===
    is_safe, violations = validate_skill_safety(skill_content)
    if not is_safe:
        return {
            "success": False,
            "path": None,
            "error": f"IRON DOME BLOCKED: {violations}"
        }
    
    # Create skill directory
    skill_dir = os.path.join(SKILLS_DIR, safe_name)
    os.makedirs(skill_dir, exist_ok=True)
    
    skill_path = os.path.join(skill_dir, "SKILL.md")
    
    try:
        res = _safe_write_skill_file(safe_name, "SKILL.md", skill_content, reason="generate_skill")
        if res.get("blocked") or not res.get("success"):
            return {
                "success": False,
                "path": None,
                "error": res.get("error")
            }

        # ★ 同時產生最小可執行 action.py stub，避免 CI/run 失敗
        action_stub = (
            '#!/usr/bin/env python3\n'
            '"""Auto-generated stub for skill: {name}"""\n'
            'import argparse, json, sys\n'
            '\n'
            'def main():\n'
            '    parser = argparse.ArgumentParser(description="{desc}")\n'
            '    parser.add_argument("--task", default="help", help="Task to execute")\n'
            '    args = parser.parse_args()\n'
            '    result = {{"success": True, "message": "Skill stub for: {name}. Task: " + args.task,\n'
            '               "note": "This is a template stub. Please enhance with real logic."}}\n'
            '    print(json.dumps(result, ensure_ascii=False))\n'
            '\n'
            'if __name__ == "__main__":\n'
            '    main()\n'
        ).format(name=safe_name, desc=description.replace('"', '\\"')[:120])
        action_res = _safe_write_skill_file(safe_name, "action.py", action_stub, reason="generate_skill_stub")
        if action_res.get("blocked"):
            import logging
            logging.getLogger("SkillGenesis").warning(f"Iron Dome blocked action.py stub for {safe_name}: {action_res.get('error')}")

        return {
            "success": True,
            "path": skill_path,
            "error": None
        }
    except Exception as e:
        return {
            "success": False,
            "path": None,
            "error": str(e)
        }


# =============================================================================
# Web Research (Fetch Skills from GitHub)
# =============================================================================

def fetch_skill_from_url(url: str) -> dict:
    """
    Fetches a SKILL.md from a URL (e.g., GitHub raw content).
    
    Args:
        url: URL to the SKILL.md file
    
    Returns:
        {"success": bool, "content": str, "error": str}
    """
    if not MAGI_ALLOW_INTERNET:
        return {"success": False, "content": None, "error": "Internet disabled (MAGI_ALLOW_INTERNET=0)"}
    try:
        # Convert GitHub blob URL to raw URL if needed
        if "github.com" in url and "/blob/" in url:
            url = url.replace("github.com", "raw.githubusercontent.com").replace("/blob/", "/")
        
        response = requests.get(url, timeout=15)
        response.raise_for_status()
        
        content = response.text
        
        # === IRON DOME CHECK ===
        is_safe, violations = validate_skill_safety(content)
        if not is_safe:
            return {
                "success": False,
                "content": None,
                "error": f"IRON DOME BLOCKED: Fetched skill contains forbidden patterns: {violations}"
            }
        
        return {
            "success": True,
            "content": content,
            "error": None
        }
    except Exception as e:
        return {
            "success": False,
            "content": None,
            "error": str(e)
        }


def install_skill_from_url(url: str, custom_name: str = None, require_hitl: bool = True) -> dict:
    """
    Downloads and installs a skill from a URL.
    
    Args:
        url: URL to the SKILL.md file
        custom_name: Optional custom name for the skill folder
        require_hitl: Whether to require Human-in-The-Loop approval before installing
    
    Returns:
        {"success": bool, "path": str, "error": str}
    """
    if not MAGI_ALLOW_INTERNET:
        return {"success": False, "path": None, "error": "Internet disabled (MAGI_ALLOW_INTERNET=0)"}
        
    if require_hitl:
        return {
            "success": False, 
            "path": None, 
            "error": "IRON DOME HITL APPROVAL REQUIRED: Automated script pulling from external sources is restricted to prevent malicious execution. Please review the script manually."
        }
        
    result = fetch_skill_from_url(url)
    
    if not result["success"]:
        return result
    
    content = result["content"]
    
    # Extract name from frontmatter or use custom name
    name_match = re.search(r'^name:\s*(.+)$', content, re.MULTILINE)
    if name_match:
        name = name_match.group(1).strip()
    elif custom_name:
        name = custom_name
    else:
        name = "imported-skill-" + datetime.now().strftime("%Y%m%d%H%M%S")
    
    safe_name = re.sub(r'[^a-z0-9_-]', '-', name.lower())
    
    # Create skill directory
    skill_dir = os.path.join(SKILLS_DIR, safe_name)
    os.makedirs(skill_dir, exist_ok=True)
    
    skill_path = os.path.join(skill_dir, "SKILL.md")
    
    try:
        res = _safe_write_skill_file(safe_name, "SKILL.md", content, reason="install_from_url")
        if res.get("blocked") or not res.get("success"):
            return {
                "success": False,
                "path": None,
                "error": res.get("error")
            }
        
        return {
            "success": True,
            "path": skill_path,
            "error": None
        }
    except Exception as e:
        return {
            "success": False,
            "path": None,
            "error": str(e)
        }


# =============================================================================
# GitHub Discovery (Automated Skill Search)
# =============================================================================

SKILL_SOURCES = [
    "https://api.github.com/repos/openai/skills/contents/skills/.curated",
    "https://api.github.com/repos/anthropics/skills/contents",
]

def search_github_skills(query: str, max_results: int = 10) -> dict:
    """Search GitHub for AgentSkills repositories."""
    if not MAGI_ALLOW_INTERNET:
        return {"success": False, "skills": [], "error": "Internet disabled (MAGI_ALLOW_INTERNET=0)"}
    skills_found = []
    try:
        search_url = "https://api.github.com/search/code"
        params = {"q": f"filename:SKILL.md {query}", "per_page": max_results}
        response = requests.get(search_url, params=params, 
                               headers={"Accept": "application/vnd.github.v3+json"}, timeout=15)
        if response.status_code == 200:
            for item in response.json().get("items", []):
                html_url = item.get("html_url", "")
                skills_found.append({
                    "repo": item.get("repository", {}).get("full_name", ""),
                    "path": item.get("path", ""),
                    "url": html_url,
                    "raw_url": html_url.replace("github.com", "raw.githubusercontent.com").replace("/blob/", "/")
                })
        return {"success": True, "skills": skills_found, "error": None}
    except Exception as e:
        return {"success": False, "skills": [], "error": str(e)}


def discover_from_openai_skills(category: str = ".curated") -> dict:
    """Discover skills from the OpenAI skills repository."""
    if not MAGI_ALLOW_INTERNET:
        return {"success": False, "skills": [], "error": "Internet disabled (MAGI_ALLOW_INTERNET=0)"}
    skills_found = []
    try:
        url = f"https://api.github.com/repos/openai/skills/contents/skills/{category}"
        response = requests.get(url, headers={"Accept": "application/vnd.github.v3+json"}, timeout=15)
        if response.status_code == 200:
            for item in response.json():
                if item.get("type") == "dir":
                    name = item.get("name", "")
                    skills_found.append({
                        "name": name,
                        "repo": "openai/skills",
                        "url": f"https://github.com/openai/skills/blob/main/skills/{category}/{name}/SKILL.md",
                        "raw_url": f"https://raw.githubusercontent.com/openai/skills/main/skills/{category}/{name}/SKILL.md"
                    })
        return {"success": True, "skills": skills_found, "error": None}
    except Exception as e:
        return {"success": False, "skills": [], "error": str(e)}


def auto_discover_and_suggest(need_description: str) -> dict:
    """Search for skills matching a need. Returns suggestions with safety status."""
    if not MAGI_ALLOW_INTERNET:
        return {"success": True, "suggestions": [], "error": "Internet disabled (MAGI_ALLOW_INTERNET=0)"}
    keywords = need_description.lower().replace("i need to", "").replace("i want to", "").strip()
    search_result = search_github_skills(keywords, max_results=5)
    suggestions = []
    
    if search_result["success"]:
        for skill in search_result["skills"]:
            if skill.get("raw_url"):
                preview = fetch_skill_from_url(skill["raw_url"])
                if preview["success"]:
                    desc_match = re.search(r'^description:\s*(.+)$', preview["content"], re.MULTILINE)
                    suggestions.append({
                        "name": skill.get("path", "").split("/")[-2] if "/" in skill.get("path", "") else "unknown",
                        "description": desc_match.group(1)[:150] if desc_match else "No description",
                        "repo": skill.get("repo"),
                        "install_url": skill.get("raw_url"),
                        "safe": True
                    })
    return {"success": True, "suggestions": suggestions[:5], "error": None}


def auto_install_skill(skill_name_or_url: str) -> dict:
    """Install skill by name (searches known sources) or URL."""
    if not MAGI_ALLOW_INTERNET:
        return {"success": False, "path": None, "error": "Internet disabled (MAGI_ALLOW_INTERNET=0)"}
    if skill_name_or_url.startswith("http"):
        return install_skill_from_url(skill_name_or_url)
    
    # Search OpenAI skills
    result = discover_from_openai_skills(".curated")
    if result["success"]:
        for skill in result["skills"]:
            if skill_name_or_url.lower() in skill["name"].lower():
                return install_skill_from_url(skill["raw_url"], require_hitl=True)
    
    return {"success": False, "path": None, "error": f"Skill '{skill_name_or_url}' not found"}


# =============================================================================
# Melchior Fallback (For Complex Generation)
# =============================================================================

try:
    from api.routing.service_registry import get_service_url as _get_svc_url
    OMLX_HOST = _get_svc_url("omlx_inference")
except Exception:
    OMLX_HOST = "http://127.0.0.1:8080"
try:
    from api.routing.node_registry import get_node_ip as _get_node_ip
    _melchior_host = os.environ.get("MELCHIOR_HOST") or _get_node_ip("melchior") or "127.0.0.1"
except Exception:
    _melchior_host = os.environ.get("MELCHIOR_HOST", "127.0.0.1")
MELCHIOR_PORT = os.environ.get("MELCHIOR_PORT", "5002").strip() or "5002"
MELCHIOR_BASE = (
    _melchior_host
    if str(_melchior_host).startswith(("http://", "https://"))
    else f"http://{_melchior_host}:{MELCHIOR_PORT}"
)
PREFERRED_MODELS = [os.environ.get("MAGI_MAIN_MODEL", ""), os.environ.get("MAGI_TEXT_PRIMARY_MODEL", ""), os.environ.get("MAGI_OMLX_CODE_MODEL", "")]

def get_available_melchior_model(preferred: list = PREFERRED_MODELS) -> str:
    """Get an available model from oMLX."""
    try:
        response = requests.get(f"{OMLX_HOST}/v1/models", timeout=5)
        if response.status_code == 200:
            available = [m.get("id", "") for m in response.json().get("data", [])]
            for model in preferred:
                for avail in available:
                    if model.lower() in avail.lower():
                        return avail
        return preferred[0] if preferred else os.environ.get("MAGI_MAIN_MODEL", "")
    except Exception:
        return preferred[0] if preferred else os.environ.get("MAGI_MAIN_MODEL", "")


def request_local_skill_generation(prompt: str, model: str = os.environ.get("MAGI_MAIN_MODEL", "")) -> dict:
    """
    Uses CASPER's Local Compute (Mac M4) to generate skills.
    This is preferred when Casper is in 'Local Mode' (Engineer Priority).
    """
    try:
        OMLX_CHAT_URL = f"{OMLX_HOST}/v1/chat/completions"

        system_prompt = (
            "You are CASPER, the Chief Engineer of the MAGI system.\n"
            "Generate a SKILL.md file and an executable Python script (action.py) for the following request.\n\n"
            "Requirements:\n"
            "1. **SKILL.md**: Follow Anthropic Skills format (YAML frontmatter). Instructions MUST reference `python3 action.py` for execution.\n"
            "2. **action.py**: A complete, standalone Python script to perform the task.\n"
            "   - Must be safe (NO destructive commands).\n"
            "   - Use standard libraries or `requests`, `pip install` only if necessary (but prefer standard).\n"
            "   - For image generation, use `requests` to call OpenAI API or local Stable Diffusion if applicable.\n"
            "   - For image generation, SAVE the image to current directory and PRINT the filename.\n\n"
            "Output Format:\n"
            "Please output the content in two blocks:\n\n"
            "--- SKILL_START ---\n"
            "(Content of SKILL.md)\n"
            "--- SKILL_END ---\n\n"
            "--- PROG_START ---\n"
            "(Content of action.py)\n"
            "--- PROG_END ---"
        )

        response = requests.post(
            OMLX_CHAT_URL,
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": prompt},
                ],
                "stream": False,
                "temperature": 0.2,
                "max_tokens": 4096,
            },
            timeout=180
        )
        response.raise_for_status()

        choices = response.json().get("choices") or []
        full_response = (choices[0].get("message") or {}).get("content", "") if choices else ""
        return parse_skill_response(full_response, model_used=model)

    except Exception as e:
        return {"success": False, "error": str(e)}

def parse_skill_response(full_response: str, model_used: str) -> dict:
    """Helper to parse the LLM response for SKILL and PROG blocks."""
    # Parse SKILL.md
    skill_match = re.search(r'--- SKILL_START ---\n?(.*?)\n?--- SKILL_END ---', full_response, re.DOTALL)
    if not skill_match:
        skill_match = re.search(r'(^---.+?---.+)', full_response, re.DOTALL | re.MULTILINE)
        
    skill_content = skill_match.group(1).strip() if skill_match else full_response
    
    # Parse action.py
    action_code = None
    prog_match = re.search(r'--- PROG_START ---\n?(.*?)\n?--- PROG_END ---', full_response, re.DOTALL)
    if prog_match:
        action_code = prog_match.group(1).strip()
        if action_code.startswith("```python"):
            action_code = action_code.replace("```python", "", 1)
        if action_code.startswith("```"):
            action_code = action_code.replace("```", "", 1)
        if action_code.endswith("```"):
            action_code = action_code[:-3]
        action_code = action_code.strip()
    else:
        code_match = re.search(r'```python\n?(.*?)\n?```', full_response, re.DOTALL)
        if code_match:
            action_code = code_match.group(1).strip()

    # === IRON DOME CHECK ===
    is_safe, violations = validate_skill_safety(skill_content)
    if not is_safe:
        return {
            "success": False,
            "error": f"IRON DOME BLOCKED SKILL: {violations}"
        }
        
    if action_code:
        is_safe_code, code_violations = validate_skill_safety(action_code)
        if not is_safe_code:
            return {
                "success": False,
                "error": f"IRON DOME BLOCKED CODE: {code_violations}"
            }
    
    return {
        "success": True,
        "content": skill_content,
        "action_code": action_code,
        "model_used": model_used,
        "error": None
    }

def request_melchior_skill_generation(prompt: str, model: str = None) -> dict:
    """
    Falls back to MELCHIOR for complex skill generation.
    Will auto-select available model or pull if needed.
    """
    try:
        # Auto-select available model
        use_model = model if model else get_available_melchior_model()
        
        response = requests.post(
            f"{MELCHIOR_BASE}/api/generate",
            json={
                "model": use_model,
                "prompt": f"""You are MELCHIOR, the Scientist of the MAGI system.
Generate a SKILL.md file and an executable Python script (action.py) for the following request:

{prompt}

Requirements:
1. **SKILL.md**: Follow Anthropic Skills format (YAML frontmatter). Instructions MUST reference `python3 action.py` for execution.
2. **action.py**: A complete, standalone Python script to perform the task.
   - Must be safe (NO destructive commands).
   - Use standard libraries or `requests`, `pip install` only if necessary (but prefer standard).
   - For image generation, use `requests` to call OpenAI API or local Stable Diffusion if applicable.
   - For image generation, SAVE the image to current directory and PRINT the filename.

Output Format:
Please output the content in two blocks:

--- SKILL_START ---
(Content of SKILL.md)
--- SKILL_END ---

--- PROG_START ---
(Content of action.py)
--- PROG_END ---
""",
                "stream": False,
                "options": {
                    "num_ctx": 8192
                }
            },
            timeout=180
        )
        response.raise_for_status()
        
        full_response = response.json().get("response", "")
        return parse_skill_response(full_response, model_used=use_model)

    except Exception as e:
        return {"success": False, "error": str(e)}



# =============================================================================
# List Installed Skills
# =============================================================================

def list_skills() -> list[dict]:
    """
    Lists all installed skills.
    
    Returns:
        List of skill info dicts
    """
    skills = []
    
    if not os.path.exists(SKILLS_DIR):
        return skills
    
    for item in os.listdir(SKILLS_DIR):
        skill_path = os.path.join(SKILLS_DIR, item, "SKILL.md")
        if os.path.exists(skill_path):
            try:
                with open(skill_path, 'r') as f:
                    content = f.read()
                
                # Extract metadata
                name_match = re.search(r'^name:\s*(.+)$', content, re.MULTILINE)
                desc_match = re.search(r'^description:\s*(.+)$', content, re.MULTILINE)
                
                skills.append({
                    "folder": item,
                    "name": name_match.group(1).strip() if name_match else item,
                    "description": desc_match.group(1).strip() if desc_match else "No description",
                    "path": skill_path
                })
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 1865, exc_info=True)
    
    return skills


# =============================================================================
# Model Safety & Prerequisites
# =============================================================================

# Known safe models (High efficiency, low VRAM)
SAFE_MODELS = [
    "mistral-nemo:12b",
    os.environ.get("MAGI_MAIN_MODEL", ""),
    "gemma2:9b",
    "qwen2.5-coder:7b",
    "phi3.5:3.8b",
    "gemma-3-12b-it-4bit",
    "deepseek-r1:14b",
    os.environ.get("MAGI_MAIN_MODEL", "")
]

# Explicitly blocked heavy models
BLOCKED_MODELS = [
    "command-r-plus:104b",
    "gemma2:27b",  # Too heavy for some tasks
    "mixtral:8x22b"
]

def check_model_safety(model_name: str) -> tuple[bool, str]:
    """
    Check if a model is safe to run on Melchior (RTX 3060 12GB).
    
    Args:
        model_name: Name of the model
    
    Returns:
        (is_safe, reason)
    """
    model = model_name.lower().strip()
    
    # 1. Check Block List
    for blocked in BLOCKED_MODELS:
        if blocked in model:
            return False, f"Model '{model_name}' is in the BLOCKED list (Too heavy)"
            
    # 2. Check Allow List (Fast pass)
    for safe in SAFE_MODELS:
        if safe in model:
            return True, "Model is in the SAFE list"
            
    # 3. Heuristic Check (Parameter size)
    # Extract size suffix like :70b, :27b
    size_match = re.search(r':(\d+)b', model)
    if size_match:
        size = int(size_match.group(1))
        if size > 20:
             return False, f"Model '{model_name}' parameter size ({size}B) exceeds safety limit (20B)"
             
    return True, "Model passed heuristic safety check"


def validate_prerequisites(skill_content: str) -> dict:
    """
    Check skill prerequisites (e.g., Required Model) and auto-pull if needed.
    
    Returns:
        {"success": bool, "error": str}
    """
    # Parse for "Required Model: <name>" or "Dependencies: <name>"
    # Support YAML frontmatter or Markdown body
    patterns = [
        r'Required Model:\s*([a-zA-Z0-9_:.-]+)',
        r'Dependencies:\s*.*model\s*=\s*([a-zA-Z0-9_:.-]+)',
        r'model["\']?\s*:\s*["\']([a-zA-Z0-9_:.-]+)["\']'
    ]
    
    required_models = []
    
    for pattern in patterns:
        matches = re.findall(pattern, skill_content, re.IGNORECASE)
        required_models.extend(matches)
        
    # Remove duplicates
    required_models = list(set(required_models))
    
    if not required_models:
        return {"success": True, "error": None}
        
    import logging
    logger = logging.getLogger("SkillAcquisition")
        
    for model in required_models:
        # 1. Safety Check
        is_safe, reason = check_model_safety(model)
        if not is_safe:
            return {
                "success": False, 
                "error": f"Prerequisite Check Failed: {reason}"
            }
            
        # 2. Availability Check & Auto-Pull
        logger.info(f"Checking prerequisite model: {model}")
        available_model = get_available_melchior_model([model])
        
        # If get_available_melchior_model returns the model, it means it's available OR it tried to pull it
        # But we need to be sure it actually exists now
        try:
            # Double check existence
            response = requests.get(f"{MELCHIOR_BASE}/api/tags", timeout=5)
            if response.status_code == 200:
                available = [m["name"] for m in response.json().get("models", [])]
                if model not in available:
                    # Try explicit pull one last time
                    logger.info(f"Auto-pulling required model: {model}")
                    pull_result = pull_melchior_model(model)
                    if not pull_result["success"]:
                        return {
                            "success": False,
                            "error": f"Failed to auto-pull required model '{model}': {pull_result.get('error')}"
                        }
            else:
                return {"success": False, "error": "Could not contact Melchior to verify models"}
        except Exception as e:
            return {"success": False, "error": str(e)}
            
    return {"success": True, "error": None}


def request_distributed_skill_generation(prompt: str) -> dict:
    """
    Uses the Distributed 70B Model (Casper + Melchior) for skill generation.
    This provides the highest intelligence.
    """
    try:
        # Distributed model is hosted on Casper (oMLX) which connects to Melchior via RPC
        CASPER_URL = OMLX_HOST + "/v1/chat/completions"
        
        # We use chat completions for the big model as it might be an instruction-tuned model (e.g., GLM-4, Llama-3-70B-Instruct)
        response = requests.post(
            CASPER_URL,
            json={
                "model": os.environ.get("MAGI_MAIN_MODEL", ""), # Model name is often just a placeholder for llama-server, but we set it anyway
                "messages": [
                    {"role": "system", "content": "You are the MAGI System's Primary Intelligence (Casper + Melchior)."},
                    {"role": "user", "content": f"""Generate a SKILL.md file and an executable Python script (action.py) for the following request:

{prompt}

Requirements:
1. **SKILL.md**: Follow Anthropic Skills format (YAML frontmatter). Instructions MUST reference `python3 action.py` for execution.
2. **action.py**: A complete, standalone Python script to perform the task.
   - Must be safe (NO destructive commands).
   - Use standard libraries or `requests`, `pip install` only if necessary (but prefer standard).
   - For image generation, use `requests` to call OpenAI API or local Stable Diffusion if applicable.
   - For image generation, SAVE the image to current directory and PRINT the filename.

Output Format:
Please output the content in two blocks:

--- SKILL_START ---
(Content of SKILL.md)
--- SKILL_END ---

--- PROG_START ---
(Content of action.py)
--- PROG_END ---
"""}
                ],
                "stream": False,
                "temperature": 0.1,
                "max_tokens": 8192
            },
            timeout=180
        )
        response.raise_for_status()
        
        full_response = response.json()['choices'][0]['message']['content']
        return parse_skill_response(full_response, model_used="Distributed 70B")

    except Exception as e:
        return {"success": False, "error": str(e)}


def run_skill_action(
    skill: str,
    task: str,
    timeout_sec: int = 30,
    auto_repair: bool = True,
    rollback_on_fail: bool = True,
    auto_install_deps: bool = True,
    route_key: str = "",
) -> dict:
    """
    Execute generated skill action.py with safe invocation contract.
    """
    skill = (skill or "").strip()
    task = (task or "").strip()
    if not skill:
        return {"success": False, "error": "Missing skill folder name"}

    timeout_sec = int(timeout_sec or SKILL_EXEC_TIMEOUT_SEC)
    route = _resolve_run_target(skill, route_key=route_key)
    if not route.get("success"):
        _record_skill_event("run", skill, "error", route.get("error", "route resolution failed"))
        return {"success": False, "error": route.get("error", "route resolution failed")}

    skill_dir = route["skill_dir"]
    channel = route.get("channel", "live")
    version_id = route.get("version_id", "")
    release_state = route.get("state", {})

    action_path = os.path.join(skill_dir, "action.py")
    if not os.path.exists(action_path):
        _record_skill_event("run", skill, "error", f"action.py not found ({channel}:{version_id})")
        return {"success": False, "error": f"action.py not found in routed target ({channel})"}

    def _attempt():
        task_arg = task or "help"
        # Most skills accept `--task "<task string>"`.
        # Some older skills accept a positional `<task string>`.
        # We must NOT treat `--help` output as a successful execution for non-help tasks,
        # otherwise callers will get a false-positive "success" (and often non-JSON output).
        commands = [
            _skill_cmd("--task", task_arg),
            _skill_cmd(task_arg),
        ]
        if task_arg in {"help", "--help", "-h"}:
            commands.append(_skill_cmd("--help"))

        try:
            stdout_cap = int(os.environ.get("MAGI_SKILL_STDOUT_MAX_CHARS", "20000") or "20000")
        except Exception:
            stdout_cap = 20000
        stdout_cap = max(1200, min(stdout_cap, 200000))

        try:
            stderr_cap = int(os.environ.get("MAGI_SKILL_STDERR_MAX_CHARS", "8000") or "8000")
        except Exception:
            stderr_cap = 8000
        stderr_cap = max(300, min(stderr_cap, 60000))
        traces = []
        if auto_install_deps:
            dep_bootstrap = _ensure_skill_runtime_dependencies(skill_dir, force_scan=True)
            if dep_bootstrap.get("installed"):
                traces.append(
                    {
                        "cmd": "auto_pip bootstrap",
                        "rc": 0,
                        "stdout": "",
                        "stderr": "",
                        "installed": dep_bootstrap.get("installed", []),
                    }
                )
            if dep_bootstrap.get("errors"):
                traces.append(
                    {
                        "cmd": "auto_pip bootstrap",
                        "rc": 1,
                        "stdout": "",
                        "stderr": str(dep_bootstrap.get("errors", []))[:300],
                    }
                )
        for cmd in commands:
            try:
                r = _isolated_run(cmd, skill_dir, timeout_sec)
                traces.append({"cmd": " ".join(cmd), "rc": r["rc"], "stdout": r["stdout"][:600], "stderr": r["stderr"][:240], "duration_ms": r["duration_ms"]})
                if r["rc"] == 0:
                    return {
                        "success": True,
                        "skill": skill,
                        "channel": channel,
                        "version_id": version_id,
                        "command": " ".join(cmd),
                        "output": r["stdout"][:stdout_cap],
                        "stderr": r["stderr"][:stderr_cap],
                        "trace": traces,
                    }
                if auto_install_deps:
                    dep_fix = _ensure_skill_runtime_dependencies(skill_dir, stderr_text=f"{r['stderr']}\n{r['stdout']}")
                    if dep_fix.get("installed"):
                        traces.append(
                            {
                                "cmd": "auto_pip on_error",
                                "rc": 0,
                                "stdout": "",
                                "stderr": "",
                                "installed": dep_fix.get("installed", []),
                            }
                        )
                        rerun = _isolated_run(cmd, skill_dir, timeout_sec)
                        traces.append({"cmd": " ".join(cmd) + " [retry]", "rc": rerun["rc"], "stdout": rerun["stdout"][:600], "stderr": rerun["stderr"][:240], "duration_ms": rerun["duration_ms"]})
                        if rerun["rc"] == 0:
                            return {
                                "success": True,
                                "skill": skill,
                                "channel": channel,
                                "version_id": version_id,
                                "command": " ".join(cmd),
                                "output": rerun["stdout"][:stdout_cap],
                                "stderr": rerun["stderr"][:stderr_cap],
                                "trace": traces,
                            }
                    if dep_fix.get("errors"):
                        traces.append(
                            {
                                "cmd": "auto_pip on_error",
                                "rc": 1,
                                "stdout": "",
                                "stderr": str(dep_fix.get("errors", []))[:300],
                            }
                        )
            except Exception as e:
                traces.append({"cmd": " ".join(cmd), "rc": -1, "stdout": "", "stderr": str(e)})
        return {"success": False, "skill": skill, "error": "Action execution failed", "trace": traces}

    first = _attempt()
    if channel == "canary":
        outcome = _update_canary_outcome(skill, release_state, bool(first.get("success")), first.get("error", ""))
        if outcome.get("auto_promoted"):
            first["canary_auto_promoted"] = True
            first["promoted_version"] = outcome.get("promoted_version")
        if first.get("success"):
            first["usage_tracking"] = _track_skill_usage(skill, first, task=task)
            _record_skill_event("run", skill, "ok", f"canary success:{version_id}")
            return first
        _record_skill_event("run", skill, "error", f"canary failed:{version_id}")
        # fallback immediately to stable/live path to preserve service continuity
        fallback_route = _resolve_run_target(skill, route_key="__stable_fallback__", force_non_canary=True)
        if fallback_route.get("success"):
            fallback_dir = fallback_route["skill_dir"]
            fallback_channel = fallback_route.get("channel", "live")
            fallback_version = fallback_route.get("version_id", "")
            if os.path.exists(os.path.join(fallback_dir, "action.py")):
                original_dir, original_channel, original_version = skill_dir, channel, version_id
                skill_dir, channel, version_id = fallback_dir, fallback_channel, fallback_version
                fallback = _attempt()
                skill_dir, channel, version_id = original_dir, original_channel, original_version
                fallback["canary_fallback"] = True
                fallback["canary_result"] = first
                fallback["usage_tracking"] = _track_skill_usage(skill, fallback, task=task)
                _record_skill_event("run", skill, "ok" if fallback.get("success") else "error", f"fallback after canary failure -> {fallback_channel}:{fallback_version}")
                return fallback
        first["usage_tracking"] = _track_skill_usage(skill, first, task=task)
        return first

    if first.get("success") or not auto_repair:
        first["usage_tracking"] = _track_skill_usage(skill, first, task=task)
        _record_skill_event("run", skill, "ok" if first.get("success") else "error", first.get("error", first.get("command", "")))
        return first

    snapshot = _snapshot_skill_version(skill_dir, reason="pre_runtime_auto_repair")
    _record_skill_event("run_auto_repair", skill, "info", "runtime failure detected, trying auto repair")
    repair = _auto_runtime_repair_action(skill_dir, task or skill, max_rounds=MAX_RUNTIME_REPAIR_ROUNDS)
    if repair.get("success"):
        second = _attempt()
        second["auto_repaired"] = True
        second["repair"] = repair
        second["usage_tracking"] = _track_skill_usage(skill, second, task=task)
        _record_skill_event("run_auto_repair", skill, "ok" if second.get("success") else "error", "auto repair applied")
        return second

    rollback_result = None
    if rollback_on_fail and snapshot.get("success"):
        rollback_result = rollback_skill_version(skill, snapshot.get("version_id", ""))
        _record_skill_event("rollback", skill, "ok" if bool(rollback_result and rollback_result.get("success")) else "error", "rollback after runtime repair failure")

    result = {
        "success": False,
        "skill": skill,
        "error": first.get("error", "Action execution failed"),
        "trace": first.get("trace", []),
        "auto_repair": repair,
        "rollback": rollback_result,
    }
    result["usage_tracking"] = _track_skill_usage(skill, result, task=task)
    _record_skill_event("run", skill, "error", result["error"], {"auto_repair": repair.get("error") if isinstance(repair, dict) else str(repair)})
    return result


def run_skill_ci(skill: str, task: str = "self test", attempt_repair: bool = False) -> dict:
    """
    CI check for a skill package:
    - path safety
    - safety scan
    - syntax compile
    - smoke run
    """
    skill = (skill or "").strip()
    skill_dir = _safe_skill_dir(skill)
    if not skill_dir:
        return {"success": False, "error": "Invalid skill path"}
    if not os.path.isdir(skill_dir):
        return {"success": False, "error": "Skill folder not found"}

    skill_md = os.path.join(skill_dir, "SKILL.md")
    action_py = os.path.join(skill_dir, "action.py")
    checks = []

    if os.path.exists(skill_md):
        try:
            with open(skill_md, "r", encoding="utf-8") as f:
                content = f.read()
            safe, violations = validate_skill_safety(content)
            checks.append({"check": "skill_safety", "ok": safe, "detail": "" if safe else "; ".join(violations[:4])})
        except Exception as e:
            checks.append({"check": "skill_safety", "ok": False, "detail": str(e)})
    else:
        checks.append({"check": "skill_exists", "ok": False, "detail": "SKILL.md missing"})

    if os.path.exists(action_py):
        try:
            with open(action_py, "r", encoding="utf-8") as f:
                code = f.read()
            safe_code, violations_code = validate_skill_safety(code)
            checks.append({"check": "action_safety", "ok": safe_code, "detail": "" if safe_code else "; ".join(violations_code[:4])})
            verify = _validate_and_debug_action_code(code, task, max_rounds=0)
            checks.append({"check": "action_compile", "ok": verify.get("success", False), "detail": verify.get("error", "")})
            if attempt_repair and not verify.get("success"):
                repaired = _validate_and_debug_action_code(code, task, max_rounds=MAX_DEBUG_ROUNDS)
                if repaired.get("success"):
                    res = _safe_write_skill_file(skill, "action.py", repaired.get("code", code), reason="ci_auto_repair")
                    if res.get("blocked"):
                        checks.append({"check": "action_repair_blocked", "ok": False, "detail": res.get("error")})
                checks.append({"check": "action_repair", "ok": repaired.get("success", False), "detail": repaired.get("error", "")})
        except Exception as e:
            checks.append({"check": "action_compile", "ok": False, "detail": str(e)})
    else:
        checks.append({"check": "action_exists", "ok": False, "detail": "action.py missing"})

    smoke = _smoke_test_action(skill_dir, timeout_sec=min(SKILL_EXEC_TIMEOUT_SEC, 20), auto_install_deps=True)
    checks.append({"check": "smoke", "ok": bool(smoke.get("success")), "detail": smoke.get("stderr", "")[:240]})

    ok = all(c.get("ok") for c in checks if c.get("check") not in {"action_exists"})
    _record_skill_event("ci", skill, "ok" if ok else "error", "skill ci run", {"checks": checks, "smoke": smoke.get("command", "")})
    return {
        "success": ok,
        "skill": skill,
        "checks": checks,
        "smoke": smoke,
    }

# =============================================================================
# Unified Skill Acquisition Pipeline (完整技能獲取流程)
# =============================================================================

def acquire_skill(need_description: str, auto_generate: bool = True, auto_activate: bool = True) -> dict:
    """
    Complete skill acquisition pipeline:
    1. Search GitHub for matching skills
    2. Download and analyze with Iron Dome
    3. Install if safe
    4. If nothing found, generate with BEST AVAILABLE BRAIN
    """
    import logging
    logger = logging.getLogger("SkillAcquisition")
    
    need_description = (need_description or "").strip()
    _record_skill_event("acquire", "", "info", f"need={need_description[:120]}")
    if not need_description:
        result = {
            "success": False,
            "action": "failed",
            "skill_path": None,
            "message": "❌ need_description is empty",
        }
        _record_skill_event("acquire", "", "error", result["message"])
        return result

    logger.info(f"🔍 Step 1: Searching GitHub for: {need_description}")
    
    # === Step 1: Search GitHub ===
    search_result = {"success": False, "skills": [], "error": "skipped"}
    if MAGI_ALLOW_INTERNET:
        search_result = search_github_skills(need_description, max_results=5)
    candidates = []
    
    if search_result["success"] and search_result["skills"]:
        logger.info(f"Found {len(search_result['skills'])} candidates on GitHub")
        
        for skill in search_result["skills"]:
            if skill.get("raw_url"):
                # === Step 2: Download and Analyze ===
                logger.info(f"📥 Step 2: Downloading from {skill['repo']}")
                fetch_result = fetch_skill_from_url(skill["raw_url"])
                
                if fetch_result["success"]:
                    content = fetch_result["content"]
                    
                    # Iron Dome check passed in fetch_skill_from_url
                    # Now check Model Prerequisites
                    prereq_result = validate_prerequisites(content)
                    
                    if prereq_result["success"]:
                        candidates.append({
                            "repo": skill["repo"],
                            "url": skill["raw_url"],
                            "content": content,
                            "safe": True
                        })
                        logger.info(f"✅ Safety & Prerequisite checks passed for {skill['repo']}")
                    else:
                        logger.warning(f"⚠️ Rejected {skill['repo']}: {prereq_result['error']}")
                else:
                    logger.warning(f"⚠️ Rejected {skill['repo']}: {fetch_result['error']}")
    
    # Also check OpenAI curated skills
    openai_result = {"success": False, "skills": [], "error": "skipped"}
    if MAGI_ALLOW_INTERNET:
        openai_result = discover_from_openai_skills(".curated")
    if openai_result["success"]:
        keywords = need_description.lower().split()
        for skill in openai_result["skills"]:
            if any(kw in skill["name"].lower() for kw in keywords):
                fetch_result = fetch_skill_from_url(skill["raw_url"])
                if fetch_result["success"]:
                    content = fetch_result["content"]
                    prereq_result = validate_prerequisites(content)
                    
                    if prereq_result["success"]:
                        candidates.append({
                            "repo": "openai/skills",
                            "name": skill["name"],
                            "url": skill["raw_url"],
                            "content": content,
                            "safe": True
                        })
    
    # === Step 3: Install Best Candidate ===
    if candidates:
        best = candidates[0]  # First safe candidate
        logger.info(f"📦 Step 3: Installing skill from {best.get('repo', 'unknown')}")
        
        install_result = install_skill_from_url(best["url"])
        if install_result["success"]:
            skill_path = install_result["path"]
            skill_dir = os.path.dirname(skill_path)
            skill_folder = os.path.basename(skill_dir)
            action_path = os.path.join(skill_dir, "action.py")
            activation = {"compiled": None, "smoke": None, "definition": None, "runtime_repair": None, "snapshot": None}
            snapshot = _snapshot_skill_version(skill_dir, reason="post_install_pre_activation")
            if snapshot.get("success"):
                activation["snapshot"] = snapshot

            if os.path.exists(action_path):
                try:
                    with open(action_path, "r", encoding="utf-8") as f:
                        action_code = f.read()
                    verify = _validate_and_debug_action_code(action_code, need_description)
                    if not verify["success"]:
                        result = {
                            "success": False,
                            "action": "failed_validation",
                            "skill_path": skill_path,
                            "skill_folder": skill_folder,
                            "message": f"❌ 已下載技能但 action.py 驗證失敗: {verify.get('error')}",
                            "debug_log": verify.get("debug_log", []),
                        }
                        _record_skill_event("acquire", skill_folder, "error", result["message"])
                        return result
                    if verify["code"] != action_code:
                        res = _safe_write_skill_file(skill_folder, "action.py", verify["code"], reason="acquire_validation_repair")
                        if res.get("blocked"):
                            return {"success": False, "message": f"IRON DOME BLOCKED: {res.get('error')}"}
                    activation["compiled"] = True
                    runtime = _auto_runtime_repair_action(skill_dir, need_description, max_rounds=MAX_RUNTIME_REPAIR_ROUNDS)
                    activation["runtime_repair"] = runtime
                    activation["smoke"] = runtime.get("smoke")
                    if not runtime.get("success"):
                        rollback_result = None
                        if snapshot.get("success"):
                            rollback_result = rollback_skill_version(skill_folder, snapshot.get("version_id", ""))
                        result = {
                            "success": False,
                            "action": "failed_runtime_validation",
                            "skill_path": skill_path,
                            "skill_folder": skill_folder,
                            "message": f"❌ 安裝技能後執行驗證失敗: {runtime.get('error')}",
                            "activation": activation,
                            "rollback": rollback_result,
                        }
                        _record_skill_event("acquire", skill_folder, "error", result["message"])
                        return result
                except Exception as e:
                    rollback_result = None
                    if snapshot.get("success"):
                        rollback_result = rollback_skill_version(skill_folder, snapshot.get("version_id", ""))
                    result = {
                        "success": False,
                        "action": "failed_validation",
                        "skill_path": skill_path,
                        "skill_folder": skill_folder,
                        "message": f"❌ action.py 驗證流程失敗: {e}",
                        "rollback": rollback_result,
                    }
                    _record_skill_event("acquire", skill_folder, "error", result["message"])
                    return result

            if auto_activate:
                desc = f"Run generated/imported skill {skill_folder}"
                activation["definition"] = _register_skill_tool_definition(skill_folder, desc)

            result = {
                "success": True,
                "action": "installed_from_github",
                "skill_path": skill_path,
                "skill_folder": skill_folder,
                "action_path": action_path if os.path.exists(action_path) else None,
                "source": best.get("repo", "unknown"),
                "activation": activation,
                "message": f"✅ 已從 {best.get('repo', 'GitHub')} 安裝技能並完成驗證"
            }
            _record_skill_event("acquire", skill_folder, "ok", result["message"])
            return result
    
    # === Step 4: Generate if Nothing Found ===
    if auto_generate:
        logger.info("🧬 Step 4: No suitable skill found, determining best brain for generation...")
        
        genesis_result = {"success": False, "error": "No brain available"}
        
        # Respect MAGI_AVOID_DISTRIBUTED: skip localhost:8080 entirely when set
        _avoid_distributed = os.environ.get("MAGI_AVOID_DISTRIBUTED", "0").strip() == "1"

        if not _avoid_distributed:
            # Quick socket probe before attempting 180s-timeout request
            import socket as _socket
            def _port_open(host: str, port: int, timeout: float = 0.5) -> bool:
                try:
                    with _socket.create_connection((host, port), timeout=timeout):
                        return True
                except OSError:
                    return False

            if _port_open("localhost", 8080):
                try:
                    logger.info("⚡ Using Casper (Primary Brain) for Generation...")
                    genesis_result = request_distributed_skill_generation(f"Create a skill for: {need_description}")
                    if not genesis_result["success"]:
                        logger.warning(f"⚠️ Casper Generation Failed: {genesis_result.get('error')}. Fallback to Melchior Direct.")
                        genesis_result = request_melchior_skill_generation(f"Create a skill for: {need_description}")
                except Exception:
                    genesis_result = request_melchior_skill_generation(f"Create a skill for: {need_description}")
            else:
                logger.info("ℹ️ Casper port 8080 not open, using Melchior Direct.")
                genesis_result = request_melchior_skill_generation(f"Create a skill for: {need_description}")
        else:
            logger.info("ℹ️ MAGI_AVOID_DISTRIBUTED=1, using Melchior Direct for generation.")
            genesis_result = request_melchior_skill_generation(f"Create a skill for: {need_description}")

        if genesis_result["success"] and genesis_result.get("content"):
            safe_name = _build_skill_slug(need_description, prefix="generated")
            skill_dir = os.path.join(SKILLS_DIR, safe_name)
            os.makedirs(skill_dir, exist_ok=True)
            skill_path = os.path.join(skill_dir, "SKILL.md")

            skill_content = _ensure_skill_instructions(genesis_result["content"])
            is_safe_skill, violations = validate_skill_safety(skill_content)
            if not is_safe_skill:
                result = {
                    "success": False,
                    "action": "failed_validation",
                    "skill_path": None,
                    "message": f"❌ IRON DOME BLOCKED SKILL: {violations}",
                }
                _record_skill_event("acquire", safe_name, "error", result["message"])
                return result

            action_code = genesis_result.get("action_code", "") or ""
            verify = _validate_and_debug_action_code(action_code, need_description)
            if not verify["success"] and action_code:
                result = {
                    "success": False,
                    "action": "failed_validation",
                    "skill_path": None,
                    "message": f"❌ 生成程式碼驗證失敗: {verify.get('error')}",
                    "debug_log": verify.get("debug_log", []),
                }
                _record_skill_event("acquire", safe_name, "error", result["message"])
                return result

            res_md = _safe_write_skill_file(safe_name, "SKILL.md", skill_content, reason="acquire_generate_md")
            if res_md.get("blocked"):
                logger.error(f"Iron Dome Blocked SKILL.md write: {res_md.get('error')}")
                return {
                    "success": False,
                    "action": "failed_write",
                    "skill_path": None,
                    "message": f"❌ IRON DOME BLOCKED SKILL.md write: {res_md.get('error')}",
                }

            action_path = None
            if verify.get("code"):
                action_code_to_write = verify["code"]
                res_py = _safe_write_skill_file(safe_name, "action.py", action_code_to_write, reason="acquire_generate_action")
                if res_py.get("blocked"):
                    logger.error(f"Iron Dome Blocked action.py write: {res_py.get('error')}")
                    return {
                        "success": False,
                        "action": "failed_write",
                        "skill_path": None,
                        "message": f"❌ IRON DOME BLOCKED action.py write: {res_py.get('error')}",
                    }
                action_path = os.path.join(skill_dir, "action.py")

            activation = {"compiled": bool(verify.get("code")), "smoke": None, "definition": None, "runtime_repair": None, "snapshot": None}
            snapshot = _snapshot_skill_version(skill_dir, reason="generated_initial_release")
            if snapshot.get("success"):
                activation["snapshot"] = snapshot

            if action_path:
                runtime = _auto_runtime_repair_action(skill_dir, need_description, max_rounds=MAX_RUNTIME_REPAIR_ROUNDS)
                activation["runtime_repair"] = runtime
                activation["smoke"] = runtime.get("smoke")
                if not runtime.get("success"):
                    rollback_result = None
                    if snapshot.get("success"):
                        rollback_result = rollback_skill_version(safe_name, snapshot.get("version_id", ""))
                    result = {
                        "success": False,
                        "action": "failed_runtime_validation",
                        "skill_path": skill_path,
                        "skill_folder": safe_name,
                        "message": f"❌ 新技能執行驗證失敗: {runtime.get('error')}",
                        "activation": activation,
                        "rollback": rollback_result,
                    }
                    _record_skill_event("acquire", safe_name, "error", result["message"])
                    return result
            if auto_activate:
                activation["definition"] = _register_skill_tool_definition(
                    safe_name,
                    f"Run generated skill for: {need_description}",
                )

            model_used = genesis_result.get("model_used", "Unknown")
            source_agent = "CASPER" if "70B" in model_used or "gpt-oss" in model_used else "MELCHIOR"
            message = f"🧬 {source_agent} ({model_used}) 已生成並上線技能: {safe_name}"
            if action_path:
                message += " (含 action.py，已驗證)"

            result = {
                "success": True,
                "action": f"generated_by_{source_agent.lower()}",
                "skill_path": skill_path,
                "skill_folder": safe_name,
                "action_path": action_path,
                "source": source_agent,
                "activation": activation,
                "message": message,
            }
            _record_skill_event("acquire", safe_name, "ok", message, {"source": source_agent})
            return result
        
        # Fallback to simple template generation if ALL LLMs fail
        simple_result = generate_skill(
            name=_build_skill_slug(need_description, prefix="auto"),
            description=need_description,
            instructions=f"Capability to handle: {need_description}",
            author="CASPER-AUTO"
        )
        
        if simple_result["success"]:
            skill_folder = os.path.basename(os.path.dirname(simple_result["path"]))
            definition_result = _register_skill_tool_definition(
                skill_folder,
                f"Run generated fallback skill for: {need_description}",
            ) if auto_activate else None
            result = {
                "success": True,
                "action": "generated_by_casper",
                "skill_path": simple_result["path"],
                "skill_folder": skill_folder,
                "source": "CASPER",
                "activation": {"compiled": False, "smoke": None, "definition": definition_result},
                "message": f"🧬 CASPER 已生成新技能（模板模式）"
            }
            _record_skill_event("acquire", skill_folder, "ok", result["message"], {"source": "CASPER"})
            return result
    
    result = {
        "success": False,
        "action": "failed",
        "skill_path": None,
        "message": "❌ 無法找到或生成合適的技能"
    }
    _record_skill_event("acquire", "", "error", result["message"])
    return result


# =============================================================================
# Module Test
# =============================================================================
if __name__ == "__main__":
    print("🧬 SKILL GENESIS MODULE TEST")
    print("=" * 50)
    
    # Test: Generate a simple skill
    result = generate_skill(
        name="test-skill",
        description="A test skill for validation",
        instructions="This is a test skill that does nothing harmful.",
        author="CASPER"
    )
    print(f"Generate Test Skill: {result}")
    
    # Test: List skills
    print(f"\nInstalled Skills: {list_skills()}")
    
    # Test: Safety check
    dangerous = "rm -rf /"
    is_safe, violations = validate_skill_safety(dangerous)
    print(f"\nSafety Check (rm -rf /): Safe={is_safe}, Violations={violations}")
