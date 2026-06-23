#!/usr/bin/env python3
"""Validate customer installer payloads after build.

This checks the release zip embedded in installer artifacts, not merely the
source checkout. It is intentionally public-safe and does not run live probes.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _find_archive(manifest_path: Path) -> Path:
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    archive = Path(str(payload.get("archive") or ""))
    if not archive.is_absolute():
        archive = (manifest_path.parent / archive).resolve()
    if not archive.is_file():
        raise FileNotFoundError(f"release archive missing: {archive}")
    return archive


def _extract_release(archive: Path, dest: Path) -> Path:
    with zipfile.ZipFile(archive) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            rel = PurePosixPath(info.filename)
            if rel.is_absolute() or ".." in rel.parts:
                raise ValueError(f"unsafe zip member: {info.filename}")
            target = dest / Path(*rel.parts)
            target.resolve().relative_to(dest.resolve())
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, target.open("wb") as dst:
                dst.write(src.read())
    candidates = [
        path
        for path in dest.iterdir()
        if path.is_dir() and (path / "scripts" / "customer_install_wizard.py").is_file()
    ]
    if not candidates:
        raise FileNotFoundError("release archive does not contain scripts/customer_install_wizard.py")
    return candidates[0]


def _all_release_paths(root: Path) -> list[str]:
    paths: list[str] = []
    for path in root.rglob("*"):
        if path.is_file():
            paths.append(path.relative_to(root).as_posix())
    return sorted(paths)


def _run(cmd: list[str], *, cwd: Path, timeout: int = 180) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
    )


def validate_payload(manifest_path: Path) -> dict[str, Any]:
    from scripts import public_release_audit

    archive = _find_archive(manifest_path)
    with tempfile.TemporaryDirectory(prefix="magi_installer_payload_") as tmp:
        release_root = _extract_release(archive, Path(tmp))
        findings = public_release_audit.scan_tracked_files(
            paths=_all_release_paths(release_root),
            repo_root=release_root,
            public_isolation=True,
        )
        strict_findings = [
            public_release_audit.Finding(
                f.path,
                f.line,
                f.kind,
                "error" if f.severity == "warning" else f.severity,
                f.detail,
            )
            for f in findings
        ]
        audit = public_release_audit.summarize(strict_findings)

        wizard_out = release_root / ".runtime" / "installer_payload_wizard.json"
        wizard = _run(
            [
                sys.executable,
                "scripts/customer_install_wizard.py",
                "--public",
                "--no-live",
                "--skip-readiness",
                "--no-optional",
                "--json",
                "--output",
                str(wizard_out),
            ],
            cwd=release_root,
            timeout=240,
        )
        return {
            "ok": bool(audit.get("ok")) and wizard.returncode == 0,
            "archive": str(archive),
            "audit": audit,
            "wizard_returncode": wizard.returncode,
            "wizard_output_tail": (wizard.stdout or "")[-1200:],
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate MAGI installer release payload.")
    parser.add_argument("--manifest", default=str(REPO_ROOT / "dist" / "installers" / "installer_manifest.json"))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    result = validate_payload(Path(args.manifest).resolve())
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print("installer payload:", "PASS" if result.get("ok") else "FAIL")
        print("archive:", result.get("archive"))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
