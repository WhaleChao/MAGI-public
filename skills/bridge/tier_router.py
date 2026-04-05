"""
Tier Router — 動態雙層推理分流
================================
輕型任務 → oMLX E4B (port 8080, 常駐)
重型任務 → Ollama Gemma4:26B (port 11434, 按需載入)

使用者可透過任何通訊頻道手動切換模式。
"""

import json
import logging
import os
import threading
import time
from typing import Callable, Optional

import requests

logger = logging.getLogger("tier_router")

# ── 環境變數 ────────────────────────────────────────────────────
TIER_ENABLED = os.environ.get("MAGI_TIER_ENABLED", "1").strip() in ("1", "true", "yes")
DEFAULT_MODE = os.environ.get("MAGI_TIER_DEFAULT_MODE", "auto").strip().lower()

OLLAMA_BASE = os.environ.get("MAGI_OLLAMA_26B_BASE", "http://127.0.0.1:11434").rstrip("/")
OLLAMA_MODEL = os.environ.get("MAGI_OLLAMA_26B_MODEL", "gemma4:26b")
OLLAMA_KEEP_ALIVE = os.environ.get("MAGI_OLLAMA_26B_KEEP_ALIVE", "5m")
WARMUP_TIMEOUT = int(os.environ.get("MAGI_OLLAMA_26B_WARMUP_TIMEOUT", "30"))

SUMMARY_THRESHOLD = int(os.environ.get("MAGI_TIER_SUMMARY_THRESHOLD", "2000"))
TRANSLATE_THRESHOLD = int(os.environ.get("MAGI_TIER_TRANSLATE_THRESHOLD", "1000"))
TRANSCRIBE_THRESHOLD = int(os.environ.get("MAGI_TIER_TRANSCRIBE_THRESHOLD", "3000"))

# ── 分層規則 ────────────────────────────────────────────────────
HEAVY_TASKS = {"legal_analysis", "coding", "reflection"}
CONDITIONAL_TASKS = {"summary", "translate", "transcribe"}
# 其餘全部走 E4B（general, tc_review, vision, ocr, captcha, date_extract, embedding, night_talk）

# ── 狀態 ────────────────────────────────────────────────────────
_lock = threading.Lock()
_override_mode: str = DEFAULT_MODE  # "auto" | "e4b" | "26b"
_warmup_lock = threading.Lock()  # 防止多個 warmup 同時跑


def resolve_tier(task_type: str, prompt: str = "") -> str:
    """決定任務走 E4B 還是 26B。回傳 "e4b" 或 "26b"。"""
    if not TIER_ENABLED:
        return "e4b"

    with _lock:
        mode = _override_mode

    if mode == "e4b":
        return "e4b"
    if mode == "26b":
        return "26b"

    # auto 模式
    if task_type in HEAVY_TASKS:
        return "26b"

    if task_type in CONDITIONAL_TASKS:
        prompt_len = len(prompt)
        if task_type == "summary" and prompt_len > SUMMARY_THRESHOLD:
            return "26b"
        if task_type == "translate" and prompt_len > TRANSLATE_THRESHOLD:
            return "26b"
        if task_type == "transcribe" and prompt_len > TRANSCRIBE_THRESHOLD:
            return "26b"

    return "e4b"


def _check_26b_loaded() -> bool:
    """檢查 Ollama 是否已載入 26B 模型。"""
    try:
        r = requests.get(f"{OLLAMA_BASE}/api/ps", timeout=3)
        if r.status_code == 200:
            data = r.json()
            models = data.get("models", [])
            for m in models:
                if OLLAMA_MODEL in m.get("name", ""):
                    return True
    except Exception:
        pass
    return False


def _ollama_serve_alive() -> bool:
    """檢查 Ollama serve 進程是否在線。"""
    try:
        r = requests.get(f"{OLLAMA_BASE}/api/tags", timeout=2)
        return r.status_code == 200
    except Exception:
        return False


def ensure_26b_ready(progress_fn: Optional[Callable] = None) -> bool:
    """確保 26B 模型載入就緒。回傳 True=就緒, False=失敗。

    progress_fn: 可選回呼函數，用於發送「載入中」通知。
    """
    if _check_26b_loaded():
        return True

    if not _ollama_serve_alive():
        logger.warning("Ollama serve 不在線，無法載入 26B")
        return False

    # 防止多個 warmup 同時跑
    acquired = _warmup_lock.acquire(timeout=WARMUP_TIMEOUT + 5)
    if not acquired:
        logger.warning("26B warmup lock 等待逾時")
        return _check_26b_loaded()

    try:
        # 再檢查一次（可能另一個 thread 已載入）
        if _check_26b_loaded():
            return True

        # 發送通知
        if progress_fn:
            try:
                progress_fn("🧠 載入重型模型中（約 15-20 秒）...")
            except Exception as e:
                logger.debug("progress_fn failed: %s", e)

        logger.info("26B 模型未載入，開始 warmup...")

        # 觸發模型載入
        try:
            requests.post(
                f"{OLLAMA_BASE}/api/generate",
                json={"model": OLLAMA_MODEL, "prompt": "", "keep_alive": OLLAMA_KEEP_ALIVE},
                timeout=5,
            )
        except Exception:
            pass  # generate 可能 timeout（模型正在載入），沒關係

        # 輪詢等待載入完成
        deadline = time.time() + WARMUP_TIMEOUT
        while time.time() < deadline:
            time.sleep(2)
            if _check_26b_loaded():
                logger.info("26B 模型載入完成")
                return True

        logger.warning("26B warmup 逾時 (%ds)", WARMUP_TIMEOUT)
        return False

    finally:
        _warmup_lock.release()


def set_mode(mode: str) -> str:
    """設定手動覆蓋模式。回傳格式化的狀態訊息。"""
    global _override_mode
    mode = mode.strip().lower()
    if mode not in ("auto", "e4b", "26b"):
        return f"❌ 無效模式: {mode}（可用: auto, e4b, 26b）"

    with _lock:
        old = _override_mode
        _override_mode = mode

    mode_names = {"auto": "🔄 自動分層", "e4b": "⚡ 輕型 E4B", "26b": "🧠 重型 26B"}
    msg = f"✅ 推理模式已切換: {mode_names.get(old, old)} → {mode_names.get(mode, mode)}"

    if mode == "26b":
        loaded = _check_26b_loaded()
        msg += f"\n26B 狀態: {'已載入' if loaded else '待命（下次推理時自動載入）'}"

    logger.info("Tier mode changed: %s → %s", old, mode)
    return msg


def get_status() -> dict:
    """回傳當前 tier 狀態。"""
    with _lock:
        mode = _override_mode

    ollama_alive = _ollama_serve_alive()
    model_loaded = _check_26b_loaded() if ollama_alive else False

    return {
        "enabled": TIER_ENABLED,
        "mode": mode,
        "ollama_alive": ollama_alive,
        "model_26b_loaded": model_loaded,
        "model_26b": OLLAMA_MODEL,
        "ollama_base": OLLAMA_BASE,
        "thresholds": {
            "summary": SUMMARY_THRESHOLD,
            "translate": TRANSLATE_THRESHOLD,
            "transcribe": TRANSCRIBE_THRESHOLD,
        },
        "heavy_tasks": sorted(HEAVY_TASKS),
    }


def format_status() -> str:
    """回傳格式化的狀態字串（給使用者看的）。"""
    s = get_status()
    mode_names = {"auto": "🔄 自動分層", "e4b": "⚡ 輕型 E4B 固定", "26b": "🧠 重型 26B 固定"}
    lines = [
        "📊 **推理分層狀態**",
        f"模式: {mode_names.get(s['mode'], s['mode'])}",
        f"Ollama: {'在線' if s['ollama_alive'] else '離線'}",
        f"26B ({s['model_26b']}): {'已載入 🟢' if s['model_26b_loaded'] else '待命 🟡' if s['ollama_alive'] else '不可用 🔴'}",
        f"重型任務: {', '.join(s['heavy_tasks'])}",
        f"門檻: 摘要>{s['thresholds']['summary']}字 / 翻譯>{s['thresholds']['translate']}字",
        "",
        "指令: 切換26B / 切換E4B / 自動模式",
    ]
    return "\n".join(lines)
