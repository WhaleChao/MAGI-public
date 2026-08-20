#!/usr/bin/env python3
"""Audit and safely repair transcript PDFs archived under the wrong case.

The audit reads docket identities from the first three PDF pages.  A mutation
is allowed only when the PDF excludes the source case docket and identifies one
unambiguous database case.  Nothing is deleted; duplicates and unresolved
files are preserved under the shared transcript quarantine.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import plistlib
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_live_environment() -> None:
    for path in (
        Path.home() / "Library/LaunchAgents/com.magi.v3.supervisor.plist",
        Path.home() / "Library/LaunchAgents/com.magi.v3.control.plist",
    ):
        if not path.is_file():
            continue
        data = plistlib.loads(path.read_bytes())
        for key, value in (data.get("EnvironmentVariables") or {}).items():
            os.environ.setdefault(str(key), str(value))
    env_path = Path(os.environ.get("MAGI_ENV_FILE", "")).expanduser()
    if not env_path.is_file():
        return
    from dotenv import dotenv_values

    for key, value in dotenv_values(env_path, encoding="utf-8", interpolate=True).items():
        if key and value is not None:
            os.environ.setdefault(str(key), str(value))


def _load_judicial_module():
    path = ROOT / "casper_ecosystem/law_firm_orchestrators/judicial_automation_v2.py"
    spec = importlib.util.spec_from_file_location("magi_transcript_archive_judicial", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("judicial module unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _pdf_dockets(path: Path, judicial) -> set[tuple[str, str, int]]:
    import fitz

    document = fitz.open(path)
    try:
        text = "\n".join(
            str(document[index].get_text() or "")
            for index in range(min(3, len(document)))
        )
    finally:
        document.close()
    return set(judicial._portal_docket_identities(text))


def _transcript_pdfs(case_root: Path) -> list[Path]:
    candidates: list[Path] = []
    for path in case_root.rglob("*.pdf"):
        # SMB exposes macOS AppleDouble sidecars as ``._*.pdf``.  They are
        # metadata, not documents, and must never become health failures.
        if path.name.startswith("._"):
            continue
        if any("筆錄" in part for part in path.relative_to(case_root).parts[:-1]):
            candidates.append(path)
    return sorted(set(candidates))


def _unique_destination(folder: Path, source: Path) -> Path:
    destination = folder / source.name
    if not destination.exists():
        return destination
    stem, suffix = source.stem, source.suffix
    counter = 2
    while True:
        destination = folder / f"{stem}_{counter}{suffix}"
        if not destination.exists():
            return destination
        counter += 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--json-out", default="")
    args = parser.parse_args()
    _load_live_environment()

    from api.osc.utils import _osc_exec, _osc_resolve_existing_local_path

    rows, _ = _osc_exec(
        """
        SELECT case_number, court_case_number, folder_path
          FROM cases
         WHERE COALESCE(folder_path,'')<>''
           AND COALESCE(court_case_number,'')<>''
         ORDER BY case_number
        """,
        fetch="all",
    )
    judicial = _load_judicial_module()
    cases: list[dict[str, Any]] = []
    identity_map: dict[tuple[str, str, int], list[dict[str, Any]]] = {}
    for raw in rows or []:
        row = dict(raw or {})
        local = _osc_resolve_existing_local_path(str(row.get("folder_path") or ""), prefer_dir=True)
        if not local:
            continue
        row["local_path"] = str(local)
        row["dockets"] = set(judicial._portal_docket_identities(row.get("court_case_number")))
        cases.append(row)
        for identity in row["dockets"]:
            identity_map.setdefault(identity, []).append(row)

    report: dict[str, Any] = {
        "schema": "magi.transcript-archive-consistency/v1",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "applied": bool(args.apply),
        "ok": True,
        "case_count": len(cases),
        "pdf_count": 0,
        "matched": 0,
        "inconclusive": 0,
        "misfiled": 0,
        "moved": 0,
        "duplicate_quarantined": 0,
        "ambiguous": 0,
        "vanished_during_scan": 0,
        "errors": [],
        "actions": [],
    }
    shared_root = str(os.environ.get("MAGI_SHARED_ROOT", "") or "").strip()
    if not shared_root:
        runtime_dir = Path(os.environ.get("MAGI_RUNTIME_DIR", str(ROOT / ".runtime"))).expanduser()
        shared_root = str(runtime_dir.parent if runtime_dir.name == "runtime" else runtime_dir)
    quarantine = (
        Path(shared_root).expanduser()
        / "exports/transcript-quarantine/archive-consistency"
        / datetime.now().strftime("%Y%m%d")
    )

    lock_acquired = False
    if args.apply:
        from api.domains.case_file_operation_lock import (
            acquire_case_file_operation_lock,
            release_case_file_operation_lock,
        )

        lock = acquire_case_file_operation_lock(owner="transcript_archive_consistency")
        lock_acquired = bool(lock.get("acquired"))
        if not lock_acquired:
            raise SystemExit("case_file_operation_lock_busy")

    try:
        for source_case in cases:
            source_root = Path(source_case["local_path"])
            for pdf in _transcript_pdfs(source_root):
                report["pdf_count"] += 1
                try:
                    pdf_dockets = _pdf_dockets(pdf, judicial)
                except Exception as exc:
                    if not pdf.exists() or "no such file" in str(exc).lower():
                        # A concurrent NAS/Drive reconciler may have moved an
                        # item after the read-only enumeration.  Apply mode
                        # holds the shared case-file lock, so this is evidence
                        # only and not a corrupt-PDF finding.
                        report["vanished_during_scan"] += 1
                        continue
                    report["errors"].append({"path": str(pdf), "error": str(exc)[:160]})
                    continue
                if not pdf_dockets:
                    report["inconclusive"] += 1
                    continue
                if pdf_dockets.intersection(source_case["dockets"]):
                    report["matched"] += 1
                    continue

                targets: dict[str, dict[str, Any]] = {}
                for identity in pdf_dockets:
                    for target in identity_map.get(identity, []):
                        targets[str(target.get("case_number") or "")] = target
                if len(targets) != 1:
                    report["ambiguous"] += 1
                    report["actions"].append(
                        {
                            "status": "unresolved",
                            "source_case": source_case.get("case_number"),
                            "path": str(pdf),
                            "candidate_cases": sorted(targets),
                        }
                    )
                    continue
                target = next(iter(targets.values()))
                if target.get("case_number") == source_case.get("case_number"):
                    report["matched"] += 1
                    continue
                report["misfiled"] += 1
                action = {
                    "status": "planned",
                    "source_case": source_case.get("case_number"),
                    "target_case": target.get("case_number"),
                    "path": str(pdf),
                    "sha256": _sha256(pdf),
                }
                if args.apply:
                    target_folder = Path(target["local_path"]) / "08_筆錄"
                    target_folder.mkdir(parents=True, exist_ok=True)
                    same = next(
                        (
                            candidate for candidate in target_folder.glob("*.pdf")
                            if _sha256(candidate) == action["sha256"]
                        ),
                        None,
                    )
                    if same is not None:
                        quarantine.mkdir(parents=True, exist_ok=True)
                        destination = _unique_destination(quarantine, pdf)
                        shutil.move(str(pdf), str(destination))
                        action.update(status="duplicate_quarantined", destination=str(destination))
                        report["duplicate_quarantined"] += 1
                    else:
                        destination = _unique_destination(target_folder, pdf)
                        shutil.move(str(pdf), str(destination))
                        action.update(status="moved", destination=str(destination))
                        report["moved"] += 1
                report["actions"].append(action)
    finally:
        if lock_acquired:
            release_case_file_operation_lock()

    report["ok"] = not report["errors"]
    output = (
        Path(args.json_out).expanduser()
        if args.json_out
        else Path(os.environ.get("MAGI_RUNTIME_DIR", str(ROOT / ".runtime")))
        / "transcript_archive_consistency_latest.json"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
