#!/usr/bin/env python3
"""Validate the public MAGI agent capability catalog without running tools."""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
from pathlib import Path
from typing import Any, Iterable


MAGI_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CAPABILITIES = MAGI_ROOT / "config" / "agent_capabilities.json"
DEFAULT_LIVE_RUNTIME_ROOT = Path.home() / "Library" / "Application Support" / "MAGI" / "runtime" / "MAGI_v2"
ALLOWED_SIDE_EFFECTS = {
    "read_only",
    "external_read",
    "external_write",
    "local_write",
    "db_write",
    "portal_write",
    "external_notify",
    "database_restore",
    "filesystem_delete",
}
HIGH_RISK_SIDE_EFFECTS = {
    "external_write",
    "db_write",
    "portal_write",
    "external_notify",
    "database_restore",
    "filesystem_delete",
}
MEDIUM_RISK_SIDE_EFFECTS = {"external_read", "local_write"}
TOOL_PATTERN = re.compile(r"^(?P<path>(?:api|scripts|skills)/[A-Za-z0-9_./-]+\.py):(?P<symbol>[A-Za-z_][A-Za-z0-9_]*)$")
CAPABILITY_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]{2,127}$")
PRIVATE_CONTENT_PATTERNS = (
    re.compile(r"(?i)(?:password|secret|api[_-]?key|bearer\s+)"),
    re.compile(r"/(?:Users|Volumes)/"),
    re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
)


def _issue(capability_id: str, field: str, code: str, severity: str) -> dict[str, str]:
    return {"id": capability_id, "field": field, "code": code, "severity": severity}


def _has_value(value: Any) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, dict):
        return bool(value)
    if isinstance(value, list):
        return bool(value)
    return False


def _text_for_public_check(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return ""


def _contains_private_content(value: Any) -> bool:
    text = _text_for_public_check(value)
    return bool(text and any(pattern.search(text) for pattern in PRIVATE_CONTENT_PATTERNS))


def _normalize_side_effects(value: Any) -> tuple[list[str], bool]:
    if isinstance(value, str):
        effects = [value.strip()] if value.strip() else []
    elif isinstance(value, list) and all(isinstance(item, str) for item in value):
        effects = [item.strip() for item in value if item.strip()]
    else:
        return [], False
    return list(dict.fromkeys(effects)), bool(effects)


def _risk_for(effects: Iterable[str]) -> str:
    effect_set = set(effects)
    if effect_set & HIGH_RISK_SIDE_EFFECTS:
        return "high"
    if effect_set & MEDIUM_RISK_SIDE_EFFECTS:
        return "medium"
    return "low"


def _tool_issues(tool: Any, root: Path, capability_id: str) -> list[dict[str, str]]:
    if not isinstance(tool, str) or not tool.strip():
        return [_issue(capability_id, "tool", "missing_tool", "error")]
    match = TOOL_PATTERN.fullmatch(tool.strip())
    if not match:
        return [_issue(capability_id, "tool", "invalid_tool_reference", "error")]

    source_path = (root / match.group("path")).resolve()
    try:
        source_path.relative_to(root.resolve())
    except ValueError:
        return [_issue(capability_id, "tool", "tool_path_outside_root", "error")]
    if not source_path.is_file():
        return [_issue(capability_id, "tool", "tool_source_missing", "error")]
    try:
        tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
    except (OSError, SyntaxError):
        return [_issue(capability_id, "tool", "tool_source_unreadable", "error")]
    symbols = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }
    if match.group("symbol") not in symbols:
        return [_issue(capability_id, "tool", "tool_symbol_missing", "error")]
    return []


def _validate_capability(capability: Any, *, index: int, root: Path) -> tuple[dict[str, Any], list[dict[str, str]]]:
    if not isinstance(capability, dict):
        capability_id = f"invalid-{index}"
        return ({"id": capability_id, "domain": "unknown", "risk": "unknown"}, [_issue(capability_id, "capability", "invalid_capability", "error")])

    capability_id = str(capability.get("id") or f"invalid-{index}")
    domain = str(capability.get("domain") or "unknown")
    effects, effects_valid = _normalize_side_effects(capability.get("side_effect"))
    risk = _risk_for(effects) if effects_valid else "unknown"
    issues: list[dict[str, str]] = []

    if not CAPABILITY_ID_PATTERN.fullmatch(capability_id):
        issues.append(_issue(capability_id, "id", "invalid_capability_id", "error"))
    if not isinstance(capability.get("domain"), str) or not domain.strip():
        issues.append(_issue(capability_id, "domain", "missing_domain", "error"))
    if not _has_value(capability.get("intent")):
        issues.append(_issue(capability_id, "intent", "missing_intent", "error"))
    if not effects_valid:
        issues.append(_issue(capability_id, "side_effect", "missing_side_effect", "error"))
    else:
        for effect in effects:
            if effect not in ALLOWED_SIDE_EFFECTS:
                issues.append(_issue(capability_id, "side_effect", "unknown_side_effect", "error"))

    issues.extend(_tool_issues(capability.get("tool"), root, capability_id))

    verification_severity = "error" if risk == "high" else "warning"
    if not _has_value(capability.get("verify")):
        issues.append(_issue(capability_id, "verify", "missing_verify", verification_severity))

    recovery_severity = "error" if risk == "high" else "warning"
    if not (_has_value(capability.get("rollback")) or _has_value(capability.get("human_handling"))):
        issues.append(_issue(capability_id, "recovery", "missing_recovery", recovery_severity))

    for field in ("intent", "tool", "verify", "rollback", "human_handling"):
        if _contains_private_content(capability.get(field)):
            issues.append(_issue(capability_id, field, "private_content", "error"))

    return ({"id": capability_id, "domain": domain, "risk": risk}, issues)


def build_report(
    catalog: Any,
    *,
    root: Path = MAGI_ROOT,
    strict: bool = False,
    initial_issues: Iterable[dict[str, str]] = (),
) -> dict[str, Any]:
    issues = list(initial_issues)
    capabilities = catalog.get("capabilities") if isinstance(catalog, dict) else None
    if not isinstance(catalog, dict):
        issues.append(_issue("catalog", "catalog", "invalid_catalog", "error"))
        capabilities = []
    elif catalog.get("schema_version") != 1:
        issues.append(_issue("catalog", "schema_version", "unsupported_schema", "error"))
    if not isinstance(capabilities, list) or not capabilities:
        issues.append(_issue("catalog", "capabilities", "missing_capabilities", "error"))
        capabilities = []

    summaries: list[dict[str, Any]] = []
    known_ids: set[str] = set()
    domain_stats: dict[str, dict[str, int]] = {}
    for index, capability in enumerate(capabilities):
        summary, capability_issues = _validate_capability(capability, index=index, root=root)
        capability_id = summary["id"]
        if capability_id in known_ids:
            capability_issues.append(_issue(capability_id, "id", "duplicate_capability_id", "error"))
        known_ids.add(capability_id)
        issues.extend(capability_issues)

        errors = [item for item in capability_issues if item["severity"] == "error"]
        warnings = [item for item in capability_issues if item["severity"] == "warning"]
        ready = not errors and (not strict or not warnings)
        summaries.append(
            {
                "id": capability_id,
                "domain": summary["domain"],
                "risk": summary["risk"],
                "ready": ready,
                "issues": [item["code"] for item in capability_issues],
            }
        )
        stats = domain_stats.setdefault(summary["domain"], {"total": 0, "ready": 0, "high_risk": 0, "issues": 0})
        stats["total"] += 1
        stats["ready"] += int(ready)
        stats["high_risk"] += int(summary["risk"] == "high")
        stats["issues"] += len(capability_issues)

    error_count = sum(item["severity"] == "error" for item in issues)
    warning_count = sum(item["severity"] == "warning" for item in issues)
    ok = error_count == 0 and (not strict or warning_count == 0)
    return {
        "schema_version": 1,
        "compact": True,
        "ok": ok,
        "success": ok,
        "strict": bool(strict),
        "summary": {
            "capability_count": len(summaries),
            "domain_count": len(domain_stats),
            "high_risk_count": sum(item["risk"] == "high" for item in summaries),
            "ready_count": sum(item["ready"] for item in summaries),
            "error_count": error_count,
            "warning_count": warning_count,
        },
        "domains": dict(sorted(domain_stats.items())),
        "capabilities": summaries,
        "issues": issues,
    }


def load_catalog(path: Path) -> tuple[Any, list[dict[str, str]]]:
    try:
        return json.loads(path.read_text(encoding="utf-8")), []
    except FileNotFoundError:
        return {}, [_issue("catalog", "catalog", "catalog_missing", "error")]
    except (OSError, json.JSONDecodeError):
        return {}, [_issue("catalog", "catalog", "catalog_unreadable", "error")]


def run_gate(
    *,
    capabilities_path: Path = DEFAULT_CAPABILITIES,
    root: Path = MAGI_ROOT,
    strict: bool = False,
) -> dict[str, Any]:
    root = root.resolve()
    path = capabilities_path if capabilities_path.is_absolute() else root / capabilities_path
    catalog, initial_issues = load_catalog(path)
    return build_report(catalog, root=root, strict=strict, initial_issues=initial_issues)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n"
    try:
        temp_path.write_text(encoded, encoding="utf-8")
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)


def mirror_live_report(
    report: dict[str, Any],
    output_path: Path,
    *,
    source_root: Path,
    live_root: Path | None = None,
) -> Path | None:
    """Mirror public-safe gate evidence into an installed runtime when present."""
    destination_root = Path(
        live_root
        or os.environ.get("MAGI_LIVE_RUNTIME_ROOT")
        or DEFAULT_LIVE_RUNTIME_ROOT
    ).expanduser()
    if not destination_root.exists() or destination_root.resolve() == source_root.resolve():
        return None
    destination = destination_root / ".runtime" / output_path.name
    _write_json(destination, report)
    return destination


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate MAGI public agent capability readiness contracts.")
    parser.add_argument("--capabilities", default=str(DEFAULT_CAPABILITIES), help="Capability catalog JSON path.")
    parser.add_argument("--root", default=str(MAGI_ROOT), help="Repository root used to validate tool references.")
    parser.add_argument("--json-out", default="", help="Optional compact JSON report path.")
    parser.add_argument("--mirror-live-runtime", action="store_true", help="Mirror public-safe evidence into an installed live runtime when present.")
    parser.add_argument("--strict", action="store_true", help="Treat low-risk contract warnings as failures.")
    args = parser.parse_args(argv)

    root = Path(args.root).expanduser()
    report = run_gate(
        capabilities_path=Path(args.capabilities).expanduser(),
        root=root,
        strict=bool(args.strict),
    )
    if args.json_out:
        output_path = Path(args.json_out).expanduser()
        if not output_path.is_absolute():
            output_path = root / output_path
        _write_json(output_path, report)
        if args.mirror_live_runtime:
            mirror_live_report(report, output_path, source_root=root)
    print(json.dumps(report, ensure_ascii=False, separators=(",", ":"), sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
