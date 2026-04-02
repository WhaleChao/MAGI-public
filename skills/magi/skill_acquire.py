#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ClaWHub-based skill acquisition with Iron Dome security review.

Flow:
  1. clawhub search <query>  — find candidates
  2. clawhub install <slug>  — download to temp dir
  3. Iron Dome scan every file — reject if any violation
  4. If clean: move to MAGI skills dir + update definitions.json
  5. Notify user with result

Environment variables:
  CLAWHUB_BIN         path to clawhub CLI (default: "clawhub")
  MAGI_SKILLS_DIR     skills install target (default: <MAGI_ROOT>/skills)
  MAGI_IRON_DOME_SKIP_ACQUIRE  set to "1" to bypass scan (NOT recommended)
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("SkillAcquire")

MAGI_ROOT = os.environ.get("MAGI_ROOT", str(Path(__file__).resolve().parents[2]))
SKILLS_DIR = os.environ.get("MAGI_SKILLS_DIR", os.path.join(MAGI_ROOT, "skills"))
DEFINITIONS_PATH = os.path.join(SKILLS_DIR, "definitions.json")
CLAWHUB_BIN = os.environ.get("CLAWHUB_BIN", "clawhub")

# Files to always skip during security scan (binary / data)
_SKIP_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico",
    ".woff", ".woff2", ".ttf", ".eot",
    ".zip", ".tar", ".gz", ".tgz",
    ".pdf", ".mp3", ".mp4", ".wav",
    ".pyc", ".pyo",
}
_MAX_SCAN_BYTES = 512 * 1024  # skip files larger than 512 KB


def _run(cmd: List[str], cwd: str = None, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, capture_output=True, text=True, cwd=cwd, timeout=timeout
    )


def search_clawhub(query: str, limit: int = 5) -> Dict[str, Any]:
    """Run `clawhub search <query>` and return parsed results."""
    try:
        r = _run([CLAWHUB_BIN, "search", query, "--json"], timeout=30)
        if r.returncode == 0 and r.stdout.strip():
            try:
                data = json.loads(r.stdout)
                items = data if isinstance(data, list) else data.get("results", data.get("skills", []))
                return {"ok": True, "results": items[:limit], "raw": r.stdout[:2000]}
            except json.JSONDecodeError:
                pass
        # Fallback: parse plain-text output
        lines = [ln.strip() for ln in (r.stdout or "").splitlines() if ln.strip()]
        return {"ok": True, "results": lines[:limit], "raw": r.stdout[:2000]}
    except FileNotFoundError:
        return {"ok": False, "error": f"clawhub CLI not found at '{CLAWHUB_BIN}'. Install with: npm i -g clawhub"}
    except Exception as e:
        return {"ok": False, "error": f"clawhub search failed: {e}"}


def _iron_dome_scan_file(path: str) -> Optional[str]:
    """
    Scan a single file with Iron Dome.
    Returns violation description string on failure, None if clean.
    """
    ext = Path(path).suffix.lower()
    if ext in _SKIP_EXTENSIONS:
        return None
    try:
        size = os.path.getsize(path)
        if size > _MAX_SCAN_BYTES:
            return None  # skip large binaries
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None

    try:
        from skills.iron_dome.core import is_safe
        safe, why = is_safe(text)
        if not safe:
            return f"{os.path.basename(path)}: {why}"
    except ImportError:
        logger.warning("Iron Dome core not available — skipping scan")
    return None


def _iron_dome_scan_dir(skill_dir: str) -> List[str]:
    """Recursively scan all files in a directory. Returns list of violations."""
    violations: List[str] = []
    for root, _, files in os.walk(skill_dir):
        # Skip hidden dirs and node_modules
        rel = os.path.relpath(root, skill_dir)
        if any(part.startswith(".") or part == "node_modules" for part in Path(rel).parts):
            continue
        for fname in files:
            fpath = os.path.join(root, fname)
            v = _iron_dome_scan_file(fpath)
            if v:
                violations.append(v)
    return violations


def acquire_skill(slug: str, *, force: bool = False, dry_run: bool = False) -> Dict[str, Any]:
    """
    Search ClaWHub for `slug`, install it to a temp dir, run Iron Dome scan,
    then (if clean) move into MAGI skills directory.

    Returns a result dict with:
        ok, slug, installed_path, violations, skipped_reason, dry_run
    """
    skip_scan = os.environ.get("MAGI_IRON_DOME_SKIP_ACQUIRE", "0").strip() in {"1", "true", "yes"}
    if skip_scan:
        logger.warning("Iron Dome scan bypassed (MAGI_IRON_DOME_SKIP_ACQUIRE=1)")

    # Normalise slug — strip whitespace/leading @
    slug = re.sub(r"^@", "", (slug or "").strip())
    if not slug:
        return {"ok": False, "error": "slug_required"}

    target_dir = os.path.join(SKILLS_DIR, slug)
    if os.path.exists(target_dir) and not force:
        return {
            "ok": False,
            "error": f"Skill '{slug}' already installed at {target_dir}. Use force=True to overwrite.",
            "slug": slug,
        }

    with tempfile.TemporaryDirectory(prefix="magi_skill_acquire_") as tmp:
        # --- Download ---
        logger.info(f"Downloading skill '{slug}' from ClaWHub...")
        try:
            r = _run(
                [CLAWHUB_BIN, "install", slug, "--dir", tmp],
                cwd=tmp, timeout=120,
            )
        except FileNotFoundError:
            return {"ok": False, "error": f"clawhub CLI not found at '{CLAWHUB_BIN}'. Install: npm i -g clawhub", "slug": slug}
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "clawhub install timed out (120s)", "slug": slug}

        if r.returncode != 0:
            return {
                "ok": False,
                "error": f"clawhub install exited {r.returncode}: {(r.stderr or r.stdout or '')[:500]}",
                "slug": slug,
            }

        # Find installed directory (clawhub puts it in tmp/<slug>/ or tmp/<slug-folder>/)
        installed_dirs = [
            d for d in Path(tmp).iterdir()
            if d.is_dir() and not d.name.startswith(".")
        ]
        if not installed_dirs:
            return {"ok": False, "error": "clawhub install produced no directory", "slug": slug}
        skill_tmp = str(installed_dirs[0])

        # --- Iron Dome Scan ---
        violations: List[str] = []
        if not skip_scan:
            logger.info(f"Iron Dome scanning '{slug}'...")
            violations = _iron_dome_scan_dir(skill_tmp)

        if violations:
            logger.warning(f"Iron Dome rejected skill '{slug}': {violations}")
            return {
                "ok": False,
                "error": "iron_dome_rejected",
                "slug": slug,
                "violations": violations,
                "message": (
                    f"技能 '{slug}' 被鐵穹安全審查拒絕，共 {len(violations)} 個違規項目：\n"
                    + "\n".join(f"  - {v}" for v in violations[:10])
                ),
            }

        if dry_run:
            return {
                "ok": True,
                "dry_run": True,
                "slug": slug,
                "scanned_clean": True,
                "message": f"技能 '{slug}' 安全審查通過（dry-run，未實際安裝）。",
            }

        # --- Install ---
        if os.path.exists(target_dir) and force:
            shutil.rmtree(target_dir)
        shutil.copytree(skill_tmp, target_dir)
        logger.info(f"Skill '{slug}' installed to {target_dir}")

    return {
        "ok": True,
        "slug": slug,
        "installed_path": target_dir,
        "violations": [],
        "message": f"技能 '{slug}' 安裝成功，鐵穹審查通過。路徑：{target_dir}",
    }


def format_search_result(results: Dict[str, Any]) -> str:
    """Format clawhub search results as a readable string."""
    if not results.get("ok"):
        return f"ClaWHub 搜尋失敗：{results.get('error', '未知錯誤')}"
    items = results.get("results", [])
    if not items:
        return "ClaWHub 搜尋無結果。"
    lines = ["ClaWHub 搜尋結果："]
    for i, item in enumerate(items, 1):
        if isinstance(item, dict):
            slug = item.get("slug") or item.get("name") or str(item)
            desc = item.get("description") or item.get("desc") or ""
            lines.append(f"{i}. `{slug}` — {desc[:120]}")
        else:
            lines.append(f"{i}. {str(item)[:120]}")
    lines.append("\n安裝指令：`@MAGI 安裝skill <slug>`")
    return "\n".join(lines)
