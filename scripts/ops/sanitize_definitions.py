#!/usr/bin/env python3
"""
Sanitize skills/definitions.json to keep only runnable tools.

Safe behavior:
- Creates timestamped backup before overwrite.
- Writes dropped-tool report for audit.
- Keeps non /skills/run tools unchanged, except endpoint dependency checks.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from skills.catalog import iter_top_level_skill_dirs

SKILLS_ROOT = ROOT / "skills"
DEFINITIONS_PATH = SKILLS_ROOT / "definitions.json"
DEFAULT_SKILL_RE = re.compile(r"default:\s*([A-Za-z0-9._-]+)")

# Endpoint-level dependency guard:
# if endpoint requires a specific runnable skill and that skill is missing, drop the tool.
TOOL_ENDPOINT_SKILL_DEPENDENCIES = {
    "/laf/smoke_login": "code-laf_automation_v2",
}


def _discover_runnable_skill_dirs() -> set[str]:
    return {entry.name for entry in iter_top_level_skill_dirs(SKILLS_ROOT, runnable_only=True)}


def _extract_default_skill_from_tool(tool: dict[str, Any]) -> str:
    params = tool.get("parameters") or {}
    props = params.get("properties") or {}
    skill_prop = props.get("skill") or {}
    default_value = str(skill_prop.get("default") or "").strip()
    if default_value:
        return default_value
    desc = str(skill_prop.get("description") or "")
    m = DEFAULT_SKILL_RE.search(desc)
    return m.group(1).strip() if m else ""


def _infer_skill_from_run_tool_name(tool_name: str, available_dirs: set[str]) -> str:
    if not (isinstance(tool_name, str) and tool_name.startswith("run_")):
        return ""
    raw = tool_name[4:]
    candidates = [raw, raw.replace("_", "-"), raw.replace("-", "_")]
    for c in candidates:
        if c in available_dirs:
            return c
    return ""


def _sanitize_payload(payload: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, str]]]:
    if not isinstance(payload, dict):
        return payload, []
    tools = payload.get("tools")
    if not isinstance(tools, list):
        return payload, []

    available_dirs = _discover_runnable_skill_dirs()
    filtered_tools: list[dict[str, Any]] = []
    dropped: list[dict[str, str]] = []

    for tool in tools:
        if not isinstance(tool, dict):
            dropped.append({"name": "<invalid>", "reason": "invalid_tool_record"})
            continue

        endpoint = str(tool.get("endpoint") or "").strip()
        name = str(tool.get("name") or "").strip()

        dep_skill = TOOL_ENDPOINT_SKILL_DEPENDENCIES.get(endpoint)
        if dep_skill and dep_skill not in available_dirs:
            dropped.append({"name": name, "reason": f"missing_dependency_skill:{dep_skill}"})
            continue

        if endpoint != "/skills/run":
            filtered_tools.append(tool)
            continue

        default_skill = _extract_default_skill_from_tool(tool)
        if not default_skill:
            default_skill = _infer_skill_from_run_tool_name(name, available_dirs)
        if (not default_skill) or (default_skill not in available_dirs):
            dropped.append({"name": name, "reason": "unrunnable_skill"})
            continue

        # Normalize run-tool schema to pin skill target.
        t = dict(tool)
        params = dict(t.get("parameters") or {})
        props = dict(params.get("properties") or {})
        skill_prop = dict(props.get("skill") or {})
        skill_desc = str(skill_prop.get("description") or "").strip() or f"Skill folder name (default: {default_skill})"
        skill_prop.update(
            {
                "type": "string",
                "default": default_skill,
                "enum": [default_skill],
                "description": skill_desc,
            }
        )
        props["skill"] = skill_prop

        params["type"] = "object"
        params["properties"] = props
        required = params.get("required")
        if not isinstance(required, list):
            required = []
        if "task" not in required:
            required.append("task")
        if "skill" not in required:
            required.append("skill")
        params["required"] = required
        t["parameters"] = params
        filtered_tools.append(t)

    out = dict(payload)
    out["tools"] = filtered_tools
    meta = dict(out.get("_meta") or {})
    meta["updated"] = datetime.now().strftime("%Y-%m-%d")
    meta["sanitized_by"] = "scripts/ops/sanitize_definitions.py"
    meta["sanitized_at"] = datetime.now().isoformat(timespec="seconds")
    meta["runtime_filter"] = {
        "available_skill_dirs": len(available_dirs),
        "tools_total": len(tools),
        "tools_exposed": len(filtered_tools),
        "dropped_unrunnable_run_tools": len(dropped),
    }
    out["_meta"] = meta
    return out, dropped


def main() -> int:
    ap = argparse.ArgumentParser(description="Sanitize MAGI skills/definitions.json")
    ap.add_argument("--apply", action="store_true", help="Write sanitized output back to definitions.json")
    args = ap.parse_args()

    if not DEFINITIONS_PATH.exists():
        print(json.dumps({"ok": False, "error": "definitions.json not found", "path": str(DEFINITIONS_PATH)}, ensure_ascii=False))
        return 2

    with open(DEFINITIONS_PATH, "r", encoding="utf-8") as f:
        payload = json.load(f)

    out, dropped = _sanitize_payload(payload)
    before = len(payload.get("tools") or []) if isinstance(payload, dict) else 0
    after = len(out.get("tools") or []) if isinstance(out, dict) else 0

    summary = {
        "ok": True,
        "before_tools": before,
        "after_tools": after,
        "dropped_tools": len(dropped),
        "dropped_sample": dropped[:20],
        "definitions_path": str(DEFINITIONS_PATH),
        "applied": bool(args.apply),
    }

    if not args.apply:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = DEFINITIONS_PATH.with_name(f"definitions.backup_{ts}.json")
    dropped_path = DEFINITIONS_PATH.with_name(f"definitions.dropped_{ts}.json")

    with open(backup_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    with open(DEFINITIONS_PATH, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    with open(dropped_path, "w", encoding="utf-8") as f:
        json.dump({"dropped": dropped}, f, ensure_ascii=False, indent=2)

    summary["backup_path"] = str(backup_path)
    summary["dropped_report_path"] = str(dropped_path)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
