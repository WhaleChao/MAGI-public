#!/usr/bin/env python3
"""Fail-closed portability audit for the MAGI self-host deployment layer."""

from __future__ import annotations

import argparse
import json
import shlex
import sys
import tempfile
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from magi_v3.selfhost import (  # noqa: E402
    build_distribution_archive,
    build_portable_cron_jobs,
    build_service_plan,
    default_config,
    default_layout,
    initialise_instance,
    install_commands,
    stage_release,
    validate_config,
    venv_python,
)


PRIVATE_MARKERS = ("/Users/" + "ai", "lumi" + "63181107", "/Volumes/" + "lumi")
PORTABLE_SURFACES = (
    "magi_v3/selfhost.py",
    "magi_v3/fcntl_compat.py",
    "magi_v3/process_compat.py",
    "scripts/magi_selfhost.py",
    "scripts/ops/selfhost_minimal_import_check.py",
    "install-magi.ps1",
    "install-magi.cmd",
    "install-magi.command",
    "config/selfhost.schema.json",
    "config/selfhost.example.json",
    ".env.example",
    "README.md",
    "requirements-selfhost.txt",
    "docs/SELFHOST_DEPLOYMENT.md",
)


def _finding(key: str, ok: bool, detail: str, action: str = "") -> dict[str, object]:
    return {"key": key, "ok": ok, "detail": detail, "action": action}


def collect() -> dict[str, object]:
    findings: list[dict[str, object]] = []
    missing = [rel for rel in PORTABLE_SURFACES if not (ROOT / rel).is_file()]
    findings.append(_finding(
        "required_files",
        not missing,
        "all deployment files present" if not missing else "missing: " + ", ".join(missing),
        "restore the missing installer or configuration file" if missing else "",
    ))

    leaked: list[str] = []
    for rel in PORTABLE_SURFACES:
        path = ROOT / rel
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for marker in PRIVATE_MARKERS:
            if marker in text:
                leaked.append(f"{rel}:{marker}")
    findings.append(_finding(
        "private_paths",
        not leaked,
        "none" if not leaked else "; ".join(leaked),
        "replace machine-specific literals with selfhost configuration" if leaked else "",
    ))

    direct_fcntl_imports: list[str] = []
    native_lock_backends = {"magi_v3/fcntl_compat.py", "skills/ops/platform_utils.py"}
    for path in ROOT.rglob("*.py"):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        relative = str(path.relative_to(ROOT))
        if relative in native_lock_backends:
            continue
        if "import fcntl" in text and "fcntl_compat" not in text:
            direct_fcntl_imports.append(relative)
    findings.append(_finding(
        "windows_file_locks",
        not direct_fcntl_imports,
        "all direct locks use magi_v3.fcntl_compat" if not direct_fcntl_imports else "; ".join(direct_fcntl_imports),
        "replace direct fcntl imports with the cross-platform compatibility layer" if direct_fcntl_imports else "",
    ))

    cron_source = ROOT / "cron_jobs.json"
    with tempfile.TemporaryDirectory(prefix="magi-selfhost-audit-") as raw_tmp:
        tmp = Path(raw_tmp)
        for system in ("Darwin", "Windows"):
            env = {
                "USERPROFILE": str(tmp / system / "User"),
                "LOCALAPPDATA": str(tmp / system / "LocalAppData"),
            }
            layout = default_layout(system=system, home=tmp / system / "User", environ=env)
            config = default_config(layout=layout, source_root=ROOT)
            errors = validate_config(config)
            findings.append(_finding(
                f"config:{system}",
                not errors,
                "valid" if not errors else "; ".join(errors),
                "correct platform defaults" if errors else "",
            ))
            plan = build_service_plan(
                config,
                python_executable=venv_python(layout),
                launcher_path=layout.launcher_path,
                uid=501,
            )
            commands = install_commands(ROOT, layout, include_optional=False)
            plan_text = json.dumps({"service": plan.as_dict(), "commands": commands}, ensure_ascii=False)
            plan_text = plan_text.replace(str(ROOT), "<SOURCE_ROOT>").replace(str(tmp), "<AUDIT_TMP>")
            plan_leaks = [marker for marker in PRIVATE_MARKERS if marker in plan_text]
            findings.append(_finding(
                f"plan:{system}",
                not plan_leaks,
                "portable" if not plan_leaks else "private markers: " + ", ".join(plan_leaks),
                "remove builder-specific data from the platform plan" if plan_leaks else "",
            ))
            if cron_source.is_file():
                jobs, stats = build_portable_cron_jobs(
                    cron_source,
                    source_root=ROOT,
                    release_root=layout.releases_dir / "audit-release",
                    python_executable=venv_python(layout),
                    config=config,
                )
                rendered = json.dumps(jobs, ensure_ascii=False)
                cron_leaks = [marker for marker in PRIVATE_MARKERS if marker in rendered]
                parse_errors: list[str] = []
                for job in jobs:
                    command = str(job.get("command") or "").strip()
                    if not command:
                        continue
                    try:
                        shlex.split(command, posix=True)
                    except ValueError as exc:
                        parse_errors.append(f"{job.get('id')}: {exc}")
                findings.append(_finding(
                    f"cron:{system}",
                    not cron_leaks and not parse_errors,
                    json.dumps({**stats, "private_markers": cron_leaks, "parse_errors": parse_errors[:5]}, ensure_ascii=False),
                    "repair cron rebasing before producing a release" if cron_leaks or parse_errors else "",
                ))
            initialise_instance(config)
            staged = stage_release(ROOT, config, release_id=f"audit-{system.lower()}")
            staged_root = Path(str(staged["root"]))
            required_runtime = (
                "daemon.py",
                "magi_v3/selfhost.py",
                "magi_v3/fcntl_compat.py",
                "magi_v3/process_compat.py",
                "scripts/magi_selfhost.py",
                "requirements-selfhost.txt",
                "cron_jobs.json",
            )
            missing_runtime = [rel for rel in required_runtime if not (staged_root / rel).is_file()]
            leaked_secret_files = [
                str(path.relative_to(staged_root))
                for path in staged_root.rglob("*")
                if path.is_file() and path.name in {".env", "magi.env"}
            ]
            excluded_runtime = [
                rel for rel in (
                    "tests/test_selfhost_portability.py",
                    "resources/osc/photo/lawyer_stamp.png",
                    "skills/laf-portal-automation/references/snapshot_training.json",
                )
                if (staged_root / rel).exists()
            ]
            release_manifest = (staged_root / "selfhost-release.json").read_text(encoding="utf-8")
            manifest_leaks = [marker for marker in PRIVATE_MARKERS if marker in release_manifest]
            findings.append(_finding(
                f"clean_stage:{system}",
                not missing_runtime
                and not leaked_secret_files
                and not excluded_runtime
                and not manifest_leaks
                and len(str(staged.get("tree_sha256") or "")) == 64,
                json.dumps({
                    "file_count": staged.get("file_count"),
                    "tree_sha256": staged.get("tree_sha256"),
                    "missing_runtime": missing_runtime,
                    "secret_files": leaked_secret_files,
                    "excluded_runtime": excluded_runtime,
                    "manifest_private_markers": manifest_leaks,
                }, ensure_ascii=False),
                "repair the immutable release allowlist, privacy exclusion, or manifest" if missing_runtime or leaked_secret_files or excluded_runtime or manifest_leaks else "",
            ))

        archive_path = tmp / "MAGI-V3-selfhost-audit.zip"
        archive_result = build_distribution_archive(ROOT, archive_path)
        with zipfile.ZipFile(archive_path) as archive:
            names = archive.namelist()
            distribution_manifest = json.loads(archive.read("MAGI-DISTRIBUTION.json"))
            distribution_cron = archive.read("cron_jobs.json").decode("utf-8", errors="replace")
        forbidden_names = [
            name for name in names
            if name in {".env", "magi.env", "credentials.json", "token.json"}
            or name.startswith("tests/")
            or name.startswith("resources/osc/photo/")
            or name == "skills/laf-portal-automation/references/snapshot_training.json"
        ]
        findings.append(_finding(
            "distribution_archive",
            not forbidden_names
            and not any(marker in distribution_cron for marker in PRIVATE_MARKERS)
            and "__MAGI_ROOT__" in distribution_cron
            and "__MAGI_PYTHON__" in distribution_cron
            and distribution_manifest.get("contains_secrets") is False
            and len(str(archive_result.get("archive_sha256") or "")) == 64,
            json.dumps({
                "file_count": distribution_manifest.get("file_count"),
                "content_sha256": distribution_manifest.get("content_sha256"),
                "archive_sha256": archive_result.get("archive_sha256"),
                "forbidden_names": forbidden_names,
                "cron_private_markers": [marker for marker in PRIVATE_MARKERS if marker in distribution_cron],
                "cron_uses_placeholders": "__MAGI_ROOT__" in distribution_cron and "__MAGI_PYTHON__" in distribution_cron,
            }, ensure_ascii=False),
            "repair distribution exclusions before sharing the archive" if forbidden_names else "",
        ))

    failed = [item for item in findings if not item["ok"]]
    return {
        "schema": "magi.selfhost.portability-audit/v1",
        "ok": not failed,
        "summary": {"pass": len(findings) - len(failed), "fail": len(failed)},
        "findings": findings,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json-out", type=Path)
    args = parser.parse_args(argv)
    payload = collect()
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
