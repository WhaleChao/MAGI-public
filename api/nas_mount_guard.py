"""NAS SMB 自動掛載守衛 — 確保 NAS share 始終可用，斷線自動重連。

用法：
    from api.nas_mount_guard import ensure_nas_mounts, start_nas_mount_guard

    # 一次性檢查 + 掛載
    ensure_nas_mounts()

    # 背景守衛（每 interval 秒巡檢一次）
    start_nas_mount_guard(interval=120)
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import threading
import time

logger = logging.getLogger("magi.nas_mount_guard")

# ── NAS 連線設定 ─────────────────────────────────────────────
_NAS_LAN_HOST = os.getenv("MAGI_NAS_HOST", "192.168.1.3")
_NAS_TS_HOST = os.getenv("MAGI_NAS_TAILSCALE_HOST", "100.111.10.126")
NAS_USER = os.getenv("MAGI_NAS_USER", "lumi63181107")

# 動態解析結果快取（host, expiry_time）
_resolved_host: str | None = None
_resolved_expiry: float = 0.0
_RESOLVE_TTL = 120  # 快取 120 秒


def _ping_ok(host: str, timeout: int = 2) -> bool:
    """檢查主機是否可達：優先用 TCP 445 (SMB)，fallback 到 ICMP ping。
    Synology NAS 可能擋 ICMP，所以 TCP port check 更可靠。"""
    import socket
    try:
        sock = socket.create_connection((host, 445), timeout=timeout)
        sock.close()
        return True
    except (OSError, socket.timeout):
        pass
    # fallback: ICMP ping
    try:
        r = subprocess.run(
            ["ping", "-c", "1", "-W", str(timeout), host],
            capture_output=True, timeout=timeout + 2,
        )
        return r.returncode == 0
    except Exception:
        return False


def resolve_nas_host() -> str:
    """動態解析 NAS IP：LAN 優先，不通走 Tailscale。結果快取 120 秒。"""
    global _resolved_host, _resolved_expiry, NAS_HOST

    now = time.time()
    if _resolved_host and now < _resolved_expiry:
        return _resolved_host

    # 強制離線模式
    if os.getenv("MAGI_FORCE_NAS_OFFLINE"):
        _resolved_host = _NAS_LAN_HOST
        _resolved_expiry = now + _RESOLVE_TTL
        NAS_HOST = _resolved_host
        return _resolved_host

    # 優先嘗試 LAN（2s timeout），不通走 Tailscale（3s timeout, relay 延遲較高）
    if _ping_ok(_NAS_LAN_HOST, timeout=2):
        chosen = _NAS_LAN_HOST
    elif _ping_ok(_NAS_TS_HOST, timeout=3):
        chosen = _NAS_TS_HOST
        logger.info("NAS LAN %s 不可達，切換 Tailscale %s", _NAS_LAN_HOST, _NAS_TS_HOST)
    else:
        chosen = _NAS_LAN_HOST  # 兩個都不通，保持預設讓後續報錯
        logger.warning("NAS LAN %s 和 Tailscale %s 皆不可達", _NAS_LAN_HOST, _NAS_TS_HOST)

    _resolved_host = chosen
    _resolved_expiry = now + _RESOLVE_TTL
    NAS_HOST = chosen
    return chosen


# 初始化時立即解析
NAS_HOST = _NAS_LAN_HOST  # 先設預設，啟動時由 resolve_nas_host() 覆蓋

# (share_name, expected_volume_path)
_SHARES: list[tuple[str, str]] = [
    ("homes", "/Volumes/homes"),
    ("lumi",  "/Volumes/lumi"),
]

# ── 掛載邏輯 ─────────────────────────────────────────────────

def _is_mounted(volume_path: str) -> bool:
    """檢查 volume 是否已掛載且可存取（含 macOS automount 後綴 -1）。"""
    for path in (volume_path, f"{volume_path}-1"):
        if os.path.ismount(path):
            try:
                os.listdir(path)
                return True
            except OSError:
                continue
    return False


def _is_correct_host(volume_path: str) -> bool:
    """檢查掛載是否指向正確的 NAS_HOST（而非舊 IP）。"""
    try:
        result = subprocess.run(
            ["mount"], capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.splitlines():
            if f"on {volume_path} " in line:
                return NAS_HOST in line
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 56, exc_info=True)
    return False


def _mount_share(share_name: str, volume_path: str) -> bool:
    """用 osascript mount volume 靜默掛載（走 Finder 原生 Keychain，不彈窗）。"""
    smb_url = f"smb://{NAS_USER}@{NAS_HOST}/{share_name}"
    try:
        # osascript mount volume 讓 Finder 處理認證（自動用 Keychain）
        result = subprocess.run(
            ["osascript", "-e", f'mount volume "{smb_url}"'],
            capture_output=True, text=True, timeout=30,
        )
        # 等待掛載完成
        for _ in range(15):
            time.sleep(1)
            if _is_mounted(volume_path):
                return True

        logger.warning("NAS mount 逾時: %s（15 秒內未出現 %s）", smb_url, volume_path)
        return False

    except Exception as e:
        logger.error("NAS mount 異常: %s → %s", smb_url, e)
        return False


def _unmount_path(volume_path: str) -> None:
    """卸載指定路徑。"""
    try:
        subprocess.run(
            ["diskutil", "unmount", "force", volume_path],
            capture_output=True, text=True, timeout=10,
        )
        logger.info("已卸載: %s", volume_path)
    except Exception as e:
        logger.warning("卸載失敗: %s → %s", volume_path, e)


def _cleanup_wrong_host_mounts() -> None:
    """卸載所有非 NAS_HOST 的 SMB mount 和 -N 後綴的重複 mount。"""
    try:
        result = subprocess.run(
            ["mount"], capture_output=True, text=True, timeout=5,
        )
    except Exception:
        return

    for line in result.stdout.splitlines():
        if "smbfs" not in line:
            continue
        m = re.search(r"on (/Volumes/\S+)", line)
        if not m:
            continue
        vol = m.group(1)
        base_name = vol.split("/")[-1]

        # 清理 -1, -2 等重複 mount
        if re.match(r"^(homes|lumi)-\d+$", base_name):
            logger.info("清理重複 mount: %s", vol)
            _unmount_path(vol)
            continue

        # 清理指向錯誤 IP 的 mount
        if base_name in ("homes", "lumi") and NAS_HOST not in line:
            logger.info("清理舊 IP mount: %s", vol)
            _unmount_path(vol)


# ── 公開 API ─────────────────────────────────────────────────

def ensure_nas_mounts() -> dict[str, bool]:
    """檢查並掛載所有 NAS share，回傳各 share 狀態。"""
    results: dict[str, bool] = {}

    # 動態解析 NAS IP（LAN → Tailscale fallback）
    host = resolve_nas_host()
    if not _ping_ok(host):
        logger.warning("NAS %s 不可達（ping 失敗），跳過掛載", host)
        return {vol: False for _, vol in _SHARES}

    # 清理舊 IP 或重複 mount
    _cleanup_wrong_host_mounts()

    for share_name, volume_path in _SHARES:
        short_name = volume_path.split("/")[-1]

        # 已掛載且指向正確 host → OK
        if _is_mounted(volume_path) and _is_correct_host(volume_path):
            results[short_name] = True
            continue

        # 掛載了但 IP 不對或 stale → 先卸載
        if os.path.exists(volume_path):
            if os.path.ismount(volume_path):
                logger.info("掛載 IP 不正確或 stale: %s，重新掛載", volume_path)
            _unmount_path(volume_path)
            time.sleep(2)

        logger.info("掛載 NAS share: %s → %s", share_name, volume_path)
        ok = _mount_share(share_name, volume_path)
        results[short_name] = ok

        if ok:
            logger.info("NAS share 掛載成功: %s", volume_path)
        else:
            logger.error("NAS share 掛載失敗: %s", volume_path)

    return results


# ── 背景守衛 ─────────────────────────────────────────────────

_guard_thread: threading.Thread | None = None


def _guard_loop(interval: int) -> None:
    """背景巡檢迴圈。"""
    logger.info("NAS mount guard 啟動（每 %d 秒巡檢）", interval)
    while True:
        try:
            time.sleep(interval)
            ensure_nas_mounts()
        except Exception as e:
            logger.error("NAS mount guard 異常: %s", e)


def start_nas_mount_guard(interval: int = 120) -> None:
    """啟動背景守衛執行緒（只會啟動一次）。"""
    global _guard_thread
    if _guard_thread is not None and _guard_thread.is_alive():
        return

    ensure_nas_mounts()

    _guard_thread = threading.Thread(
        target=_guard_loop,
        args=(interval,),
        daemon=True,
        name="nas-mount-guard",
    )
    _guard_thread.start()
    logger.info("NAS mount guard 背景執行緒已啟動（每 %d 秒）", interval)
