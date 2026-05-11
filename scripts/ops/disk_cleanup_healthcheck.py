#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Layer 4 — 磁碟自動清理健檢

每日 03:45 cron 觸發；dry-run 預設開（MAGI_DISK_CLEANUP_DRY_RUN=1），
切 0 才真的刪／rotate。

管轄範圍：
  - .runtime/metrics/*.jsonl (含巢狀 ocr.jsonl/ 子目錄)：> 10MB → rotate 保留 tail 1000 行
  - ~/.omlx/cache-*/：保留 atime ≥ 7 天以內的檔，其餘視為可釋放
  - /tmp/magi_*、/tmp/omlx_* 與 *.png / *.json / *.log：mtime > 48h 刪除
  - .agent/server.log*：僅回報總大小，既有 rotate 機制已處理

紅線：
  - 不碰六模組資料（LAF / 閱卷 / 筆錄 / 摘要 / 翻譯 / 逐字稿）
  - 不碰 runtime pending/*（正在等律師確認碼的檔案）
  - 不碰 cron_state.json
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import time
import argparse
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


def cleanup_omlx_cache(dry_run: bool) -> List[Dict[str, Any]]:
    home = Path(os.environ.get("HOME", "/Users/ai"))
    cutoff = time.time() - OMLX_CACHE_KEEP_DAYS * 86400
    actions: List[Dict[str, Any]] = []
    for cache_root in home.glob(".omlx/cache-*"):
        if not cache_root.is_dir():
            continue
        total_candidate_bytes = 0
        candidate_count = 0
        deleted_bytes = 0
        deleted_count = 0
        candidates: List[Tuple[Path, int]] = []
        for f in _walk_cache_files(cache_root):
            try:
                st = f.stat()
            except OSError:
                continue
            # 用 atime（讀取時間）；若 fs 不支援則退回 mtime
            last_access = getattr(st, "st_atime", st.st_mtime)
            if last_access >= cutoff:
                continue
            total_candidate_bytes += st.st_size
            candidate_count += 1
            candidates.append((f, st.st_size))
        if not dry_run and total_candidate_bytes <= OMLX_CACHE_MAX_DELETE_BYTES:
            for f, size in candidates:
                try:
                    f.unlink()
                    deleted_bytes += size
                    deleted_count += 1
                except OSError as e:
                    _log(f"cache unlink failed: {f} ({e})")
        elif not dry_run and total_candidate_bytes > OMLX_CACHE_MAX_DELETE_BYTES:
            _log(
                f"SKIP oMLX cache {cache_root.name}: "
                f"{total_candidate_bytes / 1024 / 1024 / 1024:.2f} GB exceeds "
                f"safety cap {OMLX_CACHE_MAX_DELETE_BYTES / 1024 / 1024 / 1024:.2f} GB"
            )
        info = {
            "cache": str(cache_root),
            "candidate_files": candidate_count,
            "candidate_bytes": total_candidate_bytes,
            "deleted_files": deleted_count,
            "deleted_bytes": deleted_bytes,
            "dry_run": dry_run,
        }
        if not dry_run and total_candidate_bytes > OMLX_CACHE_MAX_DELETE_BYTES:
            info["skipped"] = True
            info["reason"] = "candidate_bytes_exceeds_safety_cap"
            actions.append(info)
            continue
        _log(
            f"oMLX cache {cache_root.name}: "
            f"{'would free' if dry_run else 'freed'} "
            f"{total_candidate_bytes / 1024 / 1024:.2f} MB "
            f"({candidate_count} files, atime > {OMLX_CACHE_KEEP_DAYS}d)"
        )
        actions.append(info)
    return actions


# ---- /tmp cleanup -------------------------------------------------------

_TMP_PREFIXES = ("magi_", "omlx_")
_TMP_SUFFIXES = (".png", ".json", ".log", ".jsonl", ".txt", ".tmp")


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
    _log(
        f"summary: metrics_rotated={total_metrics}, "
        f"omlx_cache_candidates={total_cache_candidates}, "
        f"tmp_candidates={tmp_entry.get('candidate_count', 0)}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
