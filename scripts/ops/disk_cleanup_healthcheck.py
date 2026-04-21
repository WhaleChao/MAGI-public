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
import sys
import time
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
TMP_MAX_AGE_HOURS = int(os.environ.get("MAGI_DISK_TMP_MAX_AGE_HOURS", "48"))

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
            if dry_run:
                continue
            try:
                f.unlink()
                deleted_bytes += st.st_size
                deleted_count += 1
            except OSError as e:
                _log(f"cache unlink failed: {f} ({e})")
        info = {
            "cache": str(cache_root),
            "candidate_files": candidate_count,
            "candidate_bytes": total_candidate_bytes,
            "deleted_files": deleted_count,
            "deleted_bytes": deleted_bytes,
            "dry_run": dry_run,
        }
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

def main() -> int:
    dry_run = _is_dry_run()
    _log(f"start (dry_run={dry_run})")
    summary: Dict[str, Any] = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "dry_run": dry_run,
        "metrics": cleanup_metrics(dry_run),
        "omlx_cache": cleanup_omlx_cache(dry_run),
        "tmp": cleanup_tmp(dry_run),
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
    sys.exit(main())
