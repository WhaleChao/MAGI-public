"""
distill_collector — 知識蒸餾訓練資料收集器

收集 NIM / oMLX 高品質判決摘要 (prompt, response) 對，
作為 Gemma E4B LoRA 微調的訓練資料。

儲存位置: ~/.omlx/training/gemma-distill/raw_pairs.jsonl
"""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import os
import random
import shutil
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger("distill_collector")

# ── 蒸餾目標 ─────────────────────────────────────────────────────────
ACTIVE_DISTILL_TARGET = os.environ.get("MAGI_DISTILL_TARGET", "gemma").lower()


def _paths_for(target: str) -> dict:
    """回傳蒸餾目標的路徑 dict（raw / train / eval / state / dir）。"""
    d = Path(os.environ.get("GEMMA_DISTILL_DIR", str(Path.home() / ".omlx/training/gemma-distill")))
    return {
        "dir": d,
        "raw": d / "raw_pairs.jsonl",
        "train": d / "train.jsonl",
        "eval": d / "eval.jsonl",
        "state": d / "collector_state.json",
    }


# ── 路徑 ─────────────────────────────────────────────────────────────
_active_paths = _paths_for(ACTIVE_DISTILL_TARGET)
DISTILL_DIR = _active_paths["dir"]
RAW_PATH = _active_paths["raw"]
TRAIN_PATH = _active_paths["train"]
EVAL_PATH = _active_paths["eval"]
STATE_PATH = _active_paths["state"]

# ── 品質門檻 ──────────────────────────────────────────────────────────
MIN_OUTPUT_LEN = 100
MAX_OUTPUT_LEN = 5000
MIN_STRUCTURE_HEADERS = 1  # 新 prompt 只需「實務見解」+「適用法條」即可
RAW_MAX_BYTES = 50 * 1024 * 1024  # 50 MB 上限，超過就 rotate

STRUCTURE_HEADERS = [
    "實務見解", "法院見解", "適用法條", "法院認為", "應解為",
    "裁判要旨", "爭點", "法律分析",
]

REJECT_KEYWORDS = [
    "系統降級回覆", "摘要失敗", "逾時", "timeout",
    "無法摘要", "無法擷取", "案由不符", "無可擷取", "error", "Error",
    "服務暫時不可用", "請再試一次",
]

SYSTEM_PROMPT = "你是資深法律研究助理，專精司法見解分析。"


# ── State 管理 ─────────────────────────────────────────────────────────
def _load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text("utf-8"))
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 60, exc_info=True)
    return {"seen_hashes": [], "total_collected": 0, "last_collected_at": None}


def _save_state(state: dict) -> None:
    DISTILL_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), "utf-8")
    tmp.replace(STATE_PATH)


def _content_hash(prompt: str, response: str) -> str:
    return hashlib.sha256(f"{prompt}||{response}".encode()).hexdigest()


# ── 品質檢查 ──────────────────────────────────────────────────────────
def _passes_quality(response: str) -> bool:
    """檢查 Codex 回覆是否達到訓練品質門檻。"""
    if not response:
        return False
    length = len(response)
    if length < MIN_OUTPUT_LEN or length > MAX_OUTPUT_LEN:
        return False

    # 拒絕含降級關鍵字的回覆
    for kw in REJECT_KEYWORDS:
        if kw in response:
            return False

    # 檢查結構標題數量
    header_count = sum(1 for h in STRUCTURE_HEADERS if h in response)
    if header_count < MIN_STRUCTURE_HEADERS:
        return False

    return True


# ── 檔案 Rotation ────────────────────────────────────────────────────
def _rotate_if_needed() -> None:
    """raw_pairs.jsonl 超過 50MB 時 gzip 舊段。"""
    if not RAW_PATH.exists():
        return
    try:
        size = RAW_PATH.stat().st_size
    except OSError:
        return
    if size < RAW_MAX_BYTES:
        return

    ts = time.strftime("%Y%m%d_%H%M%S")
    archive = DISTILL_DIR / f"raw_pairs_{ts}.jsonl.gz"
    try:
        with open(RAW_PATH, "rb") as f_in, gzip.open(archive, "wb") as f_out:
            shutil.copyfileobj(f_in, f_out)
        RAW_PATH.write_text("", "utf-8")  # 清空
        logger.info("Rotated raw_pairs.jsonl → %s (%.1f MB)", archive.name, size / 1e6)
    except Exception as e:
        logger.warning("raw_pairs rotation failed: %s", e)


def _cleanup_old_archives(keep_days: int = 90) -> None:
    """清理超過 keep_days 的 gzip 歸檔。"""
    cutoff = time.time() - keep_days * 86400
    for f in DISTILL_DIR.glob("raw_pairs_*.jsonl.gz"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
                logger.info("Cleaned up old archive: %s", f.name)
        except OSError:
            pass


# ── 主要 API ──────────────────────────────────────────────────────────
def _write_to_target(paths: dict, record: dict, h: str) -> bool:
    """寫入一筆 record 到指定 target 的 raw_pairs.jsonl。"""
    target_dir = paths["dir"]
    raw_path = paths["raw"]
    target_dir.mkdir(parents=True, exist_ok=True)

    # 對此目標做 rotate 檢查
    try:
        if raw_path.exists() and raw_path.stat().st_size > RAW_MAX_BYTES:
            ts = time.strftime("%Y%m%d_%H%M%S")
            archive = target_dir / f"raw_pairs_{ts}.jsonl.gz"
            import gzip as _gzip
            with open(raw_path, "rb") as f_in, _gzip.open(archive, "wb") as f_out:
                f_out.write(f_in.read())
            raw_path.write_text("", "utf-8")
    except Exception as e:
        logger.warning("Distill: rotate failed for %s: %s", target_dir.name, e)

    try:
        with open(raw_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        return True
    except Exception as e:
        logger.warning("Distill: write failed to %s: %s", target_dir.name, e)
        return False


def collect_summary_pair(
    prompt: str,
    response: str,
    case_reason: str = "",
    source: str = "nim_resummary",
) -> bool:
    """
    收集一組 (prompt, response) 訓練資料。

    Returns True if collected, False if rejected (quality / dedup).
    """
    if not _passes_quality(response):
        logger.debug("Distill: rejected (quality gate)")
        return False

    h = _content_hash(prompt, response)
    state = _load_state()
    seen = set(state.get("seen_hashes", []))
    if h in seen:
        logger.debug("Distill: rejected (duplicate)")
        return False

    # 組裝 JSONL record
    record = {
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
            {"role": "assistant", "content": response},
        ],
        "metadata": {
            "source": source,
            "case_reason": case_reason,
            "collected_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "content_hash": f"sha256:{h}",
        },
    }

    primary_paths = _paths_for(ACTIVE_DISTILL_TARGET)
    ok = _write_to_target(primary_paths, record, h)
    if not ok:
        return False

    # 更新 state
    seen.add(h)
    # 只保留最近 30000 個 hash 以免 state 太大
    seen_list = list(seen)
    if len(seen_list) > 30000:
        seen_list = seen_list[-25000:]
    state["seen_hashes"] = seen_list
    state["total_collected"] = state.get("total_collected", 0) + 1
    state["last_collected_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    _save_state(state)

    logger.info(
        "Distill: collected pair #%d (reason=%s, len=%d)",
        state["total_collected"], case_reason or "?", len(response),
    )
    _cleanup_old_archives()
    return True


def build_training_set(eval_ratio: float = 0.1, seed: int = 42) -> dict:
    """
    從 raw_pairs.jsonl 建立 train.jsonl + eval.jsonl (90/10 split)。

    Returns: {"train": int, "eval": int, "skipped": int}
    """
    if not RAW_PATH.exists():
        return {"train": 0, "eval": 0, "skipped": 0}

    records: list[str] = []
    seen_hashes: set[str] = set()
    skipped = 0

    with open(RAW_PATH, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                skipped += 1
                continue

            # 再次驗證品質（raw 可能含早期低品質資料）
            messages = rec.get("messages", [])
            assistant_msg = next(
                (m["content"] for m in messages if m.get("role") == "assistant"), ""
            )
            if not _passes_quality(assistant_msg):
                skipped += 1
                continue

            h = rec.get("metadata", {}).get("content_hash", "")
            if h in seen_hashes:
                skipped += 1
                continue
            seen_hashes.add(h)
            records.append(line)

    if not records:
        return {"train": 0, "eval": 0, "skipped": skipped}

    rng = random.Random(seed)
    rng.shuffle(records)

    split_idx = max(1, int(len(records) * (1 - eval_ratio)))
    train_records = records[:split_idx]
    eval_records = records[split_idx:]

    TRAIN_PATH.write_text("\n".join(train_records) + "\n", "utf-8")
    EVAL_PATH.write_text("\n".join(eval_records) + "\n", "utf-8")

    logger.info(
        "Built training set: %d train, %d eval, %d skipped",
        len(train_records), len(eval_records), skipped,
    )
    return {"train": len(train_records), "eval": len(eval_records), "skipped": skipped}


def get_stats() -> dict:
    """回傳收集統計資訊。"""
    state = _load_state()
    raw_size = RAW_PATH.stat().st_size if RAW_PATH.exists() else 0
    train_count = 0
    eval_count = 0
    if TRAIN_PATH.exists():
        train_count = sum(1 for l in open(TRAIN_PATH) if l.strip())
    if EVAL_PATH.exists():
        eval_count = sum(1 for l in open(EVAL_PATH) if l.strip())

    return {
        "total_collected": state.get("total_collected", 0),
        "last_collected_at": state.get("last_collected_at"),
        "raw_pairs_bytes": raw_size,
        "raw_pairs_mb": round(raw_size / 1e6, 2),
        "train_count": train_count,
        "eval_count": eval_count,
        "dedup_hashes": len(state.get("seen_hashes", [])),
        "archives": len(list(DISTILL_DIR.glob("raw_pairs_*.jsonl.gz"))),
    }
