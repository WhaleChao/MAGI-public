#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Iterable, Set


_MAGI_ROOT = Path(__file__).resolve().parent.parent.parent
MAGI_ROOT = _MAGI_ROOT
SKILLS_DIR = MAGI_ROOT / "skills"
OPENCLAW_ROOT = Path("/Users/ai/.openclaw/skills/magi-office-ops")
ARCHIVE_ROOT = MAGI_ROOT / "archive" / "skills_quarantine"
REPORT_ROOT = MAGI_ROOT / "static"


MANUAL_KEEP: Set[str] = {
    # Core runtime modules
    "bridge",
    "memory",
    "ops",
    "research",
    "documents",
    "evolution",
    "management",
    "magi",
    "casper",
    "casper-client",
    "brain_manager",
    # Primary MAGI business features
    "file-review-orchestrator",
    "transcript-downloader",
    "judgment-collector",
    "laf-portal-automation",
    "laf-orchestrator",
    "laf-refine-case",
    "laf-withdrawal-report",
    "osc-orchestrator",
    "osc-scan-folder",
    "pdf-namer",
    "crawler-targets",
    "statutes-vdb",
    "magi-autopilot",
    "magi-doctor",
    "market-briefing",
    "translator",
    "gmail-drafts",
    "iron-dome",
    "iron_dome",
    # Legal helpers currently referenced by bridges/orchestrators
    "legal",
    "law_firm",
    "law_review",
    "legal_attest",
    "db-dual-sync",
    "worldmonitor-intel",
    # Keep these utility packs to avoid regression for document workflows
    "docx",
    "pdf",
    "pptx",
    "xlsx",
}

PROTECTED_DIRS = {".versions", "__pycache__"}


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return ""


def _extract_skill_names(text: str) -> Set[str]:
    names: Set[str] = set()
    if not text:
        return names

    for m in re.finditer(r"(?:from|import)\s+skills\.([A-Za-z0-9_-]+)", text):
        names.add(m.group(1))

    for m in re.finditer(rf"{MAGI_ROOT}/skills/([A-Za-z0-9_-]+)(?:/|\b)", text):
        names.add(m.group(1))

    # For shell scripts using $MAGI_DIR/skills/<name>/...
    for m in re.finditer(r"skills/([A-Za-z0-9_-]+)(?:/|\b)", text):
        names.add(m.group(1))

    return names


def _collect_referenced_skills(files: Iterable[Path], existing: Set[str]) -> Set[str]:
    refs: Set[str] = set()
    for fp in files:
        refs |= _extract_skill_names(_read_text(fp))
    return {n for n in refs if n in existing}


def main() -> int:
    ap = argparse.ArgumentParser(description="Quarantine non-core MAGI skills (non-destructive).")
    ap.add_argument("--execute", action="store_true", help="Actually move non-allowlisted skill dirs.")
    args = ap.parse_args()

    if not SKILLS_DIR.exists():
        print(json.dumps({"ok": False, "error": "skills_dir_missing", "skills_dir": str(SKILLS_DIR)}, ensure_ascii=False))
        return 1

    actual = sorted([p.name for p in SKILLS_DIR.iterdir() if p.is_dir()])
    actual_set = set(actual)

    scan_files = [
        MAGI_ROOT / "api" / "server.py",
        MAGI_ROOT / "api" / "orchestrator.py",
        MAGI_ROOT / "api" / "tools_api.py",
        MAGI_ROOT / "daemon.py",
        MAGI_ROOT / "skills" / "ops" / "openclaw_cron_runner.py",
        OPENCLAW_ROOT / "run.sh",
        OPENCLAW_ROOT / "intent_router.py",
    ]
    referenced = _collect_referenced_skills(scan_files, actual_set)

    keep = set(MANUAL_KEEP) | referenced
    keep = {k for k in keep if k in actual_set}
    keep |= {k for k in PROTECTED_DIRS if k in actual_set}

    move_targets = [name for name in actual if name not in keep]
    moved = []
    quarantine_dir = None

    if args.execute and move_targets:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        quarantine_dir = ARCHIVE_ROOT / ts
        quarantine_dir.mkdir(parents=True, exist_ok=True)
        for name in move_targets:
            src = SKILLS_DIR / name
            dst = quarantine_dir / name
            if dst.exists():
                dst = quarantine_dir / f"{name}__dup_{ts}"
            shutil.move(str(src), str(dst))
            moved.append(name)

    report = {
        "ok": True,
        "execute": bool(args.execute),
        "skills_dir": str(SKILLS_DIR),
        "quarantine_dir": str(quarantine_dir) if quarantine_dir else "",
        "keep_count": len(sorted(keep)),
        "move_count": len(move_targets),
        "moved_count": len(moved),
        "keep": sorted(keep),
        "move_targets": move_targets,
        "moved": moved,
        "referenced_detected": sorted(referenced),
        "manual_keep": sorted([k for k in MANUAL_KEEP if k in actual_set]),
        "scanned_files": [str(p) for p in scan_files],
    }

    REPORT_ROOT.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = REPORT_ROOT / f"skills_purify_report_{stamp}.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    report["report_path"] = str(report_path)

    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
