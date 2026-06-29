#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ast
import json
import os
import re
import shlex
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


MAGI_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MATRIX = MAGI_ROOT / "config" / "test_matrix.json"
DEFAULT_RUNTIME_DIR = MAGI_ROOT / ".runtime"
DEFAULT_LIVE_RUNTIME_ROOT = Path("/Users/ai/Library/Application Support/MAGI/runtime/MAGI_v2")

_SKIP_PARTS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".versions",
    "__pycache__",
    "_bg_jobs",
    "node_modules",
    "venv",
}
_SCRIPT_RE = re.compile(
    r"(?:^|[\s'\"/])"
    r"((?:api|config|scripts|skills)/[^'\"\s]+?\.(?:py|sh))"
)
_JSON_OUT_FLAGS = {"--json-out", "--output-json", "--report-json"}
_FAILED_STATUS = {"error", "failed", "fail", "down", "not_ready", "unhealthy"}
_OK_STATUS = {"ok", "ready", "live", "success", "passed", "healthy", "skipped"}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _rel(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except Exception:
        return path.as_posix()


def default_runtime_dir(root: Path) -> Path:
    source_runtime = root / ".runtime"
    live_env = os.environ.get("MAGI_LIVE_RUNTIME_ROOT")
    live_root = Path(live_env or DEFAULT_LIVE_RUNTIME_ROOT).expanduser()
    live_runtime = live_root / ".runtime"
    try:
        use_live = bool(live_env) or root.resolve() == MAGI_ROOT.resolve()
        if use_live and live_root.exists() and live_runtime.exists() and live_root.resolve() != root.resolve():
            return live_runtime
    except Exception:
        pass
    return source_runtime


def _read_json(path: Path, default: Any = None) -> tuple[Any, str]:
    try:
        return json.loads(path.read_text(encoding="utf-8")), ""
    except FileNotFoundError:
        return default, "missing"
    except Exception as exc:
        return default, f"{type(exc).__name__}: {exc}"


def _parse_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    for candidate in (text, text.replace(" ", "T", 1)):
        try:
            dt = datetime.fromisoformat(candidate)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except ValueError:
            pass
    return None


def _mtime_dt(path: Path) -> datetime | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    except Exception:
        return None


def _age_hours(dt: datetime | None, now: datetime) -> float | None:
    if dt is None:
        return None
    return round(max(0.0, (now - dt).total_seconds() / 3600.0), 3)


def _has_skip_part(path: Path) -> bool:
    return any(part in _SKIP_PARTS or part.startswith(".") for part in path.parts)


def _literal_string(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts: list[str] = []
        for item in node.values:
            if isinstance(item, ast.Constant) and isinstance(item.value, str):
                parts.append(item.value)
            elif isinstance(item, ast.FormattedValue):
                parts.append("{}")
            else:
                return None
        return "".join(parts)
    return None


def _route_methods(call: ast.Call) -> list[str]:
    for keyword in call.keywords:
        if keyword.arg != "methods":
            continue
        value = keyword.value
        if isinstance(value, (ast.List, ast.Tuple, ast.Set)):
            out: list[str] = []
            for item in value.elts:
                text = _literal_string(item)
                if text:
                    out.append(text.upper())
            return sorted(set(out or ["GET"]))
        text = _literal_string(value)
        if text:
            return [text.upper()]
    return ["GET"]


def _route_calls_from_decorator(node: ast.AST) -> ast.Call | None:
    if not isinstance(node, ast.Call):
        return None
    func = node.func
    if isinstance(func, ast.Attribute) and func.attr == "route":
        return node
    return None


def _is_add_url_rule_call(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "add_url_rule"
    )


def classify_route_domain(route: str, file_path: str = "") -> str:
    route_l = route.lower()
    file_l = file_path.lower()
    if route in {"/health", "/livez", "/readyz"} or "health" in route_l or "status" in route_l:
        return "health"
    if route_l.startswith("/api/osc") or route_l.startswith("/osc") or "osc_" in file_l:
        return "osc"
    if route_l.startswith("/api/nerv") or route_l.startswith("/admin") or "admin_runtime" in file_l:
        return "admin"
    if route_l.startswith("/skills") or route_l.startswith("/api/skills"):
        return "skills"
    if "webhook" in route_l or route_l.startswith("/line") or route_l.startswith("/telegram") or route_l == "/callback":
        return "webhooks"
    if route_l.startswith("/api/memory") or route_l in {"/remember", "/recall"}:
        return "memory"
    if route_l.startswith("/legal") or "judicial" in route_l:
        return "legal"
    if route_l.startswith("/collab"):
        return "collab"
    if route_l.startswith("/shortcut") or route_l in {"/search", "/research", "/fetch", "/vision", "/summarize"}:
        return "tools"
    if route_l.startswith("/mobile") or route_l.startswith("/app"):
        return "mobile"
    if route_l.startswith("/static") or route_l.startswith("/exports") or route_l.startswith("/s/"):
        return "files"
    if "golem" in route_l or "golem" in file_l:
        return "golem"
    if route_l.startswith("/dashboard") or route_l in {"/", "/login", "/logout", "/register"}:
        return "web"
    return "other"


def discover_api_routes(root: Path) -> dict[str, Any]:
    routes: list[dict[str, Any]] = []
    api_root = root / "api"
    if not api_root.exists():
        return {"present": False, "total": 0, "domains": {}, "routes": []}

    for path in sorted(api_root.rglob("*.py")):
        rel = Path(_rel(path, root))
        if _has_skip_part(rel):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"), filename=str(path))
        except SyntaxError as exc:
            routes.append(
                {
                    "route": "",
                    "methods": [],
                    "domain": "parse_error",
                    "file": rel.as_posix(),
                    "line": exc.lineno or 0,
                    "handler": "",
                    "error": str(exc),
                }
            )
            continue

        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for decorator in node.decorator_list:
                    call = _route_calls_from_decorator(decorator)
                    if not call or not call.args:
                        continue
                    route = _literal_string(call.args[0])
                    if not route:
                        continue
                    routes.append(
                        {
                            "route": route,
                            "methods": _route_methods(call),
                            "domain": classify_route_domain(route, rel.as_posix()),
                            "file": rel.as_posix(),
                            "line": getattr(call, "lineno", getattr(node, "lineno", 0)),
                            "handler": node.name,
                        }
                    )
            elif _is_add_url_rule_call(node) and node.args:
                route = _literal_string(node.args[0])
                if not route:
                    continue
                routes.append(
                    {
                        "route": route,
                        "methods": _route_methods(node),
                        "domain": classify_route_domain(route, rel.as_posix()),
                        "file": rel.as_posix(),
                        "line": getattr(node, "lineno", 0),
                        "handler": "add_url_rule",
                    }
                )

    domains: dict[str, dict[str, Any]] = {}
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for route in routes:
        grouped[route["domain"]].append(route)
    for domain, items in sorted(grouped.items()):
        method_counts: dict[str, int] = defaultdict(int)
        files = set()
        for item in items:
            files.add(item["file"])
            for method in item["methods"]:
                method_counts[method] += 1
        domains[domain] = {
            "count": len(items),
            "method_counts": dict(sorted(method_counts.items())),
            "files": sorted(files),
        }

    return {
        "present": True,
        "total": len(routes),
        "domains": domains,
        "routes": sorted(routes, key=lambda item: (item["domain"], item["route"], item["file"], item["line"])),
    }


def _skill_frontmatter(path: Path) -> dict[str, str]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return {}
    out: dict[str, str] = {}
    lines = text.splitlines()
    in_frontmatter = bool(lines and lines[0].strip() == "---")
    for raw in lines[1:] if in_frontmatter else lines[:30]:
        line = raw.strip()
        if in_frontmatter and line == "---":
            break
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip().lower()
        if key in {"name", "description"}:
            out[key] = value.strip().strip('"').strip("'")
    return out


def discover_skills(root: Path) -> dict[str, Any]:
    skills_root = root / "skills"
    entries: list[dict[str, Any]] = []
    if not skills_root.exists():
        return {
            "present": False,
            "total": 0,
            "with_skill_md": 0,
            "with_action": 0,
            "missing_skill_md": [],
            "missing_action": [],
            "duplicate_canonical": [],
            "entries": [],
        }

    for path in sorted(skills_root.rglob("*")):
        if not path.is_dir():
            continue
        rel = Path(_rel(path, root))
        if _has_skip_part(rel):
            continue
        skill_md = path / "SKILL.md"
        action_py = path / "action.py"
        if not skill_md.exists() and not action_py.exists():
            continue
        folder = _rel(path, skills_root)
        meta = _skill_frontmatter(skill_md) if skill_md.exists() else {}
        name = meta.get("name") or path.name
        entries.append(
            {
                "folder": folder,
                "name": name,
                "canonical": folder.replace("_", "-"),
                "description": meta.get("description", ""),
                "has_skill_md": skill_md.exists(),
                "has_action": action_py.exists(),
                "python_files": len(list(path.glob("*.py"))),
            }
        )

    duplicate_canonical: list[dict[str, Any]] = []
    by_canonical: dict[str, list[str]] = defaultdict(list)
    for entry in entries:
        by_canonical[entry["canonical"]].append(entry["folder"])
    for canonical, folders in sorted(by_canonical.items()):
        if len(folders) > 1:
            duplicate_canonical.append({"canonical": canonical, "folders": sorted(folders)})

    missing_skill_md = sorted(entry["folder"] for entry in entries if not entry["has_skill_md"])
    missing_action = sorted(entry["folder"] for entry in entries if not entry["has_action"])
    return {
        "present": True,
        "total": len(entries),
        "with_skill_md": sum(1 for entry in entries if entry["has_skill_md"]),
        "with_action": sum(1 for entry in entries if entry["has_action"]),
        "missing_skill_md": missing_skill_md,
        "missing_action": missing_action,
        "duplicate_canonical": duplicate_canonical,
        "entries": entries,
    }


def _split_command(command: Any) -> list[str]:
    if isinstance(command, list):
        return [str(part) for part in command]
    text = str(command or "")
    try:
        return shlex.split(text)
    except ValueError:
        return text.split()


def _command_text(command: Any) -> str:
    if isinstance(command, list):
        return " ".join(str(part) for part in command)
    return str(command or "")


def _resolve_output_path(raw: str, root: Path, runtime_dir: Path) -> Path:
    text = str(raw).replace("{root}", str(root))
    path = Path(text).expanduser()
    if path.is_absolute():
        return path
    if text.startswith(".runtime/"):
        return runtime_dir / text[len(".runtime/") :]
    if text.startswith("static/"):
        return root / text
    if text.startswith("runtime/"):
        return runtime_dir / text[len("runtime/") :]
    return root / text


def extract_json_outputs(command: Any, root: Path, runtime_dir: Path) -> list[str]:
    parts = _split_command(command)
    outputs: list[str] = []
    for index, part in enumerate(parts):
        if part in _JSON_OUT_FLAGS and index + 1 < len(parts):
            outputs.append(_rel(_resolve_output_path(parts[index + 1], root, runtime_dir), root))
            continue
        for flag in _JSON_OUT_FLAGS:
            prefix = flag + "="
            if part.startswith(prefix):
                outputs.append(_rel(_resolve_output_path(part[len(prefix) :], root, runtime_dir), root))
    return sorted(set(outputs))


def command_script_keys(command: Any) -> list[str]:
    text = _command_text(command)
    return sorted(set(match.group(1) for match in _SCRIPT_RE.finditer(text)))


def _cron_expected_interval_hours(expr: str) -> float | None:
    parts = str(expr or "").split()
    if len(parts) != 5:
        return None
    minute, hour, day_of_month, _month, day_of_week = parts
    if day_of_month not in {"*", "?"}:
        return 31 * 24.0
    if day_of_week not in {"*", "?"}:
        return 7 * 24.0
    if hour in {"*", "*/1"}:
        if minute.startswith("*/"):
            try:
                return max(1.0 / 60.0, int(minute[2:]) / 60.0)
            except Exception:
                return 1.0
        return 1.0
    if hour.startswith("*/"):
        try:
            return float(max(1, int(hour[2:])))
        except Exception:
            return 1.0
    if "," in hour:
        slots = [part for part in hour.split(",") if part.strip()]
        if slots:
            return max(1.0, 24.0 / len(slots))
    return 24.0


def _cron_stale_threshold_hours(expr: str) -> float:
    interval = _cron_expected_interval_hours(expr)
    if interval is None:
        return 72.0
    return max(2.0, round(interval * 2.5 + 6.0, 3))


def _infer_payload_ok(data: Any) -> tuple[bool | None, str]:
    if isinstance(data, dict):
        for key in ("ok", "success", "passed"):
            if isinstance(data.get(key), bool):
                return bool(data[key]), key
        failed = data.get("failed")
        if isinstance(failed, int):
            return failed == 0, "failed"
        failures = data.get("failures")
        if isinstance(failures, list):
            return len(failures) == 0, "failures"
        errors = data.get("errors")
        if isinstance(errors, int):
            return errors == 0, "errors"
        status = str(data.get("status") or "").strip().lower()
        if status in _FAILED_STATUS:
            return False, "status"
        if status in _OK_STATUS:
            return True, "status"
    return None, "unknown"


def _payload_timestamp(data: Any) -> datetime | None:
    if not isinstance(data, dict):
        return None
    for key in ("generated_at", "timestamp", "created_at", "updated_at", "last_run", "last_success_at"):
        dt = _parse_dt(data.get(key))
        if dt:
            return dt
    return None


def _health_file_candidate(path: Path, base: Path) -> bool:
    name = path.name.lower()
    if name.startswith("function_health_index_"):
        return False
    parts = {part.lower() for part in path.parts}
    if "latest" in name or "health" in name:
        return True
    if name == "cron_state.json":
        return True
    if "test_reports" in parts and name.endswith(".json"):
        return True
    return False


def discover_runtime_health_files(root: Path, runtime_dir: Path, *, include_static: bool = True) -> list[Path]:
    bases = [runtime_dir]
    if include_static:
        bases.append(root / "static")
    out: list[Path] = []
    seen: set[Path] = set()
    for base in bases:
        if not base.exists():
            continue
        for path in sorted(base.rglob("*.json")):
            if path in seen:
                continue
            try:
                rel = path.relative_to(base)
            except Exception:
                rel = path
            if _has_skip_part(rel):
                continue
            if _health_file_candidate(path, base):
                out.append(path)
                seen.add(path)
    return out


def evaluate_health_file(path: Path, root: Path, now: datetime, max_age_hours: float) -> dict[str, Any]:
    data, error = _read_json(path, None)
    mtime = _mtime_dt(path)
    timestamp = _payload_timestamp(data) or mtime
    age = _age_hours(timestamp, now)
    ok, ok_source = _infer_payload_ok(data)
    status = "ok"
    reason = ""
    if error:
        status = "missing" if error == "missing" else "failed"
        reason = error
    elif ok is False:
        status = "failed"
        reason = f"{ok_source}=false"
    elif max_age_hours > 0 and age is not None and age > max_age_hours:
        status = "stale"
        reason = f"age_hours>{max_age_hours:g}"
    elif ok is None:
        status = "observed"
        reason = "no explicit ok/success/failed contract"

    return {
        "path": _rel(path, root),
        "status": status,
        "ok": status in {"ok", "observed"},
        "reason": reason,
        "contract": ok_source,
        "timestamp": timestamp.isoformat() if timestamp else "",
        "age_hours": age,
        "size_bytes": path.stat().st_size if path.exists() else 0,
    }


def _expected_health_paths_from_matrix(matrix: dict[str, Any], root: Path, runtime_dir: Path) -> list[dict[str, Any]]:
    expected: list[dict[str, Any]] = []
    suites = matrix.get("suites") if isinstance(matrix, dict) else {}
    if not isinstance(suites, dict):
        return expected
    for suite_name, suite in suites.items():
        checks = suite.get("checks") if isinstance(suite, dict) else []
        if not isinstance(checks, list):
            continue
        for check in checks:
            if not isinstance(check, dict):
                continue
            command = check.get("command") or []
            if "scripts/ops/function_health_index.py" in _command_text(command):
                continue
            for out in extract_json_outputs(command, root, runtime_dir):
                expected.append(
                    {
                        "path": out,
                        "owner": f"matrix:{suite_name}:{check.get('id') or check.get('name') or 'unnamed'}",
                        "source": "matrix_json_out",
                    }
                )
    return expected


def discover_test_suites(matrix_path: Path, root: Path, runtime_dir: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    matrix, error = _read_json(matrix_path, {})
    suites = matrix.get("suites") if isinstance(matrix, dict) else {}
    suite_entries: list[dict[str, Any]] = []
    check_count = 0
    if isinstance(suites, dict):
        for name, suite in sorted(suites.items()):
            checks = suite.get("checks") if isinstance(suite, dict) else []
            if not isinstance(checks, list):
                checks = []
            check_count += len(checks)
            suite_entries.append(
                {
                    "name": name,
                    "description": str(suite.get("description") or "") if isinstance(suite, dict) else "",
                    "check_count": len(checks),
                    "checks": [
                        {
                            "id": str(check.get("id") or check.get("name") or "unnamed"),
                            "name": str(check.get("name") or check.get("id") or "unnamed"),
                            "timeout_sec": int(check.get("timeout_sec") or 300),
                            "json_outputs": extract_json_outputs(check.get("command") or [], root, runtime_dir),
                            "requires_env": check.get("require_env") or [],
                        }
                        for check in checks
                        if isinstance(check, dict)
                    ],
                }
            )

    return (
        {
            "matrix": _rel(matrix_path, root),
            "present": not bool(error),
            "error": "" if error == "missing" else error,
            "total": len(suite_entries),
            "check_count": check_count,
            "suites": suite_entries,
        },
        _expected_health_paths_from_matrix(matrix, root, runtime_dir),
    )


def discover_cron_jobs(root: Path, runtime_dir: Path, now: datetime) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    runtime_root = runtime_dir.parent
    runtime_cron = runtime_root / "cron_jobs.json"
    cron_path = runtime_cron if runtime_cron.exists() and runtime_root.resolve() != root.resolve() else root / "cron_jobs.json"
    jobs, error = _read_json(cron_path, [])
    state_path = runtime_dir / "cron_state.json"
    state, state_error = _read_json(state_path, {})
    entries: list[dict[str, Any]] = []
    missing_state: list[dict[str, Any]] = []
    stale: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    expected_health: list[dict[str, Any]] = [
        {"path": _rel(state_path, root), "owner": "cron_state", "source": "cron_state"}
    ]

    if not isinstance(jobs, list):
        jobs = []
        error = error or "cron_jobs.json is not a list"
    if not isinstance(state, dict):
        state = {}
        state_error = state_error or "cron_state.json is not an object"

    for job in jobs:
        if not isinstance(job, dict):
            continue
        job_id = str(job.get("id") or "")
        enabled = bool(job.get("enabled", True))
        command = job.get("command") or ""
        json_outputs = extract_json_outputs(command, root, runtime_dir)
        for out in json_outputs:
            expected_health.append({"path": out, "owner": f"cron:{job_id}", "source": "cron_json_out"})
        state_item = state.get(job_id) if isinstance(state, dict) else None
        if not isinstance(state_item, dict):
            state_item = {}
        last_run = _parse_dt(state_item.get("last_run")) or _parse_dt(job.get("last_run"))
        age = _age_hours(last_run, now)
        threshold = _cron_stale_threshold_hours(str(job.get("cron") or ""))
        state_ok, state_contract = _infer_payload_ok(state_item)
        entry = {
            "id": job_id,
            "enabled": enabled,
            "cron": str(job.get("cron") or ""),
            "description": str(job.get("desc") or ""),
            "command_kind": "macro" if str(command).strip().startswith("@MAGI") else "script",
            "scripts": command_script_keys(command),
            "json_outputs": json_outputs,
            "last_run": last_run.isoformat() if last_run else "",
            "age_hours": age,
            "stale_threshold_hours": threshold,
            "state_present": bool(state_item),
        }
        entries.append(entry)
        if not enabled:
            continue
        if not state_item and state_error != "missing":
            missing_state.append({"id": job_id, "reason": "missing cron_state entry"})
            continue
        if not state_item and state_error == "missing":
            missing_state.append({"id": job_id, "reason": "cron_state.json missing"})
            continue
        if state_ok is False:
            failed.append({"id": job_id, "reason": f"{state_contract}=false"})
        if last_run is None:
            missing_state.append({"id": job_id, "reason": "missing last_run"})
        elif age is not None and age > threshold:
            stale.append({"id": job_id, "age_hours": age, "threshold_hours": threshold})

    return (
        {
            "source": _rel(cron_path, root),
            "present": not bool(error),
            "error": "" if error == "missing" else error,
            "state": _rel(state_path, root),
            "state_present": not bool(state_error),
            "state_error": "" if state_error == "missing" else state_error,
            "total": len(entries),
            "enabled": sum(1 for entry in entries if entry["enabled"]),
            "missing_state": missing_state,
            "stale": stale,
            "failed": failed,
            "entries": entries,
        },
        expected_health,
    )


def _dedupe_expected(paths: list[dict[str, Any]]) -> list[dict[str, Any]]:
    owners: dict[str, set[str]] = defaultdict(set)
    sources: dict[str, set[str]] = defaultdict(set)
    for item in paths:
        path = str(item.get("path") or "")
        if not path:
            continue
        owners[path].add(str(item.get("owner") or "unknown"))
        sources[path].add(str(item.get("source") or "unknown"))
    return [
        {"path": path, "owners": sorted(owners[path]), "sources": sorted(sources[path])}
        for path in sorted(owners)
    ]


def build_index(
    *,
    root: Path = MAGI_ROOT,
    matrix_path: Path | None = None,
    runtime_dir: Path | None = None,
    now: datetime | None = None,
    max_health_age_hours: float = 72.0,
    include_static: bool = True,
) -> dict[str, Any]:
    root = root.resolve()
    matrix_path = (matrix_path or root / "config" / "test_matrix.json").resolve()
    runtime_dir = (runtime_dir or default_runtime_dir(root)).resolve()
    now = now or _utc_now()
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now = now.astimezone(timezone.utc)

    api_routes = discover_api_routes(root)
    skills = discover_skills(root)
    test_suites, expected_from_matrix = discover_test_suites(matrix_path, root, runtime_dir)
    cron_jobs, expected_from_cron = discover_cron_jobs(root, runtime_dir, now)
    expected = _dedupe_expected(expected_from_matrix + expected_from_cron)

    health_paths = discover_runtime_health_files(root, runtime_dir, include_static=include_static)
    health_by_rel = {_rel(path, root): path for path in health_paths}
    for item in expected:
        rel_path = str(item["path"])
        if rel_path not in health_by_rel:
            health_by_rel[rel_path] = root / rel_path

    health_files = [
        evaluate_health_file(path, root, now, max_health_age_hours)
        for _rel_path, path in sorted(health_by_rel.items())
    ]
    expected_paths = {str(item["path"]) for item in expected}
    missing = [
        {
            "path": item["path"],
            "owners": item["owners"],
            "sources": item["sources"],
            "reason": "expected health file missing",
        }
        for item in expected
        if not (root / str(item["path"])).exists()
    ]
    failed = [
        {"path": item["path"], "reason": item["reason"], "contract": item["contract"]}
        for item in health_files
        if item["status"] == "failed" and item["path"] in expected_paths
    ]
    stale = [
        {"path": item["path"], "age_hours": item["age_hours"], "reason": item["reason"]}
        for item in health_files
        if item["status"] == "stale" and item["path"] in expected_paths
    ]
    observed_failed = [
        {"path": item["path"], "reason": item["reason"], "contract": item["contract"]}
        for item in health_files
        if item["status"] == "failed" and item["path"] not in expected_paths
    ]
    observed_stale = [
        {"path": item["path"], "age_hours": item["age_hours"], "reason": item["reason"]}
        for item in health_files
        if item["status"] == "stale" and item["path"] not in expected_paths
    ]
    missing.extend(
        {"path": item["path"], "owners": [], "sources": ["scan"], "reason": item["reason"]}
        for item in health_files
        if item["status"] == "missing" and item["path"] not in expected_paths
    )

    failed.extend({"path": f"cron:{item['id']}", "reason": item["reason"], "contract": "cron_state"} for item in cron_jobs["failed"])
    stale.extend({"path": f"cron:{item['id']}", "age_hours": item["age_hours"], "reason": "cron last_run stale"} for item in cron_jobs["stale"])
    missing.extend(
        {"path": f"cron:{item['id']}", "owners": [f"cron:{item['id']}"], "sources": ["cron_state"], "reason": item["reason"]}
        for item in cron_jobs["missing_state"]
    )

    health_ok = not failed and not stale and not missing
    skill_issue_count = (
        len(skills.get("missing_skill_md") or [])
        + len(skills.get("missing_action") or [])
        + len(skills.get("duplicate_canonical") or [])
    )
    report = {
        "ok": health_ok,
        "generated_at": now.isoformat(),
        "root": str(root),
        "matrix": _rel(matrix_path, root),
        "runtime_dir": _rel(runtime_dir, root),
        "summary": {
            "api_route_count": api_routes["total"],
            "api_route_domain_count": len(api_routes["domains"]),
            "skill_count": skills["total"],
            "skill_issue_count": skill_issue_count,
            "cron_job_count": cron_jobs["total"],
            "enabled_cron_job_count": cron_jobs["enabled"],
            "test_suite_count": test_suites["total"],
            "test_check_count": test_suites["check_count"],
            "runtime_health_file_count": len(health_files),
            "failed_health_count": len(failed),
            "stale_health_count": len(stale),
            "missing_health_count": len(missing),
            "observed_failed_health_count": len(observed_failed),
            "observed_stale_health_count": len(observed_stale),
        },
        "api_routes": api_routes,
        "skills": skills,
        "cron_jobs": cron_jobs,
        "test_suites": test_suites,
        "runtime_health": {
            "max_age_hours": max_health_age_hours,
            "expected": expected,
            "files": health_files,
            "failed": failed,
            "stale": stale,
            "missing": missing,
            "observed_failed": observed_failed,
            "observed_stale": observed_stale,
        },
        "health": {
            "ok": health_ok,
            "failed": failed,
            "stale": stale,
            "missing": missing,
        },
    }
    return report


def _print_human_summary(report: dict[str, Any]) -> None:
    summary = report.get("summary") or {}
    print(
        "function_health_index "
        f"ok={report.get('ok')} "
        f"routes={summary.get('api_route_count', 0)} "
        f"skills={summary.get('skill_count', 0)} "
        f"cron={summary.get('enabled_cron_job_count', 0)}/{summary.get('cron_job_count', 0)} "
        f"suites={summary.get('test_suite_count', 0)} "
        f"health_files={summary.get('runtime_health_file_count', 0)} "
        f"failed={summary.get('failed_health_count', 0)} "
        f"stale={summary.get('stale_health_count', 0)} "
        f"missing={summary.get('missing_health_count', 0)}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build a public-safe MAGI function health index.")
    parser.add_argument("--root", default=str(MAGI_ROOT), help="MAGI repository root.")
    parser.add_argument("--matrix", default="", help="Path to config/test_matrix.json.")
    parser.add_argument("--runtime-dir", default="", help="Runtime health directory; defaults to <root>/.runtime.")
    parser.add_argument("--json-out", default="", help="Write the full index JSON to this path.")
    parser.add_argument("--max-health-age-hours", type=float, default=72.0, help="Mark health files older than this as stale; <=0 disables.")
    parser.add_argument("--no-static", action="store_true", help="Do not scan static/*latest*.json health artifacts.")
    parser.add_argument("--compact", action="store_true", help="Print compact JSON.")
    parser.add_argument("--summary", action="store_true", help="Print a one-line summary before JSON.")
    parser.add_argument("--fail-on-health", action="store_true", help="Return 1 when failed/stale/missing health is found.")
    args = parser.parse_args(argv)

    root = Path(args.root).expanduser().resolve()
    matrix = Path(args.matrix).expanduser().resolve() if args.matrix else root / "config" / "test_matrix.json"
    runtime_dir = Path(args.runtime_dir).expanduser().resolve() if args.runtime_dir else default_runtime_dir(root)
    report = build_index(
        root=root,
        matrix_path=matrix,
        runtime_dir=runtime_dir,
        max_health_age_hours=args.max_health_age_hours,
        include_static=not args.no_static,
    )

    payload = json.dumps(report, ensure_ascii=False, indent=None if args.compact else 2, sort_keys=False)
    if args.json_out:
        out = Path(args.json_out).expanduser()
        if not out.is_absolute():
            out = root / out
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(payload + "\n", encoding="utf-8")
    if args.summary:
        _print_human_summary(report)
    print(payload)
    return 1 if args.fail_on_health and not report["ok"] else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
