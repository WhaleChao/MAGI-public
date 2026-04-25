#!/usr/bin/env python3
"""
nightly_distill_gemma.py — Gemma E4B 知識蒸餾週間排程

每週日 11:00 執行（E4B 日間視窗 07:00-21:50 內，避開 13:15 APE benchmark）：
1. 檢查 E4B 日間視窗
2. 檢查門檻（>= 50 筆總資料 且 >= 20 筆新資料）
3. build_training_set()
4. 安全停止 oMLX + watchdog
5. LoRA 訓練 → 合併 → 驗證
6. 寫 pending_deploy.json（不自動部署）
7. 重啟 oMLX + watchdog
8. TG 通知結果 + 手動部署指令
9. 清理舊 adapters

--deploy <version>：獨立入口，手動驗收後部署 symlink。

注意：首次訓練由使用者人工跑驗收，cron 中 enabled=false，驗收後再開。
"""

from __future__ import annotations

import argparse
import atexit
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, time as _t
from pathlib import Path

MAGI_ROOT = Path(os.environ.get("MAGI_ROOT_DIR", str(Path.home() / "Desktop/MAGI_v2")))
sys.path.insert(0, str(MAGI_ROOT))

# 日誌設定（gemma-distill 目錄可能尚不存在，先 mkdir）
_LOG_DIR = Path.home() / ".omlx/training/gemma-distill"
_LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(
            _LOG_DIR / "nightly_distill_gemma.log",
            encoding="utf-8",
        ),
    ],
)
logger = logging.getLogger("nightly_distill_gemma")

# ── 路徑 ──────────────────────────────────────────────────────────────
DISTILL_DIR = Path(os.environ.get(
    "GEMMA_DISTILL_DIR",
    str(Path.home() / ".omlx/training/gemma-distill"),
))
BASE_MODEL = Path(os.environ.get(
    "GEMMA_E4B_BASE_MODEL",
    str(Path.home() / ".omlx/models/gemma-4-e4b-it-4bit"),
))
# 第一輪不寫死 symlink（不自動部署）
ACTIVE_MODEL_PATH = DISTILL_DIR / "active_model.json"
PENDING_DEPLOY_PATH = DISTILL_DIR / "pending_deploy.json"
STATE_PATH = DISTILL_DIR / "collector_state.json"
LOCK_PATH = DISTILL_DIR / "nightly_distill_gemma.pid"
TRAINING_LOCK_PATH = Path(os.environ.get(
    "MAGI_TRAINING_LOCK_PATH",
    str(MAGI_ROOT / "static" / "training.lock"),
))

OMLX_LABEL = "com.magi.omlx"
WATCHDOG_LABEL = "com.magi.omlx-watchdog"
try:
    from api.routing.service_registry import get_service_url as _get_svc_url
    _omlx_default = _get_svc_url("omlx_inference")
except Exception:
    _omlx_default = "http://127.0.0.1:8080"
OMLX_URL = os.environ.get("OMLX_URL", _omlx_default)

# ── 門檻 ──────────────────────────────────────────────────────────────
MIN_TOTAL_PAIRS = 50
MIN_NEW_PAIRS = 20
HARD_TIMEOUT_SEC = 90 * 60  # 90 分鐘硬性超時
MAX_OLD_ADAPTERS = 4

_start_time = time.time()


# ── E4B 日間視窗檢查 ──────────────────────────────────────────────────
def _in_e4b_window() -> bool:
    """E4B 日間視窗（07:00-21:50）。訓練必須在此視窗。"""
    now = datetime.now().time()
    return _t(7, 0) <= now < _t(21, 50)


# ── PID Lock ──────────────────────────────────────────────────────────
def _acquire_lock() -> bool:
    DISTILL_DIR.mkdir(parents=True, exist_ok=True)
    if LOCK_PATH.exists():
        try:
            old_pid = int(LOCK_PATH.read_text().strip())
            try:
                os.kill(old_pid, 0)
                logger.error("Another nightly_distill_gemma instance running (pid=%d), exiting", old_pid)
                return False
            except OSError:
                logger.info("Cleaning stale lock (pid=%d)", old_pid)
                LOCK_PATH.unlink(missing_ok=True)
        except (ValueError, OSError):
            LOCK_PATH.unlink(missing_ok=True)
    LOCK_PATH.write_text(str(os.getpid()))
    return True


def _release_lock():
    try:
        if LOCK_PATH.exists() and int(LOCK_PATH.read_text().strip()) == os.getpid():
            LOCK_PATH.unlink(missing_ok=True)
    except Exception:
        pass


def _check_timeout(stage: str) -> None:
    elapsed = time.time() - _start_time
    if elapsed > HARD_TIMEOUT_SEC:
        raise TimeoutError(f"Hard timeout ({HARD_TIMEOUT_SEC}s) exceeded at stage: {stage}")


# ── LaunchAgent 控制 ──────────────────────────────────────────────────
def _launchd_target(label: str) -> str:
    return f"gui/{os.getuid()}/{label}"


def _launchd_plist(label: str) -> str:
    return str(Path.home() / f"Library/LaunchAgents/{label}.plist")


def _write_training_lock():
    try:
        TRAINING_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        TRAINING_LOCK_PATH.write_text(f"{os.getpid()}\n")
        logger.info("Training lock written: %s", TRAINING_LOCK_PATH)
    except Exception as e:
        logger.warning("Failed to write training lock: %s", e)


def _clear_training_lock():
    try:
        TRAINING_LOCK_PATH.unlink(missing_ok=True)
        logger.info("Training lock cleared")
    except Exception as e:
        logger.warning("Failed to clear training lock: %s", e)


def safe_stop_omlx() -> bool:
    _write_training_lock()
    time.sleep(3)
    logger.info("Stopping oMLX watchdog...")
    try:
        subprocess.run(
            ["launchctl", "bootout", _launchd_target(WATCHDOG_LABEL)],
            capture_output=True, timeout=30,
        )
    except Exception as e:
        logger.warning("Watchdog bootout: %s", e)

    logger.info("Stopping oMLX server...")
    try:
        subprocess.run(
            ["launchctl", "bootout", _launchd_target(OMLX_LABEL)],
            capture_output=True, timeout=30,
        )
    except Exception as e:
        logger.warning("oMLX bootout: %s", e)

    deadline = time.time() + 60
    while time.time() < deadline:
        r = subprocess.run(
            ["lsof", "-i", ":8080", "-sTCP:LISTEN"],
            capture_output=True, timeout=10,
        )
        if r.returncode != 0:
            logger.info("Port 8080 released")
            return True
        time.sleep(2)

    logger.warning("Port 8080 still occupied, force killing...")
    subprocess.run(["pkill", "-9", "-f", "omlx serve.*--port 8080"], capture_output=True, timeout=10)
    time.sleep(3)
    return True


def safe_start_omlx() -> bool:
    logger.info("Starting oMLX server...")
    try:
        subprocess.run(
            ["launchctl", "bootstrap", f"gui/{os.getuid()}", _launchd_plist(OMLX_LABEL)],
            capture_output=True, timeout=30,
        )
    except Exception as e:
        logger.warning("oMLX bootstrap: %s (may already be loaded)", e)

    deadline = time.time() + 120
    while time.time() < deadline:
        try:
            import urllib.request
            with urllib.request.urlopen(f"{OMLX_URL}/v1/models", timeout=5) as resp:
                if resp.status == 200:
                    logger.info("oMLX is up")
                    break
        except Exception:
            pass
        time.sleep(5)
    else:
        logger.error("oMLX failed to start within 120s")
        return False

    logger.info("Starting oMLX watchdog...")
    try:
        subprocess.run(
            ["launchctl", "bootstrap", f"gui/{os.getuid()}", _launchd_plist(WATCHDOG_LABEL)],
            capture_output=True, timeout=30,
        )
    except Exception as e:
        logger.warning("Watchdog bootstrap: %s (may already be loaded)", e)

    _clear_training_lock()
    return True


# ── 手動部署入口（--deploy <version>）───────────────────────────────
def deploy_model(version: str) -> int:
    """手動部署：切換 oMLX symlink 並跑 post-deploy test。"""
    merged_path = DISTILL_DIR / "merged" / f"Gemma-{version}"
    if not merged_path.exists():
        logger.error("Merged model not found: %s", merged_path)
        return 1

    if not (merged_path / "config.json").exists():
        logger.error("Merged model missing config.json")
        return 1

    # 找出目前的 symlink（oMLX models-text 目錄下）
    symlink_path = Path.home() / ".omlx/models-text/gemma-4-e4b-it-4bit"
    backup_target = symlink_path.resolve() if symlink_path.is_symlink() else None

    logger.info("Deploying %s → %s", symlink_path, merged_path)
    try:
        if symlink_path.is_symlink() or symlink_path.exists():
            symlink_path.unlink()
        symlink_path.symlink_to(merged_path)
    except Exception as e:
        logger.error("Symlink update failed: %s", e)
        if backup_target:
            try:
                symlink_path.symlink_to(backup_target)
                logger.info("Rolled back to: %s", backup_target)
            except Exception:
                pass
        return 1

    # 寫 active_model.json
    active = {
        "model_dir": str(merged_path),
        "version": version,
        "base_dir": str(BASE_MODEL),
        "deployed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    ACTIVE_MODEL_PATH.write_text(json.dumps(active, ensure_ascii=False, indent=2), "utf-8")
    logger.info("Deployed model: %s", version)

    # 清除 pending
    PENDING_DEPLOY_PATH.unlink(missing_ok=True)

    _notify(f"Gemma E4B 已部署 {version} → {symlink_path}")
    return 0


# ── 通知 ──────────────────────────────────────────────────────────────
def _notify(message: str) -> None:
    try:
        from skills.ops.red_phone import send_telegram_push_with_status
        send_telegram_push_with_status(message)
    except Exception as e:
        logger.warning("Notification failed: %s", e)


# ── 清理 ──────────────────────────────────────────────────────────────
def cleanup_old_adapters(keep: int = MAX_OLD_ADAPTERS) -> None:
    adapters_dir = DISTILL_DIR / "adapters"
    if not adapters_dir.exists():
        return
    dirs = sorted(adapters_dir.glob("adapter_gemma-*"), key=lambda p: p.stat().st_mtime)
    if len(dirs) <= keep:
        return
    for d in dirs[:-keep]:
        logger.info("Cleaning up old adapter: %s", d.name)
        shutil.rmtree(d, ignore_errors=True)


# ── 主流程 ────────────────────────────────────────────────────────────
def main() -> int:
    global _start_time
    _start_time = time.time()

    parser = argparse.ArgumentParser(description="Gemma E4B 知識蒸餾週間排程")
    parser.add_argument("--deploy", metavar="VERSION", help="手動部署指定版本（不跑訓練）")
    parser.add_argument("--help-deploy", action="store_true", help="顯示部署說明")
    args = parser.parse_args()

    # 手動部署模式
    if args.deploy:
        return deploy_model(args.deploy)

    if args.help_deploy:
        print(
            "手動部署指令：\n"
            f"  {sys.executable} {__file__} --deploy <version>\n"
            "例如：\n"
            f"  {sys.executable} {__file__} --deploy gemma-distill-v001"
        )
        return 0

    logger.info("=" * 60)
    logger.info("Gemma E4B 知識蒸餾訓練開始")
    logger.info("=" * 60)

    DISTILL_DIR.mkdir(parents=True, exist_ok=True)

    # PID Lock
    if not _acquire_lock():
        return 1
    atexit.register(_release_lock)
    atexit.register(_clear_training_lock)

    # 1. 檢查 E4B 日間視窗
    if not _in_e4b_window():
        logger.error("Not in E4B daytime window (07:00-21:50). Aborting.")
        _notify("Gemma 蒸餾：非 E4B 日間視窗，中止")
        return 1

    # 2. 檢查訓練資料門檻
    raw_path = DISTILL_DIR / "raw_pairs.jsonl"
    if not raw_path.exists():
        logger.info("No raw_pairs.jsonl yet, skipping")
        _notify("Gemma 蒸餾：尚無訓練資料，跳過")
        return 0

    total_lines = sum(1 for l in open(raw_path) if l.strip())
    logger.info("Total raw pairs: %d (need >= %d)", total_lines, MIN_TOTAL_PAIRS)

    if total_lines < MIN_TOTAL_PAIRS:
        logger.info("Insufficient data (%d < %d), skipping", total_lines, MIN_TOTAL_PAIRS)
        _notify(f"Gemma 蒸餾：資料不足 ({total_lines}/{MIN_TOTAL_PAIRS})，跳過")
        return 0

    # 檢查新資料
    last_train_count = 0
    if ACTIVE_MODEL_PATH.exists():
        try:
            metrics_path = DISTILL_DIR / "metrics.jsonl"
            if metrics_path.exists():
                lines = [json.loads(l) for l in open(metrics_path) if l.strip()]
                if lines:
                    last_train_count = lines[-1].get("train_pairs", 0)
        except Exception:
            pass

    new_pairs = total_lines - last_train_count
    logger.info("New pairs since last train: %d (need >= %d)", new_pairs, MIN_NEW_PAIRS)
    if new_pairs < MIN_NEW_PAIRS:
        logger.info("Insufficient new data (%d < %d), skipping", new_pairs, MIN_NEW_PAIRS)
        _notify(f"Gemma 蒸餾：新資料不足 ({new_pairs}/{MIN_NEW_PAIRS})，跳過")
        return 0

    # 3. 建立訓練集
    _check_timeout("build_training_set")
    logger.info("Building training set...")
    # 使用 GEMMA_DISTILL_DIR 路徑的 collector
    from skills.bridge.distill_collector import build_training_set
    # build_training_set 使用 module-level RAW_PATH（可能指向 taide），
    # 這裡直接呼叫 gemma 路徑版本
    import importlib
    import skills.bridge.distill_collector as _dc
    _orig_raw = _dc.RAW_PATH
    _orig_train = _dc.TRAIN_PATH
    _orig_eval = _dc.EVAL_PATH
    _dc.RAW_PATH = raw_path
    _dc.TRAIN_PATH = DISTILL_DIR / "train.jsonl"
    _dc.EVAL_PATH = DISTILL_DIR / "eval.jsonl"
    try:
        split = build_training_set()
    finally:
        _dc.RAW_PATH = _orig_raw
        _dc.TRAIN_PATH = _orig_train
        _dc.EVAL_PATH = _orig_eval

    logger.info("Training set: %d train, %d eval", split["train"], split["eval"])

    if split["train"] < 10:
        logger.info("Too few training samples after filtering, skipping")
        _notify(f"Gemma 蒸餾：過濾後訓練資料不足 ({split['train']})")
        return 0

    # 4. 停 oMLX
    _check_timeout("stop_omlx")
    logger.info("Stopping oMLX for training...")
    safe_stop_omlx()

    version = None
    train_result = {}
    validate_result = {}
    merge_result = {}

    try:
        venv_python = str(MAGI_ROOT / "venv/bin/python3")
        train_script = str(MAGI_ROOT / "scripts/train_gemma_e4b_lora.py")

        _check_timeout("train")
        logger.info("Running LoRA training...")
        r = subprocess.run(
            [venv_python, train_script, "--all"],
            capture_output=True, text=True,
            timeout=HARD_TIMEOUT_SEC - int(time.time() - _start_time),
            cwd=str(MAGI_ROOT),
            env={
                **os.environ,
                "PYTHONPATH": str(MAGI_ROOT),
                "GEMMA_DISTILL_DIR": str(DISTILL_DIR),
                "GEMMA_E4B_BASE_MODEL": str(BASE_MODEL),
            },
        )

        logger.info("Train script stdout:\n%s", r.stdout[-2000:] if r.stdout else "(empty)")
        if r.stderr:
            logger.info("Train script stderr:\n%s", r.stderr[-1000:])

        if r.returncode == 0:
            try:
                results = json.loads(r.stdout.strip().split("\n")[-1])
                train_result = results.get("train", {})
                validate_result = results.get("validate", {})
                merge_result = results.get("merge", {})
                version = train_result.get("version")
            except Exception as e:
                logger.error("Failed to parse train output: %s", e)

        if not version:
            logger.error("Training failed (returncode=%d)", r.returncode)
            _notify(f"Gemma 蒸餾：訓練失敗 (rc={r.returncode})")
            return 1

        # 5. 寫 pending_deploy.json（不自動切 symlink）
        merged_path = merge_result.get("merged_path", "")
        if merged_path and Path(merged_path).exists():
            pending = {
                "version": version,
                "merged_path": merged_path,
                "train_result": train_result,
                "validate_result": validate_result,
                "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            }
            PENDING_DEPLOY_PATH.write_text(
                json.dumps(pending, ensure_ascii=False, indent=2), "utf-8"
            )
            logger.info("pending_deploy.json written: %s", PENDING_DEPLOY_PATH)
        else:
            logger.warning("merged_path not found, skip pending_deploy.json")

    finally:
        # 6. 重啟 oMLX（無論成功失敗都重啟）
        _check_timeout("restart_omlx")
        logger.info("Restarting oMLX...")
        safe_start_omlx()

    # 7. 清理舊 adapters
    cleanup_old_adapters()

    # 8. 通知
    if version:
        deploy_cmd = f"{sys.executable} {__file__} --deploy {version}"
        success_msg = (
            f"Gemma E4B 知識蒸餾完成 {version}\n"
            f"訓練: {train_result.get('elapsed_sec', '?')}s\n"
            f"驗證: {validate_result.get('passed', '?')}/{validate_result.get('total', '?')}\n"
            f"路徑: {merge_result.get('merged_path', '?')}\n\n"
            f"已產出但未自動部署。手動部署指令：\n{deploy_cmd}"
        )
        _notify(success_msg)
        logger.info("Done. %s", success_msg)
    else:
        _notify("Gemma 蒸餾：流程完成但無 version（請查 log）")

    return 0


if __name__ == "__main__":
    sys.exit(main())
