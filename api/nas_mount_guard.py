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
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("magi.nas_mount_guard")

# ── NAS 連線設定 ─────────────────────────────────────────────
try:
    from api.routing.node_registry import get_node as _get_nas_node
    _nas_node = _get_nas_node("nas")
    _NAS_LAN_HOST = os.getenv("MAGI_NAS_HOST") or (_nas_node.lan_ip if _nas_node else None) or "192.168.1.3"
    _NAS_TS_HOST = os.getenv("MAGI_NAS_TAILSCALE_HOST") or (_nas_node.tailscale_ip if _nas_node else None) or "100.111.10.126"
except Exception:
    _NAS_LAN_HOST = os.getenv("MAGI_NAS_HOST", "192.168.1.3")
    _NAS_TS_HOST = os.getenv("MAGI_NAS_TAILSCALE_HOST", "100.111.10.126")
NAS_USER = os.getenv("MAGI_NAS_USER", "lumi63181107")

# 動態解析結果快取（host, expiry_time）
_resolved_host: Optional[str] = None
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
    """檢查 volume 是否已掛載且可存取（含 macOS automount 後綴 -1）。
    使用 os.stat() 取代 os.listdir() 避免在 SMB 延遲時 hang（0s vs 10-30s）。"""
    for path in (volume_path, f"{volume_path}-1"):
        if os.path.ismount(path):
            try:
                os.stat(path)
                return True
            except OSError:
                continue
    return False


def _known_nas_hosts() -> set:
    """所有已知的 NAS IP（LAN + Tailscale），任一都算合法掛載。"""
    return {_NAS_LAN_HOST, _NAS_TS_HOST}


def _is_correct_host(volume_path: str) -> bool:
    """檢查掛載是否指向任一已知 NAS IP（LAN 或 Tailscale 都算正確）。"""
    known = _known_nas_hosts()
    try:
        result = subprocess.run(
            ["/sbin/mount"], capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.splitlines():
            if f"on {volume_path} " in line:
                return any(ip in line for ip in known)
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
            ["/usr/sbin/diskutil", "unmount", "force", volume_path],
            capture_output=True, text=True, timeout=10,
        )
        logger.info("已卸載: %s", volume_path)
    except Exception as e:
        logger.warning("卸載失敗: %s → %s", volume_path, e)


def _cleanup_wrong_host_mounts() -> None:
    """卸載指向未知 IP 的 SMB mount；已知 IP（LAN/Tailscale）的掛載一律保留。"""
    known = _known_nas_hosts()
    try:
        result = subprocess.run(
            ["/sbin/mount"], capture_output=True, text=True, timeout=5,
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

        # 只處理 homes/lumi 相關的 mount
        if not re.match(r"^(homes|lumi)(-\d+)?$", base_name):
            continue

        # 掛載指向已知 IP → 保留（無論是 LAN 或 Tailscale）
        if any(ip in line for ip in known):
            # 但如果是 -N 後綴且正名掛載也存在且可用，清理重複
            if re.match(r"^(homes|lumi)-\d+$", base_name):
                canonical = f"/Volumes/{base_name.split('-')[0]}"
                if _is_mounted(canonical) and _is_correct_host(canonical):
                    logger.info("清理重複 mount（正名已可用）: %s", vol)
                    _unmount_path(vol)
            continue

        # 掛載指向未知 IP → 清理
        logger.info("清理未知 IP mount: %s", vol)
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

        # 檢查正名和 -N 後綴是否有可用掛載
        effective_path = volume_path
        for candidate in (volume_path, f"{volume_path}-1", f"{volume_path}-2"):
            if _is_mounted(candidate) and _is_correct_host(candidate):
                effective_path = candidate
                break

        # 已掛載且指向已知 NAS IP → 不動（無論 LAN 或 Tailscale）
        if _is_mounted(effective_path) and _is_correct_host(effective_path):
            results[short_name] = True
            continue

        # 沒有可用掛載 → 需要掛載
        logger.info("掛載 NAS share: %s → %s", share_name, volume_path)
        ok = _mount_share(share_name, volume_path)
        results[short_name] = ok

        if ok:
            logger.info("NAS share 掛載成功: %s", volume_path)
            try:
                from skills.ops.macos_notify import notify_nas_status
                notify_nas_status(connected=True, share_name=short_name)
            except Exception:
                pass
        else:
            logger.error("NAS share 掛載失敗: %s", volume_path)
            try:
                from skills.ops.macos_notify import notify_nas_status
                notify_nas_status(connected=False, share_name=short_name)
            except Exception:
                pass

    return results


# ── 背景守衛 ─────────────────────────────────────────────────

_guard_thread: threading.Optional[Thread] = None


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
