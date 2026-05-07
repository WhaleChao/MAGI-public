# -*- coding: utf-8 -*-
"""
macos_notify.py
===============
macOS 原生通知中心整合模組。

透過 osascript 和 terminal-notifier 發送桌面通知。
不佔用 Discord/LINE 的 API quota，離線時也能即時告警。

整合點：
- heartbeat.py：oMLX 服務異常時發送桌面通知
- nas_mount_guard.py：NAS 斷線/重連時通知
- cron_scheduler.py：排程任務失敗時通知
- magi_menubar.py：可從 Menu Bar 手動觸發通知測試
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
from typing import Optional

logger = logging.getLogger("MacOSNotify")

# ---------------------------------------------------------------------------
# terminal-notifier 偵測
# ---------------------------------------------------------------------------
_TERMINAL_NOTIFIER = shutil.which("terminal-notifier")
HAS_TERMINAL_NOTIFIER = _TERMINAL_NOTIFIER is not None

# MAGI 圖示路徑
_MAGI_ROOT = os.path.abspath(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
_MAGI_ICON = os.path.join(_MAGI_ROOT, "gui", "assets", "magi_icon.png")
_DASHBOARD_URL = "http://localhost:%s" % os.environ.get("MAGI_SERVER_PORT", "5002")

# 通知群組 ID（用於合併同類通知）
GROUP_SYSTEM = "magi.system"
GROUP_OMLX = "magi.omlx"
GROUP_NAS = "magi.nas"
GROUP_CRON = "magi.cron"
GROUP_PDF = "magi.pdf"
GROUP_CASE = "magi.case"


def send_notification(
    title: str,
    message: str,
    subtitle: str = "",
    sound: str = "default",
    group_id: str = GROUP_SYSTEM,
) -> bool:
    """
    透過 macOS 原生通知中心發送通知。

    使用 osascript 而非 PyObjC，因為 LaunchAgent 環境下
    UNUserNotificationCenter 需要 bundle ID 才能運作。

    Args:
        title: 通知標題
        message: 通知內容
        subtitle: 副標題（可選）
        sound: 通知音效名稱（"default", "Glass", "Basso" 等）
        group_id: 通知群組 ID（用於合併同類通知）

    Returns:
        True if notification was sent successfully
    """
    # 先嘗試 terminal-notifier（支援點擊動作、圖示）
    if HAS_TERMINAL_NOTIFIER:
        return _send_via_terminal_notifier(title, message, subtitle, sound, group_id)

    # Fallback: osascript
    return _send_via_osascript(title, message, subtitle, sound)


def _send_via_terminal_notifier(
    title: str,
    message: str,
    subtitle: str = "",
    sound: str = "default",
    group_id: str = GROUP_SYSTEM,
    action_url: Optional[str] = None,
) -> bool:
    """使用 terminal-notifier 發送可互動通知。"""
    cmd = [
        _TERMINAL_NOTIFIER,
        "-title", title,
        "-message", message,
        "-group", group_id,
    ]

    if subtitle:
        cmd += ["-subtitle", subtitle]
    if sound:
        cmd += ["-sound", sound]
    if os.path.isfile(_MAGI_ICON):
        cmd += ["-appIcon", _MAGI_ICON]

    url = action_url or _DASHBOARD_URL
    cmd += ["-open", url]

    try:
        subprocess.run(cmd, timeout=5, capture_output=True, check=False)
        return True
    except (subprocess.SubprocessError, FileNotFoundError) as e:
        logger.debug("terminal-notifier failed: %s, falling back to osascript", e)
        return _send_via_osascript(title, message, subtitle, sound)


def _send_via_osascript(
    title: str,
    message: str,
    subtitle: str = "",
    sound: str = "default",
) -> bool:
    """使用 osascript 發送基本通知。"""
    # 轉義雙引號
    title = title.replace('"', '\\"')
    message = message.replace('"', '\\"')
    subtitle = subtitle.replace('"', '\\"')

    parts = [f'display notification "{message}" with title "{title}"']
    if subtitle:
        parts[0] += f' subtitle "{subtitle}"'
    if sound:
        parts[0] += f' sound name "{sound}"'

    script = parts[0]

    try:
        subprocess.run(
            ["osascript", "-e", script],
            timeout=5, capture_output=True, check=True,
        )
        return True
    except subprocess.SubprocessError as e:
        logger.error("osascript notification failed: %s", e)
        return False


# ---------------------------------------------------------------------------
# 便捷函式：常用通知場景
# ---------------------------------------------------------------------------

def notify_omlx_error(error_msg: str) -> bool:
    """oMLX 服務異常通知。"""
    return send_notification(
        title="MAGI ⚠ oMLX 異常",
        message=error_msg[:200],
        subtitle="推理引擎需要注意",
        sound="Basso",
        group_id=GROUP_OMLX,
    )


def notify_nas_status(connected: bool, share_name: str = "homes") -> bool:
    """NAS 連線狀態通知。"""
    if connected:
        return send_notification(
            title="MAGI NAS 重連成功",
            message=f"{share_name} 已重新掛載",
            sound="Glass",
            group_id=GROUP_NAS,
        )
    else:
        return send_notification(
            title="MAGI ⚠ NAS 斷線",
            message=f"{share_name} 無法存取",
            subtitle="檔案操作可能受影響",
            sound="Basso",
            group_id=GROUP_NAS,
        )


def notify_cron_failure(job_name: str, error: str) -> bool:
    """排程任務失敗通知。"""
    return send_notification(
        title="MAGI 排程失敗",
        message=f"{job_name}: {error[:150]}",
        sound="Basso",
        group_id=GROUP_CRON,
    )


def notify_pdf_processed(filename: str, new_name: str) -> bool:
    """PDF 命名完成通知。"""
    return send_notification(
        title="MAGI PDF 歸檔完成",
        message=f"{filename} → {new_name}",
        sound="default",
        group_id=GROUP_PDF,
    )


def notify_case_deadline(case_number: str, deadline_desc: str, days_left: int) -> bool:
    """案件期限提醒通知。"""
    urgency = "Basso" if days_left <= 3 else "default"
    return send_notification(
        title=f"MAGI 期限提醒（{days_left}天）",
        message=f"{case_number}: {deadline_desc}",
        sound=urgency,
        group_id=GROUP_CASE,
    )


# ---------------------------------------------------------------------------
# CLI 入口（測試用）
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys

    if "--test" in sys.argv:
        print(f"terminal-notifier available: {HAS_TERMINAL_NOTIFIER}")
        ok = send_notification(
            title="MAGI 通知測試",
            message="如果你看到這個通知，表示通知功能正常運作！",
            subtitle="測試中",
        )
        print(f"Notification sent: {ok}")
    else:
        print("Usage: python macos_notify.py --test")
