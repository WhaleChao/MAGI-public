#!/usr/bin/env python3
"""
weekend_resummary.py — 週末 NIM 批次重摘要

用 NVIDIA NIM 405B 重新摘要所有已下載的判決全文，同時餵進知識蒸餾管線。

安全機制：
- PID lock 防止多進程同時跑
- SIGTERM/SIGINT graceful shutdown
- 連續失敗 backoff（3 連敗等 60s，5 連敗停止）
- 短文 (<1KB) 跳過不浪費 NIM 額度
- NIM 日預算防呆（WEEKEND_RESUMMARY_BUDGET_CAP）

用法：
  python weekend_resummary.py                    # 摘要所有尚未完成的
  python weekend_resummary.py --all              # 強制重摘所有
  python weekend_resummary.py --limit 100        # 限制筆數
  python weekend_resummary.py --dry-run          # 模擬模式
"""

from __future__ import annotations

import argparse
import atexit
import json
import logging
import os
import re
import signal
import sys
import time
from pathlib import Path

MAGI_ROOT = Path(os.environ.get("MAGI_ROOT_DIR", str(Path.home() / "Desktop/MAGI")))
sys.path.insert(0, str(MAGI_ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("weekend_resummary")

# ── 路徑 ──────────────────────────────────────────────────────────────
NORM_ROOT = Path.home() / ".cache/judgment_collector/judicial_api/normalized"
JUDGMENTS_JSON = MAGI_ROOT / "skills/judgment-collector/judgments.json"
STATE_PATH = Path.home() / ".cache/judgment_collector/resummary_state.json"
LOCK_PATH = Path.home() / ".cache/judgment_collector/resummary.pid"

# ── 參數 ──────────────────────────────────────────────────────────────
MIN_TEXT_LEN = 1000         # 全文太短跳過（之前 500 太小，短裁定浪費額度）
INTER_REQUEST_DELAY = 1.5   # NIM 請求間隔（秒；rate limit 較寬，但仍給點 buffer）
SUMMARY_TIMEOUT_SEC = 120
RESUMMARY_SESSION_ID = "weekend-resummary-batch"  # 獨立 session，避免跟 gateway 主 session 衝突
BATCH_NOTIFY_EVERY = 50
MAX_CONSECUTIVE_FAILS = 5   # 連續 N 次失敗就停止
BACKOFF_THRESHOLD = 3       # 連續 N 次失敗開始 backoff
BACKOFF_SECONDS = 60        # backoff 等待秒數

STRUCTURE_HEADERS = ["實務見解", "法院見解", "適用法條", "法院認為", "應解為"]

PROMPT_TEMPLATE = (
    "你是一位精確的法律助理。你的唯一任務是從一份判決書全文中，"
    "「逐字擷取」可供其他案件參考的「實務見解」或「法律原則」。\n\n"
    "案由：{case_reason}\n\n"
    "【嚴格規則】\n"
    "1. 【只要擷取】：找到判決書中「法院認為...」、「法院審酌...」、「按...」、"
    "「...應解為...」、「查...」等段落，找出最具法律原則價值的一到三個段落。\n"
    "2. 【逐字複製】：你「必須」逐字(verbatim)複製找到的段落。\n"
    "3. 【禁止】：嚴禁「摘要」、「改寫」、「精煉」或「加入你自己的文字」。"
    "你不是在寫摘要，你是在「複製」關鍵原文。\n"
    "4. 【禁止】：禁止使用「頁 1」、「頁 2」或「一、二、三」的編號格式。\n"
    "5. 【禁止】：禁止輸出案件概要、事實摘要、判決結果等敘述。只要法律見解原文。\n\n"
    "【高品質範例】\n"
    "根據最高法院109年度台上大字第3826號刑事大法庭裁定，毒品危害防制條例第20條第3項"
    "關於「3年後再犯」的定義，並不因施用毒品者於3年內是否有其他犯罪紀錄而受到影響。"
    "此裁定的立法真諦在於，鑑於施用毒品者具有「病患性犯人」的特質，應優先提供治療"
    "與戒癮協助。\n\n"
    "【格式化輸出】\n"
    "## 實務見解\n（從判決中逐字擷取的法院見解原文，一到三個關鍵段落）\n\n"
    "## 適用法條\n（列出本判決適用的法條）\n\n"
    "【注意事項】\n"
    "- 若判決中找不到有法律原則價值的見解（如純事實認定），回覆「本判決無可擷取之實務見解」\n"
    "- 若判決內文與案由明顯不符，回覆「案由不符，無法擷取」\n\n"
    "判決全文：\n{full_text}"
)

# ── 全域旗標 ──────────────────────────────────────────────────────────
_shutdown_requested = False


def _signal_handler(sig, frame):
    global _shutdown_requested
    _shutdown_requested = True
    logger.info("Shutdown requested (signal %d), finishing current item...", sig)


def _kill_child_processes():
    """清掉本進程的所有子進程，避免孤兒持有 session lock。"""
    try:
        import subprocess
        result = subprocess.run(
            ["pgrep", "-P", str(os.getpid())],
            capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.strip().splitlines():
            child_pid = int(line.strip())
            try:
                os.kill(child_pid, signal.SIGTERM)
                logger.info("Sent SIGTERM to child pid=%d", child_pid)
            except OSError:
                pass
        # 給子進程 3 秒優雅退出
        time.sleep(3)
        for line in result.stdout.strip().splitlines():
            child_pid = int(line.strip())
            try:
                os.kill(child_pid, signal.SIGKILL)
            except OSError:
                pass
    except Exception:
        pass


# ── PID Lock ──────────────────────────────────────────────────────────
def _acquire_lock() -> bool:
    """取得 PID lock，避免多進程同時跑。回傳 True 表示成功。"""
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    if LOCK_PATH.exists():
        try:
            old_pid = int(LOCK_PATH.read_text().strip())
            # 檢查舊進程是否還活著
            try:
                os.kill(old_pid, 0)
                logger.error("Another instance is running (pid=%d), exiting", old_pid)
                return False
            except OSError:
                # 舊進程已死，清理 stale lock
                logger.info("Cleaning stale lock (pid=%d)", old_pid)
                LOCK_PATH.unlink(missing_ok=True)
        except (ValueError, OSError):
            LOCK_PATH.unlink(missing_ok=True)

    LOCK_PATH.write_text(str(os.getpid()))
    return True


def _release_lock():
    """釋放 PID lock，同時清掉子進程。"""
    _kill_child_processes()
    try:
        if LOCK_PATH.exists():
            pid = int(LOCK_PATH.read_text().strip())
            if pid == os.getpid():
                LOCK_PATH.unlink(missing_ok=True)
    except Exception:
        pass


# ── State 管理 ─────────────────────────────────────────────────────────
def _load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text("utf-8"))
        except Exception:
            pass
    return {"nim_done": {}, "stats": {}}


def _save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), "utf-8")
    tmp.replace(STATE_PATH)


# ── 案由提取 ──────────────────────────────────────────────────────────
def _extract_case_reason_from_text(text: str) -> str:
    """從判決全文提取案由。"""
    m = re.search(r"案\s*由[：:]\s*(.{2,20})", text)
    if m:
        return m.group(1).strip()
    m = re.search(r"因(.{2,10})案件", text[:500])
    if m:
        return m.group(1).strip()
    return "裁判書"


def _load_judgments_reasons() -> dict:
    mapping = {}
    if JUDGMENTS_JSON.exists():
        try:
            data = json.loads(JUDGMENTS_JSON.read_text("utf-8"))
            for j in data:
                title = j.get("title", "")
                reason = j.get("case_reason", "")
                if title and reason:
                    mapping[title] = reason
        except Exception:
            pass
    return mapping


# ── NIM 呼叫 ─────────────────────────────────────────────────────────
def _nim_summarize(prompt: str) -> tuple:
    """呼叫 NVIDIA NIM 405B 摘要。

    Returns: (success, response_text, error_msg)
    """
    try:
        from skills.bridge.inference_gateway import InferenceGateway
    except Exception as e:
        return False, "", f"InferenceGateway import failed: {e}"
    try:
        gw = InferenceGateway()
        # heavy=True 強制走 NIM 405B（heavy_fast_path）；
        # require_pii_scrub 預設由 NVIDIA_NIM_REQUIRE_PII_SCRUB 控制（=1）
        r = gw.chat(
            prompt=prompt,
            heavy=True,
            timeout=SUMMARY_TIMEOUT_SEC,
            task_type="judgment_summary",
            session_id=RESUMMARY_SESSION_ID,
        )
        if not isinstance(r, dict):
            return False, "", f"unexpected return type: {type(r).__name__}"
        if not r.get("success"):
            return False, "", str(r.get("error") or "unknown")
        # 只接受走 NIM 的結果；走 oMLX fallback 不算數（因為 405B 才能取代 Codex）
        provider = str(r.get("provider") or "")
        if provider != "nvidia_nim":
            return False, "", f"wrong provider: {provider} (expected nvidia_nim)"
        text = str(r.get("response") or "").strip()
        if not text:
            return False, "", "empty response"
        return True, text, ""
    except Exception as e:
        return False, "", f"nim_summarize exception: {e}"


def _is_quality_summary(text: str) -> bool:
    if len(text) < 100:
        return False
    return sum(1 for h in STRUCTURE_HEADERS if h in text) >= 3


def _collect_for_distill(prompt: str, response: str, case_reason: str) -> None:
    try:
        from skills.bridge.distill_collector import collect_summary_pair
        collect_summary_pair(prompt, response, case_reason, "nim_resummary")
    except Exception:
        pass


def _notify_progress(message: str) -> None:
    try:
        from skills.ops.red_phone import send_telegram_push_with_status
        send_telegram_push_with_status(message)
    except Exception:
        pass


# ── 掃描 ──────────────────────────────────────────────────────────────
def scan_texts() -> list:
    entries = []
    if not NORM_ROOT.exists():
        return entries
    for txt_path in sorted(NORM_ROOT.glob("*/*.txt")):
        try:
            size = txt_path.stat().st_size
            if size < MIN_TEXT_LEN:
                continue
            entries.append({
                "path": txt_path,
                "date_dir": txt_path.parent.name,
                "slug": txt_path.stem,
                "size": size,
            })
        except OSError:
            continue
    return entries


# ── 主流程 ────────────────────────────────────────────────────────────
def main():
    global _shutdown_requested

    # 註冊信號處理
    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)

    parser = argparse.ArgumentParser(description="週末 NIM 批次重摘要")
    parser.add_argument("--all", action="store_true", help="強制重摘所有")
    parser.add_argument("--limit", type=int, default=0, help="限制筆數（0=不限）")
    parser.add_argument("--dry-run", action="store_true", help="模擬模式")
    parser.add_argument("--delay", type=float, default=INTER_REQUEST_DELAY, help="請求間隔秒數")
    args = parser.parse_args()

    # PID Lock
    if not _acquire_lock():
        return 1
    atexit.register(_release_lock)

    state = _load_state()
    # 向下相容：舊 state 使用 codex_done，遷移到 nim_done
    nim_done = state.get("nim_done") or state.get("codex_done", {})

    # NIM 日預算防呆
    NIM_DAILY_BUDGET = int(os.environ.get("NVIDIA_NIM_DAILY_BUDGET", "500"))
    RESUMMARY_BUDGET_CAP = int(os.environ.get("WEEKEND_RESUMMARY_BUDGET_CAP",
                                               str(max(50, NIM_DAILY_BUDGET - 200))))

    # RunAtLoad 守衛：非手動啟動時，只在有未完成工作時才繼續
    from datetime import datetime
    today = datetime.now().strftime("%Y-%m-%d")
    weekday = datetime.now().weekday()  # 0=Mon, 5=Sat, 6=Sun
    today_stats = state.get("stats", {}).get(today, {})
    has_pending_work = today_stats and today_stats.get("stopped_by") not in ("complete", None)

    if not (args.all or args.limit or args.dry_run):
        # 非手動模式：只在週六、週日、或有中斷未完成的工作時才跑
        if weekday not in (5, 6) and not has_pending_work:
            logger.info("Not weekend and no pending work, skipping (weekday=%d)", weekday)
            return 0
        if has_pending_work:
            logger.info("Resuming interrupted session from %s (stopped_by=%s)",
                        today, today_stats.get("stopped_by"))

    reason_map = _load_judgments_reasons()

    entries = scan_texts()
    logger.info("Found %d normalized texts (>= %d bytes)", len(entries), MIN_TEXT_LEN)

    if not args.all:
        entries = [e for e in entries if e["slug"] not in nim_done]
        logger.info("After filtering already done: %d remaining", len(entries))

    # 套用 budget cap
    effective_limit = args.limit if args.limit > 0 else RESUMMARY_BUDGET_CAP
    if len(entries) > effective_limit:
        logger.warning(
            "Clamping entries from %d to %d (budget_cap=%d)",
            len(entries), effective_limit, RESUMMARY_BUDGET_CAP,
        )
        entries = entries[:effective_limit]
    elif args.limit > 0:
        entries = entries[:args.limit]

    if not entries:
        logger.info("Nothing to do")
        return 0

    logger.info("Starting NIM re-summary of %d judgments (budget_cap=%d)",
                len(entries), RESUMMARY_BUDGET_CAP)
    if not args.dry_run:
        _notify_progress(f"週末 NIM 重摘要開始：{len(entries)} 筆")

    success_count = 0
    fail_count = 0
    distill_count = 0
    consecutive_fails = 0
    processed = 0
    start_time = time.time()

    for i, entry in enumerate(entries):
        # Graceful shutdown 檢查
        if _shutdown_requested:
            logger.info("Shutdown requested, stopping at %d/%d", i, len(entries))
            break

        txt_path = entry["path"]
        slug = entry["slug"]

        try:
            full_text = txt_path.read_text("utf-8", errors="replace")
        except Exception as e:
            logger.warning("Read failed %s: %s", txt_path, e)
            fail_count += 1
            continue

        if len(full_text) < MIN_TEXT_LEN:
            continue

        # 提取案由
        case_reason = ""
        for title, reason in reason_map.items():
            if slug in title.replace(" ", "").replace("　", ""):
                case_reason = reason
                break
        if not case_reason:
            case_reason = _extract_case_reason_from_text(full_text)

        # 截斷過長文本
        if len(full_text) > 15000:
            full_text = full_text[:15000]

        prompt = PROMPT_TEMPLATE.format(
            case_reason=case_reason,
            full_text=full_text,
        )

        if args.dry_run:
            logger.info("[DRY-RUN] %d/%d %s (reason=%s, len=%d) provider=nvidia_nim",
                        i + 1, len(entries), slug, case_reason, len(full_text))
            continue

        # 連續失敗 backoff
        if consecutive_fails >= BACKOFF_THRESHOLD:
            logger.info("Consecutive fails=%d, backing off %ds...", consecutive_fails, BACKOFF_SECONDS)
            deadline = time.time() + BACKOFF_SECONDS
            while time.time() < deadline and not _shutdown_requested:
                time.sleep(min(10, deadline - time.time()))

        if consecutive_fails >= MAX_CONSECUTIVE_FAILS:
            logger.error("Too many consecutive NIM failures (%d), stopping", consecutive_fails)
            # 呼叫 issue tracker 記錄連續失敗
            try:
                from skills.management.issue_tracker import log_issue
                log_issue(
                    command="weekend_resummary",
                    error_msg=f"NIM consecutive failures: {consecutive_fails}",
                    context=f"budget_remaining={RESUMMARY_BUDGET_CAP - processed}",
                    severity="High",
                    source="weekend_resummary",
                )
            except Exception:
                pass
            break

        # 呼叫 NIM
        ok, summary, error = _nim_summarize(prompt)

        if _shutdown_requested:
            break

        processed += 1

        if ok and _is_quality_summary(summary):
            success_count += 1
            consecutive_fails = 0
            nim_done[slug] = {
                "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "len": len(summary),
            }
            _collect_for_distill(prompt, summary, case_reason)
            distill_count += 1
            logger.info(
                "%d/%d OK: %s (reason=%s, summary=%d chars, provider=nvidia_nim)",
                i + 1, len(entries), slug, case_reason, len(summary),
            )
        else:
            fail_count += 1
            consecutive_fails += 1
            logger.warning(
                "%d/%d FAIL: %s — %s (consecutive=%d)",
                i + 1, len(entries), slug, error or "quality check failed",
                consecutive_fails,
            )

        # 定期儲存 state（每筆成功都存，避免丟失進度）
        if success_count > 0 and (success_count % 5 == 0 or (i + 1) % 10 == 0):
            state["nim_done"] = nim_done
            _save_state(state)

        # 進度通知
        if (i + 1) % BATCH_NOTIFY_EVERY == 0:
            elapsed = (time.time() - start_time) / 60
            _notify_progress(
                f"重摘要進度：{i + 1}/{len(entries)} "
                f"(成功 {success_count}, 失敗 {fail_count}, "
                f"蒸餾 {distill_count}, {elapsed:.0f}min)"
            )

        # 間隔
        time.sleep(args.delay)

    # 最終儲存
    state["nim_done"] = nim_done
    state["stats"][time.strftime("%Y-%m-%d")] = {
        "total": len(entries),
        "success": success_count,
        "fail": fail_count,
        "distill": distill_count,
        "stopped_by": "shutdown" if _shutdown_requested else (
            "max_fails" if consecutive_fails >= MAX_CONSECUTIVE_FAILS else "complete"
        ),
    }
    _save_state(state)

    elapsed_min = (time.time() - start_time) / 60
    report = (
        f"週末 NIM 重摘要{'中斷' if _shutdown_requested else '完成'}\n"
        f"處理: {i + 1}/{len(entries)} 筆\n"
        f"成功: {success_count}\n"
        f"失敗: {fail_count}\n"
        f"蒸餾收集: {distill_count}\n"
        f"耗時: {elapsed_min:.0f} 分鐘"
    )
    logger.info(report)
    if not args.dry_run:
        _notify_progress(report)

    return 0


if __name__ == "__main__":
    sys.exit(main())
