#!/usr/bin/env python3
"""Health/LIVE checks for MAGI business modules.

The checks are intentionally non-destructive:
- LAF logs in and scans portal draft/list state without submitting forms.
- File review runs self_test and the portal downloadable probe.
- Transcript runs self_test and DB probe; full sync remains on its own cron.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import argparse
import re
import plistlib
import html
from datetime import datetime
from pathlib import Path
from collections import defaultdict
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
PYTHON = os.environ.get("MAGI_SKILL_PYTHON") or str(REPO_ROOT / "venv" / "bin" / "python3")
if not Path(PYTHON).exists():
    PYTHON = sys.executable

_ACTIVE_SCAN_DIRS = ("api", "casper_ecosystem", "scripts", "skills")
_SOURCE_SKIP_PARTS = {".git", ".pytest_cache", "__pycache__", "venv", "node_modules", "_bg_jobs"}
_HIGH_RISK_ROUTES = {
    "/line/webhook",
    "/telegram/webhook",
    "/webhook/external",
    "/skills/run",
    "/jobs/<job_id>",
    "/api/osc/files/upload",
    "/api/osc/files/upload-multi",
    "/api/osc/files/upload-chunked",
    "/api/osc/files/share",
}
_DEPRECATED_AUTO_DISPATCH_ALIASES = {
    "pdf-annotator": {"pdf_annotate", "pdf_annotator", "run_pdf_annotator"},
}
_AUTO_DISPATCH_FILES = (
    "api/pipelines/skill_dispatch.py",
    "api/pipelines/message_pipeline.py",
    "api/pipelines/message_router.py",
    "skills/bridge/semantic_router.py",
    "skills/bridge/embedding_router.py",
    "skills/definitions.json",
)


_REDACT_KEYS = {
    "applicant",
    "case_number",
    "client_name",
    "court_case_number",
    "email",
    "folder_path",
    "local_path",
    "name",
    "path",
    "phone",
    "recipient",
    "sample",
    "token",
}
_REDACT_PATTERNS = (
    (re.compile(r"\b20\d{2}-\d{4,}\b"), "<CASE_ID>"),
    (re.compile(r"\b1\d{2}年度[^\\s,，。；;\"']{1,28}?字第\d{1,8}號"), "<COURT_CASE_NO>"),
    (re.compile(r"\b09\d{2}[- ]?\d{3}[- ]?\d{3}\b"), "<PHONE>"),
    (re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}"), "<EMAIL>"),
    (re.compile(r"(?i)(token|password|secret|api[_-]?key)[\"':= ]+[^\\s,，。；;\"']+"), r"\1=<REDACTED>"),
    (re.compile(r"(/Users/[^\\s,，。；;\"']+|/Volumes/[^\\s,，。；;\"']+)"), "<PATH>"),
)


def _redact_text(text: Any) -> str:
    out = str(text or "")
    for pattern, replacement in _REDACT_PATTERNS:
        out = pattern.sub(replacement, out)
    return out


def _redact_obj(value: Any, *, key: str = "") -> Any:
    key_lower = str(key or "").lower()
    if any(marker in key_lower for marker in _REDACT_KEYS):
        if isinstance(value, (int, float, bool)) or value is None:
            return value
        if key_lower == "sample" and isinstance(value, list):
            return f"<REDACTED:{len(value)} item(s)>"
        return "<REDACTED>"
    if isinstance(value, dict):
        return {k: _redact_obj(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_obj(item, key=key) for item in value]
    if isinstance(value, str):
        return _redact_text(value)
    return value


def _run(name: str, argv: list[str], timeout: int = 600) -> dict[str, Any]:
    env = os.environ.copy()
    env.setdefault("MAGI_NO_DELETE", "1")
    env.setdefault("MAGI_PREFER_LOCAL_DB", "1")
    try:
        proc = subprocess.run(
            argv,
            cwd=str(REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as e:
        return {"name": name, "ok": False, "error": f"timeout_{timeout}s", "stdout_tail": _redact_text(e.stdout or "")[-1200:]}
    except Exception as e:
        return {"name": name, "ok": False, "error": f"{type(e).__name__}: {e}"}

    parsed = _redact_obj(_parse_last_json(proc.stdout or ""))
    ok = proc.returncode == 0
    if isinstance(parsed, dict):
        ok = ok and bool(parsed.get("success", parsed.get("ok", True)))
    return {
        "name": name,
        "ok": bool(ok),
        "returncode": proc.returncode,
        "parsed": parsed,
        "stdout_tail": _redact_text(proc.stdout or "")[-1600:],
        "stderr_tail": _redact_text(proc.stderr or "")[-1600:],
    }


def _parse_last_json(text: str) -> Any:
    decoder = json.JSONDecoder()
    candidates = [idx for idx, ch in enumerate(text or "") if ch == "{"]
    for idx in reversed(candidates):
        try:
            obj, end = decoder.raw_decode(text[idx:])
        except Exception:
            continue
        if not str(text[idx + end :]).strip():
            return obj
    return None


def _load_json_file(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _iter_source_files(root: Path) -> list[Path]:
    out: list[Path] = []
    for dirname in _ACTIVE_SCAN_DIRS:
        base = root / dirname
        if not base.exists():
            continue
        for path in base.rglob("*"):
            if not path.is_file() or path.suffix not in {".py", ".json"}:
                continue
            rel = path.relative_to(root)
            if any(part in _SOURCE_SKIP_PARTS for part in rel.parts):
                continue
            out.append(path)
    return out


def _normalize_skill_name(name: str) -> str:
    return re.sub(r"[-_\s]+", "-", str(name or "").strip().lower())


def _parse_skill_frontmatter(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8", errors="replace")
    meta: dict[str, Any] = {}
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            for raw in parts[1].splitlines():
                if ":" not in raw:
                    continue
                key, value = raw.split(":", 1)
                key = key.strip()
                value = value.strip().strip("'\"")
                if value.lower() in {"true", "false"}:
                    meta[key] = value.lower() == "true"
                elif key:
                    meta[key] = value
    if "deprecated: true" in text.lower():
        meta["deprecated"] = True
    if "alias_of:" in text:
        match = re.search(r"alias_of:\s*([A-Za-z0-9_.-]+)", text)
        if match:
            meta["alias_of"] = match.group(1)
    if "type: internal-alias" in text:
        meta["type"] = "internal-alias"
    if "shim" in text.lower() and "alias" in text.lower():
        meta.setdefault("shim_alias", True)
    return meta


def _skill_entries(root: Path) -> list[dict[str, Any]]:
    skills_dir = root / "skills"
    entries: list[dict[str, Any]] = []
    if not skills_dir.exists():
        return entries
    for entry in sorted(skills_dir.iterdir(), key=lambda p: p.name):
        skill_md = entry / "SKILL.md"
        if not entry.is_dir() or not skill_md.exists():
            continue
        meta = _parse_skill_frontmatter(skill_md)
        skill_name = str(meta.get("name") or entry.name)
        rel = entry.relative_to(root).as_posix()
        entries.append(
            {
                "dir": entry.name,
                "name": skill_name,
                "normalized": _normalize_skill_name(skill_name or entry.name),
                "path": rel,
                "deprecated": bool(meta.get("deprecated")) or "[deprecated]" in skill_md.read_text(encoding="utf-8", errors="replace").lower(),
                "alias_of": str(meta.get("alias_of") or ""),
                "type": str(meta.get("type") or ""),
                "shim_alias": bool(meta.get("shim_alias")),
            }
        )
    return entries


def _is_skill_alias(entry: dict[str, Any]) -> bool:
    return bool(entry.get("alias_of")) or entry.get("type") == "internal-alias" or bool(entry.get("shim_alias"))


def _audit_duplicate_skills(root: Path) -> dict[str, Any]:
    by_name: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for entry in _skill_entries(root):
        by_name[str(entry["normalized"])].append(entry)
    duplicates = []
    allowed_aliases = []
    for normalized, grouped in sorted(by_name.items()):
        if len(grouped) <= 1:
            continue
        if any(_is_skill_alias(item) for item in grouped):
            allowed_aliases.append({"normalized": normalized, "skills": grouped})
            continue
        duplicates.append({"normalized": normalized, "skills": grouped})
    return {
        "ok": not duplicates,
        "duplicate_count": len(duplicates),
        "duplicates": duplicates,
        "allowed_alias_count": len(allowed_aliases),
        "allowed_aliases": allowed_aliases,
    }


def _module_to_rel(module: str) -> str:
    return module.replace(".", "/") + ".py"


def _audit_deprecated_auto_dispatch(root: Path) -> dict[str, Any]:
    truth = _load_json_file(root / "config" / "single_source_of_truth.json", {})
    features = truth.get("features") if isinstance(truth, dict) else {}
    legacy_hits: list[dict[str, Any]] = []
    legacy_patterns: list[tuple[str, str, str]] = []
    if isinstance(features, dict):
        for feature, spec in features.items():
            if not isinstance(spec, dict):
                continue
            for legacy in spec.get("legacy_modules") or []:
                legacy = str(legacy)
                legacy_patterns.extend(
                    [
                        (str(feature), legacy, f"import {legacy}"),
                        (str(feature), legacy, f"from {legacy} import"),
                    ]
                )
            for pattern in spec.get("forbidden_imports") or []:
                legacy_patterns.append((str(feature), "forbidden_import", str(pattern)))

    dispatch_scan_files = [
        root / rel
        for rel in _AUTO_DISPATCH_FILES
        if (root / rel).exists()
    ]
    for path in dispatch_scan_files:
        rel = path.relative_to(root).as_posix()
        text = path.read_text(encoding="utf-8", errors="replace")
        for feature, legacy, pattern in legacy_patterns:
            if pattern not in text:
                continue
            if legacy != "forbidden_import" and rel == _module_to_rel(legacy):
                continue
            legacy_hits.append(
                {
                    "feature": feature,
                    "legacy_module": legacy,
                    "pattern": pattern,
                    "file": rel,
                }
            )

    deprecated_skills = [entry for entry in _skill_entries(root) if entry.get("deprecated")]
    deprecated_auto_routes: list[dict[str, Any]] = []
    for entry in deprecated_skills:
        aliases = {
            str(entry.get("dir") or "").replace("-", "_"),
            str(entry.get("name") or "").replace("-", "_"),
            f"run_{str(entry.get('dir') or '').replace('-', '_')}",
        }
        aliases.update(_DEPRECATED_AUTO_DISPATCH_ALIASES.get(str(entry.get("dir") or ""), set()))
        aliases = {a for a in aliases if a}
        for rel in _AUTO_DISPATCH_FILES:
            path = root / rel
            if not path.exists():
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for alias in sorted(aliases):
                if re.search(rf"['\"]{re.escape(alias)}['\"]", text):
                    deprecated_auto_routes.append(
                        {
                            "skill": entry.get("dir"),
                            "alias": alias,
                            "file": rel,
                            "severity": "warning",
                            "reason": "deprecated skill is still reachable from semantic/auto dispatch metadata",
                        }
                    )

    return {
        "ok": not legacy_hits,
        "legacy_hit_count": len(legacy_hits),
        "legacy_hits": legacy_hits,
        "deprecated_auto_route_count": len(deprecated_auto_routes),
        "deprecated_auto_routes": deprecated_auto_routes,
    }


_SCRIPT_RE = re.compile(
    r"(?:^|[\s'\"])(?:/[^'\"\s]+?/MAGI_v2/)?"
    r"((?:api|config|scripts|skills)/[^'\"\s]+?\.(?:py|sh))"
)


def _command_script_keys(command: str) -> set[str]:
    text = html.unescape(str(command or ""))
    return {match.group(1) for match in _SCRIPT_RE.finditer(text)}


def _launchd_is_continuous(data: dict[str, Any]) -> bool:
    return bool(data.get("KeepAlive")) or "StartInterval" in data or "StartCalendarInterval" in data


def _audit_cron_dual_executor(root: Path) -> dict[str, Any]:
    cron_jobs = _load_json_file(root / "cron_jobs.json", [])
    cron_scripts: dict[str, list[dict[str, Any]]] = defaultdict(list)
    if isinstance(cron_jobs, list):
        for job in cron_jobs:
            if not isinstance(job, dict) or not job.get("enabled", True):
                continue
            for key in _command_script_keys(str(job.get("command") or "")):
                cron_scripts[key].append({"id": job.get("id"), "cron": job.get("cron"), "desc": job.get("desc")})

    launchd_scripts: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for base in (root / "config" / "launchagents", root / "config" / "launchdaemons"):
        if not base.exists():
            continue
        for path in sorted(base.glob("*.plist")):
            try:
                data = plistlib.loads(path.read_bytes())
            except Exception:
                continue
            if not isinstance(data, dict) or not _launchd_is_continuous(data):
                continue
            args = data.get("ProgramArguments") or []
            command = " ".join(str(part) for part in args) if isinstance(args, list) else str(args)
            for key in _command_script_keys(command):
                launchd_scripts[key].append(
                    {
                        "label": data.get("Label") or path.stem,
                        "plist": path.relative_to(root).as_posix(),
                    }
                )

    conflicts = []
    for key in sorted(set(cron_scripts) & set(launchd_scripts)):
        conflicts.append({"script": key, "cron_jobs": cron_scripts[key], "launchd_jobs": launchd_scripts[key]})
    return {
        "ok": not conflicts,
        "conflict_count": len(conflicts),
        "conflicts": conflicts,
        "cron_script_count": len(cron_scripts),
        "launchd_script_count": len(launchd_scripts),
    }


_ROUTE_RE = re.compile(r"@[\w.]+\.route\(\s*f?[\"']([^\"']+)[\"'](?P<args>[^)]*)\)")
_METHODS_RE = re.compile(r"methods\s*=\s*\[([^\]]+)\]")


def _route_methods(args: str) -> set[str]:
    match = _METHODS_RE.search(args or "")
    if not match:
        return {"GET"}
    methods = re.findall(r"['\"]([A-Z]+)['\"]", match.group(1))
    return set(methods or ["GET"])


def _is_high_risk_route(route: str) -> bool:
    return route in _HIGH_RISK_ROUTES or "webhook" in route.lower()


def _audit_high_risk_endpoint_collisions(root: Path) -> dict[str, Any]:
    routes: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for dirname in ("api", "skills"):
        base = root / dirname
        if not base.exists():
            continue
        for path in base.rglob("*.py"):
            rel = path.relative_to(root)
            if any(part in _SOURCE_SKIP_PARTS for part in rel.parts):
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for match in _ROUTE_RE.finditer(text):
                route = match.group(1)
                if not _is_high_risk_route(route):
                    continue
                line = text.count("\n", 0, match.start()) + 1
                for method in _route_methods(match.group("args")):
                    routes[(route, method)].append({"file": rel.as_posix(), "line": line})
    collisions = []
    for (route, method), hits in sorted(routes.items()):
        files = sorted({hit["file"] for hit in hits})
        if len(files) > 1:
            collisions.append({"route": route, "method": method, "handlers": hits})
    return {
        "ok": not collisions,
        "collision_count": len(collisions),
        "collisions": collisions,
        "scanned_route_count": len(routes),
    }


def live_validation_commands(py: str | None = None) -> dict[str, list[str]]:
    py = py or PYTHON
    return {
        "production_live": [
            py,
            "scripts/ops/run_test_suite.py",
            "--suite",
            "production-live",
            "--json-out",
            ".runtime/production_live_latest.json",
        ],
        "business_modules": [
            py,
            "scripts/ops/business_module_live_check.py",
            "--json",
            "--json-out",
            ".runtime/business_module_live_latest.json",
        ],
        "conflict_audit": [
            py,
            "scripts/ops/business_module_live_check.py",
            "--conflict-audit",
            "--json-out",
            ".runtime/live_conflict_audit_latest.json",
        ],
        "manual_probe": [
            "curl",
            "-fsS",
            "http://127.0.0.1:${MAGI_SERVER_PORT:-5002}/health",
        ],
    }


def audit_live_conflicts(root: Path = REPO_ROOT, *, strict: bool = False) -> dict[str, Any]:
    checks = {
        "duplicate_skills": _audit_duplicate_skills(root),
        "deprecated_auto_dispatch": _audit_deprecated_auto_dispatch(root),
        "cron_dual_executor": _audit_cron_dual_executor(root),
        "high_risk_endpoint_collision": _audit_high_risk_endpoint_collisions(root),
    }
    error_count = sum(
        int(checks[name].get(key) or 0)
        for name, key in (
            ("duplicate_skills", "duplicate_count"),
            ("deprecated_auto_dispatch", "legacy_hit_count"),
            ("cron_dual_executor", "conflict_count"),
            ("high_risk_endpoint_collision", "collision_count"),
        )
    )
    warning_count = int(checks["deprecated_auto_dispatch"].get("deprecated_auto_route_count") or 0)
    ok = error_count == 0 and (warning_count == 0 if strict else True)
    return {
        "ok": ok,
        "success": ok,
        "strict": strict,
        "error_count": error_count,
        "warning_count": warning_count,
        "checks": checks,
        "commands": live_validation_commands(),
    }


def _laf_portal_live() -> dict[str, Any]:
    sys.path.insert(0, str(REPO_ROOT))
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    try:
        import scripts.laf_nightly_audit as audit

        result = audit.scan_portal_pending_drafts(db=None)
        error = _redact_text(result.get("error") or "")
        return {
            "name": "laf_portal_live",
            "ok": not bool(error),
            "parsed": {
                "error": error or None,
                "closing_drafts": len(result.get("closing_drafts") or []),
                "case_status_drafts": len(result.get("case_status_drafts") or []),
                "condition_pending": len(result.get("condition_pending") or []),
                "go_live_pending": len(result.get("go_live_pending") or []),
                "progress_pending": len(result.get("progress_pending") or []),
            },
        }
    except Exception as e:
        return {"name": "laf_portal_live", "ok": False, "error": _redact_text(f"{type(e).__name__}: {e}")}


def _summarize(results: list[dict[str, Any]]) -> str:
    lines = [f"📋 業務三模組 LIVE/健康檢查 — {datetime.now().strftime('%Y-%m-%d %H:%M')}"]
    for r in results:
        mark = "✅" if r.get("ok") else "❌"
        detail = ""
        parsed = r.get("parsed")
        if isinstance(parsed, dict):
            if "downloadable_count" in parsed:
                detail = f"可下載 {parsed.get('downloadable_count')} / 待繳費 {parsed.get('pending_payment_count')}"
            elif "eligible_cases" in parsed:
                detail = f"可同步案件 {parsed.get('eligible_cases')}"
            elif "case_status_drafts" in parsed:
                detail = (
                    f"案件狀態暫存 {parsed.get('case_status_drafts')} / "
                    f"二階段 {parsed.get('condition_pending')} / 開辦 {parsed.get('go_live_pending')}"
                )
            elif parsed.get("errors"):
                detail = str(parsed.get("errors"))[:120]
        if not detail and r.get("error"):
            detail = str(r.get("error"))[:120]
        lines.append(f"{mark} {r.get('name')}: {detail}".rstrip())
    return "\n".join(lines)


def _notify(text: str) -> None:
    if str(os.environ.get("MAGI_BUSINESS_LIVE_CHECK_NOTIFY", "0")).lower() not in {"1", "true", "yes", "on"}:
        return
    try:
        from skills.ops.red_phone import send_telegram_push_with_status

        send_telegram_push_with_status(
            text,
            severity="warning",
            source="business_module_live_check",
            topic_key="check",
        )
    except Exception:
        pass


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run non-destructive MAGI business module LIVE/health checks.")
    parser.add_argument("--json", action="store_true", help="Compatibility flag; output is JSON by default.")
    parser.add_argument("--json-out", help="Write JSON report to this path.")
    parser.add_argument("--conflict-audit", action="store_true", help="Run only the fast live conflict audit.")
    parser.add_argument("--strict-conflicts", action="store_true", help="Treat conflict-audit warnings as failures.")
    parser.add_argument("--print-live-commands", action="store_true", help="Print live validation commands and exit.")
    parser.add_argument("--skip-conflict-audit", action="store_true", help="Skip the fast local conflict audit in the live check.")
    parser.add_argument("--skip-laf-live", action="store_true", help="Skip live LAF portal login/scan.")
    parser.add_argument("--notify", action="store_true", help="Send the summary through the internal check topic.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.print_live_commands:
        payload = {"ok": True, "success": True, "commands": live_validation_commands()}
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    if args.conflict_audit:
        payload = audit_live_conflicts(REPO_ROOT, strict=bool(args.strict_conflicts))
        if args.json_out:
            out_path = Path(args.json_out)
            if not out_path.is_absolute():
                out_path = REPO_ROOT / out_path
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            payload["json_out"] = str(out_path)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if payload.get("ok") else 1

    if args.notify:
        os.environ["MAGI_BUSINESS_LIVE_CHECK_NOTIFY"] = "1"

    results = []
    if not args.skip_conflict_audit:
        conflict = audit_live_conflicts(REPO_ROOT, strict=bool(args.strict_conflicts))
        results.append(
            {
                "name": "live_conflict_audit",
                "ok": bool(conflict.get("ok")),
                "parsed": {
                    "errors": conflict.get("error_count"),
                    "warnings": conflict.get("warning_count"),
                    "commands": conflict.get("commands"),
                },
            }
        )
    results.extend([
        _run("laf_self_test", [PYTHON, str(REPO_ROOT / "skills" / "laf-orchestrator" / "action.py"), "--task", "self_test"], timeout=120),
        _run("file_review_self_test", [PYTHON, str(REPO_ROOT / "skills" / "file-review-orchestrator" / "action.py"), "--task", "self_test"], timeout=120),
        _run(
            "file_review_downloadable_probe",
            [PYTHON, str(REPO_ROOT / "skills" / "file-review-orchestrator" / "action.py"), "--task", 'downloadable_probe {"days":30,"notify":false}'],
            timeout=900,
        ),
        _run("transcript_self_test", [PYTHON, str(REPO_ROOT / "skills" / "transcript-downloader" / "action.py"), "--task", "self_test"], timeout=120),
        _run("transcript_db_probe", [PYTHON, str(REPO_ROOT / "skills" / "transcript-downloader" / "action.py"), "--task", "db_probe"], timeout=180),
    ])
    if args.skip_laf_live:
        results.insert(1, {"name": "laf_portal_live", "ok": True, "skipped": True, "parsed": {"error": None}})
    else:
        results.insert(1, _laf_portal_live())
    ok = all(bool(r.get("ok")) for r in results)
    out = {"ok": ok, "success": ok, "results": results, "message": _summarize(results), "commands": live_validation_commands()}
    if args.json_out:
        out_path = Path(args.json_out)
        if not out_path.is_absolute():
            out_path = REPO_ROOT / out_path
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        out["json_out"] = str(out_path)
    _notify(out["message"])
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
