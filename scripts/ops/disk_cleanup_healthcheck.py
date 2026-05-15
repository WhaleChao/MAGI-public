#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Layer 4 — 磁碟自動清理健檢

每日 03:45 cron 觸發；dry-run 預設開（MAGI_DISK_CLEANUP_DRY_RUN=1），
切 0 才真的刪／rotate。

管轄範圍：
  - .runtime/metrics/*.jsonl (含巢狀 ocr.jsonl/ 子目錄)：> 10MB → rotate 保留 tail 1000 行
  - ~/.omlx/cache-*/：保留 atime ≥ 7 天以內的檔，其餘視為可釋放
  - /tmp/magi_*、/tmp/omlx_* 與 *.png / *.log / *.txt / *.tmp：mtime > 48h 刪除
  - .agent/server.log*：僅回報總大小，既有 rotate 機制已處理

紅線：
  - 不碰六模組資料（LAF / 閱卷 / 筆錄 / 摘要 / 翻譯 / 逐字稿）
  - 不碰 runtime pending/*（正在等律師確認碼的檔案）
  - 不碰 cron_state.json
  - 不碰單機版 JSON / pickle / db / sqlite 狀態檔
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import time
import argparse
import gzip
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

MAGI_ROOT = Path(os.environ.get("MAGI_ROOT", "/Users/ai/Desktop/MAGI_v2")).resolve()
if str(MAGI_ROOT) not in sys.path:
    sys.path.insert(0, str(MAGI_ROOT))

from api.platforms import runtime_dir  # noqa: E402

# ---- 設定 --------------------------------------------------------------

METRICS_ROTATE_BYTES = int(os.environ.get("MAGI_DISK_METRICS_ROTATE_BYTES", str(10 * 1024 * 1024)))
METRICS_KEEP_TAIL = int(os.environ.get("MAGI_DISK_METRICS_KEEP_TAIL", "1000"))
OMLX_CACHE_KEEP_DAYS = int(os.environ.get("MAGI_DISK_OMLX_KEEP_DAYS", "7"))
OMLX_CACHE_MAX_DELETE_BYTES = int(float(os.environ.get("MAGI_DISK_OMLX_MAX_DELETE_GB", "20")) * 1024 * 1024 * 1024)
OMLX_CACHE_CAP_GB = float(os.environ.get("MAGI_DISK_OMLX_CACHE_CAP_GB", "8"))
OMLX_CACHE_LOW_WATER_CAP_GB = float(os.environ.get("MAGI_DISK_OMLX_CACHE_LOW_WATER_CAP_GB", "5"))
OMLX_CACHE_CRITICAL_CAP_GB = float(os.environ.get("MAGI_DISK_OMLX_CACHE_CRITICAL_CAP_GB", "3"))
OMLX_CACHE_LOW_WATER_FREE_GB = float(os.environ.get("MAGI_DISK_OMLX_CACHE_LOW_WATER_FREE_GB", "30"))
OMLX_CACHE_CRITICAL_FREE_GB = float(os.environ.get("MAGI_DISK_OMLX_CACHE_CRITICAL_FREE_GB", "15"))
OMLX_CACHE_RECENT_GRACE_MINUTES = int(os.environ.get("MAGI_DISK_OMLX_CACHE_RECENT_GRACE_MINUTES", "60"))
TMP_MAX_AGE_HOURS = int(os.environ.get("MAGI_DISK_TMP_MAX_AGE_HOURS", "48"))
DB_BACKUP_KEEP_LATEST = int(os.environ.get("MAGI_DISK_DB_BACKUP_KEEP_LATEST", "8"))
BUILD_ARTIFACT_MAX_AGE_DAYS = int(os.environ.get("MAGI_DISK_BUILD_ARTIFACT_MAX_AGE_DAYS", "7"))
BUILD_ARTIFACT_LOW_WATER_GB = float(os.environ.get("MAGI_DISK_BUILD_ARTIFACT_LOW_WATER_GB", "20"))
BUILD_ARTIFACT_CLEANUP_ENABLE = os.environ.get(
    "MAGI_DISK_BUILD_ARTIFACT_CLEANUP_ENABLE", "1"
).strip().lower() in {"1", "true", "on", "yes"}
GIT_TMP_PACK_MAX_AGE_HOURS = int(os.environ.get("MAGI_DISK_GIT_TMP_PACK_MAX_AGE_HOURS", "24"))
GIT_TMP_PACK_CLEANUP_ENABLE = os.environ.get(
    "MAGI_DISK_GIT_TMP_PACK_CLEANUP_ENABLE", "1"
).strip().lower() in {"1", "true", "on", "yes"}
RUNTIME_COMPRESS_ENABLE = os.environ.get(
    "MAGI_DISK_RUNTIME_COMPRESS_ENABLE", "1"
).strip().lower() in {"1", "true", "on", "yes"}
RUNTIME_COMPRESS_MAX_AGE_DAYS = float(os.environ.get("MAGI_DISK_RUNTIME_COMPRESS_MAX_AGE_DAYS", "3"))
RUNTIME_COMPRESS_LOW_WATER_MAX_AGE_HOURS = float(
    os.environ.get("MAGI_DISK_RUNTIME_COMPRESS_LOW_WATER_MAX_AGE_HOURS", "12")
)
RUNTIME_COMPRESS_MIN_BYTES = int(float(os.environ.get("MAGI_DISK_RUNTIME_COMPRESS_MIN_MB", "1")) * 1024 * 1024)
RUNTIME_COMPRESS_LOW_WATER_MIN_BYTES = int(
    float(os.environ.get("MAGI_DISK_RUNTIME_COMPRESS_LOW_WATER_MIN_MB", "0.25")) * 1024 * 1024
)
STAGING_CLEANUP_ENABLE = os.environ.get(
    "MAGI_DISK_STAGING_CLEANUP_ENABLE", "1"
).strip().lower() in {"1", "true", "on", "yes"}
PAYMENT_DUPLICATE_CLEANUP_ENABLE = os.environ.get(
    "MAGI_DISK_PAYMENT_DUPLICATE_CLEANUP_ENABLE", "1"
).strip().lower() in {"1", "true", "on", "yes"}
PAYMENT_DUPLICATE_ALLOW_SMB = os.environ.get(
    "MAGI_DISK_PAYMENT_DUPLICATE_ALLOW_SMB", "0"
).strip().lower() in {"1", "true", "on", "yes"}
NAS_RECYCLE_CLEANUP_ENABLE = os.environ.get(
    "MAGI_DISK_NAS_RECYCLE_ENABLE", "0"
).strip().lower() in {"1", "true", "on", "yes"}
NAS_RECYCLE_ALLOW_NON_VOLUME = os.environ.get(
    "MAGI_DISK_NAS_RECYCLE_ALLOW_NON_VOLUME", "0"
).strip().lower() in {"1", "true", "on", "yes"}
NAS_RECYCLE_MAX_AGE_DAYS = float(os.environ.get("MAGI_DISK_NAS_RECYCLE_MAX_AGE_DAYS", "14"))
NAS_RECYCLE_MAX_DELETE_ITEMS = int(os.environ.get("MAGI_DISK_NAS_RECYCLE_MAX_DELETE_ITEMS", "50"))
NAS_RECYCLE_MAX_RUNTIME_SEC = int(os.environ.get("MAGI_DISK_NAS_RECYCLE_MAX_RUNTIME_SEC", "180"))
NAS_RECYCLE_HEAVY_DIR_NAMES = frozenset(
    name.strip()
    for name in os.environ.get(
        "MAGI_DISK_NAS_RECYCLE_HEAVY_DIR_NAMES",
        "Backup,Drive,SteamLibrary,Applications,WindowsApps",
    ).split(",")
    if name.strip()
)
EXPORT_OUTPUT_MAX_AGE_DAYS = float(os.environ.get("MAGI_DISK_EXPORT_OUTPUT_MAX_AGE_DAYS", "3"))
MODULE_STAGING_MAX_AGE_DAYS = float(os.environ.get("MAGI_DISK_MODULE_STAGING_MAX_AGE_DAYS", "14"))
PRESERVED_STANDALONE_SUFFIXES = frozenset({
    ".json",
    ".pickle",
    ".db",
    ".sqlite",
    ".sqlite3",
})

# 受保護名單（即使符合 pattern 也不動）
_PROTECTED_METRICS_NAMES = frozenset({
    "cron_state",
    # runtime pending 類不在 metrics/ 下，但額外保險
})


def _is_dry_run() -> bool:
    return os.environ.get("MAGI_DISK_CLEANUP_DRY_RUN", "1").strip().lower() in {"1", "true", "on", "yes"}


def _log(msg: str) -> None:
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"{ts} [disk-cleanup] {msg}", flush=True)


# ---- Metrics rotate -----------------------------------------------------

def _iter_metrics_jsonl(metrics_dir: Path) -> List[Path]:
    """回傳 metrics dir 下所有 .jsonl 檔（含一層巢狀）。"""
    out: List[Path] = []
    if not metrics_dir.is_dir():
        return out
    for entry in metrics_dir.iterdir():
        if entry.is_file() and entry.suffix == ".jsonl":
            out.append(entry)
        elif entry.is_dir() and entry.name.endswith(".jsonl"):
            # 巢狀情況（runtime_dir.metrics("ocr") / "pdf_ocr_consensus.jsonl"）
            for sub in entry.iterdir():
                if sub.is_file() and sub.suffix == ".jsonl":
                    out.append(sub)
    return out


def _rotate_metrics_file(path: Path, dry_run: bool) -> Optional[Dict[str, Any]]:
    try:
        size = path.stat().st_size
    except OSError:
        return None
    if size <= METRICS_ROTATE_BYTES:
        return None
    # 受保護
    if path.stem in _PROTECTED_METRICS_NAMES:
        _log(f"SKIP protected metrics: {path}")
        return None
    info = {"path": str(path), "size_before": size, "action": "rotate"}
    if dry_run:
        _log(f"DRY-RUN rotate: {path} ({size / 1024 / 1024:.2f} MB)")
        info["dry_run"] = True
        return info
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        tail = lines[-METRICS_KEEP_TAIL:]
        # atomic write
        import tempfile
        fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.writelines(tail)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        info["size_after"] = path.stat().st_size
        info["kept_lines"] = len(tail)
        _log(f"ROTATED: {path} {size} → {info['size_after']} bytes (kept {len(tail)} lines)")
    except Exception as e:
        info["error"] = str(e)
        _log(f"ERROR rotating {path}: {e}")
    return info


def cleanup_metrics(dry_run: bool) -> List[Dict[str, Any]]:
    metrics_dir = runtime_dir.root() / "metrics"
    actions: List[Dict[str, Any]] = []
    for f in _iter_metrics_jsonl(metrics_dir):
        info = _rotate_metrics_file(f, dry_run)
        if info is not None:
            actions.append(info)
    return actions


# ---- oMLX cache LRU -----------------------------------------------------

def _walk_cache_files(cache_root: Path) -> List[Path]:
    out: List[Path] = []
    if not cache_root.is_dir():
        return out
    for dirpath, _dirnames, filenames in os.walk(cache_root):
        for name in filenames:
            out.append(Path(dirpath) / name)
    return out


def _omlx_cache_roots(home: Path) -> List[Path]:
    roots: List[Path] = []
    base = home / ".omlx"
    for root in [base / "cache", *sorted(base.glob("cache-*"))]:
        if root.is_dir() and root not in roots:
            roots.append(root)
    return roots


def _cache_last_used(st: os.stat_result) -> float:
    # Some macOS mounts do not update atime reliably. Use the newer of atime/mtime
    # so a freshly written cache shard is not removed only because atime is old.
    return max(float(getattr(st, "st_atime", 0.0)), float(getattr(st, "st_mtime", 0.0)))


def _omlx_cache_cap_bytes(free_gb: float) -> int:
    cap_gb = OMLX_CACHE_CAP_GB
    if 0 <= free_gb < OMLX_CACHE_CRITICAL_FREE_GB:
        cap_gb = OMLX_CACHE_CRITICAL_CAP_GB
    elif 0 <= free_gb < OMLX_CACHE_LOW_WATER_FREE_GB:
        cap_gb = OMLX_CACHE_LOW_WATER_CAP_GB
    if cap_gb <= 0:
        return 0
    return int(cap_gb * 1024 * 1024 * 1024)


def _select_with_delete_budget(items: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], int, int]:
    """Keep deletion bounded even when a cache exploded."""
    if OMLX_CACHE_MAX_DELETE_BYTES <= 0:
        return items, 0, 0
    selected: List[Dict[str, Any]] = []
    budget = OMLX_CACHE_MAX_DELETE_BYTES
    skipped = 0
    skipped_bytes = 0
    for item in sorted(items, key=lambda x: (x["last_used"], x["path"].name)):
        size = int(item["size"])
        if size > budget:
            skipped += 1
            skipped_bytes += size
            continue
        selected.append(item)
        budget -= size
    return selected, skipped, skipped_bytes


def cleanup_omlx_cache(dry_run: bool) -> List[Dict[str, Any]]:
    home = Path(os.environ.get("HOME", "/Users/ai"))
    now = time.time()
    cutoff = now - OMLX_CACHE_KEEP_DAYS * 86400
    recent_grace_cutoff = now - OMLX_CACHE_RECENT_GRACE_MINUTES * 60
    free_gb = _disk_free_gb(MAGI_ROOT)
    cache_cap_bytes = _omlx_cache_cap_bytes(free_gb)
    actions: List[Dict[str, Any]] = []
    for cache_root in _omlx_cache_roots(home):
        total_candidate_bytes = 0
        deleted_bytes = 0
        deleted_count = 0
        total_bytes = 0
        all_files: List[Dict[str, Any]] = []
        selected_by_path: Dict[Path, Dict[str, Any]] = {}
        for f in _walk_cache_files(cache_root):
            try:
                st = f.stat()
            except OSError:
                continue
            size = int(st.st_size)
            total_bytes += size
            info = {
                "path": f,
                "size": size,
                "last_used": _cache_last_used(st),
                "mtime": float(st.st_mtime),
                "recent_write": float(st.st_mtime) >= recent_grace_cutoff,
                "reason": "",
            }
            all_files.append(info)
            if info["last_used"] < cutoff and not info["recent_write"]:
                info["reason"] = "stale"
                selected_by_path[f] = info

        projected_bytes = total_bytes - sum(int(i["size"]) for i in selected_by_path.values())
        if cache_cap_bytes > 0 and projected_bytes > cache_cap_bytes:
            for info in sorted(all_files, key=lambda x: (x["last_used"], x["path"].name)):
                path = info["path"]
                if path in selected_by_path or info["recent_write"]:
                    continue
                info = dict(info)
                info["reason"] = "cap"
                selected_by_path[path] = info
                projected_bytes -= int(info["size"])
                if projected_bytes <= cache_cap_bytes:
                    break

        candidates = list(selected_by_path.values())
        total_candidate_bytes = sum(int(i["size"]) for i in candidates)
        final_candidates, skipped_due_safety_cap, skipped_safety_bytes = _select_with_delete_budget(candidates)
        if not dry_run:
            for item in final_candidates:
                f = item["path"]
                size = int(item["size"])
                try:
                    f.unlink()
                    deleted_bytes += size
                    deleted_count += 1
                except OSError as e:
                    _log(f"cache unlink failed: {f} ({e})")
        info = {
            "cache": str(cache_root),
            "total_files": len(all_files),
            "total_bytes": total_bytes,
            "free_gb": round(free_gb, 2),
            "cache_cap_bytes": cache_cap_bytes,
            "keep_days": OMLX_CACHE_KEEP_DAYS,
            "recent_grace_minutes": OMLX_CACHE_RECENT_GRACE_MINUTES,
            "candidate_files": len(candidates),
            "candidate_bytes": total_candidate_bytes,
            "deleted_files": deleted_count,
            "deleted_bytes": deleted_bytes,
            "skipped_due_safety_cap": skipped_due_safety_cap,
            "skipped_safety_bytes": skipped_safety_bytes,
            "dry_run": dry_run,
        }
        if candidates and not final_candidates and total_candidate_bytes > OMLX_CACHE_MAX_DELETE_BYTES:
            info["skipped"] = True
            info["reason"] = "candidate_bytes_exceeds_safety_cap"
            actions.append(info)
            continue
        _log(
            f"oMLX cache {cache_root.name}: "
            f"{'would free' if dry_run else 'freed'} "
            f"{(total_candidate_bytes if dry_run else deleted_bytes) / 1024 / 1024:.2f} MB "
            f"({len(candidates)} candidates, total={total_bytes / 1024 / 1024 / 1024:.2f} GB, "
            f"cap={cache_cap_bytes / 1024 / 1024 / 1024:.2f} GB)"
        )
        actions.append(info)
    return actions


# ---- /tmp cleanup -------------------------------------------------------

_TMP_PREFIXES = ("magi_", "omlx_")
_TMP_SUFFIXES = (".png", ".log", ".txt", ".tmp")


def cleanup_tmp(dry_run: bool) -> List[Dict[str, Any]]:
    tmp = Path("/tmp")
    cutoff = time.time() - TMP_MAX_AGE_HOURS * 3600
    candidate_bytes = 0
    candidate_count = 0
    deleted_bytes = 0
    deleted_count = 0
    for entry in tmp.iterdir():
        name = entry.name
        if not any(name.startswith(p) for p in _TMP_PREFIXES):
            continue
        # 留給 omlx_switch_paused_until 等狀態檔，不處理
        if name in {
            "omlx_switch.lock.d",
            "omlx_switch_alert.txt",
            "omlx_switch_paused_until",
            "omlx_heartbeat_kill_decisions.jsonl",
        }:
            continue
        try:
            st = entry.stat()
        except OSError:
            continue
        if st.st_mtime >= cutoff:
            continue
        # 只處理檔案（含允許副檔名）與空目錄
        if entry.is_file() and not any(name.endswith(s) for s in _TMP_SUFFIXES):
            continue
        candidate_count += 1
        candidate_bytes += st.st_size
        if dry_run:
            continue
        try:
            if entry.is_file():
                entry.unlink()
            elif entry.is_dir():
                import shutil
                shutil.rmtree(entry)
            deleted_count += 1
            deleted_bytes += st.st_size
        except OSError as e:
            _log(f"tmp remove failed: {entry} ({e})")
    info = {
        "candidate_count": candidate_count,
        "candidate_bytes": candidate_bytes,
        "deleted_count": deleted_count,
        "deleted_bytes": deleted_bytes,
        "dry_run": dry_run,
    }
    _log(
        f"/tmp cleanup: {'would remove' if dry_run else 'removed'} "
        f"{candidate_count} entries, {candidate_bytes / 1024 / 1024:.2f} MB "
        f"(mtime > {TMP_MAX_AGE_HOURS}h)"
    )
    return [info]


# ---- local DB backups ---------------------------------------------------

def cleanup_db_backups(dry_run: bool) -> List[Dict[str, Any]]:
    """Prune local DB backup bursts while preserving recent restore points."""
    backup_dir = MAGI_ROOT / "_db_backups" / "law_firm_data"
    if not backup_dir.is_dir():
        return []

    groups: Dict[str, List[Path]] = {}
    for f in backup_dir.glob("*.sql.gz"):
        name = f.name
        label = "other"
        if name.startswith("law_firm_data_local_"):
            label = "local"
        elif name.startswith("law_firm_data_remote_"):
            label = "remote"
        groups.setdefault(label, []).append(f)

    actions: List[Dict[str, Any]] = []
    keep_latest = max(1, DB_BACKUP_KEEP_LATEST)
    for label, files in sorted(groups.items()):
        files = sorted(files, key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)
        keep = set(files[:keep_latest])
        candidates = [p for p in files if p not in keep]
        deleted_bytes = 0
        deleted_files = 0
        candidate_bytes = 0
        for f in candidates:
            try:
                size = f.stat().st_size
            except OSError:
                continue
            candidate_bytes += size
            meta = Path(str(f) + ".meta.json")
            if dry_run:
                continue
            try:
                f.unlink()
                deleted_files += 1
                deleted_bytes += size
                if meta.exists():
                    meta.unlink()
            except OSError as e:
                _log(f"DB backup remove failed: {f} ({e})")
        info = {
            "label": label,
            "dir": str(backup_dir),
            "keep_latest": keep_latest,
            "kept_files": len(keep),
            "candidate_files": len(candidates),
            "candidate_bytes": candidate_bytes,
            "deleted_files": deleted_files,
            "deleted_bytes": deleted_bytes,
            "dry_run": dry_run,
        }
        _log(
            f"DB backups {label}: {'would prune' if dry_run else 'pruned'} "
            f"{len(candidates)} files, {candidate_bytes / 1024 / 1024:.2f} MB "
            f"(keep latest {keep_latest})"
        )
        actions.append(info)
    return actions


# ---- build artifacts ----------------------------------------------------

def _disk_free_gb(path: Path = MAGI_ROOT) -> float:
    try:
        return shutil.disk_usage(path).free / 1024 / 1024 / 1024
    except OSError:
        return -1.0


def _has_preserved_standalone_content(path: Path) -> bool:
    """Do not remove bundled standalone resources/state as build trash."""
    try:
        if path.is_file():
            return path.suffix.lower() in PRESERVED_STANDALONE_SUFFIXES
        if path.is_dir():
            for child in path.rglob("*"):
                if child.is_file() and child.suffix.lower() in PRESERVED_STANDALONE_SUFFIXES:
                    return True
    except OSError:
        return True
    return False


def cleanup_build_artifacts(dry_run: bool) -> List[Dict[str, Any]]:
    """Remove rebuildable packaging artifacts when stale or disk is low."""
    if not BUILD_ARTIFACT_CLEANUP_ENABLE:
        return [{"enabled": False, "reason": "MAGI_DISK_BUILD_ARTIFACT_CLEANUP_ENABLE=0"}]

    now = time.time()
    cutoff = now - max(1, BUILD_ARTIFACT_MAX_AGE_DAYS) * 86400
    free_gb = _disk_free_gb(MAGI_ROOT)
    low_water = 0 <= free_gb < BUILD_ARTIFACT_LOW_WATER_GB
    targets = [
        MAGI_ROOT / "build" / "Paperclip",
        MAGI_ROOT / "dist" / "Paperclip",
        MAGI_ROOT / "dist" / "Paperclip.app",
    ]
    actions: List[Dict[str, Any]] = []
    for target in targets:
        if not target.exists():
            continue
        try:
            st = target.stat()
        except OSError:
            continue
        stale = st.st_mtime < cutoff
        should_remove = low_water or stale
        try:
            size = sum(p.stat().st_size for p in target.rglob("*") if p.is_file()) if target.is_dir() else st.st_size
        except OSError:
            size = 0
        info = {
            "path": str(target),
            "size_bytes": size,
            "stale": stale,
            "low_water": low_water,
            "free_gb": round(free_gb, 2),
            "threshold_gb": BUILD_ARTIFACT_LOW_WATER_GB,
            "deleted": False,
            "dry_run": dry_run,
        }
        if not should_remove:
            info["skipped"] = True
            info["reason"] = "not_stale_and_disk_above_low_water"
            actions.append(info)
            continue
        if _has_preserved_standalone_content(target):
            info["skipped"] = True
            info["reason"] = "contains_preserved_standalone_content"
            actions.append(info)
            _log(f"SKIP build artifact with standalone JSON/state: {target}")
            continue
        if dry_run:
            actions.append(info)
            _log(f"DRY-RUN build artifact remove: {target} ({size / 1024 / 1024:.2f} MB)")
            continue
        try:
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink()
            info["deleted"] = True
            _log(f"REMOVED build artifact: {target} ({size / 1024 / 1024:.2f} MB)")
        except OSError as e:
            info["error"] = str(e)
            _log(f"build artifact remove failed: {target} ({e})")
        actions.append(info)
    return actions


# ---- stale git temp packs ----------------------------------------------

def _git_tmp_pack_roots() -> List[Path]:
    raw = os.environ.get("MAGI_DISK_GIT_TMP_PACK_ROOTS", "")
    if raw.strip():
        return [Path(p).expanduser().resolve() for p in raw.split(os.pathsep) if p.strip()]
    roots = [MAGI_ROOT]
    parent = MAGI_ROOT.parent
    if parent != MAGI_ROOT:
        roots.append(parent)
    return roots


def _git_process_running() -> bool:
    try:
        import subprocess
        out = subprocess.run(
            ["ps", "-axo", "command="],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
        ).stdout
    except Exception:
        return True
    for line in (out or "").splitlines():
        cmd = line.strip()
        if not cmd:
            continue
        base = Path(cmd.split()[0]).name
        if base == "git":
            return True
    return False


def cleanup_stale_git_tmp_packs(dry_run: bool) -> List[Dict[str, Any]]:
    """Remove stale .git/objects/pack/tmp_pack_* files from failed git operations."""
    if not GIT_TMP_PACK_CLEANUP_ENABLE:
        return [{"enabled": False, "reason": "MAGI_DISK_GIT_TMP_PACK_CLEANUP_ENABLE=0"}]
    if _git_process_running():
        return [{"skipped": True, "reason": "git_process_running", "dry_run": dry_run}]

    cutoff = time.time() - max(1, GIT_TMP_PACK_MAX_AGE_HOURS) * 3600
    actions: List[Dict[str, Any]] = []
    seen: set[Path] = set()
    for root in _git_tmp_pack_roots():
        git_pack = root / ".git" / "objects" / "pack"
        if git_pack in seen:
            continue
        seen.add(git_pack)
        if not git_pack.is_dir():
            continue
        candidate_count = 0
        candidate_bytes = 0
        deleted_count = 0
        deleted_bytes = 0
        for f in git_pack.glob("tmp_pack_*"):
            if not f.is_file():
                continue
            try:
                st = f.stat()
            except OSError:
                continue
            if st.st_mtime >= cutoff:
                continue
            candidate_count += 1
            candidate_bytes += st.st_size
            if dry_run:
                continue
            try:
                f.unlink()
                deleted_count += 1
                deleted_bytes += st.st_size
            except OSError as e:
                _log(f"git tmp_pack remove failed: {f} ({e})")
        info = {
            "root": str(root),
            "pack_dir": str(git_pack),
            "candidate_files": candidate_count,
            "candidate_bytes": candidate_bytes,
            "deleted_files": deleted_count,
            "deleted_bytes": deleted_bytes,
            "max_age_hours": GIT_TMP_PACK_MAX_AGE_HOURS,
            "dry_run": dry_run,
        }
        if candidate_count:
            _log(
                f"git tmp_pack {git_pack}: {'would remove' if dry_run else 'removed'} "
                f"{candidate_count} files, {candidate_bytes / 1024 / 1024:.2f} MB"
            )
        actions.append(info)
    return actions


# ---- runtime/log compression -------------------------------------------

_COMPRESS_SUFFIXES = frozenset({".log", ".jsonl", ".txt", ".md", ".out", ".err"})


def _runtime_compress_roots() -> List[Path]:
    raw = os.environ.get("MAGI_DISK_RUNTIME_COMPRESS_ROOTS", "").strip()
    if raw:
        return [Path(p).expanduser().resolve() for p in raw.split(os.pathsep) if p.strip()]
    return [
        MAGI_ROOT / "logs",
        MAGI_ROOT / "_metrics",
        MAGI_ROOT / "_autopilot_runs",
        MAGI_ROOT / "reports",
        MAGI_ROOT / ".runtime" / "metrics",
        MAGI_ROOT / "casper.log",
        MAGI_ROOT / "server.log",
        MAGI_ROOT / "tools_api.log",
        MAGI_ROOT / "rpc_server.log",
    ]


def _iter_runtime_compressible_files(root: Path) -> List[Path]:
    if root.is_file():
        return [root]
    if not root.is_dir():
        return []
    out: List[Path] = []
    try:
        for dirpath, dirnames, filenames in os.walk(root):
            # Never walk source-control or pending operator-confirmation state.
            dirnames[:] = [
                d for d in dirnames
                if d not in {".git", "venv", ".venv", "node_modules", "pending", "db_backups"}
            ]
            for name in filenames:
                out.append(Path(dirpath) / name)
    except OSError:
        return out
    return out


def _gzip_replace(path: Path, dry_run: bool) -> Dict[str, Any]:
    before = path.stat().st_size
    gz_path = path.with_name(path.name + ".gz")
    info: Dict[str, Any] = {
        "path": str(path),
        "gz_path": str(gz_path),
        "size_before": before,
        "dry_run": dry_run,
    }
    if dry_run:
        return info

    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".gz.tmp", dir=str(path.parent))
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        st = path.stat()
        with open(path, "rb") as src, gzip.open(tmp_path, "wb", compresslevel=6) as dst:
            shutil.copyfileobj(src, dst)
        os.replace(tmp_path, gz_path)
        os.utime(gz_path, (st.st_atime, st.st_mtime))
        path.unlink()
        info["size_after"] = gz_path.stat().st_size
        info["freed_bytes"] = max(0, before - int(info["size_after"]))
        _log(
            f"COMPRESSED: {path} -> {gz_path.name} "
            f"({before / 1024 / 1024:.2f} MB -> {int(info['size_after']) / 1024 / 1024:.2f} MB)"
        )
    except Exception as e:
        info["error"] = str(e)
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass
        _log(f"runtime compress failed: {path} ({e})")
    return info


def compress_runtime_artifacts(dry_run: bool) -> List[Dict[str, Any]]:
    """Compress old MAGI-owned text artifacts without touching case/user data."""
    if not RUNTIME_COMPRESS_ENABLE:
        return [{"enabled": False, "reason": "MAGI_DISK_RUNTIME_COMPRESS_ENABLE=0"}]

    free_gb = _disk_free_gb(MAGI_ROOT)
    low_water = 0 <= free_gb < OMLX_CACHE_LOW_WATER_FREE_GB
    max_age_seconds = (
        RUNTIME_COMPRESS_LOW_WATER_MAX_AGE_HOURS * 3600
        if low_water
        else RUNTIME_COMPRESS_MAX_AGE_DAYS * 86400
    )
    min_bytes = RUNTIME_COMPRESS_LOW_WATER_MIN_BYTES if low_water else RUNTIME_COMPRESS_MIN_BYTES
    cutoff = time.time() - max_age_seconds
    actions: List[Dict[str, Any]] = []
    seen: set[Path] = set()

    for root in _runtime_compress_roots():
        for path in _iter_runtime_compressible_files(root):
            if path in seen:
                continue
            seen.add(path)
            try:
                st = path.stat()
            except OSError:
                continue
            suffix = path.suffix.lower()
            if suffix == ".gz" or path.name.endswith(".gz"):
                continue
            if suffix not in _COMPRESS_SUFFIXES:
                continue
            if st.st_size < min_bytes:
                continue
            if st.st_mtime >= cutoff:
                continue
            gz_path = path.with_name(path.name + ".gz")
            if gz_path.exists():
                continue
            actions.append(_gzip_replace(path, dry_run))

    total_before = sum(int(a.get("size_before", 0)) for a in actions)
    total_freed = sum(int(a.get("freed_bytes", 0)) for a in actions)
    _log(
        f"runtime compression: {'would compress' if dry_run else 'compressed'} "
        f"{len(actions)} files, source={total_before / 1024 / 1024:.2f} MB, "
        f"freed={total_freed / 1024 / 1024:.2f} MB, low_water={low_water}"
    )
    return actions


# ---- generated output / module staging cleanup -------------------------

_STAGING_DELETE_SUFFIXES = frozenset({
    ".docx",
    ".pdf",
    ".txt",
    ".md",
    ".html",
    ".csv",
    ".xlsx",
    ".png",
    ".jpg",
    ".jpeg",
    ".log",
    ".tmp",
})


def _staging_targets() -> List[Dict[str, Any]]:
    raw = os.environ.get("MAGI_DISK_STAGING_TARGETS", "").strip()
    if raw:
        targets: List[Dict[str, Any]] = []
        for item in raw.split(os.pathsep):
            if not item.strip():
                continue
            targets.append({
                "path": Path(item).expanduser().resolve(),
                "max_age_days": MODULE_STAGING_MAX_AGE_DAYS,
                "label": Path(item).name,
            })
        return targets
    return [
        {"path": MAGI_ROOT / "exports", "max_age_days": EXPORT_OUTPUT_MAX_AGE_DAYS, "label": "exports"},
        {"path": MAGI_ROOT / ".magi_doc_runs", "max_age_days": EXPORT_OUTPUT_MAX_AGE_DAYS, "label": "doc_runs"},
        {"path": MAGI_ROOT / "downloads", "max_age_days": MODULE_STAGING_MAX_AGE_DAYS, "label": "downloads"},
        {"path": MAGI_ROOT / "閱卷下載", "max_age_days": MODULE_STAGING_MAX_AGE_DAYS, "label": "file_review_staging"},
        {"path": MAGI_ROOT / "筆錄下載", "max_age_days": MODULE_STAGING_MAX_AGE_DAYS, "label": "transcript_staging"},
        {"path": MAGI_ROOT / "laf_downloads", "max_age_days": MODULE_STAGING_MAX_AGE_DAYS, "label": "laf_downloads"},
        {"path": MAGI_ROOT / "法扶資料", "max_age_days": MODULE_STAGING_MAX_AGE_DAYS, "label": "laf_staging"},
        {"path": MAGI_ROOT / "screenshot_sorted_output", "max_age_days": EXPORT_OUTPUT_MAX_AGE_DAYS, "label": "screenshot_output"},
    ]


def _safe_staging_file(path: Path) -> bool:
    if path.suffix.lower() in PRESERVED_STANDALONE_SUFFIXES:
        return False
    if path.name.startswith("."):
        return False
    return path.suffix.lower() in _STAGING_DELETE_SUFFIXES


def cleanup_generated_staging(dry_run: bool) -> List[Dict[str, Any]]:
    """Clean MAGI-owned output/staging folders after downstream import windows."""
    if not STAGING_CLEANUP_ENABLE:
        return [{"enabled": False, "reason": "MAGI_DISK_STAGING_CLEANUP_ENABLE=0"}]
    actions: List[Dict[str, Any]] = []
    now = time.time()
    for target in _staging_targets():
        root = Path(target["path"])
        label = str(target["label"])
        max_age_days = float(target["max_age_days"])
        cutoff = now - max_age_days * 86400
        info = {
            "label": label,
            "path": str(root),
            "max_age_days": max_age_days,
            "exists": root.exists(),
            "candidate_files": 0,
            "candidate_bytes": 0,
            "deleted_files": 0,
            "deleted_bytes": 0,
            "dry_run": dry_run,
        }
        if not root.is_dir():
            actions.append(info)
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            # Avoid hidden runtime state and nested virtualenv/package caches.
            dirnames[:] = [
                d for d in dirnames
                if d not in {".git", "venv", ".venv", "node_modules", "__pycache__"}
            ]
            for name in filenames:
                path = Path(dirpath) / name
                if not _safe_staging_file(path):
                    continue
                try:
                    st = path.stat()
                except OSError:
                    continue
                if st.st_mtime >= cutoff:
                    continue
                info["candidate_files"] += 1
                info["candidate_bytes"] += st.st_size
                if dry_run:
                    continue
                try:
                    path.unlink()
                    info["deleted_files"] += 1
                    info["deleted_bytes"] += st.st_size
                except OSError as e:
                    info.setdefault("errors", []).append({"path": str(path), "error": str(e)})
        _log(
            f"staging {label}: {'would remove' if dry_run else 'removed'} "
            f"{info['candidate_files']} files, {info['candidate_bytes'] / 1024 / 1024:.2f} MB "
            f"(mtime > {max_age_days:g}d)"
        )
        actions.append(info)
    return actions


# ---- duplicate payment-slip cleanup ------------------------------------

def _payment_duplicate_roots() -> List[Path]:
    raw = os.environ.get("MAGI_DISK_PAYMENT_DUPLICATE_ROOTS", "").strip()
    if raw:
        return [Path(p).expanduser().resolve() for p in raw.split(os.pathsep) if p.strip()]
    try:
        from api.case_path_mapper import preferred_case_roots
        roots = [Path(p).expanduser().resolve() for p in preferred_case_roots(include_closed=False) if p]
    except Exception:
        roots = []
    return [p for p in roots if p.is_dir() and (PAYMENT_DUPLICATE_ALLOW_SMB or not str(p).startswith("/Volumes/"))]


def _payment_registry_path() -> Path:
    raw = os.environ.get("MAGI_PAYMENT_REGISTRY_PATH", "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return MAGI_ROOT / "閱卷下載" / "payment_registry.json"


def _load_json_object(path: Path) -> Dict[str, Any]:
    try:
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception:
        return {}
    return {}


def _write_json_object(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _normalize_payment_case_token(raw: str) -> str:
    text = re.sub(r"[年度字第號\s\.\-_/／\\()（）]+", "", str(raw or ""))

    def _strip_zeros(m: re.Match) -> str:
        try:
            return str(int(m.group(0)))
        except Exception:
            return m.group(0).lstrip("0") or "0"

    return re.sub(r"\d+", _strip_zeros, text).strip().lower()


def _payment_base_from_name(name: str) -> str:
    if not (name.startswith("繳費單_") and name.lower().endswith(".pdf")):
        return ""
    stem = Path(name).stem
    parts = stem.split("_")
    # Expected canonical shape:
    #   繳費單_{party}_{case}.pdf
    # Chrome/MAGI duplicate shape:
    #   繳費單_{party}_{case}_{seq}.pdf
    # The court case itself uses dots, not underscores, so a final all-digit
    # underscore segment is a duplicate suffix rather than part of the case no.
    if len(parts) >= 4 and parts[-1].isdigit():
        stem = "_".join(parts[:-1])
    return f"{stem}.pdf"


def _payment_party_case_from_base(base_name: str) -> Tuple[str, str]:
    stem = Path(base_name).stem
    parts = stem.split("_", 2)
    if len(parts) < 3 or parts[0] != "繳費單":
        return "", ""
    return parts[1].strip(), parts[2].strip()


def _remember_payment_registry_file(registry: Dict[str, Any], path: Path) -> bool:
    party, case_no = _payment_party_case_from_base(path.name)
    if not (party and case_no):
        return False
    norm = _normalize_payment_case_token(case_no)
    if not norm:
        return False
    key = f"case:{norm}:{party}"
    entry = registry.get(key) if isinstance(registry.get(key), dict) else {}
    file_paths = [str(x) for x in (entry.get("file_paths") or []) if str(x)]
    file_names = [str(x) for x in (entry.get("files") or []) if str(x)]
    real = str(path)
    changed = False
    if real not in file_paths:
        file_paths.insert(0, real)
        changed = True
    if path.name not in file_names:
        file_names.insert(0, path.name)
        changed = True
    registry[key] = {
        **entry,
        "processed_at": entry.get("processed_at") or time.strftime("%Y-%m-%dT%H:%M:%S"),
        "yyidno": entry.get("yyidno") or case_no,
        "case_number": entry.get("case_number") or case_no,
        "party": entry.get("party") or party,
        "files": file_names,
        "file_paths": file_paths,
        "source": entry.get("source") or "disk_cleanup_duplicate_payment_slips",
    }
    return changed


def _prune_stale_disk_cleanup_payment_registry(registry: Dict[str, Any]) -> int:
    removed = 0
    for key, entry in list(registry.items()):
        if not isinstance(entry, dict):
            continue
        if entry.get("source") != "disk_cleanup_duplicate_payment_slips":
            continue
        paths = [Path(str(x)) for x in (entry.get("file_paths") or []) if str(x)]
        if paths and any(p.is_file() for p in paths):
            continue
        registry.pop(key, None)
        removed += 1
    return removed


def cleanup_duplicate_payment_slips(dry_run: bool) -> List[Dict[str, Any]]:
    """Collapse repeated OLA payment slips in case folders.

    Keep one canonical payment slip per directory/base name, quarantine suffixed
    repeats, and seed payment_registry so portal checks stop notifying/downloading
    the same fee slip after it has already been archived.
    """
    if not PAYMENT_DUPLICATE_CLEANUP_ENABLE:
        return [{"enabled": False, "reason": "MAGI_DISK_PAYMENT_DUPLICATE_CLEANUP_ENABLE=0"}]

    roots = _payment_duplicate_roots()
    registry_path = _payment_registry_path()
    registry = _load_json_object(registry_path)
    registry_changed = False
    groups: Dict[Tuple[str, str], List[Path]] = {}
    scanned_files = 0
    errors: List[Dict[str, str]] = []

    for root in roots:
        try:
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [d for d in dirnames if d not in {".git", ".sync", "@eaDir", "__pycache__"}]
                for name in filenames:
                    if not name.startswith("繳費單_") or not name.lower().endswith(".pdf"):
                        continue
                    scanned_files += 1
                    base = _payment_base_from_name(name)
                    if not base:
                        continue
                    groups.setdefault((dirpath, base), []).append(Path(dirpath) / name)
        except Exception as e:
            errors.append({"root": str(root), "error": str(e)})

    quarantine_root = MAGI_ROOT / ".runtime" / "duplicate_payment_slips_quarantine" / time.strftime("%Y%m%d_%H%M%S")
    duplicate_files = 0
    duplicate_bytes = 0
    quarantined_files = 0
    quarantined_bytes = 0
    canonical_renamed_files = 0
    kept_files = 0

    for (dirpath, base), paths in sorted(groups.items()):
        existing = [p for p in paths if p.is_file()]
        if not existing:
            continue
        exact = [p for p in existing if p.name == base]
        keep = exact[0] if exact else min(existing, key=lambda p: (p.stat().st_mtime, p.name))
        if not exact and keep.name != base:
            canonical = keep.with_name(base)
            if canonical.exists():
                existing.append(canonical)
                keep = canonical
            elif not dry_run:
                try:
                    old_keep = keep
                    shutil.move(str(keep), str(canonical))
                    keep = canonical
                    existing = [p for p in existing if p != old_keep]
                    existing.append(keep)
                    canonical_renamed_files += 1
                except Exception as e:
                    errors.append({"path": str(keep), "error": str(e)})
            else:
                canonical_renamed_files += 1
        kept_files += 1
        registry_changed = _remember_payment_registry_file(registry, keep) or registry_changed
        duplicates = [p for p in existing if p != keep]
        for dup in duplicates:
            try:
                size = dup.stat().st_size
            except OSError:
                size = 0
            duplicate_files += 1
            duplicate_bytes += size
            if dry_run:
                continue
            try:
                rel = Path(dirpath).relative_to(next(root for root in roots if Path(dirpath).is_relative_to(root)))
            except Exception:
                rel = Path(str(dirpath).lstrip("/"))
            dst_dir = quarantine_root / rel
            dst_dir.mkdir(parents=True, exist_ok=True)
            dst = dst_dir / dup.name
            if dst.exists():
                dst = dst_dir / f"{dup.stem}_{abs(hash(str(dup))) & 0xffff:x}{dup.suffix}"
            try:
                shutil.move(str(dup), str(dst))
                quarantined_files += 1
                quarantined_bytes += size
            except Exception as e:
                errors.append({"path": str(dup), "error": str(e)})

    pruned_registry_entries = _prune_stale_disk_cleanup_payment_registry(registry)
    registry_changed = bool(pruned_registry_entries) or registry_changed

    if registry_changed and not dry_run:
        try:
            _write_json_object(registry_path, registry)
        except Exception as e:
            errors.append({"path": str(registry_path), "error": str(e)})

    info = {
        "roots": [str(p) for p in roots],
        "registry_path": str(registry_path),
        "scanned_payment_files": scanned_files,
        "groups": len(groups),
        "kept_files": kept_files,
        "duplicate_files": duplicate_files,
        "duplicate_bytes": duplicate_bytes,
        "quarantined_files": quarantined_files,
        "quarantined_bytes": quarantined_bytes,
        "canonical_renamed_files": canonical_renamed_files,
        "pruned_registry_entries": pruned_registry_entries,
        "quarantine_root": str(quarantine_root) if duplicate_files else "",
        "registry_changed": registry_changed,
        "dry_run": dry_run,
        "errors": errors,
    }
    _log(
        f"payment duplicate slips: {'would quarantine' if dry_run else 'quarantined'} "
        f"{duplicate_files} files, {duplicate_bytes / 1024 / 1024:.2f} MB; "
        f"registry_changed={registry_changed}"
    )
    return [info]


# ---- NAS recycle cleanup -----------------------------------------------

_NAS_RECYCLE_NAMES = frozenset({"#recycle", "$RECYCLE.BIN", ".Trash", ".Trashes"})


def _default_nas_recycle_roots() -> List[Path]:
    roots = [
        Path("/Volumes/homes/#recycle"),
        Path("/Volumes/homes/lumi63181107/#recycle"),
        Path("/Volumes/lumi/$RECYCLE.BIN"),
        Path("/Volumes/lumi/lumi/#recycle"),
    ]
    return [p for p in roots if p.exists()]


def _nas_recycle_roots() -> List[Path]:
    raw = os.environ.get("MAGI_DISK_NAS_RECYCLE_ROOTS", "").strip()
    if raw:
        roots = [Path(p).expanduser().resolve() for p in raw.split(os.pathsep) if p.strip()]
    else:
        roots = _default_nas_recycle_roots()
    safe: List[Path] = []
    seen: set[Path] = set()
    for root in roots:
        if root in seen:
            continue
        seen.add(root)
        if not root.exists():
            continue
        if root.name not in _NAS_RECYCLE_NAMES:
            continue
        if not NAS_RECYCLE_ALLOW_NON_VOLUME and not str(root).startswith("/Volumes/"):
            continue
        safe.append(root)
    return safe


def _nas_recycle_candidate_parents(root: Path) -> List[Path]:
    """Return shallow parent folders whose direct children can be aged out.

    Synology homes recycle bins are usually shaped as:
      /Volumes/homes/#recycle/<user>/<deleted item>
      /Volumes/homes/<user>/#recycle/<deleted item>

    Windows-style recycle bins are commonly:
      /Volumes/lumi/$RECYCLE.BIN/<SID>/<deleted item>

    We intentionally stay shallow so daily cleanup does not deep-scan NAS case
    folders. Directory contents are only traversed if an old candidate is
    actually removed.
    """
    try:
        children = [p for p in root.iterdir()]
    except OSError:
        return []

    if root.name == "$RECYCLE.BIN":
        return [p for p in children if p.is_dir()]

    if root.name == "#recycle" and root.parent == Path("/Volumes/homes"):
        return [p for p in children if p.is_dir()]

    return [root]


def _iter_nas_recycle_candidates(root: Path) -> List[Path]:
    candidates: List[Path] = []
    for parent in _nas_recycle_candidate_parents(root):
        try:
            for child in parent.iterdir():
                if child.name in {".", "..", ".DS_Store"}:
                    continue
                if child.name in _NAS_RECYCLE_NAMES and child.is_dir():
                    try:
                        candidates.extend(
                            nested
                            for nested in child.iterdir()
                            if nested.name not in {".", "..", ".DS_Store"}
                        )
                    except OSError:
                        pass
                    continue
                candidates.append(child)
        except OSError:
            continue
    return candidates


def _is_heavy_nas_recycle_candidate(path: Path) -> bool:
    if not path.is_dir() or path.is_symlink():
        return False
    if path.name in NAS_RECYCLE_HEAVY_DIR_NAMES:
        return True
    if path.suffix.lower() == ".app":
        return True
    return False


def _remove_nas_recycle_candidate(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()


def cleanup_nas_recycle(dry_run: bool) -> List[Dict[str, Any]]:
    """Clean NAS recycle bins with strict root allowlisting and age retention."""
    if not NAS_RECYCLE_CLEANUP_ENABLE:
        return [{"enabled": False, "reason": "MAGI_DISK_NAS_RECYCLE_ENABLE=0"}]

    cutoff = time.time() - max(0.0, NAS_RECYCLE_MAX_AGE_DAYS) * 86400
    max_items = max(0, NAS_RECYCLE_MAX_DELETE_ITEMS)
    roots = _nas_recycle_roots()
    actions: List[Dict[str, Any]] = []
    deleted_total = 0
    start_mono = time.monotonic()

    for root in roots:
        info: Dict[str, Any] = {
            "root": str(root),
            "max_age_days": NAS_RECYCLE_MAX_AGE_DAYS,
            "max_delete_items": max_items,
            "max_runtime_sec": NAS_RECYCLE_MAX_RUNTIME_SEC,
            "candidate_items": 0,
            "deleted_items": 0,
            "skipped_heavy_items": 0,
            "skipped_heavy_paths": [],
            "dry_run": dry_run,
            "errors": [],
        }
        candidates = []
        for path in _iter_nas_recycle_candidates(root):
            try:
                st = path.stat()
            except OSError as exc:
                info["errors"].append({"path": str(path), "error": str(exc)})
                continue
            if st.st_mtime >= cutoff:
                continue
            if _is_heavy_nas_recycle_candidate(path):
                info["skipped_heavy_items"] += 1
                if len(info["skipped_heavy_paths"]) < 20:
                    info["skipped_heavy_paths"].append(str(path))
                continue
            candidates.append((st.st_mtime, path))

        candidates.sort(key=lambda item: (item[0], str(item[1])))
        info["candidate_items"] = len(candidates)
        for _mtime, path in candidates:
            if max_items and deleted_total >= max_items:
                info["stopped_reason"] = "max_delete_items_reached"
                break
            if NAS_RECYCLE_MAX_RUNTIME_SEC > 0 and time.monotonic() - start_mono >= NAS_RECYCLE_MAX_RUNTIME_SEC:
                info["stopped_reason"] = "max_runtime_reached"
                break
            if dry_run:
                continue
            try:
                _remove_nas_recycle_candidate(path)
                info["deleted_items"] += 1
                deleted_total += 1
            except Exception as exc:
                info["errors"].append({"path": str(path), "error": str(exc)})

        _log(
            f"nas recycle {root}: {'would remove' if dry_run else 'removed'} "
            f"{info['candidate_items']} old items; deleted={info['deleted_items']}"
        )
        actions.append(info)
        if max_items and deleted_total >= max_items:
            break

    if not roots:
        actions.append({"roots": [], "candidate_items": 0, "dry_run": dry_run})
    return actions


# ---- server.log 回報 ----------------------------------------------------

def report_agent_logs(_dry_run: bool) -> List[Dict[str, Any]]:
    """.agent/server.log* 的既有 rotate 會處理；這裡只回報總大小。"""
    agent_dir = MAGI_ROOT / ".agent"
    if not agent_dir.is_dir():
        return []
    total = 0
    count = 0
    for f in agent_dir.glob("server.log*"):
        try:
            total += f.stat().st_size
            count += 1
        except OSError:
            pass
    info = {"dir": str(agent_dir), "log_count": count, "total_bytes": total}
    _log(f".agent/server.log*: {count} files, {total / 1024 / 1024:.2f} MB (existing rotate handles this)")
    return [info]


# ---- entrypoint ---------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="MAGI disk cleanup healthcheck")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="report only; do not delete/rotate")
    mode.add_argument("--apply", action="store_true", help="perform guarded cleanup")
    args = parser.parse_args([] if argv is None else argv)

    dry_run = True if args.dry_run else (False if args.apply else _is_dry_run())
    _log(f"start (dry_run={dry_run})")
    summary: Dict[str, Any] = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "dry_run": dry_run,
        "metrics": cleanup_metrics(dry_run),
        "omlx_cache": cleanup_omlx_cache(dry_run),
        "tmp": cleanup_tmp(dry_run),
        "db_backups": cleanup_db_backups(dry_run),
        "build_artifacts": cleanup_build_artifacts(dry_run),
        "git_tmp_packs": cleanup_stale_git_tmp_packs(dry_run),
        "compressed_artifacts": compress_runtime_artifacts(dry_run),
        "generated_staging": cleanup_generated_staging(dry_run),
        "duplicate_payment_slips": cleanup_duplicate_payment_slips(dry_run),
        "nas_recycle": cleanup_nas_recycle(dry_run),
        "agent_logs": report_agent_logs(dry_run),
    }
    # 寫 summary 到 runtime/metrics（自己也透過 runtime_dir 管理）
    try:
        metrics_path = runtime_dir.metrics("disk_cleanup_summary")
        runtime_dir.atomic_append_jsonl(
            metrics_path,
            summary,
            rotate_at=500,
            keep_tail=300,
        )
    except Exception as e:
        _log(f"summary write failed: {e}")
    _log("done")
    # stdout 也直接印一份簡要摘要
    total_metrics = len(summary["metrics"])
    total_cache_candidates = sum(a.get("candidate_files", 0) for a in summary["omlx_cache"])
    tmp_entry = summary["tmp"][0] if summary["tmp"] else {}
    total_compress_candidates = len(summary.get("compressed_artifacts") or [])
    total_staging_candidates = sum(a.get("candidate_files", 0) for a in summary.get("generated_staging") or [])
    total_payment_duplicates = sum(a.get("duplicate_files", 0) for a in summary.get("duplicate_payment_slips") or [])
    _log(
        f"summary: metrics_rotated={total_metrics}, "
        f"omlx_cache_candidates={total_cache_candidates}, "
        f"tmp_candidates={tmp_entry.get('candidate_count', 0)}, "
        f"compress_candidates={total_compress_candidates}, "
        f"staging_candidates={total_staging_candidates}, "
        f"payment_duplicates={total_payment_duplicates}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
