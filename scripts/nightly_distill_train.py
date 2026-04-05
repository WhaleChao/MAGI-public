#!/usr/bin/env python3
"""
nightly_distill_train.py — TAIDE 知識蒸餾夜間排程

每週日 04:00 執行：
1. 檢查門檻（>= 50 筆總資料 且 >= 20 筆新資料）
2. build_training_set()
3. 安全停止 oMLX + watchdog
4. LoRA 訓練 → 合併 → 驗證
5. 部署 or 回滾
6. 重啟 oMLX + watchdog
7. 部署後 inference 測試
8. red_phone 通知結果
9. 清理舊 adapters
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

# 確保 MAGI 可 import
MAGI_ROOT = Path(os.environ.get("MAGI_ROOT_DIR", str(Path.home() / "Desktop/MAGI")))
sys.path.insert(0, str(MAGI_ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(
            Path.home() / ".omlx/training/taide-distill/nightly_distill.log",
            encoding="utf-8",
        ),
    ],
)
logger = logging.getLogger("nightly_distill")

# ── 路徑 ──────────────────────────────────────────────────────────────
DISTILL_DIR = Path(os.environ.get(
    "TAIDE_DISTILL_DIR",
    str(Path.home() / ".omlx/training/taide-distill"),
))
BASE_MODEL = Path(os.environ.get(
    "TAIDE_BASE_MODEL",
    str(Path.home() / ".omlx/models/TAIDE-12b-Chat-mlx-4bit-textonly-backup"),
))
SYMLINK_PATH = Path.home() / ".omlx/models-text/TAIDE-12b-Chat-mlx-4bit"
ACTIVE_MODEL_PATH = DISTILL_DIR / "active_model.json"
STATE_PATH = DISTILL_DIR / "collector_state.json"

OMLX_LABEL = "com.magi.omlx"
WATCHDOG_LABEL = "com.magi.omlx-watchdog"
try:
    from api.routing.service_registry import get_service_url as _get_svc_url
    _omlx_default = _get_svc_url("omlx_inference")
except Exception:
    _omlx_default = "http://127.0.0.1:8080"
OMLX_URL = os.environ.get("OMLX_URL", _omlx_default)
LOCK_PATH = DISTILL_DIR / "nightly_distill.pid"
# Training lock — daemon + watchdog 看到此檔會跳過 oMLX 監控
TRAINING_LOCK_PATH = Path(os.environ.get(
    "MAGI_TRAINING_LOCK_PATH",
    str(MAGI_ROOT / "static" / "training.lock"),
))

# ── 門檻 ──────────────────────────────────────────────────────────────
MIN_TOTAL_PAIRS = 50
MIN_NEW_PAIRS = 20
HARD_TIMEOUT_SEC = 90 * 60  # 90 分鐘硬性超時
MAX_OLD_ADAPTERS = 4


# ── PID Lock ──────────────────────────────────────────────────────────
def _acquire_lock() -> bool:
    DISTILL_DIR.mkdir(parents=True, exist_ok=True)
    if LOCK_PATH.exists():
        try:
            old_pid = int(LOCK_PATH.read_text().strip())
            try:
                os.kill(old_pid, 0)
                logger.error("Another nightly_distill instance running (pid=%d), exiting", old_pid)
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

# ── 超時信號 ──────────────────────────────────────────────────────────
_start_time = time.time()


def _check_timeout(stage: str) -> None:
    elapsed = time.time() - _start_time
    if elapsed > HARD_TIMEOUT_SEC:
        raise TimeoutError(f"Hard timeout ({HARD_TIMEOUT_SEC}s) exceeded at stage: {stage}")


# ── LaunchAgent 控制 ──────────────────────────────────────────────────
def _launchd_target(label: str) -> str:
    uid = os.getuid()
    return f"gui/{uid}/{label}"


def _launchd_plist(label: str) -> str:
    return str(Path.home() / f"Library/LaunchAgents/{label}.plist")


def _write_training_lock():
    """寫入 training lock，通知 daemon + watchdog 暫停 oMLX 監控。"""
    try:
        TRAINING_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        TRAINING_LOCK_PATH.write_text(f"{os.getpid()}\n")
        logger.info("🔒 Training lock written: %s", TRAINING_LOCK_PATH)
    except Exception as e:
        logger.warning("Failed to write training lock: %s", e)


def _clear_training_lock():
    """清除 training lock。"""
    try:
        TRAINING_LOCK_PATH.unlink(missing_ok=True)
        logger.info("🔓 Training lock cleared")
    except Exception as e:
        logger.warning("Failed to clear training lock: %s", e)


def safe_stop_omlx() -> bool:
    """安全停止 oMLX 和 watchdog。"""
    _write_training_lock()  # 先通知 daemon/watchdog 不要干擾
    time.sleep(3)  # 等一個 watchdog 週期確認它看到 lock
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

    # 等 port 8080 釋放
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

    # 強制 kill
    logger.warning("Port 8080 still occupied, force killing...")
    subprocess.run(
        ["pkill", "-9", "-f", "omlx serve.*--port 8080"],
        capture_output=True, timeout=10,
    )
    time.sleep(3)
    return True


def safe_start_omlx() -> bool:
    """重啟 oMLX 和 watchdog。"""
    logger.info("Starting oMLX server...")
    try:
        subprocess.run(
            ["launchctl", "bootstrap", f"gui/{os.getuid()}", _launchd_plist(OMLX_LABEL)],
            capture_output=True, timeout=30,
        )
    except Exception as e:
        logger.warning("oMLX bootstrap: %s (may already be loaded)", e)

    # 等 oMLX 啟動
    deadline = time.time() + 120
    while time.time() < deadline:
        try:
            import urllib.request
            req = urllib.request.Request(f"{OMLX_URL}/v1/models", method="GET")
            with urllib.request.urlopen(req, timeout=5) as resp:
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

    _clear_training_lock()  # oMLX 已恢復，解除 lock
    return True


# ── Symlink 部署 ──────────────────────────────────────────────────────
def deploy_model(merged_path: Path, version: str) -> bool:
    """更新 symlink 指向合併後的模型。"""
    if not merged_path.exists():
        logger.error("Merged model not found: %s", merged_path)
        return False

    # 確認有 config.json
    if not (merged_path / "config.json").exists():
        logger.error("Merged model missing config.json")
        return False

    backup_target = SYMLINK_PATH.resolve() if SYMLINK_PATH.is_symlink() else None
    logger.info("Deploying %s → %s", SYMLINK_PATH, merged_path)

    try:
        if SYMLINK_PATH.is_symlink() or SYMLINK_PATH.exists():
            SYMLINK_PATH.unlink()
        SYMLINK_PATH.symlink_to(merged_path)
    except Exception as e:
        logger.error("Symlink update failed: %s", e)
        # 回滾
        if backup_target:
            try:
                SYMLINK_PATH.symlink_to(backup_target)
            except Exception:
                pass
        return False

    # 寫 active_model.json
    active = {
        "model_dir": str(merged_path),
        "version": version,
        "base_dir": str(BASE_MODEL),
        "deployed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    ACTIVE_MODEL_PATH.write_text(json.dumps(active, ensure_ascii=False, indent=2), "utf-8")
    logger.info("Deployed model: %s", version)
    return True


def rollback_model() -> bool:
    """回滾到 base model。"""
    logger.warning("Rolling back to base model: %s", BASE_MODEL)
    try:
        if SYMLINK_PATH.is_symlink() or SYMLINK_PATH.exists():
            SYMLINK_PATH.unlink()
        SYMLINK_PATH.symlink_to(BASE_MODEL)
        logger.info("Rollback complete")
        return True
    except Exception as e:
        logger.error("Rollback failed: %s", e)
        return False


# ── 部署後 Inference 測試 ─────────────────────────────────────────────
def post_deploy_test(num_tests: int = 3) -> bool:
    """重啟 oMLX 後，跑幾筆 HTTP inference 確認正常。"""
    import urllib.request

    test_prompts = [
        "請用一句話說明何謂損害賠償。",
        "刑法第339條的構成要件是什麼？",
        "何謂善意第三人？",
    ]

    passed = 0
    for i, prompt in enumerate(test_prompts[:num_tests]):
        try:
            payload = json.dumps({
                "model": os.environ.get("MAGI_TEXT_PRIMARY_MODEL", ""),
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 128,
                "temperature": 0.1,
            }).encode()

            req = urllib.request.Request(
                f"{OMLX_URL}/v1/chat/completions",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=60) as resp:
                body = json.loads(resp.read())
                text = body.get("choices", [{}])[0].get("message", {}).get("content", "")
                if len(text.strip()) > 10:
                    passed += 1
                    logger.info("  Post-deploy test %d/%d: OK (%d chars)", i + 1, num_tests, len(text))
                else:
                    logger.warning("  Post-deploy test %d/%d: too short (%d chars)", i + 1, num_tests, len(text))
        except Exception as e:
            logger.warning("  Post-deploy test %d/%d: %s", i + 1, num_tests, e)

    return passed >= 2  # 至少 2/3 通過


# ── 通知 ──────────────────────────────────────────────────────────────
def _notify(message: str) -> None:
    """透過 red_phone 發送通知。"""
    try:
        from skills.ops.red_phone import send_telegram_push_with_status
        send_telegram_push_with_status(message)
    except Exception as e:
        logger.warning("Notification failed: %s", e)


# ── 清理 ──────────────────────────────────────────────────────────────
def cleanup_old_adapters(keep: int = MAX_OLD_ADAPTERS) -> None:
    """保留最近 N 個 adapter，刪除舊的。"""
    adapters_dir = DISTILL_DIR / "adapters"
    if not adapters_dir.exists():
        return
    dirs = sorted(adapters_dir.glob("adapter_*"), key=lambda p: p.stat().st_mtime)
    if len(dirs) <= keep:
        return
    for d in dirs[:-keep]:
        logger.info("Cleaning up old adapter: %s", d.name)
        shutil.rmtree(d, ignore_errors=True)


def cleanup_old_merged(keep: int = 2) -> None:
    """保留最近 N 個合併模型，刪除舊的。"""
    merged_dir = DISTILL_DIR / "merged"
    if not merged_dir.exists():
        return
    # 不刪目前正在用的
    active_dir = None
    if ACTIVE_MODEL_PATH.exists():
        try:
            active = json.loads(ACTIVE_MODEL_PATH.read_text("utf-8"))
            active_dir = active.get("model_dir")
        except Exception:
            pass

    dirs = sorted(merged_dir.glob("TAIDE-*"), key=lambda p: p.stat().st_mtime)
    if len(dirs) <= keep:
        return
    for d in dirs[:-keep]:
        if active_dir and str(d) == active_dir:
            continue
        logger.info("Cleaning up old merged model: %s", d.name)
        shutil.rmtree(d, ignore_errors=True)


# ── 主流程 ────────────────────────────────────────────────────────────
def main() -> int:
    global _start_time
    _start_time = time.time()

    logger.info("=" * 60)
    logger.info("TAIDE 知識蒸餾夜間訓練開始")
    logger.info("=" * 60)

    DISTILL_DIR.mkdir(parents=True, exist_ok=True)

    # PID Lock — 避免多進程
    if not _acquire_lock():
        return 1
    import atexit
    atexit.register(_release_lock)
    atexit.register(_clear_training_lock)  # 異常退出時也清 training lock

    # ── 1. 檢查門檻 ──
    raw_path = DISTILL_DIR / "raw_pairs.jsonl"
    if not raw_path.exists():
        logger.info("No raw_pairs.jsonl yet, skipping")
        _notify("TAIDE 蒸餾：尚無訓練資料，跳過")
        return 0

    total_lines = sum(1 for l in open(raw_path) if l.strip())
    logger.info("Total raw pairs: %d (need >= %d)", total_lines, MIN_TOTAL_PAIRS)

    if total_lines < MIN_TOTAL_PAIRS:
        logger.info("Insufficient data (%d < %d), skipping", total_lines, MIN_TOTAL_PAIRS)
        _notify(f"TAIDE 蒸餾：資料不足 ({total_lines}/{MIN_TOTAL_PAIRS})，跳過")
        return 0

    # 檢查新資料數（自上次訓練以來）
    last_train_count = 0
    if ACTIVE_MODEL_PATH.exists():
        try:
            active = json.loads(ACTIVE_MODEL_PATH.read_text("utf-8"))
            # 從 metrics 找上次訓練的 pair 數
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
        _notify(f"TAIDE 蒸餾：新資料不足 ({new_pairs}/{MIN_NEW_PAIRS})，跳過")
        return 0

    # ── 2. 建立訓練集 ──
    _check_timeout("build_training_set")
    logger.info("Building training set...")
    from skills.bridge.distill_collector import build_training_set
    split = build_training_set()
    logger.info("Training set: %d train, %d eval", split["train"], split["eval"])

    if split["train"] < 10:
        logger.info("Too few training samples after filtering, skipping")
        _notify(f"TAIDE 蒸餾：過濾後訓練資料不足 ({split['train']})")
        return 0

    # ── 3. 停 oMLX ──
    _check_timeout("stop_omlx")
    logger.info("Stopping oMLX for training...")
    safe_stop_omlx()

    deployed = False
    version = None
    train_result = {}
    validate_result = {}

    try:
        # ── 4-6. 訓練 → 合併 → 驗證 ──
        venv_python = str(MAGI_ROOT / "venv/bin/python")
        train_script = str(MAGI_ROOT / "scripts/train_taide_lora.py")

        _check_timeout("train")
        logger.info("Running LoRA training...")
        r = subprocess.run(
            [venv_python, train_script, "--all"],
            capture_output=True, text=True,
            timeout=HARD_TIMEOUT_SEC - int(time.time() - _start_time),
            cwd=str(MAGI_ROOT),
            env={**os.environ, "PYTHONPATH": str(MAGI_ROOT)},
        )

        logger.info("Train script stdout:\n%s", r.stdout[-2000:] if r.stdout else "(empty)")
        if r.stderr:
            logger.info("Train script stderr:\n%s", r.stderr[-1000:])

        if r.returncode == 0:
            # 解析結果
            try:
                results = json.loads(r.stdout.strip().split("\n")[-1])
                train_result = results.get("train", {})
                validate_result = results.get("validate", {})
                version = train_result.get("version")
                merged_path = Path(results.get("merge", {}).get("merged_path", ""))
            except Exception as e:
                logger.error("Failed to parse train output: %s", e)
                version = None

            if version and merged_path.exists() and validate_result.get("validation_pass"):
                # ── 7. 部署 ──
                deployed = deploy_model(merged_path, version)
            else:
                logger.warning("Validation did not pass or no merged model")
        elif r.returncode == 2:
            logger.warning("Training completed but validation failed (exit 2)")
            try:
                results = json.loads(r.stdout.strip().split("\n")[-1])
                train_result = results.get("train", {})
                validate_result = results.get("validate", {})
                version = train_result.get("version")
            except Exception:
                pass
        else:
            logger.error("Training script failed (exit %d)", r.returncode)

    except TimeoutError as e:
        logger.error("Timeout: %s", e)
    except Exception as e:
        logger.error("Unexpected error: %s", e)
    finally:
        # ── 8. 重啟 oMLX（無論如何都要重啟）──
        if not deployed:
            rollback_model()

        logger.info("Restarting oMLX...")
        omlx_ok = safe_start_omlx()

        if not omlx_ok:
            logger.error("oMLX restart failed!")
            _notify("⚠️ TAIDE 蒸餾：oMLX 重啟失敗，請手動檢查！")

    # ── 9. 部署後測試 ──
    if deployed and omlx_ok:
        logger.info("Running post-deploy inference tests...")
        time.sleep(10)  # 等 oMLX 穩定
        if not post_deploy_test():
            logger.warning("Post-deploy test failed, rolling back...")
            safe_stop_omlx()
            rollback_model()
            safe_start_omlx()
            deployed = False

    # ── 10. 通知 ──
    elapsed_min = (time.time() - _start_time) / 60
    if version:
        report = (
            f"TAIDE 知識蒸餾報告 ({time.strftime('%Y-%m-%d')})\n"
            f"版本: {version}\n"
            f"資料: {split['train']} 組 (+{new_pairs} 新增)\n"
        )
        if train_result.get("train_loss") is not None:
            report += f"損失: {train_result['train_loss']:.3f}\n"
        if validate_result.get("rouge1_f1") is not None:
            report += f"ROUGE-1: {validate_result['rouge1_f1']:.3f}\n"
        if validate_result.get("pass_rate") is not None:
            report += f"驗證通過率: {validate_result['pass_rate']:.0%}\n"
        report += f"已部署: {'是' if deployed else '否'}\n"
        report += f"耗時: {elapsed_min:.0f} 分鐘"
        _notify(report)
    else:
        _notify(f"TAIDE 蒸餾：訓練失敗，耗時 {elapsed_min:.0f} 分鐘。oMLX 已{'恢復' if omlx_ok else '異常'}。")

    # ── 11. 清理 ──
    cleanup_old_adapters()
    cleanup_old_merged()

    logger.info("Nightly distill complete in %.0f min (deployed=%s)", elapsed_min, deployed)
    return 0 if deployed or not version else 1


if __name__ == "__main__":
    sys.exit(main())
