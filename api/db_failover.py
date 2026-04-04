"""law_firm_data DB 故障轉移模組 — 遠端不通自動切本機，回來後同步回寫。

使用方式：
    from api.db_failover import get_osc_host, get_osc_port, get_failover_status
    from api.db_failover import start_failover_monitor  # daemon 啟動時呼叫

邏輯：
    1. 每 PROBE_INTERVAL 秒檢查遠端 DB 可達性（TCP 3306）
    2. 遠端不通 → os.environ["OSC_DB_HOST"] 切到本機
    3. 遠端恢復 → 觸發 local→remote 同步 → 切回遠端
    4. 狀態透過 get_failover_status() 供 menubar 讀取
"""

from __future__ import annotations

import logging
import os
import socket
import subprocess
import threading
import time
from pathlib import Path

logger = logging.getLogger("magi.db_failover")

# ── 設定 ──────────────────────────────────────────────────────
_REMOTE_HOST = os.getenv("MAGI_REMOTE_DB_HOST", "100.121.61.74")
_REMOTE_PORT = int(os.getenv("MAGI_REMOTE_DB_PORT", "3306"))
_LOCAL_HOST = "127.0.0.1"
_LOCAL_PORT = 3306

PROBE_INTERVAL = int(os.getenv("MAGI_DB_FAILOVER_PROBE_SEC", "600"))  # 10 分鐘
_PROBE_TIMEOUT = 5  # TCP connect timeout

# ── 狀態 ──────────────────────────────────────────────────────
_lock = threading.Lock()
_remote_ok: bool | None = None       # None = 尚未檢查
_active_host: str = _REMOTE_HOST     # 目前生效的 host
_active_port: int = _REMOTE_PORT
_last_probe: float = 0.0
_last_switch: float = 0.0
_failover_active: bool = False       # True = 目前在用本機墊檔
_syncing: bool = False
_monitor_thread: threading.Thread | None = None


# ── 探測 ──────────────────────────────────────────────────────

def _tcp_check(host: str, port: int, timeout: int = _PROBE_TIMEOUT) -> bool:
    """TCP connect 檢查 DB 可達性。"""
    try:
        s = socket.create_connection((host, port), timeout=timeout)
        s.close()
        return True
    except (OSError, socket.timeout):
        return False


def probe_remote(force: bool = False) -> bool:
    """檢查遠端 DB 是否可達（帶快取）。"""
    global _remote_ok, _last_probe
    now = time.time()
    if not force and _remote_ok is not None and (now - _last_probe) < PROBE_INTERVAL:
        return _remote_ok

    ok = _tcp_check(_REMOTE_HOST, _REMOTE_PORT)
    with _lock:
        _remote_ok = ok
        _last_probe = now
    return ok


def _switch_to_local():
    """切換到本機 DB。"""
    global _active_host, _active_port, _failover_active, _last_switch
    with _lock:
        if _failover_active:
            return  # 已經在本機
        _active_host = _LOCAL_HOST
        _active_port = _LOCAL_PORT
        _failover_active = True
        _last_switch = time.time()

    # 更新 env，讓後續建立的連線自動走本機
    os.environ["OSC_DB_HOST"] = _LOCAL_HOST
    os.environ["OSC_DB_PORT"] = str(_LOCAL_PORT)
    logger.warning("⚠️ DB FAILOVER: 遠端 %s:%s 不可達，切換至本機 %s:%s",
                    _REMOTE_HOST, _REMOTE_PORT, _LOCAL_HOST, _LOCAL_PORT)


def _switch_to_remote():
    """切回遠端 DB。"""
    global _active_host, _active_port, _failover_active, _last_switch
    with _lock:
        if not _failover_active:
            return
        _active_host = _REMOTE_HOST
        _active_port = _REMOTE_PORT
        _failover_active = False
        _last_switch = time.time()

    os.environ["OSC_DB_HOST"] = _REMOTE_HOST
    os.environ["OSC_DB_PORT"] = str(_REMOTE_PORT)
    logger.info("✅ DB FAILOVER: 遠端 %s:%s 恢復，已切回遠端",
                _REMOTE_HOST, _REMOTE_PORT)


# ── 同步 ──────────────────────────────────────────────────────

def _sync_local_to_remote() -> bool:
    """本機 → 遠端同步（用 mysqldump + mysql push）。"""
    global _syncing
    if _syncing:
        return False
    _syncing = True

    db_user = os.getenv("OSC_DB_USER", os.getenv("MAGI_REMOTE_DB_USER", "casper_service"))
    db_pass = os.getenv("OSC_DB_PASSWORD", os.getenv("MAGI_REMOTE_DB_PASSWORD", ""))
    db_name = os.getenv("OSC_DB_NAME", "law_firm_data")
    dump_path = "/tmp/law_firm_data_local_to_remote.sql"

    try:
        logger.info("🔄 DB SYNC: 開始本機 → 遠端同步 (%s → %s)", _LOCAL_HOST, _REMOTE_HOST)

        # Step 1: Dump local
        dump_cmd = [
            "mysqldump",
            "-h", _LOCAL_HOST, "-P", str(_LOCAL_PORT),
            "-u", db_user, f"-p{db_pass}",
            "--single-transaction", "--skip-triggers", "--skip-routines",
            "--set-gtid-purged=OFF",
            db_name,
        ]
        with open(dump_path, "w") as f:
            r = subprocess.run(dump_cmd, stdout=f, stderr=subprocess.PIPE,
                               text=True, timeout=300)
        if r.returncode != 0:
            logger.error("DB SYNC: mysqldump 失敗: %s", r.stderr[:300])
            return False

        dump_size = os.path.getsize(dump_path)
        logger.info("DB SYNC: 本機 dump 完成 (%.1f MB)", dump_size / 1024 / 1024)

        # Step 2: Push to remote
        push_cmd = [
            "mysql",
            "-h", _REMOTE_HOST, "-P", str(_REMOTE_PORT),
            "-u", db_user, f"-p{db_pass}",
            db_name,
        ]
        with open(dump_path, "r") as f:
            r = subprocess.run(push_cmd, stdin=f, capture_output=True,
                               text=True, timeout=600)
        if r.returncode != 0:
            logger.error("DB SYNC: push 到遠端失敗: %s", r.stderr[:300])
            return False

        logger.info("✅ DB SYNC: 本機 → 遠端同步完成 (%.1f MB)", dump_size / 1024 / 1024)
        return True

    except subprocess.TimeoutExpired:
        logger.error("DB SYNC: 同步逾時")
        return False
    except Exception as e:
        logger.error("DB SYNC: 同步異常: %s", e)
        return False
    finally:
        _syncing = False
        try:
            os.unlink(dump_path)
        except OSError:
            pass


# ── 監控迴圈 ──────────────────────────────────────────────────

def _monitor_loop():
    """背景監控：每 PROBE_INTERVAL 秒檢查遠端，自動切換 + 同步。"""
    # 啟動時立即檢查
    _do_check()
    while True:
        time.sleep(PROBE_INTERVAL)
        _do_check()


def _do_check():
    """單次檢查 + 切換邏輯。"""
    ok = probe_remote(force=True)
    if ok and _failover_active:
        # 遠端回來了 → 先同步再切回
        logger.info("🔄 遠端 DB 恢復，開始同步本機資料...")
        sync_ok = _sync_local_to_remote()
        if sync_ok:
            _switch_to_remote()
        else:
            logger.warning("⚠️ 同步失敗，繼續使用本機 DB，下次再試")
    elif not ok and not _failover_active:
        _switch_to_local()
    elif ok and not _failover_active:
        # 一切正常
        pass
    elif not ok and _failover_active:
        # 仍在本機模式
        logger.debug("遠端仍不可達，繼續使用本機 DB")


# ── 公開 API ──────────────────────────────────────────────────

def get_osc_host() -> str:
    """取得目前應該使用的 OSC DB host。"""
    return _active_host


def get_osc_port() -> int:
    """取得目前應該使用的 OSC DB port。"""
    return _active_port


def get_failover_status() -> dict:
    """取得 failover 狀態（供 menubar 等外部使用）。"""
    return {
        "remote_host": _REMOTE_HOST,
        "remote_port": _REMOTE_PORT,
        "remote_ok": _remote_ok,
        "active_host": _active_host,
        "active_port": _active_port,
        "failover_active": _failover_active,
        "syncing": _syncing,
        "last_probe": _last_probe,
        "last_switch": _last_switch,
    }


def start_failover_monitor() -> None:
    """啟動背景監控執行緒（只會啟動一次）。"""
    global _monitor_thread
    if _monitor_thread is not None and _monitor_thread.is_alive():
        return

    _monitor_thread = threading.Thread(
        target=_monitor_loop,
        daemon=True,
        name="db-failover-monitor",
    )
    _monitor_thread.start()
    logger.info("DB Failover Monitor 啟動（每 %d 秒檢查）", PROBE_INTERVAL)
