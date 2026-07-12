#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


MAGI_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RUNTIME_DIR = MAGI_ROOT / ".runtime"
MODES = {"audit", "repair-safe", "repair-propose"}
_TMP_OWNERSHIP_SENTINEL = ".magi-self-repair-owned"
_TMP_OWNERSHIP_TEXT = "magi-self-repair-owned-v1"
_LAF_UPLOAD_STAGING_DIR = "magi_laf_upload_pdf"
_LAF_PDFTEXT_STAGING_DIR = "magi_laf_pdftotext"
_LAF_UPLOAD_STAGING_RETENTION_MINUTES = 14 * 24 * 60
_LAF_PDFTEXT_RETENTION_MINUTES = 2 * 24 * 60
_LAF_UPLOAD_RUN_NAME = re.compile(r"\d{8}_\d{6}_[\w-]+$")
_SELF_HEALTH_ARTIFACTS = {
    ".runtime/function_health_index_latest.json",
    ".runtime/magi_self_repair_guardian_latest.json",
}
_SELF_CRON_JOB_IDS = {"job_magi_self_repair_guardian"}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _json_dumps(payload: Any, *, compact: bool = False) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=None if compact else 2, sort_keys=False)


def _ensure_import_root(root: Path) -> None:
    root_text = str(root)
    if root_text not in sys.path:
        sys.path.insert(0, root_text)


def _safe_resolve(path: Path) -> Path:
    try:
        return path.expanduser().resolve()
    except Exception:
        return path.expanduser().absolute()


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except Exception:
        return False


def _tmp_ownership_marker(path: Path) -> Path:
    if path.is_dir() and not path.is_symlink():
        return path / _TMP_OWNERSHIP_SENTINEL
    return path.with_name(path.name + _TMP_OWNERSHIP_SENTINEL)


def _tmp_is_owned(path: Path) -> bool:
    marker = _tmp_ownership_marker(path)
    try:
        return (
            not marker.is_symlink()
            and marker.is_file()
            and marker.read_text(encoding="utf-8").strip() == _TMP_OWNERSHIP_TEXT
        )
    except Exception:
        return False


def _candidate_age_minutes(path: Path, *, now_ts: float) -> float | None:
    try:
        return round(max(0.0, now_ts - float(path.lstat().st_mtime)) / 60.0, 3)
    except Exception:
        return None


def _laf_upload_staging_child(path: Path, tmp_root: Path) -> bool:
    parent = tmp_root / _LAF_UPLOAD_STAGING_DIR
    literal_parent = path.expanduser().absolute().parent
    expected_parent = parent.expanduser().absolute()
    return (
        not path.is_symlink()
        and path.is_dir()
        and not parent.is_symlink()
        and literal_parent == expected_parent
        and _safe_resolve(literal_parent) == _safe_resolve(parent)
        and bool(_LAF_UPLOAD_RUN_NAME.fullmatch(path.name))
    )


def _managed_laf_tmp_candidates(*, parent: Path, tmp_root: Path, now_ts: float) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    try:
        children = sorted(parent.iterdir())
    except Exception as exc:
        return [{"path": str(parent), "age_minutes": None, "kind": "unknown", "eligible": False, "requires_human": True, "reason": f"scan failed: {exc}"}]
    for child in children:
        age_minutes = _candidate_age_minutes(child, now_ts=now_ts)
        if parent.name == _LAF_UPLOAD_STAGING_DIR and _laf_upload_staging_child(child, tmp_root):
            owned = _tmp_is_owned(child)
            eligible = (
                owned
                and age_minutes is not None
                and age_minutes >= _LAF_UPLOAD_STAGING_RETENTION_MINUTES
            )
            candidates.append(
                {
                    "path": str(child),
                    "age_minutes": age_minutes,
                    "kind": "managed_laf_upload_staging",
                    "owned": owned,
                    "eligible": eligible,
                    # Legacy run directories have no proof of MAGI ownership.  They
                    # are never auto-deleted, including while inside retention.
                    "requires_human": not owned,
                    "reason": (
                        "LAF upload staging retention elapsed"
                        if eligible
                        else (
                            "missing MAGI ownership sentinel"
                            if not owned
                            else "LAF upload staging within 14-day retention"
                        )
                    ),
                }
            )
            continue
        if (
            parent.name == _LAF_PDFTEXT_STAGING_DIR
            and not child.is_symlink()
            and child.is_file()
            and child.name.startswith("pdftotext_")
            and child.suffix == ".txt"
        ):
            eligible = age_minutes is not None and age_minutes >= _LAF_PDFTEXT_RETENTION_MINUTES
            candidates.append(
                {
                    "path": str(child),
                    "age_minutes": age_minutes,
                    "kind": "managed_laf_pdftotext",
                    "owned": True,
                    "eligible": eligible,
                    "requires_human": False,
                    "reason": "LAF pdftotext retention elapsed" if eligible else "LAF pdftotext within 2-day retention",
                }
            )
            continue
        candidates.append(
            {
                "path": str(child),
                "age_minutes": age_minutes,
                "kind": "unknown",
                "owned": False,
                "eligible": False,
                "requires_human": True,
                "reason": "unexpected item in managed LAF staging directory",
            }
        )
    return candidates


def _run_doctor_report(*, root: Path, runtime_dir: Path, live: bool) -> dict[str, Any]:
    _ensure_import_root(root)
    from scripts import magi_doctor

    return magi_doctor.collect_report(live=live, runtime_dir=runtime_dir)


def _run_function_health_report(
    *,
    root: Path,
    runtime_dir: Path,
    max_health_age_hours: float,
    include_static: bool,
) -> dict[str, Any]:
    _ensure_import_root(root)
    from scripts.ops import function_health_index

    return function_health_index.build_index(
        root=root,
        matrix_path=root / "config" / "test_matrix.json",
        runtime_dir=runtime_dir,
        max_health_age_hours=max_health_age_hours,
        include_static=include_static,
        ignore_cron_job_ids=_SELF_CRON_JOB_IDS,
    )


def _issue(
    *,
    issue_id: str,
    source: str,
    category: str,
    severity: str,
    summary: str,
    evidence: dict[str, Any] | None = None,
    recommendation: str = "",
    auto_repair: str | None = None,
    status: str = "needs_human",
) -> dict[str, Any]:
    payload = {
        "id": issue_id,
        "source": source,
        "category": category,
        "severity": severity,
        "status": status,
        "summary": summary,
        "evidence": evidence or {},
        "recommendation": recommendation,
        "auto_repair": auto_repair,
    }
    return payload


def _collect_doctor_issues(report: dict[str, Any]) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    for check in report.get("checks") or []:
        if not isinstance(check, dict):
            continue
        status = str(check.get("status") or "").lower()
        if status not in {"warn", "fail"}:
            continue
        name = str(check.get("name") or "unknown")
        if name == "cron_state_failures":
            detail = str(check.get("detail") or "")
            failed_job_ids = set(re.findall(r"\bjob_[A-Za-z0-9_]+", detail))
            if failed_job_ids and failed_job_ids <= _SELF_CRON_JOB_IDS:
                continue
        severity = "error" if status == "fail" else "warning"
        issues.append(
            _issue(
                issue_id=f"doctor:{name}",
                source="magi_doctor",
                category="runtime_health",
                severity=severity,
                summary=f"MAGI Doctor {status}: {name}",
                evidence={
                    "status": status,
                    "detail": check.get("detail"),
                    "fix": check.get("fix"),
                },
                recommendation=str(check.get("fix") or "Run scripts/magi_doctor.py --json and inspect this check."),
            )
        )
    return issues


def _collect_health_issues(report: dict[str, Any]) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    runtime_health = report.get("runtime_health") or {}
    specs = (
        ("failed", "error", "failed health artifact"),
        ("missing", "error", "missing expected health artifact"),
        ("stale", "warning", "stale expected health artifact"),
        ("observed_failed", "warning", "observed failed health artifact"),
        ("observed_stale", "info", "observed stale health artifact"),
    )
    for key, severity, label in specs:
        for item in runtime_health.get(key) or []:
            if not isinstance(item, dict):
                continue
            path = str(item.get("path") or "unknown")
            if path in _SELF_HEALTH_ARTIFACTS:
                continue
            issues.append(
                _issue(
                    issue_id=f"function_health:{key}:{path}",
                    source="function_health_index",
                    category="function_health",
                    severity=severity,
                    summary=f"{label}: {path}",
                    evidence=item,
                    recommendation=(
                        "Rerun the owning live check or cron job, then rebuild the function health index. "
                        "Do not delete expected health files unless the owning check has been retired."
                    ),
                )
            )
    return issues


def _tmp_candidates(*, tmp_dir: Path, now: datetime, min_age_minutes: float) -> list[dict[str, Any]]:
    tmp_root = _safe_resolve(tmp_dir)
    if not tmp_root.exists() or not tmp_root.is_dir():
        return []
    candidates: list[dict[str, Any]] = []
    min_age_sec = max(0.0, float(min_age_minutes) * 60.0)
    now_ts = now.timestamp()
    for path in sorted(tmp_root.glob("magi_*")):
        literal = path.expanduser().absolute()
        if not _is_relative_to(literal, tmp_root):
            continue
        try:
            stat = path.lstat()
        except FileNotFoundError:
            continue
        except Exception as exc:
            candidates.append(
                {
                    "path": str(path),
                    "age_minutes": None,
                    "kind": "unknown",
                    "eligible": False,
                    "reason": f"stat failed: {exc}",
                }
            )
            continue
        if path.name in {_LAF_UPLOAD_STAGING_DIR, _LAF_PDFTEXT_STAGING_DIR} and path.is_dir() and not path.is_symlink():
            candidates.extend(_managed_laf_tmp_candidates(parent=path, tmp_root=tmp_root, now_ts=now_ts))
            continue
        age_minutes = round(max(0.0, now_ts - float(stat.st_mtime)) / 60.0, 3)
        if path.is_symlink():
            kind = "symlink"
        elif path.is_dir():
            kind = "directory"
        elif path.is_file():
            kind = "file"
        else:
            kind = "other"
        old_enough = age_minutes * 60.0 >= min_age_sec
        owned = _tmp_is_owned(path)
        eligible = old_enough and owned and kind in {"file", "symlink"}
        requires_human = old_enough and not eligible
        if not old_enough:
            reason = "newer than threshold"
        elif kind == "directory":
            reason = "directory cleanup requires human review"
        elif not owned:
            reason = "missing MAGI ownership sentinel"
        else:
            reason = "owned stale artifact"
        candidates.append(
            {
                "path": str(path),
                "age_minutes": age_minutes,
                "kind": kind,
                "owned": owned,
                "eligible": eligible,
                "requires_human": requires_human,
                "reason": reason,
            }
        )
    return candidates


def _collect_tmp_issues(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    eligible = [item for item in candidates if item.get("eligible")]
    needs_human = [item for item in candidates if item.get("requires_human")]
    issues: list[dict[str, Any]] = []
    if eligible:
        issues.append(
            _issue(
            issue_id="tmp:magi_residue",
            source="tmp_scan",
                category="safe_runtime_cleanup",
                severity="warning",
                status="auto_repair_available",
                summary=f"{len(eligible)} stale /tmp MAGI artifact(s) can be removed safely",
                # Repair must receive the complete eligible set.  Truncating this
                # list would allow a partial cleanup to be reported as resolved.
                evidence={"candidates": eligible, "candidate_count": len(eligible)},
            recommendation="Run repair-safe mode to remove stale /tmp/magi_* artifacts.",
            auto_repair="delete_stale_tmp_magi_artifacts",
            )
        )
    if needs_human:
        issues.append(
            _issue(
                issue_id="tmp:magi_unowned_residue",
                source="tmp_scan",
                category="safe_runtime_cleanup",
                severity="warning",
                summary=f"{len(needs_human)} stale /tmp MAGI artifact(s) require ownership review",
                evidence={"candidates": needs_human[:50], "candidate_count": len(needs_human)},
                recommendation="Inspect these unowned or directory artifacts before deleting them manually.",
            )
        )
    return issues


def _delete_tmp_candidate(path_text: str, *, tmp_dir: Path) -> dict[str, Any]:
    path = Path(path_text).expanduser()
    tmp_root = _safe_resolve(tmp_dir)
    literal = path.absolute()
    if not _is_relative_to(literal, tmp_root):
        return {"path": str(path), "status": "skipped", "reason": "outside tmp root"}
    age_minutes = _candidate_age_minutes(path, now_ts=_utc_now().timestamp())
    if _laf_upload_staging_child(path, tmp_root):
        if not _tmp_is_owned(path):
            return {"path": str(path), "status": "skipped", "reason": "missing MAGI ownership sentinel"}
        if age_minutes is None or age_minutes < _LAF_UPLOAD_STAGING_RETENTION_MINUTES:
            return {"path": str(path), "status": "skipped", "reason": "LAF upload staging is within retention"}
        try:
            shutil.rmtree(path)
            return {"path": str(path), "status": "applied", "reason": "deleted expired LAF upload staging"}
        except FileNotFoundError:
            return {"path": str(path), "status": "applied", "reason": "already gone"}
        except Exception as exc:
            return {"path": str(path), "status": "failed", "reason": str(exc)}
    pdftotext_parent = tmp_root / _LAF_PDFTEXT_STAGING_DIR
    if (
        _safe_resolve(path.parent) == _safe_resolve(pdftotext_parent)
        and not path.is_symlink()
        and path.is_file()
        and path.name.startswith("pdftotext_")
        and path.suffix == ".txt"
    ):
        if age_minutes is None or age_minutes < _LAF_PDFTEXT_RETENTION_MINUTES:
            return {"path": str(path), "status": "skipped", "reason": "LAF pdftotext is within retention"}
        try:
            path.unlink()
            return {"path": str(path), "status": "applied", "reason": "deleted expired LAF pdftotext"}
        except FileNotFoundError:
            return {"path": str(path), "status": "applied", "reason": "already gone"}
        except Exception as exc:
            return {"path": str(path), "status": "failed", "reason": str(exc)}
    if path.name == "" or not path.name.startswith("magi_"):
        return {"path": str(path), "status": "skipped", "reason": "basename does not start with magi_"}
    if path.is_dir() and not path.is_symlink():
        return {"path": str(path), "status": "skipped", "reason": "directory cleanup requires human review"}
    marker = _tmp_ownership_marker(path)
    if not _tmp_is_owned(path):
        return {"path": str(path), "status": "skipped", "reason": "missing MAGI ownership sentinel"}
    try:
        if path.is_symlink() or path.is_file():
            path.unlink()
        else:
            return {"path": str(path), "status": "skipped", "reason": "unsupported artifact kind"}
        marker.unlink(missing_ok=True)
        return {"path": str(path), "status": "applied", "reason": "deleted"}
    except FileNotFoundError:
        return {"path": str(path), "status": "applied", "reason": "already gone"}
    except Exception as exc:
        return {"path": str(path), "status": "failed", "reason": str(exc)}


def _apply_safe_repairs(*, issues: list[dict[str, Any]], tmp_dir: Path) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    for issue in issues:
        if issue.get("auto_repair") != "delete_stale_tmp_magi_artifacts":
            continue
        candidates = (issue.get("evidence") or {}).get("candidates") or []
        eligible = [item for item in candidates if isinstance(item, dict) and item.get("eligible")]
        results = [
            _delete_tmp_candidate(str(item.get("path") or ""), tmp_dir=tmp_dir)
            for item in eligible
        ]
        failed = [item for item in results if item.get("status") == "failed"]
        skipped = [item for item in results if item.get("status") not in {"applied", "failed"}]
        complete = len(results) == len(eligible) and not failed and not skipped
        issue["status"] = "resolved" if complete else "partial"
        issue["repair_result"] = {
            "eligible": len(eligible),
            "attempted": len(results),
            "applied": sum(1 for item in results if item.get("status") == "applied"),
            "failed": len(failed),
            "skipped": len(skipped),
        }
        actions.append(
            {
                "id": "delete_stale_tmp_magi_artifacts",
                "issue_id": issue.get("id"),
                "kind": "safe_cleanup",
                "status": "failed" if failed else ("applied" if complete else "partial"),
                "results": results,
            }
        )
    return actions


def _planned_actions(issues: list[dict[str, Any]], *, mode: str) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    for issue in issues:
        auto_repair = issue.get("auto_repair")
        if auto_repair:
            actions.append(
                {
                    "id": auto_repair,
                    "issue_id": issue.get("id"),
                    "kind": "safe_cleanup",
                    "status": "planned" if mode != "repair-safe" else "eligible",
                }
            )
        elif issue.get("severity") in {"warning", "error"}:
            actions.append(
                {
                    "id": f"propose:{issue.get('id')}",
                    "issue_id": issue.get("id"),
                    "kind": "human_confirmed_repair",
                    "status": "requires_human",
                    "recommendation": issue.get("recommendation"),
                }
            )
    return actions


def _open_issue(issue: dict[str, Any]) -> bool:
    if issue.get("severity") == "info":
        return False
    return issue.get("status") not in {"resolved", "ignored"}


def _summarize(
    issues: list[dict[str, Any]],
    actions: list[dict[str, Any]],
    verifications: list[dict[str, Any]],
) -> dict[str, Any]:
    open_issues = [issue for issue in issues if _open_issue(issue)]
    human = [
        issue
        for issue in open_issues
        if not issue.get("auto_repair") and issue.get("severity") in {"warning", "error"}
    ]
    return {
        "issue_count": len(issues),
        "open_issue_count": len(open_issues),
        "error_count": sum(1 for issue in open_issues if issue.get("severity") == "error"),
        "warning_count": sum(1 for issue in open_issues if issue.get("severity") == "warning"),
        "safe_auto_repair_available_count": sum(
            1 for issue in open_issues if bool(issue.get("auto_repair"))
        ),
        "human_required_count": len(human),
        "action_count": len(actions),
        "applied_action_count": sum(1 for action in actions if action.get("status") == "applied"),
        "failed_action_count": sum(1 for action in actions if action.get("status") == "failed"),
        "partial_action_count": sum(1 for action in actions if action.get("status") == "partial"),
        "verification_failed_count": sum(1 for item in verifications if not item.get("ok")),
    }


def build_report(
    *,
    root: Path = MAGI_ROOT,
    runtime_dir: Path | None = None,
    mode: str = "audit",
    live_doctor: bool = True,
    include_doctor: bool = True,
    include_function_health: bool = True,
    include_static_health: bool = True,
    verify: bool = True,
    max_health_age_hours: float = 72.0,
    tmp_dir: Path | None = None,
    tmp_min_age_minutes: float = 30.0,
) -> dict[str, Any]:
    if mode not in MODES:
        raise ValueError(f"unsupported mode: {mode}")
    root = root.expanduser().resolve()
    if runtime_dir is None:
        _ensure_import_root(root)
        from scripts.ops import function_health_index

        runtime_dir = function_health_index.default_runtime_dir(root)
    runtime_dir = runtime_dir.expanduser().resolve()
    tmp_dir = (tmp_dir or Path(tempfile.gettempdir())).expanduser().resolve()
    generated_at = _utc_now()

    diagnostics: dict[str, Any] = {}
    issues: list[dict[str, Any]] = []
    if include_doctor:
        doctor_report = _run_doctor_report(root=root, runtime_dir=runtime_dir, live=live_doctor)
        diagnostics["doctor"] = {
            "ok": doctor_report.get("ok"),
            "status": doctor_report.get("status"),
            "summary": doctor_report.get("summary"),
        }
        issues.extend(_collect_doctor_issues(doctor_report))
    if include_function_health:
        health_report = _run_function_health_report(
            root=root,
            runtime_dir=runtime_dir,
            max_health_age_hours=max_health_age_hours,
            include_static=include_static_health,
        )
        diagnostics["function_health"] = {
            "ok": health_report.get("ok"),
            "summary": health_report.get("summary"),
        }
        issues.extend(_collect_health_issues(health_report))

    tmp_candidates = _tmp_candidates(tmp_dir=tmp_dir, now=generated_at, min_age_minutes=tmp_min_age_minutes)
    diagnostics["tmp_scan"] = {
        "tmp_dir": str(tmp_dir),
        "candidate_count": len(tmp_candidates),
        "eligible_count": sum(1 for item in tmp_candidates if item.get("eligible")),
        "min_age_minutes": tmp_min_age_minutes,
    }
    issues.extend(_collect_tmp_issues(tmp_candidates))

    actions = _planned_actions(issues, mode=mode)
    if mode == "repair-safe":
        applied = _apply_safe_repairs(issues=issues, tmp_dir=tmp_dir)
        applied_ids = {action.get("id") for action in applied}
        actions = [action for action in actions if action.get("id") not in applied_ids]
        actions.extend(applied)

    verifications: list[dict[str, Any]] = []
    if mode == "repair-safe" and verify:
        remaining = _tmp_candidates(tmp_dir=tmp_dir, now=_utc_now(), min_age_minutes=tmp_min_age_minutes)
        verifications.append(
            {
                "id": "tmp_residue_after_repair",
                "ok": not any(item.get("eligible") for item in remaining),
                "remaining_eligible_count": sum(1 for item in remaining if item.get("eligible")),
            }
        )

    summary = _summarize(issues, actions, verifications)
    requires_human = [
        issue
        for issue in issues
        if _open_issue(issue)
        and not issue.get("auto_repair")
        and issue.get("severity") in {"warning", "error"}
    ]
    ok = (
        summary["open_issue_count"] == 0
        and summary["failed_action_count"] == 0
        and summary["partial_action_count"] == 0
        and summary["verification_failed_count"] == 0
    )

    return {
        "ok": ok,
        "mode": mode,
        "generated_at": generated_at.isoformat(),
        "root": str(root),
        "runtime_dir": str(runtime_dir),
        "summary": summary,
        "diagnostics": diagnostics,
        "issues": issues,
        "actions": actions,
        "verifications": verifications,
        "requires_human": requires_human,
        "unresolved_issue_ids": [str(issue.get("id") or "") for issue in issues if _open_issue(issue)],
        "safety": {
            "auto_repair_policy": "Only owned stale /tmp/magi_* files or symlinks are deleted automatically.",
            "safe_auto_repairs": ["delete_stale_tmp_magi_artifacts"],
            "requires_human_categories": [
                "doctor warnings/failures",
                "function health failed/stale/missing artifacts",
                "cron failures or missing cron state",
                "daemon, credential, model, database, NAS, and portal changes",
            ],
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="MAGI conservative autonomous self-repair guardian.")
    parser.add_argument("--mode", choices=sorted(MODES), default="audit", help="audit, propose repairs, or apply safe repairs.")
    parser.add_argument("--root", default=str(MAGI_ROOT), help="MAGI root to inspect.")
    parser.add_argument("--runtime-dir", default="", help="Runtime directory; defaults to <root>/.runtime.")
    parser.add_argument("--json-out", default="", help="Write JSON report to this path.")
    parser.add_argument("--compact", action="store_true", help="Print compact JSON.")
    parser.add_argument("--fail-on-issues", action="store_true", help="Return 1 when unresolved issues remain.")
    parser.add_argument("--no-doctor", action="store_true", help="Skip MAGI Doctor checks.")
    parser.add_argument("--no-live-doctor", action="store_true", help="Skip localhost probes in MAGI Doctor.")
    parser.add_argument("--no-function-health", action="store_true", help="Skip function health index checks.")
    parser.add_argument("--no-static-health", action="store_true", help="Do not scan static/*latest*.json health artifacts.")
    parser.add_argument("--no-verify", action="store_true", help="Skip post-repair verification.")
    parser.add_argument("--max-health-age-hours", type=float, default=72.0)
    parser.add_argument("--tmp-dir", default=str(Path(tempfile.gettempdir())), help="Temp directory to scan for magi_* residue.")
    parser.add_argument("--tmp-min-age-minutes", type=float, default=30.0, help="Only clean temp artifacts older than this.")
    args = parser.parse_args(argv)

    root = Path(args.root).expanduser().resolve()
    runtime_dir = Path(args.runtime_dir).expanduser().resolve() if args.runtime_dir else None
    report = build_report(
        root=root,
        runtime_dir=runtime_dir,
        mode=args.mode,
        live_doctor=not args.no_live_doctor,
        include_doctor=not args.no_doctor,
        include_function_health=not args.no_function_health,
        include_static_health=not args.no_static_health,
        verify=not args.no_verify,
        max_health_age_hours=args.max_health_age_hours,
        tmp_dir=Path(args.tmp_dir),
        tmp_min_age_minutes=args.tmp_min_age_minutes,
    )

    payload = _json_dumps(report, compact=args.compact)
    if args.json_out:
        out = Path(args.json_out).expanduser()
        if not out.is_absolute():
            out = root / out
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(payload + "\n", encoding="utf-8")
    print(payload)
    if args.fail_on_issues and not report["ok"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
