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
    _NAS_LAN_HOST = os.getenv("MAGI_NAS_HOST") or (_nas_node.lan_ip if _nas_node else None) or ""
    _NAS_TS_HOST = os.getenv("MAGI_NAS_TAILSCALE_HOST") or (_nas_node.tailscale_ip if _nas_node else None) or ""
except Exception:
    _NAS_LAN_HOST = os.getenv("MAGI_NAS_HOST", "")
    _NAS_TS_HOST = os.getenv("MAGI_NAS_TAILSCALE_HOST", "")
NAS_USER = os.getenv("MAGI_NAS_USER", "MAGI_NAS_SHARE")

# 動態解析結果快取（host, expiry_time）
_resolved_host: Optional[str] = None
_resolved_expiry: float = 0.0
_RESOLVE_TTL = 120  # 快取 120 秒

# 邊緣觸發通知：只在 share 狀態真正改變（上→下、下→上）時才送通知，
# 避免 NAS 長期不可達時每 120 秒巡檢都發「NAS 斷線」訊息。
# 初始值 None 代表「尚未判定」，第一次結果不發「重連成功」誤報。
_LAST_MOUNT_STATUS: Dict[str, Optional[bool]] = {}
_LAST_MOUNT_STATUS_LOCK = threading.Lock()

# SynologyDrive 雲同步 fallback 路徑（Synology Drive Client 安裝後自動產生）
_SYNOLOGY_DRIVE_CANDIDATES = (
    os.path.expanduser("~/Library/CloudStorage/SynologyDrive-homes"),
    os.path.expanduser("~/Library/CloudStorage/SynologyDrive-home"),
    os.path.expanduser("~/SynologyDrive"),
)


def _synology_drive_available() -> bool:
    """檢查 SynologyDrive 本地同步是否可用（MAGI 的 NAS fallback 路徑）。"""
    return bool(get_synology_drive_fallback_path())


def get_synology_drive_fallback_path() -> str:
    """回傳可用的 Synology Drive 本地同步根目錄；沒有則回空字串。"""
    for p in _SYNOLOGY_DRIVE_CANDIDATES:
        try:
            if os.path.isdir(p):
                # 目錄存在且有內容 → 視為 fallback 可用
                entries = os.listdir(p)
                if entries:
                    return p
        except OSError:
            continue
    return ""


def get_share_available_path(share_name: str, volume_path: str) -> str:
    """回傳 share 目前可用路徑，包含 SMB 掛載與 Synology Drive fallback。"""
    user_mount = os.path.join(_USER_MOUNT_ROOT, share_name)
    for candidate in (volume_path, f"{volume_path}-1", f"{volume_path}-2", user_mount):
        if _is_mounted(candidate):
            try:
                if candidate == user_mount or _is_correct_host(candidate):
                    return candidate
            except Exception:
                if candidate == user_mount:
                    return candidate
    if share_name == "homes":
        fallback = get_synology_drive_fallback_path()
        if fallback:
            return fallback
    return ""


def _ping_ok(host: str, timeout: int = 2) -> bool:
    """檢查 NAS 是否可達：TCP 445 (SMB port) connect，fallback 到 ICMP ping。
    注意：TCP connect 成功不代表 SMB 認證會通過，但代表 NAS 在線。"""
    import socket
    try:
        sock = socket.create_connection((host, 445), timeout=timeout)
        sock.close()
        return True
    except (OSError, socket.timeout):
        pass
    # fallback: ICMP ping（Synology 可能擋 ICMP，TCP 更可靠）
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
# 可透過 MAGI_NAS_SHARES 環境變數覆寫（逗號分隔 share 名，volume path 自動推定為 /Volumes/<share>）
# 例：MAGI_NAS_SHARES=homes → 只掛 homes（若 NAS 上已無 lumi share）
_SHARES_DEFAULT: list[tuple[str, str]] = [
    ("homes", "/Volumes/homes"),
    ("lumi",  "/Volumes/lumi"),
]
_SHARES_ENV = os.getenv("MAGI_NAS_SHARES", "").strip()
if _SHARES_ENV:
    _SHARES: list[tuple[str, str]] = [
        (name.strip(), f"/Volumes/{name.strip()}")
        for name in _SHARES_ENV.split(",")
        if name.strip()
    ]
else:
    _SHARES = list(_SHARES_DEFAULT)

# 當 /Volumes/<share> 因 root 權限無法建目錄時的 fallback
_USER_MOUNT_ROOT = os.path.expanduser("~/.magi_mounts")
_ENSURE_MOUNT_LOCK = threading.Lock()  # BUG-38: 防止多執行緒同時 mount 同一 share

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


def _force_unmount_stale(volume_path: str) -> None:
    """偵測並清理 stale SMB mount（掛載點存在但 SMB session 已斷）。
    只有 errno.EIO（I/O 錯誤，stale NFS/SMB 特徵）才卸載；
    PermissionError 等其他 OSError 不卸載，避免誤殺正常掛載。
    """
    import errno as _errno
    for candidate in (volume_path, f"{volume_path}-1", f"{volume_path}-2"):
        if not os.path.exists(candidate):
            continue
        try:
            # 用 os.stat 測試可存取性（stale mount 通常會 hang 或 EIO）
            os.stat(candidate)
        except OSError as e:
            if e.errno == _errno.EIO:
                # EIO = Input/Output Error → 確實是 stale SMB/NFS mount
                logger.info("偵測到 stale mount %s (EIO)，強制卸載", candidate)
                _unmount_path(candidate)
            else:
                # EACCES/EPERM 等：只是權限問題，不卸載
                logger.debug("os.stat(%s) 失敗 (errno=%d)，非 stale mount，跳過", candidate, e.errno)


def _mount_share(share_name: str, volume_path: str) -> bool:
    """掛載 NAS share。策略：
    1. 先清理 stale mount（SMB session 斷掉但 mount point 殘留）
    2. osascript mount volume（走 Finder Keychain，30s timeout）
    3. 若 osascript 失敗，fallback 到 mount_smbfs（CLI 直接掛載）
    """
    # Step 0: 清理 stale mount
    _force_unmount_stale(volume_path)

    smb_url = f"smb://{NAS_USER}@{NAS_HOST}/{share_name}"

    # Step 1: osascript mount volume（Finder Keychain）
    try:
        subprocess.run(
            ["osascript", "-e", f'mount volume "{smb_url}"'],
            capture_output=True, text=True, timeout=30,
        )
        for _ in range(10):
            time.sleep(1)
            if _is_mounted(volume_path):
                return True
    except subprocess.TimeoutExpired:
        logger.warning("osascript mount timeout (30s): %s", smb_url)
    except Exception as e:
        logger.warning("osascript mount failed: %s → %s", smb_url, e)

    # Step 2: mount_smbfs fallback（不走 Finder，直接 CLI 掛載）
    # osascript 失敗時用 mount_smbfs 帶 keychain 密碼
    _password = _get_nas_password_from_keychain()

    # 確保 /Volumes/<share> mount point 存在且 user 有權限
    _ensure_volume_mount_point(volume_path)

    for mount_target in (volume_path, os.path.join(_USER_MOUNT_ROOT, share_name)):
        try:
            os.makedirs(mount_target, exist_ok=True)
        except OSError:
            continue
        mount_url = f"//{NAS_USER}@{NAS_HOST}/{share_name}"
        if _password:
            mount_url = f"//{NAS_USER}:{_password}@{NAS_HOST}/{share_name}"
        try:
            result = subprocess.run(
                ["mount_smbfs", "-o", "soft", mount_url, mount_target],
                capture_output=True, text=True, timeout=15,
            )
            if result.returncode == 0:
                for _ in range(5):
                    time.sleep(1)
                    if _is_mounted(mount_target):
                        logger.info("mount_smbfs fallback 成功: %s → %s", share_name, mount_target)
                        return True
            else:
                logger.debug("mount_smbfs %s → rc=%d: %s", mount_target, result.returncode, result.stderr.strip()[:100])
        except subprocess.TimeoutExpired:
            logger.debug("mount_smbfs timeout (15s): %s → %s", share_name, mount_target)
        except Exception as e:
            logger.debug("mount_smbfs 異常: %s → %s: %s", share_name, mount_target, e)

    logger.error("NAS mount 失敗（osascript + mount_smbfs 均未成功）: %s", smb_url)
    return False


def _ensure_volume_mount_point(volume_path: str) -> None:
    """確保 /Volumes/<share> mount point 存在且當前使用者有 mount 權限。

    策略（依序嘗試）：
    1. 目錄已存在且 owner 正確 → 直接返回
    2. owner 不符 → 只有在互動環境（有 GUI/TTY）才觸發 osascript 彈窗；
       daemon 環境靜默跳過（由開機時 com.magi.nas-mountpoints LaunchDaemon 預先 chown）
    3. 目錄不存在 → 先嘗試 mkdir（若 /Volumes 可寫），再 osascript（互動環境），
       最後靜默放棄（mount_smbfs 會 fallback 到 ~/.magi_mounts）
    """
    _interactive = bool(os.environ.get("DISPLAY") or os.environ.get("TERM_PROGRAM") or
                        (hasattr(os, "isatty") and os.isatty(1)))

    if os.path.isdir(volume_path):
        try:
            st = os.stat(volume_path)
            if st.st_uid != os.getuid():
                if _interactive:
                    subprocess.run(
                        ["osascript", "-e",
                         f'do shell script "chown {os.getuid()}:staff {volume_path}" with administrator privileges'],
                        capture_output=True, text=True, timeout=10,
                    )
                else:
                    # daemon 環境：靜默略過，開機 LaunchDaemon (com.magi.nas-mountpoints) 已預先 chown
                    logger.debug("_ensure_volume_mount_point: %s owned by root, skipping GUI chown in daemon context", volume_path)
        except Exception:
            pass
        return

    # 目錄不存在
    try:
        os.makedirs(volume_path, exist_ok=True)
        return
    except OSError:
        pass
    if _interactive:
        try:
            subprocess.run(
                ["osascript", "-e",
                 f'do shell script "mkdir -p {volume_path} && chown {os.getuid()}:staff {volume_path}" with administrator privileges'],
                capture_output=True, text=True, timeout=10,
            )
        except Exception as e:
            logger.debug("Cannot create %s via osascript: %s", volume_path, e)
    else:
        logger.debug("_ensure_volume_mount_point: cannot create %s in daemon context, will fallback to ~/.magi_mounts", volume_path)


def _get_nas_password_from_keychain() -> str:
    """從 macOS keychain 取出 NAS SMB 密碼。"""
    try:
        result = subprocess.run(
            ["security", "find-internet-password", "-s", NAS_HOST, "-a", NAS_USER, "-g"],
            capture_output=True, text=True, timeout=5,
        )
        for line in result.stderr.splitlines():
            if line.startswith("password: "):
                pw = line.split("password: ", 1)[1].strip().strip('"')
                return pw
    except Exception:
        pass
    return ""


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
    """檢查並掛載所有 NAS share，回傳各 share 狀態。
    BUG-38: 使用全域 lock 防止守衛執行緒與啟動路徑同時觸發重複掛載。
    """
    with _ENSURE_MOUNT_LOCK:
        return _ensure_nas_mounts_locked()


def _ensure_nas_mounts_locked() -> dict[str, bool]:
    """實際掛載邏輯（需在 _ENSURE_MOUNT_LOCK 持有期間呼叫）。"""
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

        # 已掛載且指向已知 NAS IP → 不動（無論 LAN 或 Tailscale）
        effective_path = get_share_available_path(share_name, volume_path)
        if effective_path:
            results[short_name] = True
            continue

        # 沒有可用掛載 → 需要掛載
        logger.info("掛載 NAS share: %s → %s", share_name, volume_path)
        ok = _mount_share(share_name, volume_path)
        results[short_name] = ok

        if ok:
            logger.info("NAS share 掛載成功: %s", volume_path)
        else:
            logger.error("NAS share 掛載失敗: %s", volume_path)

    # 邊緣觸發通知 — 只在狀態改變時送 macOS 通知，避免 NAS 長期不可達的通知洪水
    _dispatch_transition_notifications(results)
    return results


def _dispatch_transition_notifications(results: Dict[str, bool]) -> None:
    """根據本次與上次的掛載結果，只在狀態轉換時送通知。

    規則：
    - 上次 False → 本次 True：送「NAS 重連成功」
    - 上次 True → 本次 False：送「NAS 斷線」，但若 SynologyDrive 雲同步可用則改 log 不彈通知
    - 上次 None（首次巡檢）：不送通知，只記錄狀態（避免冷啟動誤報）
    - 其他（狀態相同）：不送通知
    """
    try:
        from skills.ops.macos_notify import notify_nas_status
    except Exception:
        notify_nas_status = None

    synology_ok = _synology_drive_available()
    with _LAST_MOUNT_STATUS_LOCK:
        for share_name, current_ok in results.items():
            last = _LAST_MOUNT_STATUS.get(share_name)
            _LAST_MOUNT_STATUS[share_name] = current_ok
            if last is None:
                # 首次巡檢：只記錄，不送通知（避免冷啟動誤報）
                if not current_ok and synology_ok:
                    logger.info("NAS share %s 不可用，MAGI 自動走 SynologyDrive 本地同步", share_name)
                continue
            if last == current_ok:
                continue  # 狀態沒變化 → 不送通知
            if notify_nas_status is None:
                continue
            if current_ok:
                try:
                    notify_nas_status(connected=True, share_name=share_name)
                except Exception:
                    pass
            else:
                # 斷線：若 SynologyDrive 可用，降級為 log，不彈通知
                if synology_ok:
                    logger.info("NAS share %s 斷線，MAGI 已自動走 SynologyDrive 本地同步（通知已抑制）", share_name)
                else:
                    try:
                        notify_nas_status(connected=False, share_name=share_name)
                    except Exception:
                        pass


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

    _guard_thread = threading.Thread(
        target=_guard_loop,
        args=(interval,),
        daemon=True,
        name="nas-mount-guard",
    )
    _guard_thread.start()
    logger.info("NAS mount guard 背景執行緒已啟動（每 %d 秒）", interval)
