#!/usr/bin/env python3
"""Generate a source-derived MAGI V2 compatibility inventory for V3 planning.

The inventory deliberately ignores README files.  It extracts observable
interfaces from Python route decorators, skill entry points, cron definitions,
daemon child-process declarations, tests, and checked-in launchd definitions.
"""

from __future__ import annotations

import argparse
import ast
import json
import plistlib
import re
import shlex
from pathlib import Path
from typing import Any


HTTP_DECORATORS = {"route", "get", "post", "put", "delete", "patch"}
CRON_CODE_ROOT_MARKERS = frozenset(
    {"api", "casper_ecosystem", "config", "gui", "scripts", "skills"}
)


def _portable_text(value: Any, root: Path) -> str:
    """Remove workstation-specific roots while preserving command structure."""

    text = str(value or "")
    text = text.replace(str(root.resolve()), "${MAGI_ROOT}")
    text = text.replace(str(Path.home()), "${HOME}")
    text = re.sub(r"/Volumes/[^\s'\"]+", "${MAGI_EXTERNAL_VOLUME_PATH}", text)
    text = re.sub(r"(?<![A-Za-z0-9_])[A-Za-z]:[\\/][^\s'\"]+", "${MAGI_EXTERNAL_PATH}", text)
    text = re.sub(r"\\\\[^\s'\"]+", "${MAGI_EXTERNAL_UNC_PATH}", text)
    return text


def _literal_string(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def collect_routes(root: Path) -> list[dict[str, Any]]:
    routes: list[dict[str, Any]] = []
    for path in sorted((root / "api").rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for decorator in node.decorator_list:
                if not (
                    isinstance(decorator, ast.Call)
                    and isinstance(decorator.func, ast.Attribute)
                    and decorator.func.attr in HTTP_DECORATORS
                    and decorator.args
                ):
                    continue
                route = _literal_string(decorator.args[0])
                if route is None:
                    continue
                methods: list[str] = []
                for keyword in decorator.keywords:
                    if keyword.arg != "methods" or not isinstance(keyword.value, (ast.List, ast.Tuple)):
                        continue
                    methods = [
                        value
                        for item in keyword.value.elts
                        if (value := _literal_string(item)) is not None
                    ]
                if not methods:
                    methods = [decorator.func.attr.upper() if decorator.func.attr != "route" else "ANY"]
                routes.append(
                    {
                        "source": str(path.relative_to(root)),
                        "line": node.lineno,
                        "handler": node.name,
                        "path": route,
                        "methods": sorted(methods),
                    }
                )
    return sorted(routes, key=lambda item: (item["source"], item["line"], item["path"]))


def collect_skills(root: Path) -> list[dict[str, Any]]:
    skills: list[dict[str, Any]] = []
    for path in sorted((root / "skills").rglob("action.py")):
        rel = path.relative_to(root)
        skill_id = str(rel.parent.relative_to("skills"))
        skills.append(
            {
                "id": skill_id,
                "entrypoint": str(rel),
                "lifecycle": "versioned_rollback_artifact" if skill_id.startswith(".versions/") else "active",
            }
        )
    return skills


def _cron_jobs(raw: Any, root: Path) -> list[dict[str, Any]]:
    jobs = raw.get("jobs", raw) if isinstance(raw, dict) else raw
    if not isinstance(jobs, list):
        raise ValueError("cron source must contain a job list")
    result: list[dict[str, Any]] = []
    for job in jobs:
        if not isinstance(job, dict):
            continue
        result.append(
            {
                "id": str(job.get("id") or ""),
                "cron": str(job.get("cron") or ""),
                "enabled": bool(job.get("enabled", True)),
                "timeout_sec": job.get("timeout_sec"),
                "resource_guarded": bool(job.get("resource_guarded", False)),
                "long_job": bool(job.get("long_job", False)),
                "command": _portable_text(job.get("command") or "", root),
            }
        )
    return sorted(result, key=lambda item: item["id"])


def collect_cron(root: Path) -> list[dict[str, Any]]:
    path = root / "cron_jobs.json"
    return _cron_jobs(json.loads(path.read_text(encoding="utf-8")), root)


def collect_portable_cron_bytes(data: bytes) -> list[dict[str, Any]]:
    """Normalize a copied V2 cron source without trusting its current path.

    Formal validation copies ``cron_jobs.json`` into a candidate-owned input
    directory.  The commands still identify the original MAGI source root, so
    derive that root from executable arguments before applying the same
    workstation-path normalization used by :func:`collect_cron`.
    """

    try:
        raw = json.loads(data.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("cron source is not valid UTF-8 JSON") from exc
    jobs = raw.get("jobs", raw) if isinstance(raw, dict) else raw
    if not isinstance(jobs, list) or not jobs:
        raise ValueError("cron source must contain a non-empty job list")
    roots: set[Path] = set()
    for row in jobs:
        if not isinstance(row, dict):
            continue
        try:
            arguments = shlex.split(str(row.get("command") or ""), posix=True)
        except ValueError as exc:
            raise ValueError("cron command is not shell-parseable") from exc
        for argument in arguments:
            value = argument.split("=", 1)[1] if re.fullmatch(r"[A-Z][A-Z0-9_]*=/.*", argument) else argument
            if not value.startswith("/"):
                continue
            candidate = Path(value)
            if (
                re.fullmatch(r"python(?:3(?:\.\d+)?)?", candidate.name)
                and candidate.parent.name == "bin"
                and candidate.parent.parent.name in {"venv", ".venv"}
            ):
                roots.add(candidate.parent.parent.parent)
                continue
            for marker in CRON_CODE_ROOT_MARKERS:
                if marker in candidate.parts:
                    marker_index = candidate.parts.index(marker)
                    if marker_index > 0:
                        roots.add(Path(*candidate.parts[:marker_index]))
                    break
    if len(roots) != 1:
        raise ValueError(
            "cron source must identify exactly one MAGI executable root; "
            f"observed {len(roots)}"
        )
    return _cron_jobs(raw, next(iter(roots)))


def project_inventory_to_release(
    inventory: dict[str, Any],
    release_paths: set[str],
    *,
    cron_jobs: list[dict[str, Any]],
) -> dict[str, Any]:
    """Project the full source inventory onto files shipped by one release.

    V3 bundles intentionally omit rollback-only skills, workstation-only
    launch agents, and hundreds of non-release tests.  Comparing a full V2
    inventory with that filtered bundle can never succeed.  This projection
    preserves every shipped interface and the complete, separately hash-bound
    cron source while excluding only paths absent from the release manifest.
    """

    routes = [
        dict(row)
        for row in inventory.get("http_routes", ())
        if isinstance(row, dict) and row.get("source") in release_paths
    ]
    skills = [
        dict(row)
        for row in inventory.get("skill_entrypoints", ())
        if isinstance(row, dict) and row.get("entrypoint") in release_paths
    ]
    daemon_children = (
        [dict(row) for row in inventory.get("daemon_children", ()) if isinstance(row, dict)]
        if "daemon.py" in release_paths
        else []
    )
    checked_in = [
        dict(row)
        for row in inventory.get("launchagents", {}).get("checked_in", ())
        if isinstance(row, dict) and row.get("source") in release_paths
    ]
    tests = [path for path in inventory.get("test_modules", ()) if path in release_paths]
    projected = {
        "schema_version": inventory.get("schema_version"),
        "source": inventory.get("source"),
        "root_name": inventory.get("root_name"),
        "counts": {
            "http_routes": len(routes),
            "skill_entrypoints": len(skills),
            "active_skill_entrypoints": sum(
                1 for skill in skills if skill.get("lifecycle") == "active"
            ),
            "versioned_skill_artifacts": sum(
                1
                for skill in skills
                if skill.get("lifecycle") == "versioned_rollback_artifact"
            ),
            "cron_jobs": len(cron_jobs),
            "enabled_cron_jobs": sum(1 for job in cron_jobs if job.get("enabled") is True),
            "daemon_child_declarations": len(daemon_children),
            "checked_in_launchagents": len(checked_in),
            "installed_launchagents": 0,
            "test_modules": len(tests),
        },
        "http_routes": routes,
        "skill_entrypoints": skills,
        "cron_jobs": [dict(row) for row in cron_jobs],
        "daemon_children": daemon_children,
        "launchagents": {"checked_in": checked_in, "installed": []},
        "test_modules": tests,
    }
    return projected


def collect_daemon_children(root: Path) -> list[dict[str, Any]]:
    path = root / "daemon.py"
    text = path.read_text(encoding="utf-8")
    pattern = re.compile(
        r'start_process\(\s*["\'](?P<name>[^"\']+)["\']\s*,\s*'
        r'(?P<command>\[[^\n]+\]|[^\n\)]+)',
    )
    children = []
    for match in pattern.finditer(text):
        line = text.count("\n", 0, match.start()) + 1
        children.append(
            {
                "name": match.group("name"),
                "declaration": " ".join(match.group("command").split()),
                "source": "daemon.py",
                "line": line,
            }
        )
    unique = {(item["name"], item["line"]): item for item in children}
    return sorted(unique.values(), key=lambda item: (item["name"], item["line"]))


def _plist_record(path: Path, *, root: Path | None = None) -> dict[str, Any] | None:
    try:
        value = plistlib.loads(path.read_bytes())
    except (OSError, plistlib.InvalidFileException):
        return None
    if root is not None:
        source = str(path.relative_to(root))
    else:
        source = str(path)
    return {
        "label": str(value.get("Label") or path.stem),
        "source": source,
        "run_at_load": bool(value.get("RunAtLoad", False)),
        "keep_alive": value.get("KeepAlive", False),
        "start_interval": value.get("StartInterval"),
        "start_calendar_interval": value.get("StartCalendarInterval"),
        "program_arguments": [
            _portable_text(item, root) if root is not None else str(item)
            for item in (value.get("ProgramArguments") or [])
        ],
    }


def collect_launchagents(root: Path, *, include_installed: bool) -> dict[str, list[dict[str, Any]]]:
    checked_in: list[dict[str, Any]] = []
    for path in sorted((root / "config" / "launchagents").glob("*.plist")):
        if record := _plist_record(path, root=root):
            checked_in.append(record)
    installed: list[dict[str, Any]] = []
    if include_installed:
        for path in sorted((Path.home() / "Library" / "LaunchAgents").glob("com.magi*.plist")):
            if record := _plist_record(path):
                installed.append(record)
    return {"checked_in": checked_in, "installed": installed}


def build_inventory(root: Path, *, include_installed_launchagents: bool = False) -> dict[str, Any]:
    routes = collect_routes(root)
    skills = collect_skills(root)
    cron = collect_cron(root)
    tests = sorted(str(path.relative_to(root)) for path in (root / "tests").glob("test_*.py"))
    launchagents = collect_launchagents(root, include_installed=include_installed_launchagents)
    return {
        "schema_version": 1,
        "source": "derived_from_executable_source_not_readme",
        "root_name": root.name,
        "counts": {
            "http_routes": len(routes),
            "skill_entrypoints": len(skills),
            "active_skill_entrypoints": sum(1 for skill in skills if skill["lifecycle"] == "active"),
            "versioned_skill_artifacts": sum(
                1 for skill in skills if skill["lifecycle"] == "versioned_rollback_artifact"
            ),
            "cron_jobs": len(cron),
            "enabled_cron_jobs": sum(1 for job in cron if job["enabled"]),
            "daemon_child_declarations": len(collect_daemon_children(root)),
            "checked_in_launchagents": len(launchagents["checked_in"]),
            "installed_launchagents": len(launchagents["installed"]),
            "test_modules": len(tests),
        },
        "http_routes": routes,
        "skill_entrypoints": skills,
        "cron_jobs": cron,
        "daemon_children": collect_daemon_children(root),
        "launchagents": launchagents,
        "test_modules": tests,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--output", type=Path)
    parser.add_argument("--include-installed-launchagents", action="store_true")
    parser.add_argument("--check", action="store_true", help="Fail when output differs from the generated inventory")
    args = parser.parse_args()
    root = args.root.resolve()
    inventory = build_inventory(root, include_installed_launchagents=args.include_installed_launchagents)
    rendered = json.dumps(inventory, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.check:
        if not args.output or not args.output.exists():
            print("inventory output is missing")
            return 1
        if args.output.read_text(encoding="utf-8") != rendered:
            print("V2 compatibility inventory is stale; regenerate it")
            return 1
        print("V2 compatibility inventory is current")
        return 0
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
