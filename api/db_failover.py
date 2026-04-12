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
try:
    from api.routing.node_registry import get_node_ip as _get_node_ip
    _REMOTE_HOST = os.getenv("MAGI_REMOTE_DB_HOST") or _get_node_ip("nas") or "100.121.61.74"
except Exception:
    _REMOTE_HOST = os.getenv("MAGI_REMOTE_DB_HOST", "100.121.61.74")
_REMOTE_PORT = int(os.getenv("MAGI_REMOTE_DB_PORT", "3306"))
_LOCAL_HOST = "127.0.0.1"
_LOCAL_PORT = 3306

PROBE_INTERVAL = int(os.getenv("MAGI_DB_FAILOVER_PROBE_SEC", "600"))  # 10 分鐘
_PROBE_TIMEOUT = 5  # TCP connect timeout

# ── 狀態 ──────────────────────────────────────────────────────
_lock = threading.Lock()
_remote_ok: Optional[bool] = None       # None = 尚未檢查
_active_host: str = _REMOTE_HOST     # 目前生效的 host
_active_port: int = _REMOTE_PORT
_last_probe: float = 0.0
_last_switch: float = 0.0
_failover_active: bool = False       # True = 目前在用本機墊檔
_syncing: bool = False
_monitor_thread: threading.Optional[Thread] = None


# ── 探測 ──────────────────────────────────────────────────────

def _tcp_check(host: str, port: int, timeout: int = _PROBE_TIMEOUT) -> bool:
    """TCP connect + MySQL handshake 檢查 DB 可達性。

    僅 TCP 連通不代表 MariaDB 可用（例如 max_connections 已滿、認證問題）。
    先做 TCP 檢查，成功後嘗試一個輕量 MySQL 連線以驗證真正可用。
    """
    try:
        s = socket.create_connection((host, port), timeout=timeout)
        s.close()
    except (OSError, socket.timeout):
        return False

    # TCP 通了，再做真正的 MySQL handshake 驗證
    try:
        import mysql.connector
        db_user = os.getenv("OSC_DB_USER", os.getenv("MAGI_REMOTE_DB_USER", "casper_service"))
        db_pass = os.getenv("OSC_DB_PASSWORD", os.getenv("MAGI_REMOTE_DB_PASSWORD", ""))
        conn = mysql.connector.connect(
            host=host, port=port,
            user=db_user, password=db_pass,
            connection_timeout=timeout,
        )
        conn.close()
        return True
    except Exception as exc:
        logger.debug("TCP ok but MySQL handshake failed for %s:%s: %s", host, port, exc)
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
    """Bidirectional sync using table-aware merge instead of dump-and-restore."""
    global _syncing
    if _syncing:
        return False
    _syncing = True

    import json
    import mysql.connector
    from api.db_sync import sync_bidirectional

    db_user = os.getenv("OSC_DB_USER", os.getenv("MAGI_REMOTE_DB_USER", "casper_service"))
    db_pass = os.getenv("OSC_DB_PASSWORD", os.getenv("MAGI_REMOTE_DB_PASSWORD", ""))
    db_name = os.getenv("OSC_DB_NAME", "law_firm_data")

    # Pre-sync backup (safety net): dump remote before merge
    backup_path = "/tmp/law_firm_data_pre_sync_backup.sql"
    try:
        dump_cmd = [
            "mysqldump",
            "-h", _REMOTE_HOST, "-P", str(_REMOTE_PORT),
            "-u", db_user, f"-p{db_pass}",
            "--single-transaction", "--skip-triggers", "--skip-routines",
            "--set-gtid-purged=OFF",
            db_name,
        ]
        with open(backup_path, "w") as f:
            r = subprocess.run(dump_cmd, stdout=f, stderr=subprocess.PIPE,
                               text=True, timeout=300)
        if r.returncode == 0:
            sz = os.path.getsize(backup_path)
            logger.info("DB SYNC: pre-sync remote backup saved (%.1f MB)", sz / 1024 / 1024)
        else:
            logger.warning("DB SYNC: pre-sync backup failed: %s (continuing anyway)",
                           r.stderr[:300])
    except Exception as e:
        logger.warning("DB SYNC: pre-sync backup failed: %s (continuing anyway)", e)

    local_conn = None
    remote_conn = None
    try:
        logger.info("DB SYNC: starting bidirectional sync (%s <-> %s)",
                     _LOCAL_HOST, _REMOTE_HOST)

        local_conn = mysql.connector.connect(
            host=_LOCAL_HOST, port=_LOCAL_PORT,
            user=db_user, password=db_pass,
            database=db_name,
        )
        remote_conn = mysql.connector.connect(
            host=_REMOTE_HOST, port=_REMOTE_PORT,
            user=db_user, password=db_pass,
            database=db_name,
        )

        report = sync_bidirectional(local_conn, remote_conn, database=db_name)

        # Save report
        report_path = os.path.join(os.path.dirname(__file__), "..", ".agent",
                                   "db_sync_report.json")
        os.makedirs(os.path.dirname(report_path), exist_ok=True)
        with open(report_path, "w") as f:
            json.dump(report, f, indent=2, ensure_ascii=False, default=str)

        if report["errors"]:
            logger.warning("DB SYNC: completed with %d errors: %s",
                           len(report["errors"]), report["errors"][:3])
            return False
        else:
            logger.info("DB SYNC: completed successfully: %s",
                        {t: r.get("status") for t, r in report["tables"].items()})
            return True

    except Exception as e:
        logger.error("DB SYNC: sync failed: %s", e)
        return False
    finally:
        _syncing = False
        if local_conn:
            try:
                local_conn.close()
            except Exception:
                pass
        if remote_conn:
            try:
                remote_conn.close()
            except Exception:
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
