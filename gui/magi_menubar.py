#!/usr/bin/env python3
"""
MAGI 選單列狀態監控
在 macOS 選單列顯示 MAGI 系統健康狀態。

v3 — 增強版：
  - 遠端節點狀態（Melchior / Balthasar / Keeper）
  - 本機 DB 狀態（舊遠端 DB 已退役）
  - NAS 分卷掛載狀態 + 容量
  - 排程任務逐條顯示 + 最後執行時間
  - 移除已廢棄的推理分層 tier
"""

import os
import subprocess
import json
import plistlib
import threading
import time
import re
import logging
import urllib.request
import urllib.error
import atexit
import sys
import math
import textwrap
import hashlib
import shutil
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from magi_v3 import fcntl_compat as fcntl


_MENUBAR_LOCK_HANDLE = None


def _acquire_menubar_singleton() -> None:
    """Prevent duplicate macOS status bar icons across launchd/direct starts."""
    global _MENUBAR_LOCK_HANDLE
    if fcntl is None:
        return
    lock_path = os.environ.get("MAGI_MENUBAR_LOCK_PATH", "/tmp/magi-menubar.lock")
    try:
        fh = open(lock_path, "w", encoding="utf-8")
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        fh.seek(0)
        fh.truncate(0)
        fh.write(str(os.getpid()))
        fh.flush()
        _MENUBAR_LOCK_HANDLE = fh

        def _release_lock() -> None:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
                fh.close()
            except Exception:
                pass

        atexit.register(_release_lock)
    except BlockingIOError:
        print("MAGI menubar already running; exiting duplicate instance.", file=sys.stderr)
        sys.exit(0)
    except Exception as exc:
        print(f"MAGI menubar singleton lock unavailable: {exc}", file=sys.stderr)

try:
    import rumps
    _HAS_RUMPS = True
except ImportError:  # pragma: no cover - exercised by import-only tests
    _HAS_RUMPS = False

    class _FallbackMenu(list):
        def insert_after(self, title, item):
            for idx, existing in enumerate(self):
                if getattr(existing, "title", existing) == title:
                    self.insert(idx + 1, item)
                    return
            self.append(item)

    class _FallbackMenuItem:
        def __init__(self, title="", callback=None):
            self.title = title
            self._callback = callback

        def set_callback(self, callback):
            self._callback = callback

    class _FallbackApp:
        def __init__(self, title="", quit_button=None):
            self.title = title
            self.quit_button = quit_button
            self._menu = _FallbackMenu()

        @property
        def menu(self):
            return self._menu

        @menu.setter
        def menu(self, value):
            self._menu = _FallbackMenu(value or [])

        def run(self):
            raise RuntimeError("rumps is required to run the macOS MAGI menubar app")

    class _FallbackRumps:
        App = _FallbackApp
        MenuItem = _FallbackMenuItem
        separator = "---"

        @staticmethod
        def timer(_interval):
            def _decorator(func):
                return func

            return _decorator

        @staticmethod
        def quit_application():
            return None

    rumps = _FallbackRumps()

# PyObjC: 強制上色 + 隱藏 Dock 圖示
try:
    if os.environ.get("MAGI_MENUBAR_NO_APPKIT", "").strip() == "1":
        raise ImportError("AppKit disabled for headless verification")
    import objc
    from AppKit import (
        NSAttributedString,
        NSForegroundColorAttributeName,
        NSColor,
        NSFont,
        NSFontAttributeName,
        NSParagraphStyleAttributeName,
        NSView,
        NSBezierPath,
        NSString,
        NSMutableParagraphStyle,
        NSMakeRect,
        NSMakePoint,
        NSLeftTextAlignment,
        NSCenterTextAlignment,
        NSRightTextAlignment,
        NSApplication,
        NSApplicationActivationPolicyAccessory,
        NSGradient,
        NSWindow,
        NSScreen,
        NSWindowStyleMaskBorderless,
        NSBackingStoreBuffered,
        NSMainMenuWindowLevel,
        NSWindowCollectionBehaviorCanJoinAllSpaces,
        NSWindowCollectionBehaviorFullScreenAuxiliary,
        NSWindowCollectionBehaviorStationary,
        NSPasteboard,
        NSPasteboardTypeString,
        NSImage,
        NSCompositingOperationSourceOver,
    )
    from Foundation import NSObject, NSURL
    import AppKit as _AppKit
    _NS_VISUAL_EFFECT_VIEW = getattr(_AppKit, "NSVisualEffectView", None)
    # Under-window material preserves the real desktop/app background.  The
    # former HUD material adds a strong dark tint and made an otherwise clear
    # window look opaque.
    _NS_VISUAL_EFFECT_MATERIAL = getattr(
        _AppKit, "NSVisualEffectMaterialUnderWindowBackground", 21
    )
    _NS_VISUAL_EFFECT_BEHIND = getattr(_AppKit, "NSVisualEffectBlendingModeBehindWindow", 0)
    _NS_VISUAL_EFFECT_ACTIVE = getattr(_AppKit, "NSVisualEffectStateActive", 1)
    _HAS_APPKIT = True
except ImportError:
    objc = None
    _HAS_APPKIT = False
    _NS_VISUAL_EFFECT_VIEW = None
    _NS_VISUAL_EFFECT_MATERIAL = None
    _NS_VISUAL_EFFECT_BEHIND = None
    _NS_VISUAL_EFFECT_ACTIVE = None


# The cockpit uses a small, cached still preview of the current Apple desktop
# plus translucent HUD layers. Dynamic wallpaper movies are never streamed;
# one bounded frame extraction is reused until the wallpaper changes.
_HAS_AVFOUNDATION = False
_AV_QUEUE_PLAYER = None
_AV_PLAYER_ITEM = None
_AV_PLAYER_LOOPER = None
_AV_PLAYER_LAYER = None


def _initialize_menubar_application():
    """Register AppKit only when the real GUI runtime starts.

    Importing this module is also useful to headless verifiers and evidence
    compilers.  NSApplication.sharedApplication() aborts the interpreter when
    those callers do not own a macOS GUI application session, so application
    registration must never happen as an import side effect.
    """
    if not _HAS_APPKIT:
        return None
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
    return app

# ── 設定 ──────────────────────────────────────────────────────────
MAGI_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if MAGI_ROOT not in sys.path:
    sys.path.insert(0, MAGI_ROOT)

from magi_v3.process_monitor import (  # noqa: E402 - root must be installed first
    ZombiePersistence,
    collect_process_monitor as _collect_shared_process_monitor,
)


def _configured_path(env_name: str, fallback: str) -> str:
    raw = os.environ.get(env_name, "").strip()
    return os.path.abspath(os.path.expanduser(raw)) if raw else fallback


def _mutable_static_path(filename: str) -> str:
    root = _configured_path("MAGI_MUTABLE_STATIC_DIR", os.path.join(MAGI_ROOT, "static"))
    return os.path.join(root, filename)


def _agent_path(filename: str) -> str:
    root = _configured_path("MAGI_AGENT_DIR", os.path.join(MAGI_ROOT, ".agent"))
    return os.path.join(root, filename)

try:
    from scripts.ops.omlx_profile_policy import (
        DAY_FALLBACK_MODEL_KEYWORD,
        DAY_MODEL_KEYWORD,
        NIGHT_FALLBACK_MODEL_KEYWORD,
        NIGHT_MODEL_KEYWORD,
        expected_profile_now as expected_omlx_profile_now,
        profile_transition_in_progress,
    )
except Exception:
    DAY_MODEL_KEYWORD = "e4b"
    DAY_FALLBACK_MODEL_KEYWORD = "e4b"
    NIGHT_MODEL_KEYWORD = "26b"
    NIGHT_FALLBACK_MODEL_KEYWORD = "12b"

    def expected_omlx_profile_now():
        now = datetime.now()
        minutes = now.hour * 60 + now.minute
        return ("day", DAY_MODEL_KEYWORD) if 395 <= minutes < 1310 else ("night", NIGHT_MODEL_KEYWORD)

    def profile_transition_in_progress(active_profile, expected_profile, now=None):
        active_base = str(active_profile or "").strip().lower().split("-", 1)[0]
        expected = str(expected_profile or "").strip().lower()
        if not active_base or not expected or active_base == expected:
            return False
        now = now or datetime.now()
        minutes = now.hour * 60 + now.minute
        boundary = 395 if expected == "day" else 1310
        return boundary <= minutes < boundary + 10


def _load_local_env_keys(keys: set[str]) -> None:
    """Load non-secret menubar settings when LaunchAgent lacks shell env."""
    env_path = _configured_path("MAGI_ENV_FILE", os.path.join(MAGI_ROOT, ".env"))
    try:
        with open(env_path, "r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                if key in keys and key not in os.environ:
                    os.environ[key] = value.strip().strip('"').strip("'")
    except OSError:
        return


_load_local_env_keys({"MAGI_NAS_SHARES", "MAGI_NAS_HOST", "MAGI_NAS_TAILSCALE_HOST"})

CHECK_INTERVAL = 5  # UI 心跳（秒）；重型狀態採集另由前／後景間隔管制。
MENUBAR_VISIBLE_COLLECT_INTERVAL = 5.0
MENUBAR_HIDDEN_COLLECT_INTERVAL = 30.0
_KNOWLEDGE_RADAR_LOCK = threading.Lock()
_KNOWLEDGE_RADAR_MEMO = {"signature": None, "snapshot": {}}

SERVICES = [
    ("守護程式", ("magi_v3.control", "daemon.py", "run_daemon_no_site.py")),
    ("主伺服器", ("magi_v3.gateway", "api/server.py")),
    ("通訊機器人", ("api/discord_bot.py",)),
    ("工具介面", ("magi_v3.gateway", "api/tools_api.py")),
]

OMLX_ENGINES = [
    ("文字推理", int(os.environ.get("MAGI_OMLX_PORT", "8080"))),
    ("邏輯推理", int(os.environ.get("MAGI_OMLX_PHI4_PORT", "8082"))),
    ("交叉驗證", int(os.environ.get("MAGI_OMLX_SMOL_PORT", "8083"))),
    ("向量嵌入", 8081),
]

# 遠端節點定義（名稱, registry key, 角色, 檢測 port, 檢測類型）
# Melchior/Balthasar/Keeper 已停用（推理走本機 oMLX，DB 走本機 MariaDB）
REMOTE_NODES = []

# NAS 掛載卷 — 可透過 MAGI_NAS_SHARES env var 覆寫（與 nas_mount_guard 同步）
_NAS_SHARES_ENV = os.environ.get("MAGI_NAS_SHARES", "").strip()
if _NAS_SHARES_ENV:
    NAS_SHARES = [
        (name.strip(), os.path.join(os.sep, "Volumes", name.strip()))
        for name in _NAS_SHARES_ENV.split(",")
        if name.strip()
    ]
else:
    _archive_share = (
        os.environ.get("MAGI_NAS_CLOSED_SHARE_NAME")
        or os.environ.get("MAGI_NAS_ARCHIVE_SHARE")
        or "lumi"
    ).strip().strip("/\\") or "lumi"
    NAS_SHARES = [
        ("homes", os.path.join(os.sep, "Volumes", "homes")),
        (_archive_share, os.path.join(os.sep, "Volumes", _archive_share)),
    ]
_USER_MOUNT_ROOT = os.path.expanduser("~/.magi_mounts")
_SYNOLOGY_DRIVE_CANDIDATES = (
    os.path.expanduser("~/Library/CloudStorage/SynologyDrive-homes"),
    os.path.expanduser("~/Library/CloudStorage/SynologyDrive-home"),
    os.path.expanduser("~/SynologyDrive"),
)

# 背景監控 thread 名稱（用於偵測是否在線）
MONITOR_THREADS = [
    ("法扶信箱監控", "laf-gmail-monitor"),
    ("法扶附件重試", "laf-portal-retry-loop"),
    ("閱卷信箱監控", "filereview-email-monitor"),
    ("閱卷入口掃描", "filereview-portal-scan"),
]

# 排程任務最多顯示的條數
CRON_DISPLAY_MAX = 15
LIVE_LOG_DISPLAY_MAX = 7
LIVE_PULSE_POINT_COUNT = 18
BUSINESS_LIVE_JOB_ID = "job_business_module_live_check"
BUSINESS_LIVE_TIMEOUT_SEC = int(os.environ.get("MAGI_BUSINESS_LIVE_TIMEOUT_SEC", "1200") or "1200")
SERVER_HEALTH_URL = os.environ.get("MAGI_SERVER_HEALTH_URL", "http://127.0.0.1:5002/health").strip()
TOOLS_HEALTH_URL = os.environ.get("MAGI_TOOLS_HEALTH_URL", "http://127.0.0.1:5003/health").strip()
HEALTH_ARTIFACTS = {
    "guardian": {
        "paths": ("magi_self_repair_guardian_latest.json",),
        "max_age_sec": 30 * 3600,
        "label": "自我修復",
    },
    "function_health": {
        "paths": ("magi_acceptance_function_health_latest.json", "function_health_index_latest.json"),
        "max_age_sec": 30 * 3600,
        "label": "功能健康",
    },
}
AGENT_STATUS_PUBLIC_FILENAME = "agent_status_public_latest.json"
AGENT_STATUS_ACTIVE_MAX_AGE_SEC = max(
    60,
    int(os.environ.get("MAGI_AGENT_STATUS_ACTIVE_MAX_AGE_SEC", "900") or "900"),
)
_PUBLIC_AGENT_INTENTS = {
    "general": "一般作業",
    "cases": "案件管理",
    "clients": "當事人管理",
    "calendar": "行事曆作業",
    "todos": "待辦作業",
    "documents": "文件作業",
    "files": "檔案作業",
    "nas": "NAS 作業",
    "drive": "雲端同步",
    "research": "資料研究",
    "legal": "法律作業",
    "legal_statutes": "法規查詢",
    "judgments": "裁判查詢",
    "laf": "法扶作業",
    "file_review": "閱卷作業",
    "transcript": "筆錄作業",
    "transcription": "錄音轉文字",
    "translation": "翻譯作業",
    "ocr": "文字辨識",
    "drafting": "書狀草擬",
    "accounting": "帳務作業",
    "quotation": "報價作業",
    "memory": "記憶查詢",
    "obsidian": "知識庫作業",
    "realtime": "即時資料",
    "web": "網路作業",
    "models": "模型作業",
    "system": "系統維運",
    "backup": "備份作業",
    "notifications": "通知作業",
    "automation": "自動化作業",
}
_PUBLIC_AGENT_PLAN_STEPS = {
    "classify": "判定意圖",
    "route": "選擇路由",
    "check_permissions": "確認權限",
    "retrieve": "取得資料",
    "execute": "執行工具",
    "verify": "驗證結果",
    "respond": "整理回覆",
    "await_confirmation": "等待確認",
}
_PUBLIC_AGENT_STEP_STATES = {
    "pending": "等待",
    "running": "進行中",
    "done": "完成",
    "blocked": "受阻",
    "skipped": "略過",
}
_PUBLIC_AGENT_TOOL_CATEGORIES = {
    "web": "網路工具",
    "search": "搜尋工具",
    "fetch": "擷取工具",
    "database": "資料庫工具",
    "calendar": "行事曆工具",
    "drive": "檔案同步工具",
    "files": "檔案工具",
    "nas": "NAS 工具",
    "documents": "文件工具",
    "todos": "待辦工具",
    "file_review": "閱卷工具",
    "transcript": "筆錄工具",
    "transcription": "轉錄工具",
    "translation": "翻譯工具",
    "ocr": "文字辨識工具",
    "laf": "法扶工具",
    "legal": "法律工具",
    "accounting": "帳務工具",
    "memory": "記憶工具",
    "notifications": "通知工具",
    "code": "程式工具",
    "system": "系統工具",
    "none": "未使用工具",
}
_PUBLIC_AGENT_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+-]{0,63}$")
TASK_MODULE_SECTION_HEIGHT = 144
TASK_MODULE_ROW_Y = 44
TASK_MODULE_ROW_STEP = 31
TASK_MODULE_ROW_HEIGHT = 26
DASHBOARD_WIDTH = 1040
DASHBOARD_HEIGHT = 760
MAGI_HOME_URL = os.environ.get("MAGI_HOME_URL", "http://127.0.0.1:5002/dashboard").strip() or "http://127.0.0.1:5002/dashboard"

# The drawing code uses a small semantic token set instead of literal
# day/night colors.  Keep the persisted forest/cyber values for compatibility,
# while presenting them to people as 日／夜.
MENUBAR_TECH_PALETTES = {
    "cyber": {
        "030507": "02040A",
        "181D21": "08101D",
        "71808A": "3B5870",
        "060C10": "030913",
        "288A88": "16768C",
        "35F5E8": "00E7FF",
        "31F6E2": "4DF8FF",
        "67F58D": "34F5A0",
        "FFC857": "FFD166",
        "FF5F5F": "FF4D6D",
        "8AA3A4": "8EABBE",
        "0A141A": "071321",
        "4B626B": "35536B",
        "081B22": "061827",
        "D8F7F4": "E4FBFF",
        "071117": "06111D",
        "0A1B22": "091B2A",
        "050B0F": "050B0F",
        "31505A": "284760",
        "E9F5F1": "EAFBFF",
        "0A252C": "0A2635",
        "64747D": "607D8D",
        "72E8DC": "29D9FF",
        "E6F7F4": "EAFBFF",
    },
    "forest": {
        "030507": "E6EDF5",
        "181D21": "CBD8E7",
        "71808A": "8297AC",
        "060C10": "F7FAFE",
        "288A88": "6F91B5",
        "35F5E8": "006FE8",
        "31F6E2": "008FC7",
        "67F58D": "007A55",
        "FFC857": "A76000",
        "FF5F5F": "C53650",
        "8AA3A4": "60758A",
        "0A141A": "EDF4FB",
        "4B626B": "9AAFC3",
        "081B22": "E7F1FB",
        "D8F7F4": "153A59",
        "071117": "FBFDFF",
        "0A1B22": "EFF5FB",
        "050B0F": "F0F6FC",
        "31505A": "B5C6D7",
        "E9F5F1": "17324A",
        "0A252C": "E4EFF8",
        "64747D": "8195A8",
        "72E8DC": "2380D8",
        "E6F7F4": "17324A",
    },
}

MENUBAR_STARFIELD_PROFILES = {
    "cyber": {
        "count": 78,
        "nebula_alpha": 0.045,
        "base_rgb": (0.72, 0.92, 1.00),
        "alpha_floor": 0.34,
        "alpha_range": 0.55,
    },
    "forest": {
        "count": 42,
        "nebula_alpha": 0.025,
        "base_rgb": (0.04, 0.25, 0.52),
        "alpha_floor": 0.22,
        "alpha_range": 0.36,
    },
}


def _normalize_menubar_theme(theme: str) -> str:
    value = str(theme or "").strip().lower()
    return "forest" if value in {"day", "light", "forest"} else "cyber"


def _menubar_theme_palette(theme: str) -> dict[str, str]:
    return dict(MENUBAR_TECH_PALETTES[_normalize_menubar_theme(theme)])


def _menubar_starfield_profile(theme: str) -> dict:
    return dict(MENUBAR_STARFIELD_PROFILES[_normalize_menubar_theme(theme)])


def _wallpaper_contrast_profile(theme: str) -> dict[str, float]:
    """Balance visible desktop translucency with local text contrast.

    The full-screen scrim must stay light enough for the real desktop to show
    through.  Readability is supplied by the smaller panel/row layers rather
    than by painting an almost opaque sheet over the whole screen.
    """
    if _normalize_menubar_theme(theme) == "forest":
        return {
            "scrim_start": 0.14,
            "scrim_end": 0.26,
            "panel": 0.76,
            "row": 0.62,
            "core": 0.66,
            "header": 0.70,
            "terminal": 0.84,
        }
    return {
        "scrim_start": 0.10,
        "scrim_end": 0.22,
        "panel": 0.72,
        "row": 0.58,
        "core": 0.64,
        "header": 0.66,
        "terminal": 0.82,
    }


def _wallpaper_path_candidates(value) -> list[str]:
    """Extract local image/movie paths from Apple's nested wallpaper data."""
    candidates: list[str] = []
    if isinstance(value, dict):
        for nested in value.values():
            candidates.extend(_wallpaper_path_candidates(nested))
    elif isinstance(value, (list, tuple)):
        for nested in value:
            candidates.extend(_wallpaper_path_candidates(nested))
    elif isinstance(value, str):
        raw = value.strip()
        if raw.startswith("file://"):
            raw = urllib.request.url2pathname(raw[7:])
        if raw.startswith("/") and raw.lower().endswith(
            (".heic", ".heif", ".jpg", ".jpeg", ".png", ".tiff", ".mov", ".mp4", ".m4v")
        ):
            candidates.append(os.path.expanduser(raw))
    return candidates


def _resolve_apple_wallpaper_source(
    *,
    index_path: str | None = None,
    home: str | None = None,
) -> dict:
    """Resolve the active macOS wallpaper without scripting System Settings.

    Sonoma and later store either a file path or an Aerial ``assetID`` inside
    Store/Index.plist.  The latter maps directly to the locally downloaded
    Apple movie, so MAGI can reuse the exact desktop asset without copying it.
    """
    override = str(os.environ.get("MAGI_MENUBAR_WALLPAPER") or "").strip()
    if override:
        override = os.path.abspath(os.path.expanduser(override))
        if os.path.isfile(override):
            return {
                "path": override,
                "kind": "video" if override.lower().endswith((".mov", ".mp4", ".m4v")) else "image",
                "provider": "override",
                "asset_id": "",
            }

    home_dir = os.path.abspath(os.path.expanduser(home or "~"))
    store_path = os.path.abspath(
        os.path.expanduser(
            index_path
            or os.path.join(
                home_dir,
                "Library",
                "Application Support",
                "com.apple.wallpaper",
                "Store",
                "Index.plist",
            )
        )
    )
    try:
        with open(store_path, "rb") as handle:
            store = plistlib.load(handle)
    except (OSError, ValueError, plistlib.InvalidFileException):
        store = {}

    choices: list[dict] = []
    if isinstance(store, dict):
        for key in ("AllSpacesAndDisplays", "SystemDefault"):
            branch = store.get(key)
            if not isinstance(branch, dict):
                continue
            linked = branch.get("Linked") if isinstance(branch.get("Linked"), dict) else {}
            content = linked.get("Content") if isinstance(linked.get("Content"), dict) else {}
            branch_choices = content.get("Choices") if isinstance(content.get("Choices"), list) else []
            choices.extend(item for item in branch_choices if isinstance(item, dict))

    for choice in choices:
        provider = str(choice.get("Provider") or "")
        decoded_config = {}
        configuration = choice.get("Configuration")
        if isinstance(configuration, (bytes, bytearray)):
            try:
                decoded = plistlib.loads(bytes(configuration))
                decoded_config = decoded if isinstance(decoded, dict) else {}
            except (ValueError, plistlib.InvalidFileException):
                decoded_config = {}

        for candidate in _wallpaper_path_candidates(
            [choice.get("Files"), decoded_config]
        ):
            if os.path.isfile(candidate):
                return {
                    "path": candidate,
                    "kind": "video" if candidate.lower().endswith((".mov", ".mp4", ".m4v")) else "image",
                    "provider": provider,
                    "asset_id": str(decoded_config.get("assetID") or ""),
                }

        asset_id = str(decoded_config.get("assetID") or "").strip()
        if asset_id and re.fullmatch(r"[A-Fa-f0-9-]{8,64}", asset_id):
            aerial_path = os.path.join(
                home_dir,
                "Library",
                "Application Support",
                "com.apple.wallpaper",
                "aerials",
                "videos",
                f"{asset_id}.mov",
            )
            if os.path.isfile(aerial_path):
                return {
                    "path": aerial_path,
                    "kind": "video",
                    "provider": provider,
                    "asset_id": asset_id,
                }

    # Ventura and older versions stored ordinary picture paths in SQLite.  A
    # read-only standard-library lookup preserves compatibility and does not
    # launch Finder/System Settings or alter the desktop.
    legacy_db = os.path.join(
        home_dir,
        "Library",
        "Application Support",
        "Dock",
        "desktoppicture.db",
    )
    if os.path.isfile(legacy_db):
        try:
            import sqlite3

            connection = sqlite3.connect(f"file:{legacy_db}?mode=ro", uri=True)
            try:
                rows = connection.execute(
                    "SELECT value FROM data ORDER BY rowid DESC LIMIT 24"
                ).fetchall()
            finally:
                connection.close()
            for (value,) in rows:
                candidate = os.path.abspath(os.path.expanduser(str(value or "")))
                if os.path.isfile(candidate):
                    return {
                        "path": candidate,
                        "kind": "video" if candidate.lower().endswith((".mov", ".mp4", ".m4v")) else "image",
                        "provider": "legacy-desktop-picture",
                        "asset_id": "",
                    }
        except Exception:
            pass

    return {"path": "", "kind": "fallback", "provider": "", "asset_id": ""}


def _wallpaper_ffmpeg_path(candidates: tuple[str, ...] | None = None) -> str:
    """Resolve ffmpeg in both interactive shells and launchd services."""
    resolved = shutil.which("ffmpeg")
    if resolved and os.path.isfile(resolved) and os.access(resolved, os.X_OK):
        return resolved
    for candidate in candidates or (
        "/opt/homebrew/bin/ffmpeg",
        "/usr/local/bin/ffmpeg",
        "/usr/bin/ffmpeg",
    ):
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return ""


def _cached_wallpaper_preview(source: dict, cache_dir: str | None = None) -> str:
    """Return a lightweight still image for the active wallpaper.

    Static pictures are used directly. A dynamic Apple wallpaper movie is
    decoded once into a 1920px JPEG under MAGI's mutable runtime directory;
    its path, size and modification time form the cache key. No player or
    per-frame decoder remains alive after this function returns.
    """
    source = source if isinstance(source, dict) else {}
    path = os.path.abspath(os.path.expanduser(str(source.get("path") or "")))
    kind = str(source.get("kind") or "fallback").strip().lower()
    if not path or not os.path.isfile(path):
        return ""
    if kind == "image":
        return path
    if kind != "video":
        return ""
    ffmpeg = _wallpaper_ffmpeg_path()
    if not ffmpeg:
        return ""
    try:
        stat = os.stat(path)
    except OSError:
        return ""
    key = hashlib.sha256(
        f"{path}\0{stat.st_size}\0{stat.st_mtime_ns}".encode("utf-8")
    ).hexdigest()[:20]
    root = os.path.abspath(
        os.path.expanduser(
            cache_dir
            or os.environ.get("MAGI_MENUBAR_WALLPAPER_CACHE_DIR", "").strip()
            or os.path.join(
                os.environ.get("MAGI_RUNTIME_DIR", "/tmp/magi-menubar"),
                "wallpaper-preview",
            )
        )
    )
    try:
        os.makedirs(root, mode=0o700, exist_ok=True)
    except OSError:
        return ""
    output = os.path.join(root, f"desktop-{key}.jpg")
    if os.path.isfile(output) and os.path.getsize(output) > 0:
        return output
    temporary = f"{output}.{os.getpid()}.tmp.jpg"
    try:
        result = subprocess.run(
            [
                ffmpeg,
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-ss",
                "4",
                "-i",
                path,
                "-frames:v",
                "1",
                "-vf",
                "scale=1920:-2:force_original_aspect_ratio=decrease",
                "-q:v",
                "4",
                "-y",
                temporary,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=25,
            check=False,
        )
        if (
            result.returncode != 0
            or not os.path.isfile(temporary)
            or os.path.getsize(temporary) <= 0
        ):
            return ""
        os.replace(temporary, output)
        return output
    except (OSError, subprocess.SubprocessError):
        return ""
    finally:
        if os.path.exists(temporary):
            try:
                os.unlink(temporary)
            except OSError:
                pass


def _telemetry_state_code(state: str) -> str:
    visual = _visual_state(str(state or "waiting"))
    return {"ok": "OK", "waiting": "SYNC", "attention": "ALERT"}.get(visual, "ALERT")

OPERATIONAL_TEXT = "運作正常"
CHECK_PASSED_TEXT = "檢查通過"
CHECK_WAITING_TEXT = "等待檢查"
OVERALL_WAITING_TEXT = "有待辦"
ATTENTION_TEXT = "需處理"

BUSINESS_MODULE_CHECKS = {
    "法扶模組": ("laf_portal_live", "laf_self_test", "laf_closing_transfer_notice"),
    "閱卷模組": ("file_review_self_test", "file_review_downloadable_probe"),
    "筆錄模組": ("transcript_self_test", "transcript_db_probe"),
}

FACTORY_CHECKS = {
    "程序衛生": ("live_conflict_audit",),
    "系統狀態": ("nas_mounts_live", "drive_sync_status_live"),
    "任務模組": tuple(
        check_name
        for check_names in BUSINESS_MODULE_CHECKS.values()
        for check_name in check_names
    ),
    "排程載入": ("calendar_todo_status_live",),
    "指紋一致": ("live_runtime_root_fingerprint",),
}

# ── 顏色 ──
if _HAS_APPKIT:
    # 使用 macOS 原生語意顏色 (Semantic Colors)
    _GREEN  = NSColor.systemGreenColor()
    _YELLOW = NSColor.systemOrangeColor() # Orange 更有警示感且在淺色底較清晰
    _RED    = NSColor.systemRedColor()
    _GRAY   = NSColor.labelColor()        # 使用 labelColor 獲得最強烈的黑/白適應對比
    _CYAN   = NSColor.systemBlueColor()   # Blue 在淺色底比 Teal 清晰
    # 加粗預設字型重量 (原本 0.0 -> 0.3) 讓字型在淺色背景下更顯眼
    _FONT   = NSFont.monospacedSystemFontOfSize_weight_(12.0, 0.3)
    _FONT_S = NSFont.monospacedSystemFontOfSize_weight_(11.0, 0.2)
    _FONT_B = NSFont.monospacedSystemFontOfSize_weight_(12.0, 0.6)
else:
    _GREEN = _YELLOW = _RED = _GRAY = _CYAN = _FONT = _FONT_S = _FONT_B = None


def _set_colored_title(menu_item, text: str, color=None, bold=False, small=False):
    if _HAS_APPKIT and color and hasattr(menu_item, '_menuitem'):
        font = _FONT_S if small else (_FONT_B if bold else _FONT)
        attrs = {
            NSForegroundColorAttributeName: color,
            NSFontAttributeName: font,
        }
        astr = NSAttributedString.alloc().initWithString_attributes_(text, attrs)
        menu_item._menuitem.setAttributedTitle_(astr)
    else:
        menu_item.title = text


def _visual_state(state: str) -> str:
    if state in {"ok", "alive", "idle"}:
        return "ok"
    if state in {"waiting", "starting", "stale", "processing"}:
        return "waiting"
    return "attention"


def _state_icon(state: str) -> str:
    visual_state = _visual_state(state)
    if visual_state == "ok":
        return "🟢"
    if visual_state == "waiting":
        return "🟡"
    return "🔴"


def _state_color(state: str):
    visual_state = _visual_state(state)
    if visual_state == "ok":
        return _GREEN
    if visual_state == "waiting":
        return _YELLOW
    return _RED


def _checks_state(result_ok: dict, check_names: tuple[str, ...]) -> dict:
    missing = []
    failed = []
    present = []
    for name in check_names:
        if name not in result_ok:
            missing.append(name)
        elif bool(result_ok.get(name)):
            present.append(name)
        else:
            failed.append(name)
    if failed:
        return {"state": "attention", "missing": missing, "failed": failed, "present": present}
    if missing:
        return {"state": "waiting", "missing": missing, "failed": failed, "present": present}
    return {"state": "ok", "missing": missing, "failed": failed, "present": present}


def _label_for_state(state: str, ok_label: str) -> str:
    if state == "ok":
        return ok_label
    if state == "waiting":
        return CHECK_WAITING_TEXT
    if state == "idle":
        return "未啟用"
    return ATTENTION_TEXT


def _business_module_status_from_payload(payload: dict) -> dict:
    result_ok = {}
    if isinstance(payload, dict):
        results = payload.get("results") if isinstance(payload.get("results"), list) else []
        for item in results:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            if "ok" in item:
                result_ok[name] = bool(item.get("ok"))
            elif "success" in item:
                result_ok[name] = bool(item.get("success"))

    modules = {}
    for label, checks in BUSINESS_MODULE_CHECKS.items():
        state_info = _checks_state(result_ok, checks)
        state = state_info["state"]
        modules[label] = {
            **state_info,
            "label": _label_for_state(state, OPERATIONAL_TEXT),
        }

    factory = {}
    for label, checks in FACTORY_CHECKS.items():
        state_info = _checks_state(result_ok, checks)
        state = state_info["state"]
        factory[label] = {
            **state_info,
            "label": _label_for_state(state, CHECK_PASSED_TEXT),
        }

    credential = _checks_state(result_ok, ("token_health_refresh",))
    credential_state = credential["state"]
    return {
        "ok": bool(payload.get("ok") if isinstance(payload, dict) else False),
        "result_ok": result_ok,
        "modules": modules,
        "factory": factory,
        "credential": {
            **credential,
            "label": _label_for_state(credential_state, OPERATIONAL_TEXT),
        },
    }


def _business_module_status_failure(reason: str, *, returncode: int | None = None) -> dict:
    """Represent the current live-check round without reusing an older report."""
    status = _business_module_status_from_payload({"ok": False, "results": []})
    for group_name in ("modules", "factory"):
        for info in status.get(group_name, {}).values():
            if isinstance(info, dict):
                info["state"] = "attention"
                info["label"] = "本輪檢查失敗"
    credential = status.get("credential")
    if isinstance(credential, dict):
        credential["state"] = "attention"
        credential["label"] = "本輪檢查失敗"
    status.update({"ok": False, "failed_reason": str(reason or "live_check_failed")[:160]})
    if returncode is not None:
        status["returncode"] = int(returncode)
    return status


def _health_state_from_payload(kind: str, payload: dict, *, age_sec: float | None = None) -> dict:
    spec = HEALTH_ARTIFACTS[kind]
    label = str(spec["label"])
    if not isinstance(payload, dict) or not payload:
        return {"state": "attention", "label": f"{label}無法解析", "detail": "健康報告不存在或不是有效的 JSON。"}
    if age_sec is not None and age_sec > int(spec["max_age_sec"]):
        return {"state": "waiting", "label": f"{label}檢查逾時", "detail": f"健康報告已超過 {round(age_sec / 3600, 1)} 小時未更新。"}
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    requires_human = payload.get("requires_human") if isinstance(payload.get("requires_human"), list) else []
    errors = int(summary.get("error_count") or 0)
    warnings = int(summary.get("warning_count") or 0)
    nested_failed = []
    nested_missing = []
    nested_stale = []
    for container_name in ("health", "runtime_health"):
        container = payload.get(container_name)
        if not isinstance(container, dict):
            continue
        if isinstance(container.get("failed"), list):
            nested_failed.extend(container["failed"])
        if isinstance(container.get("missing"), list):
            nested_missing.extend(container["missing"])
        if isinstance(container.get("stale"), list):
            nested_stale.extend(container["stale"])
    errors = max(
        errors,
        int(summary.get("failed_health_count") or 0)
        + int(summary.get("missing_health_count") or 0),
        len(nested_failed) + len(nested_missing),
    )
    warnings = max(
        warnings,
        int(summary.get("stale_health_count") or 0),
        len(nested_stale),
    )
    if payload.get("ok") is not True or requires_human or errors or warnings:
        reasons = []
        for item in payload.get("unresolved_issue_ids") or []:
            reasons.append(str(item))
        for item in payload.get("failed") or []:
            if isinstance(item, dict):
                reasons.append(f"{item.get('path') or item.get('name') or '檢查項目'}：{item.get('reason') or item.get('detail') or '失敗'}")
        for item in nested_failed + nested_missing + nested_stale:
            if isinstance(item, dict):
                reasons.append(
                    f"{item.get('name') or item.get('path') or '檢查項目'}："
                    f"{item.get('reason') or item.get('detail') or '未通過'}"
                )
            elif item:
                reasons.append(str(item))
        if requires_human:
            reasons.extend(str(item.get("reason") or item) if isinstance(item, dict) else str(item) for item in requires_human)
        detail = "\n".join(dict.fromkeys(reason for reason in reasons if reason))
        if not detail:
            detail = f"錯誤 {errors} 項、警告 {warnings} 項；請開啟健康報告查看。"
        # Warnings and human review items mean the service is still
        # operational.  Reserve red for an active error (or an unclassified
        # failed report), and use amber for review/retry work.
        hard_failure = errors > 0 or (
            payload.get("ok") is not True
            and warnings == 0
            and not requires_human
        )
        state = "attention" if hard_failure else "waiting"
        suffix = "需處理" if hard_failure else "待確認"
        return {"state": state, "label": f"{label}{suffix}", "detail": detail}
    return {"state": "ok", "label": f"{label}通過", "detail": "最近一次健康檢查通過。"}


def _cron_failure_detail(details: list[dict]) -> str:
    lines = []
    for item in details or []:
        status = str(item.get("status") or "")
        if status not in {"failed", "stale", "deferred", "queued"}:
            continue
        state = {
            "failed": "執行失敗",
            "stale": "超過預期時間未執行",
            "deferred": "資源保護延後",
            "queued": "已排入執行佇列",
        }[status]
        if status == "deferred":
            state = {
                "upstream": "上游服務待重試",
                "partial": "部分完成待續跑",
                "semantic_collision": "路徑衝突安全等待",
                "large_offpeak": "大型檔案等待離峰",
                "storage": "儲存裝置待重連",
                "auto_repair": "MAGI 自動修復重試中",
                "resource": "資源保護延後",
                "candidate_rejected": "品質閘門拒絕候選，待確認",
            }.get(str(item.get("wait_reason") or ""), state)
        lines.append(f"{item.get('desc') or item.get('id') or '排程'}：{state}（{item.get('relative') or '時間不明'}）")
    return "\n".join(lines) or "排程摘要未提供進一步原因。"


def _status_detail_text(detail: dict) -> str:
    state_labels = {
        "ok": "功能正常",
        "alive": "功能正常",
        "attention": "需要處理",
        "down": "需要處理",
        "waiting": "等待確認",
        "idle": "待命",
        "stale": "資料逾時",
        "processing": "處理中",
    }
    state = str(detail.get("state") or "waiting")
    reason = str(detail.get("detail") or "未提供進一步原因。").strip()
    detail_heading = "原因" if state in {"attention", "down", "stale"} else "說明"
    return (
        f"項目：{detail.get('title') or '狀態項目'}\n"
        f"狀態：{state_labels.get(state, state)}\n"
        f"顯示：{detail.get('value') or '未提供'}\n\n"
        f"{detail_heading}：\n{reason}"
    )


def _business_readiness_detail(label: str, info: dict) -> str:
    info = info if isinstance(info, dict) else {}
    state = str(info.get("state") or "waiting")
    if label == "案件回報":
        blocked = int(info.get("count") or 0) if state == "attention" else 0
        pending = int(info.get("pending") or 0)
        review_pending = int(info.get("review_pending") or 0)
        attention = int(info.get("attention") or 0)
        branch_pending = int(info.get("branch_pending") or 0)
        lines = []
        if blocked:
            lines.append(f"目前有 {blocked} 件案件的結案回報受阻，需補齊資料或排除錯誤。")
        pending_items = info.get("pending_items") if isinstance(info.get("pending_items"), list) else []
        review_items = info.get("review_items") if isinstance(info.get("review_items"), list) else []
        attention_items = info.get("attention_items") if isinstance(info.get("attention_items"), list) else []
        branch_items = info.get("branch_pending_items") if isinstance(info.get("branch_pending_items"), list) else []
        if attention:
            lines.append(f"法扶官網要求處理（{attention} 件）：")
            for index, item in enumerate(attention_items, 1):
                identity = "｜".join(
                    part for part in (
                        str(item.get("case_number") or "案號未填"),
                        str(item.get("client_name") or "當事人未填"),
                    ) if part
                )
                lines.append(f"{index}. {identity}｜{item.get('approval_status') or '需補正'}")
            if not attention_items:
                lines.append("清單尚在同步，請稍後重新整理。")
        if pending:
            if lines:
                lines.append("")
            lines.append(f"待完成結案回報（{pending} 件）：")
            for index, item in enumerate(pending_items, 1):
                if not isinstance(item, dict):
                    continue
                identity = "｜".join(
                    part
                    for part in (str(item.get("case_number") or "案號未填"), str(item.get("client_name") or "當事人未填"))
                    if part
                )
                status = str(item.get("status") or "等待回報")
                lines.append(f"{index}. {identity}｜{status}")
            if not pending_items:
                lines.append("清單尚在同步，請稍後重新整理。")
            elif pending > len(pending_items):
                lines.append(f"另有 {pending - len(pending_items)} 件未列出。")
        if review_pending:
            if lines:
                lines.append("")
            lines.append(f"待人工確認的逾期事項（{review_pending} 項）：")
            for index, item in enumerate(review_items, 1):
                if not isinstance(item, dict):
                    continue
                identity = "｜".join(
                    part
                    for part in (str(item.get("case_number") or "案號未填"), str(item.get("client_name") or "當事人未填"))
                    if part
                )
                meta = "｜".join(
                    part
                    for part in (
                        f"原期限 {item.get('original_due_date')}" if item.get("original_due_date") else "",
                        str(item.get("original_type") or ""),
                    )
                    if part
                )
                summary = str(item.get("summary") or "請確認是否已辦理")
                lines.append(f"{index}. {identity}{'｜' + meta if meta else ''}")
                lines.append(f"   {summary}")
            if not review_items:
                lines.append("清單尚在同步，請稍後重新整理。")
            elif review_pending > len(review_items):
                lines.append(f"另有 {review_pending - len(review_items)} 項未列出。")
        if branch_pending:
            if lines:
                lines.append("")
            lines.append(f"已送出，分會審核中或已轉入（{branch_pending} 件；不需律師操作）：")
            for index, item in enumerate(branch_items, 1):
                identity = "｜".join(
                    part for part in (
                        str(item.get("case_number") or "案號未填"),
                        str(item.get("client_name") or "當事人未填"),
                    ) if part
                )
                lines.append(f"{index}. {identity}｜{item.get('approval_status') or '待轉入'}")
            if not branch_items:
                lines.append("清單尚在同步，請稍後重新整理。")
        if not lines:
            lines.append("目前沒有待完成的案件回報或待確認事項。")
        if state == "waiting" and not pending_items and not review_items:
            lines.append("這些是業務待辦，不代表 MAGI 功能故障。")
        return "\n".join(lines)

    if label == "法扶附件":
        missing = int(info.get("missing") or 0)
        pending_retry = int(info.get("pending_retry") or 0)
        manual_review = int(info.get("manual_review") or 0)
        retry_items = info.get("retry_items") if isinstance(info.get("retry_items"), list) else []
        missing_items = info.get("missing_items") if isinstance(info.get("missing_items"), list) else []
        lines = []
        if info.get("scan_error"):
            lines.append("法扶入口下載清單本次讀取失敗，MAGI 已保留既有欠檔狀態，不會誤判為附件齊全。")
            lines.append(f"原因：{info.get('scan_error')}")
        if missing:
            lines.append(f"法扶網站仍有 {missing} 份附件尚未歸檔：")
            for index, item in enumerate(missing_items, 1):
                identity = "｜".join(
                    part for part in (str(item.get("laf_case_number") or "法扶案號未填"), str(item.get("client_name") or "當事人未填")) if part
                )
                files = "、".join(str(name) for name in (item.get("missing_files") or [])) or "附件名稱未提供"
                lines.append(f"{index}. {identity}")
                lines.append(f"   尚缺：{files}")
            if not missing_items:
                lines.append("附件明細尚在同步，請稍後重新整理。")
        if retry_items:
            if lines:
                lines.append("")
            automatic_items = [item for item in retry_items if item.get("status") == "自動重試中"]
            manual_items = [item for item in retry_items if item.get("status") == "需人工確認"]

            def append_retry_items(items, heading):
                if not items:
                    return
                lines.append(f"{heading}（{len(items)} 件）：")
                for index, item in enumerate(items, 1):
                    identity = "｜".join(
                        part
                        for part in (
                            str(item.get("case_number") or "OSC案號未填"),
                            str(item.get("laf_case_number") or "法扶案號未填"),
                            str(item.get("client_name") or "當事人未填"),
                        )
                        if part
                    )
                    case_kind = "／".join(part for part in (str(item.get("case_type") or ""), str(item.get("case_reason") or "")) if part)
                    lines.append(f"{index}. {identity}")
                    if case_kind:
                        lines.append(f"   案件：{case_kind}")
                    lines.append(f"   狀況：{item.get('reason') or '附件尚未取得'}")
                    attempt = f"已重試 {int(item.get('tries') or 0)} 次"
                    if item.get("last_try_at"):
                        attempt += f"；最後檢查 {item.get('last_try_at')}"
                    lines.append(f"   {attempt}")
                    if item.get("expires_at"):
                        lines.append(f"   MAGI 自動補抓觀察期限：{item.get('expires_at')}")

            append_retry_items(automatic_items, "期限內自動補抓")
            if automatic_items and manual_items:
                lines.append("")
            append_retry_items(manual_items, "需要處理")
            if pending_retry:
                lines.append("")
                lines.append("MAGI 下一步：每小時自動重試；附件出現後會下載並歸檔，再更新本清單。")
            if manual_review:
                lines.append("標示「需人工確認」的案件不會繼續盲目重試，需先修正案件資料或登入問題。")
        elif pending_retry or manual_review:
            lines.append("案件明細尚在同步，請稍後重新整理。")
        return "\n".join(lines) or "目前法扶附件均已取得。"

    if label == "閱卷下載":
        if state == "attention":
            reason = str(info.get("failure_reason") or "").strip().lower()
            if reason == "portal_downloadable_not_reconciled":
                try:
                    expected = max(
                        0, int(info.get("expected_portal_downloadable_count") or 0)
                    )
                except (TypeError, ValueError):
                    expected = 0
                try:
                    accounted = max(
                        0, int(info.get("accounted_portal_downloadable_count") or 0)
                    )
                except (TypeError, ValueError):
                    accounted = 0
                return (
                    f"入口偵測{expected}件、已驗證{accounted}件，本輪簽章未完全對上，"
                    "MAGI將自動重試；未完成前維持紅燈。"
                )
            if not bool(info.get("auto_download")):
                return "閱卷入口目前只會掃描，不會自動下載檔案。"
            # Never echo an untrusted worker error into the menu.  Unknown or
            # newly introduced failure codes remain actionable without
            # exposing a case number, local path, token, or raw exception.
            return "閱卷下載上輪未完成，MAGI將自動重試；未完成前維持紅燈。"
        ready = int(info.get("ready_to_download") or 0)
        pending_payment = int(info.get("pending_payment") or 0)
        court_payload_waiting_raw = info.get("court_payload_waiting")
        court_payload_waiting = (
            court_payload_waiting_raw
            if type(court_payload_waiting_raw) is int
            and court_payload_waiting_raw >= 0
            else 0
        )
        ready_items = info.get("ready_items") if isinstance(info.get("ready_items"), list) else []
        if not bool(info.get("auto_download")):
            return "閱卷入口目前只會掃描，不會自動下載檔案。"
        if court_payload_waiting:
            return (
                f"法院入口有 {court_payload_waiting} 件下載列曾回傳其他案件卷宗；"
                "MAGI 已隔離錯案檔案，並會在法院列資料更新或安全冷卻到期後自動重驗。"
                "目前不需要人工重傳。"
            )
        if pending_payment:
            return (
                f"法院入口現有 {pending_payment} 件繳費待處理。\n"
                "MAGI 會自動下載繳費單、歸檔並通知；只有實際處理完成後才會從清單移除。"
            )
        if ready:
            lines = [f"待自動下載的閱卷資料（{ready} 件）："]
            for index, item in enumerate(ready_items, 1):
                identity = "｜".join(
                    part
                    for part in (
                        str(item.get("case_number") or item.get("laf_case_number") or item.get("application_no") or "案號未填"),
                        str(item.get("client_name") or "當事人未填"),
                        str(item.get("court") or ""),
                    )
                    if part
                )
                lines.append(f"{index}. {identity}")
            if not ready_items:
                lines.append("案件明細尚在同步，請稍後重新整理。")
            lines.append("MAGI 下一步：背景下載完成並歸檔後會更新本清單。")
            return "\n".join(lines)
        return "閱卷資料自動掃描與下載功能正常，目前沒有待下載檔案。"

    if label in {"筆錄下載", "錄音轉文字"}:
        eligible = int(info.get("eligible_cases") or 0)
        scanned = int(info.get("cycle_scanned_cases") or 0)
        remaining = int(info.get("remaining_cases") or 0)
        retry = int(info.get("retry_pending_cases") or 0)
        failed = int(info.get("failed_cases") or 0)
        lines = ["這是司法院電子筆錄入口的自動掃描與下載，不是錄音轉文字作業。"]
        if eligible:
            lines.append(f"本輪已掃描 {scanned}/{eligible} 案，尚餘 {remaining} 案；系統會自動繼續。")
        if retry:
            lines.append(f"另有 {retry} 案入口結果由 MAGI 自動復核，不需要人工處理。")
        if failed:
            lines.append(f"目前有 {failed} 案同步失敗需處理。")
        return "\n".join(lines)

    if label == "NVIDIA重型":
        if info.get("enabled") is False:
            return (
                "NVIDIA 高階雲端模型是選配，目前未啟用；"
                "MAGI 的本機模型與一般業務功能仍正常運作。"
            )
        model = str(info.get("model") or "")
        if model:
            return f"重型指令目前會交由 NVIDIA 高階模型處理。\n目前模型：{model}"
        return "NVIDIA 高階模型已啟用，但尚未設定模型名稱，請檢查設定。"

    visible = str(info.get("label") or CHECK_WAITING_TEXT)
    return f"目前狀態：{visible}。"


def _credential_detail(info: dict) -> str:
    state = str(info.get("state") or "waiting") if isinstance(info, dict) else "waiting"
    if state in {"ok", "alive"}:
        return "MAGI 使用中的登入憑證與授權均通過最近一次檢查。"
    if state == "waiting":
        return "憑證檢查尚未完成，MAGI 會在下一輪檢查後更新狀態。"
    return "至少一項登入憑證或授權未通過檢查，可能影響需要登入外部服務的功能。"


def _runtime_health_states() -> dict:
    runtime_dir = _runtime_dir_path()
    states = {}
    for kind, spec in HEALTH_ARTIFACTS.items():
        candidates = []
        for filename in spec["paths"]:
            path = os.path.join(runtime_dir, filename)
            if os.path.isfile(path):
                try:
                    candidates.append((os.path.getmtime(path), path))
                except OSError:
                    continue
        if not candidates:
            states[kind] = {"state": "waiting", "label": f"{spec['label']}等待檢查", "path": ""}
            continue
        checked_epoch, path = max(candidates)
        state = _health_state_from_payload(kind, _load_json_file(path), age_sec=max(0.0, time.time() - checked_epoch))
        state.update({"path": path, "checked_epoch": checked_epoch})
        states[kind] = state
    return states


def _business_readiness_live() -> dict:
    path = _mutable_static_path("business_readiness_latest.json")
    payload = _load_json_file(path)
    items = payload.get("items") if isinstance(payload.get("items"), dict) else {}
    # Rolling-upgrade compatibility: an old snapshot used the misleading
    # audio-transcription label for the court transcript downloader.
    if "錄音轉文字" in items and "筆錄下載" not in items:
        items["筆錄下載"] = items.pop("錄音轉文字")
    generated_epoch = _epoch_from_iso(payload.get("generated_at", ""))
    if not items:
        return {
            "state": "waiting",
            "summary": {"attention": 0, "waiting": 5, "ok": 0},
            "items": {
                label: {"state": "waiting", "label": CHECK_WAITING_TEXT}
                for label in ("案件回報", "法扶附件", "閱卷下載", "筆錄下載", "NVIDIA重型")
            },
            "path": path,
            "generated_epoch": generated_epoch,
        }
    age_sec = max(0.0, time.time() - generated_epoch) if generated_epoch else None
    if age_sec is None or age_sec > 30 * 60:
        for info in items.values():
            if isinstance(info, dict):
                info["state"] = "attention"
                info["label"] = "快照逾時"
        payload["state"] = "attention"
    payload.update({"items": items, "path": path, "generated_epoch": generated_epoch, "age_sec": age_sec})
    return payload


def _monitor_display_state(info: dict, compact) -> tuple[str, str]:
    if not isinstance(info, dict):
        return "waiting", CHECK_WAITING_TEXT
    state = str(info.get("state") or ("alive" if info.get("alive") else "down"))
    if state == "alive":
        return "ok", OPERATIONAL_TEXT
    detail = str(info.get("detail") or "").strip()
    if state in {"starting", "stale", "idle", "waiting"}:
        return "waiting", compact(detail or "待確認", 14)
    return "attention", compact(detail or ATTENTION_TEXT, 14)


def _business_module_status_live() -> dict:
    candidates = []
    try:
        candidates.append(os.path.join(_runtime_dir_path(), "business_module_live_check_latest.json"))
    except Exception:
        pass
    candidates.append(os.path.join(MAGI_ROOT, ".runtime", "business_module_live_check_latest.json"))

    seen = set()
    for path in candidates:
        if not path or path in seen:
            continue
        seen.add(path)
        if not os.path.isfile(path):
            continue
        payload = _load_json_file(path)
        status = _business_module_status_from_payload(payload)
        status["path"] = path
        try:
            checked_epoch = os.path.getmtime(path)
            status["checked_epoch"] = checked_epoch
            status["checked_at"] = datetime.fromtimestamp(checked_epoch).strftime("%H:%M")
            cron = next((job for job in _load_cron_jobs() if str(job.get("id") or "") == BUSINESS_LIVE_JOB_ID), {})
            threshold_h = _cron_stale_threshold_hours(str(cron.get("cron") or ""))
            age_h = max(0.0, (time.time() - checked_epoch) / 3600.0)
            status["age_hours"] = round(age_h, 3)
            status["stale"] = age_h > threshold_h
            if status["stale"]:
                for group_name in ("modules", "factory"):
                    for info in (status.get(group_name) or {}).values():
                        if isinstance(info, dict):
                            info["state"] = "attention"
                            info["label"] = "檢查逾時"
                credential = status.get("credential")
                if isinstance(credential, dict):
                    credential["state"] = "attention"
                    credential["label"] = "檢查逾時"
        except OSError:
            status["checked_at"] = ""
            status["age_hours"] = None
            status["stale"] = False
        return status

    status = _business_module_status_from_payload({})
    status["path"] = ""
    status["checked_at"] = ""
    status["checked_epoch"] = 0.0
    status["age_hours"] = None
    status["stale"] = False
    return status


def _module_group_state(modules: dict) -> str:
    states = [str(info.get("state") or "waiting") for info in (modules or {}).values()]
    if not states:
        return "waiting"
    if any(state == "attention" for state in states):
        return "attention"
    if any(state == "waiting" for state in states):
        return "waiting"
    return "ok"


def _state_from_infos(infos) -> str:
    states = [str(info.get("state") or "waiting") for info in infos if isinstance(info, dict)]
    if not states:
        return "waiting"
    if any(state in {"attention", "down", "failed"} for state in states):
        return "attention"
    if any(state not in {"ok", "alive"} for state in states):
        return "waiting"
    return "ok"


def _nas_state(nas: dict) -> str:
    if not isinstance(nas, dict) or not nas:
        return "waiting"
    shares = nas.get("shares") if isinstance(nas.get("shares"), dict) else {}
    if shares and any(not bool(info.get("mounted")) for info in shares.values() if isinstance(info, dict)):
        return "attention"
    if (nas.get("lan") or nas.get("vpn") or nas.get("synology_drive")) and nas.get("mounted"):
        return "ok"
    if nas.get("mounted") or nas.get("lan") or nas.get("vpn"):
        return "waiting"
    return "attention"


def _model_expected(name: str, profile_info: dict) -> bool:
    expected_profile = str(profile_info.get("expected_profile") or "") if isinstance(profile_info, dict) else ""
    if expected_profile == "night":
        return name in {"文字推理", "向量嵌入"}
    return True


def _model_state(c: dict) -> str:
    profile = c.get("omlx_profile", {}) if isinstance(c, dict) else {}
    engines = c.get("engines", {}) if isinstance(c, dict) else {}
    text_status = profile.get("text_status", {}) if isinstance(profile, dict) else {}
    if not isinstance(text_status, dict) or not text_status:
        return "waiting"
    if text_status.get("transitioning"):
        return "waiting"
    if text_status.get("mismatch"):
        return "attention"
    if text_status.get("degraded"):
        return "waiting"
    if not text_status.get("label"):
        return "attention"
    if not isinstance(engines, dict):
        return "waiting"
    for name, _port in OMLX_ENGINES:
        if _model_expected(name, profile) and not engines.get(name):
            return "attention"
    return "ok"


def _cron_summary(enabled_count: int, cron_bot: bool, details: list[dict]) -> dict:
    failed = sum(1 for detail in details if detail.get("status") == "failed")
    stale = sum(1 for detail in details if detail.get("status") == "stale")
    deferred = sum(1 for detail in details if detail.get("status") == "deferred")
    queued = sum(1 for detail in details if detail.get("status") == "queued")
    pending = deferred + queued
    # A storage-deferred cron occurrence is already guarded by the dedicated
    # live NAS/Drive status module and will resume on its next bounded run.  Do
    # not duplicate that condition as a human-confirmation yellow light here;
    # an actually unavailable mount remains visible in the network-drive row.
    automatic_wait_reasons = {
        "upstream",
        "partial",
        "large_offpeak",
        "resource",
        "storage",
        "auto_repair",
        "candidate_rejected",
    }
    deferred_requiring_attention = sum(
        1
        for detail in details
        if detail.get("status") == "deferred"
        and str(detail.get("wait_reason") or "") not in automatic_wait_reasons
    )
    if enabled_count <= 0:
        return {"state": "waiting", "label": "讀取失敗", "failed": failed, "stale": stale}
    parts = [f"{enabled_count}個啟用"]
    if failed:
        parts.append(f"{failed}個失敗")
    if stale:
        parts.append(f"{stale}個逾時")
    if pending:
        parts.append(
            f"{pending}個延後"
            if deferred_requiring_attention
            else f"{pending}個待續跑"
        )
    if failed:
        state = "attention"
    elif stale or deferred_requiring_attention:
        state = "waiting"
    elif not cron_bot:
        parts.append("Bot停止")
        state = "attention"
    else:
        parts.append(OPERATIONAL_TEXT)
        state = "ok"
    return {
        "state": state,
        "label": "・".join(parts),
        "failed": failed,
        "stale": stale,
        "deferred": deferred,
        "queued": queued,
        "pending": pending,
        "deferred_requiring_attention": deferred_requiring_attention,
    }


def _task_module_row_geometry(index: int) -> tuple[int, int]:
    return TASK_MODULE_ROW_Y + int(index) * TASK_MODULE_ROW_STEP, TASK_MODULE_ROW_HEIGHT


def _service_state(c: dict, *names: str) -> str:
    services = c.get("services", {}) if isinstance(c, dict) else {}
    return "ok" if _service_alive(services, *names) else "attention"


def _factory_state(c: dict, label: str) -> dict:
    business_live = c.get("business_live", {}) if isinstance(c, dict) else {}
    factory = business_live.get("factory", {}) if isinstance(business_live, dict) else {}
    info = factory.get(label) if isinstance(factory, dict) else None
    if isinstance(info, dict):
        return info
    return {"state": "waiting", "label": CHECK_WAITING_TEXT}


def _format_live_events(c: dict, now: datetime | None = None, limit: int = LIVE_LOG_DISPLAY_MAX) -> list[dict]:
    actual_events = c.get("live_events") if isinstance(c, dict) else None
    if isinstance(actual_events, list):
        events = [item for item in actual_events if isinstance(item, dict)]
        if events:
            return events[-limit:]
    return [{"time": "--:--", "source": "即時紀錄", "state": "waiting", "label": "無可解析紀錄"}]


def _overall_state(c: dict) -> str:
    if not isinstance(c, dict) or not c:
        return "waiting"

    services = c.get("services", {})
    if any(not bool(services.get(name)) for name, _ in SERVICES):
        return "attention"

    db = c.get("db", {})
    if isinstance(db, dict) and db.get("local") is False:
        return "attention"

    process_health = c.get("process_health", {}) if isinstance(c, dict) else {}
    process_summary = process_health.get("summary", {}) if isinstance(process_health, dict) else {}
    if process_health and process_health.get("ok") is not True:
        return "attention"
    anomaly_count = int(process_summary.get("anomaly_count", 0) or 0)
    if not process_health:
        zombies, _detail = c.get("zombies", (0, ""))
        anomaly_count = int(zombies or 0)
    if anomaly_count > 0:
        return "attention"

    business_live = c.get("business_live", {})
    modules = business_live.get("modules", {}) if isinstance(business_live, dict) else {}
    factory = business_live.get("factory", {}) if isinstance(business_live, dict) else {}
    credential = business_live.get("credential", {}) if isinstance(business_live, dict) else {}
    health = c.get("health", {}) if isinstance(c, dict) else {}
    monitors = c.get("monitors", {}) if isinstance(c, dict) else {}
    cron_summary = c.get("cron_summary", {}) if isinstance(c, dict) else {}

    states = [
        _module_group_state(modules),
        _state_from_infos(factory.values() if isinstance(factory, dict) else []),
        _state_from_infos([credential]),
        str(cron_summary.get("state") or "waiting") if isinstance(cron_summary, dict) else "waiting",
        _state_from_infos(health.values() if isinstance(health, dict) else []),
        _nas_state(c.get("nas", {})),
        _model_state(c),
        _state_from_infos(monitors.values() if isinstance(monitors, dict) else []),
    ]
    if any(state in {"attention", "down", "failed"} for state in states):
        return "attention"
    if any(state != "ok" for state in states):
        return "waiting"
    return "ok"


if _HAS_APPKIT:
    class _CockpitFullscreenWindow(NSWindow):
        """Borderless cockpit window that can still receive Escape and clicks."""

        def canBecomeKeyWindow(self):
            return True

        def canBecomeMainWindow(self):
            return True


    class _StatusItemClickTarget(NSObject):
        """Route the status-item click directly into the panoramic cockpit."""

        def initWithController_(self, controller):
            self = objc.super(_StatusItemClickTarget, self).init()
            if self is None:
                return None
            self._controller = controller
            return self

        def toggleCockpit_(self, _sender):
            controller = getattr(self, "_controller", None)
            if controller is not None:
                controller._toggle_cockpit_fullscreen()


    class _CockpitWallpaperView(NSView):
        """Draw the cached active Apple desktop still behind the cockpit HUD."""

        def initWithFrame_(self, frame):
            self = objc.super(_CockpitWallpaperView, self).initWithFrame_(frame)
            if self is None:
                return None
            self._source = {"path": "", "kind": "fallback"}
            self._last_source_check = 0.0
            self._image = None
            self._player = None
            self._player_item = None
            self._player_looper = None
            self._player_layer = None
            self._dashboard_view = None
            try:
                self.setWantsLayer_(True)
            except Exception:
                pass
            self.refresh_source(force=True)
            return self

        def isOpaque(self):
            return False

        @objc.python_method
        def has_wallpaper(self) -> bool:
            return self._image is not None

        @objc.python_method
        def set_dashboard_view(self, dashboard_view) -> None:
            self._dashboard_view = dashboard_view
            self._publish_active_state()

        @objc.python_method
        def _publish_active_state(self) -> None:
            dashboard = getattr(self, "_dashboard_view", None)
            if dashboard is None:
                return
            try:
                dashboard.set_wallpaper_active(
                    bool(self._image is not None or self._player_layer is not None)
                )
            except Exception:
                pass

        @objc.python_method
        def _stop_player(self) -> None:
            try:
                if self._player is not None:
                    self._player.pause()
            except Exception:
                pass
            try:
                if self._player_layer is not None:
                    self._player_layer.removeFromSuperlayer()
            except Exception:
                pass
            self._player = None
            self._player_item = None
            self._player_looper = None
            self._player_layer = None

        @objc.python_method
        def refresh_source(self, force: bool = False) -> dict:
            now = time.monotonic()
            if not force and now - float(self._last_source_check or 0.0) < 60.0:
                return dict(self._source)
            self._last_source_check = now
            source = _resolve_apple_wallpaper_source()
            current_key = (str(self._source.get("path") or ""), str(self._source.get("kind") or ""))
            source_key = (str(source.get("path") or ""), str(source.get("kind") or ""))
            already_loaded = bool(
                self._image is not None
                or self._player_layer is not None
                or source_key[1] == "fallback"
            )
            if not force and current_key == source_key and already_loaded:
                return dict(self._source)

            self._stop_player()
            self._image = None
            self._source = dict(source)
            display_path = _cached_wallpaper_preview(source)
            if display_path:
                try:
                    image = NSImage.alloc().initWithContentsOfFile_(display_path)
                    self._image = image if image is not None else None
                except Exception:
                    self._image = None
            self._publish_active_state()
            self.setNeedsDisplay_(True)
            return dict(self._source)

        @objc.python_method
        def start(self) -> None:
            self.refresh_source()
            try:
                if self._player is not None:
                    # Apple's Aerial assets are high-frame-rate slow-motion
                    # masters.  Quarter speed preserves their intended motion
                    # while keeping the cockpit's GPU demand bounded.
                    self._player.play()
                    self._player.setRate_(0.25)
            except Exception:
                logging.getLogger("menubar").warning(
                    "Apple dynamic wallpaper failed to start",
                    exc_info=True,
                )

        @objc.python_method
        def pause(self) -> None:
            try:
                if self._player is not None:
                    self._player.pause()
            except Exception:
                pass

        def setFrame_(self, frame):
            objc.super(_CockpitWallpaperView, self).setFrame_(frame)
            try:
                if self._player_layer is not None:
                    self._player_layer.setFrame_(self.bounds())
            except Exception:
                pass

        @objc.python_method
        def draw_current_wallpaper(self, bounds) -> bool:
            """Aspect-fill the active still in the caller's graphics context.

            AppKit does not reliably composite transparent drawing in one
            sibling NSView against an image drawn by another sibling NSView.
            The cockpit therefore calls this method before drawing its HUD so
            the wallpaper and translucent controls share one graphics layer.
            """
            image = getattr(self, "_image", None)
            if image is None:
                return False
            size = image.size()
            target_w = max(1.0, float(bounds.size.width))
            target_h = max(1.0, float(bounds.size.height))
            source_w = max(1.0, float(size.width))
            source_h = max(1.0, float(size.height))
            target_ratio = target_w / target_h
            source_ratio = source_w / source_h
            if source_ratio > target_ratio:
                crop_w = source_h * target_ratio
                source_rect = NSMakeRect((source_w - crop_w) / 2.0, 0, crop_w, source_h)
            else:
                crop_h = source_w / target_ratio
                source_rect = NSMakeRect(0, (source_h - crop_h) / 2.0, source_w, crop_h)
            image.drawInRect_fromRect_operation_fraction_(
                bounds,
                source_rect,
                NSCompositingOperationSourceOver,
                1.0,
            )
            return True

        def drawRect_(self, rect):
            bounds = self.bounds()
            NSColor.blackColor().setFill()
            NSBezierPath.bezierPathWithRect_(bounds).fill()
            self.draw_current_wallpaper(bounds)


    class _CockpitDashboardView(NSView):
        """Full-screen circular cockpit status dashboard."""

        def initWithFrame_(self, frame):
            self = objc.super(_CockpitDashboardView, self).initWithFrame_(frame)
            if self is None:
                return None
            self._status_cache = {}
            self._controller = None
            self._button_regions = []
            self._status_regions = []
            self._overlay_regions = []
            self._detail_overlay = None
            self._detail_page = 0
            self._action_notice = ""
            self._theme = _load_menubar_theme()
            self._fullscreen_mode = True
            self._wallpaper_active = False
            self._wallpaper_view = None
            return self

        def isFlipped(self):
            return True

        def isOpaque(self):
            # Required for NSVisualEffectView's behind-window material to be
            # visible through untouched dashboard pixels.
            return False

        @objc.python_method
        def update_status(self, cache: dict) -> None:
            self._status_cache = dict(cache or {})
            try:
                self.setNeedsDisplay_(True)
            except Exception:
                pass

        @objc.python_method
        def set_controller(self, controller) -> None:
            self._controller = controller

        @objc.python_method
        def set_action_notice(self, text: str) -> None:
            self._action_notice = str(text or "")[:32]
            try:
                self.setNeedsDisplay_(True)
            except Exception:
                pass

        @objc.python_method
        def set_theme(self, theme: str) -> None:
            self._theme = _normalize_menubar_theme(theme)
            try:
                self.setNeedsDisplay_(True)
            except Exception:
                pass

        @objc.python_method
        def set_wallpaper_view(self, wallpaper_view) -> None:
            self._wallpaper_view = wallpaper_view

        @objc.python_method
        def set_wallpaper_active(self, active: bool) -> None:
            self._wallpaper_active = bool(active)
            try:
                self.setNeedsDisplay_(True)
            except Exception:
                pass

        @objc.python_method
        def show_status_detail(self, detail: dict) -> None:
            self._detail_overlay = dict(detail or {})
            self._detail_page = 0
            try:
                self.setNeedsDisplay_(True)
            except Exception:
                pass

        @objc.python_method
        def hide_status_detail(self) -> None:
            self._detail_overlay = None
            self._detail_page = 0
            try:
                self.setNeedsDisplay_(True)
            except Exception:
                pass

        @objc.python_method
        def _detail_pages(self, detail: dict) -> list[str]:
            text = str(detail.get("detail") or "未提供進一步原因。").strip()
            wrapped: list[str] = []
            for raw in text.splitlines() or [text]:
                if not raw:
                    wrapped.append("")
                    continue
                wrapped.extend(
                    textwrap.wrap(
                        raw,
                        width=66,
                        replace_whitespace=False,
                        drop_whitespace=True,
                    )
                    or [raw]
                )
            page_size = 13
            return [
                "\n".join(wrapped[index:index + page_size])
                for index in range(0, max(len(wrapped), 1), page_size)
            ] or [""]

        @objc.python_method
        def _point_in_rect(self, point, rect) -> bool:
            x, y, w, h = rect
            return x <= point.x <= x + w and y <= point.y <= y + h

        def mouseDown_(self, event):
            point = self.convertPoint_fromView_(event.locationInWindow(), None)
            if getattr(self, "_detail_overlay", None):
                for rect, action in getattr(self, "_overlay_regions", []):
                    if not self._point_in_rect(point, rect):
                        continue
                    if action == "overlay_close":
                        self.hide_status_detail()
                    elif action == "overlay_copy":
                        text = _status_detail_text(self._detail_overlay)
                        pasteboard = NSPasteboard.generalPasteboard()
                        pasteboard.clearContents()
                        pasteboard.setString_forType_(text, NSPasteboardTypeString)
                        self.set_action_notice("狀態內容已複製")
                    elif action == "overlay_prev":
                        self._detail_page = max(0, int(self._detail_page) - 1)
                        self.setNeedsDisplay_(True)
                    elif action == "overlay_next":
                        pages = self._detail_pages(self._detail_overlay)
                        self._detail_page = min(len(pages) - 1, int(self._detail_page) + 1)
                        self.setNeedsDisplay_(True)
                    return
                # The modal layer consumes background clicks so controls under
                # it cannot be triggered accidentally.
                return
            for rect, detail in getattr(self, "_status_regions", []):
                if self._point_in_rect(point, rect):
                    controller = getattr(self, "_controller", None)
                    if controller is not None:
                        controller._show_dashboard_status_detail(detail)
                    return
            for rect, action in getattr(self, "_button_regions", []):
                if self._point_in_rect(point, rect):
                    controller = getattr(self, "_controller", None)
                    if controller is not None:
                        controller._handle_dashboard_action(action)
                    return
            objc.super(_CockpitDashboardView, self).mouseDown_(event)

        def acceptsFirstResponder(self):
            return True

        def keyDown_(self, event):
            try:
                if int(event.keyCode()) == 53 or str(event.characters() or "") == "\x1b":
                    if getattr(self, "_detail_overlay", None):
                        self.hide_status_detail()
                        return
                    controller = getattr(self, "_controller", None)
                    if controller is not None:
                        controller._hide_cockpit_fullscreen()
                    return
            except Exception:
                pass
            objc.super(_CockpitDashboardView, self).keyDown_(event)

        @objc.python_method
        def _color(self, value: str, alpha: float = 1.0):
            value = value.strip().lstrip("#")
            palette = MENUBAR_TECH_PALETTES.get(
                _normalize_menubar_theme(getattr(self, "_theme", "cyber")),
                MENUBAR_TECH_PALETTES["cyber"],
            )
            value = palette.get(value.upper(), value)
            r = int(value[0:2], 16) / 255.0
            g = int(value[2:4], 16) / 255.0
            b = int(value[4:6], 16) / 255.0
            return NSColor.colorWithCalibratedRed_green_blue_alpha_(r, g, b, alpha)

        @objc.python_method
        def _font(self, size: float, weight: float = 0.35, mono: bool = False):
            if mono:
                return NSFont.monospacedSystemFontOfSize_weight_(size, weight)
            return NSFont.systemFontOfSize_weight_(size, weight)

        @objc.python_method
        def _draw_text(self, text: str, x: float, y: float, w: float, h: float, *, size=12,
                       color=None, weight=0.35, align=NSLeftTextAlignment, mono=False):
            style = NSMutableParagraphStyle.alloc().init()
            style.setAlignment_(align)
            attrs = {
                NSForegroundColorAttributeName: color or self._color("E6F7F4"),
                NSFontAttributeName: self._font(size, weight, mono=mono),
                NSParagraphStyleAttributeName: style,
            }
            NSString.stringWithString_(str(text)).drawInRect_withAttributes_(
                NSMakeRect(x, y, w, h),
                attrs,
            )

        @objc.python_method
        def _rounded(self, x: float, y: float, w: float, h: float, r: float, *,
                     fill=None, stroke=None, line_width: float = 1.0):
            path = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                NSMakeRect(x, y, w, h),
                r,
                r,
            )
            if fill is not None:
                fill.setFill()
                path.fill()
            if stroke is not None:
                stroke.setStroke()
                path.setLineWidth_(line_width)
                path.stroke()

        @objc.python_method
        def _chamfered(self, x: float, y: float, w: float, h: float, cut: float = 7, *,
                       fill=None, stroke=None, line_width: float = 1.0):
            if getattr(self, "_theme", "cyber") == "forest":
                self._rounded(
                    x,
                    y,
                    w,
                    h,
                    max(8.0, min(16.0, float(cut) + 3.0)),
                    fill=fill,
                    stroke=stroke,
                    line_width=line_width,
                )
                return
            cut = max(0.0, min(float(cut), w / 3.0, h / 3.0))
            path = NSBezierPath.bezierPath()
            path.moveToPoint_(NSMakePoint(x + cut, y))
            path.lineToPoint_(NSMakePoint(x + w - cut, y))
            path.lineToPoint_(NSMakePoint(x + w, y + cut))
            path.lineToPoint_(NSMakePoint(x + w, y + h - cut))
            path.lineToPoint_(NSMakePoint(x + w - cut, y + h))
            path.lineToPoint_(NSMakePoint(x + cut, y + h))
            path.lineToPoint_(NSMakePoint(x, y + h - cut))
            path.lineToPoint_(NSMakePoint(x, y + cut))
            path.closePath()
            if fill is not None:
                fill.setFill()
                path.fill()
            if stroke is not None:
                stroke.setStroke()
                path.setLineWidth_(line_width)
                path.stroke()

        @objc.python_method
        def _line(self, x1: float, y1: float, x2: float, y2: float, color, width: float = 1.0):
            path = NSBezierPath.bezierPath()
            path.moveToPoint_(NSMakePoint(x1, y1))
            path.lineToPoint_(NSMakePoint(x2, y2))
            color.setStroke()
            path.setLineWidth_(width)
            path.stroke()

        @objc.python_method
        def _gradient(self, x: float, y: float, w: float, h: float, start, end, angle: float = 90):
            gradient = NSGradient.alloc().initWithStartingColor_endingColor_(start, end)
            gradient.drawInRect_angle_(NSMakeRect(x, y, w, h), angle)

        @objc.python_method
        def _glow_line(self, x1: float, y1: float, x2: float, y2: float, color, width: float = 1.0):
            self._line(x1, y1, x2, y2, color.colorWithAlphaComponent_(0.07), width + 6.0)
            self._line(x1, y1, x2, y2, color.colorWithAlphaComponent_(0.16), width + 3.0)
            self._line(x1, y1, x2, y2, color, width)

        @objc.python_method
        def _hex(self, x: float, y: float, size: float, *, fill=None, stroke=None, line_width: float = 1.0):
            half = size / 2.0
            quarter = size / 4.0
            path = NSBezierPath.bezierPath()
            path.moveToPoint_(NSMakePoint(x + quarter, y))
            path.lineToPoint_(NSMakePoint(x + size - quarter, y))
            path.lineToPoint_(NSMakePoint(x + size, y + half))
            path.lineToPoint_(NSMakePoint(x + size - quarter, y + size))
            path.lineToPoint_(NSMakePoint(x + quarter, y + size))
            path.lineToPoint_(NSMakePoint(x, y + half))
            path.closePath()
            if fill is not None:
                fill.setFill()
                path.fill()
            if stroke is not None:
                stroke.setStroke()
                path.setLineWidth_(line_width)
                path.stroke()

        @objc.python_method
        def _tech_tag(self, text: str, x: float, y: float, w: float, color):
            self._chamfered(
                x,
                y,
                w,
                16,
                4,
                fill=color.colorWithAlphaComponent_(0.10),
                stroke=color.colorWithAlphaComponent_(0.46),
                line_width=0.65,
            )
            self._draw_text(
                str(text),
                x,
                y + 2,
                w,
                12,
                size=8.2,
                color=color,
                weight=0.72,
                align=NSCenterTextAlignment,
                mono=True,
            )

        @objc.python_method
        def _orbit(self, cx: float, cy: float, radius: float, color):
            for offset, alpha, width in ((0, 0.34, 0.8), (7, 0.16, 0.65), (14, 0.08, 0.55)):
                ring = NSBezierPath.bezierPathWithOvalInRect_(
                    NSMakeRect(
                        cx - radius - offset,
                        cy - radius * 0.34 - offset * 0.34,
                        (radius + offset) * 2,
                        (radius * 0.34 + offset * 0.34) * 2,
                    )
                )
                color.colorWithAlphaComponent_(alpha).setStroke()
                ring.setLineWidth_(width)
                ring.stroke()
            self._hex(
                cx + radius - 2,
                cy - 3,
                6,
                fill=color.colorWithAlphaComponent_(0.88),
            )

        @objc.python_method
        def _dot(self, x: float, y: float, r: float, color):
            color.setFill()
            NSBezierPath.bezierPathWithOvalInRect_(NSMakeRect(x, y, r * 2, r * 2)).fill()

        @objc.python_method
        def _hud_brackets(self, x: float, y: float, w: float, h: float, color, arm: float = 18):
            if getattr(self, "_theme", "cyber") == "forest":
                return
            self._line(x, y + arm, x, y, color, 1.4)
            self._line(x, y, x + arm, y, color, 1.4)
            self._line(x + w - arm, y, x + w, y, color, 1.4)
            self._line(x + w, y, x + w, y + arm, color, 1.4)
            self._line(x, y + h - arm, x, y + h, color, 1.4)
            self._line(x, y + h, x + arm, y + h, color, 1.4)
            self._line(x + w - arm, y + h, x + w, y + h, color, 1.4)
            self._line(x + w, y + h - arm, x + w, y + h, color, 1.4)

        @objc.python_method
        def _scan_field(self, width: float, height: float):
            day = getattr(self, "_theme", "cyber") == "forest"
            minor = self._color("72E8DC", 0.026 if day else 0.040)
            major = self._color("72E8DC", 0.052 if day else 0.080)
            for x in range(32, int(width) - 31, 64):
                self._line(x, 28, x, height - 28, major, 0.55)
                self._line(x + 32, 28, x + 32, height - 28, minor, 0.45)
            for y in range(30, int(height) - 29, 24):
                self._line(28, y, width - 28, y, major if y % 96 == 30 else minor, 0.45)
            # Circuit traces break the rectangular grid and make both themes
            # feel like one control system rather than a collection of cards.
            trace = self._color("31F6E2", 0.18 if day else 0.28)
            for base_y in (154, 438, 642):
                self._line(26, base_y, 54, base_y, trace, 0.8)
                self._line(54, base_y, 72, base_y - 12, trace, 0.8)
                self._line(72, base_y - 12, 124, base_y - 12, trace, 0.8)
                self._hex(124, base_y - 15, 6, fill=trace)
                self._line(width - 26, base_y + 8, width - 54, base_y + 8, trace, 0.8)
                self._line(width - 54, base_y + 8, width - 72, base_y + 20, trace, 0.8)
                self._line(width - 72, base_y + 20, width - 124, base_y + 20, trace, 0.8)
                self._hex(width - 130, base_y + 17, 6, fill=trace)
            scan_y = 116 + int(time.time()) % 520
            self._line(
                30,
                min(height - 78, scan_y),
                width - 30,
                min(height - 78, scan_y),
                self._color("31F6E2", 0.045 if day else 0.09),
                1.0,
            )

        @objc.python_method
        def _starfield(self, width: float, height: float) -> None:
            """Draw a bounded, five-second-refresh space field without assets."""
            day = getattr(self, "_theme", "cyber") == "forest"
            profile = _menubar_starfield_profile(getattr(self, "_theme", "cyber"))
            phase = time.time() / 18.0
            star_count = int(profile["count"])

            # Two faint nebula layers add depth without video, textures,
            # network requests or a high-frequency animation timer.
            nebula_alpha = float(profile["nebula_alpha"])
            nebula_blue = NSColor.colorWithCalibratedRed_green_blue_alpha_(
                0.08,
                0.30,
                0.58,
                nebula_alpha,
            )
            nebula_violet = NSColor.colorWithCalibratedRed_green_blue_alpha_(
                0.35,
                0.12,
                0.55,
                nebula_alpha * 0.78,
            )
            for radius, color, nx, ny in (
                (210.0, nebula_blue, width * 0.18, height * 0.22),
                (170.0, nebula_violet, width * 0.79, height * 0.70),
            ):
                self._dot(nx - radius, ny - radius, radius, color)

            for index in range(star_count):
                seed_x = (math.sin((index + 1) * 12.9898) * 43758.5453) % 1.0
                seed_y = (math.sin((index + 1) * 78.233) * 24634.6345) % 1.0
                speed = 0.0015 + (index % 7) * 0.00018
                x = (seed_x * width + phase * width * speed) % width
                y = (seed_y * height + phase * height * speed * 0.34) % height
                twinkle = 0.55 + 0.45 * math.sin(phase * 2.4 + index * 1.73)
                alpha = float(profile["alpha_floor"]) + twinkle * float(
                    profile["alpha_range"]
                )
                radius = (0.55 if day else 0.65) + (index % 5) * 0.16
                if index % 9 == 0:
                    star = self._color("31F6E2", alpha * 0.90)
                elif day and index % 13 == 0:
                    star = self._color("FFC857", alpha * 0.82)
                else:
                    base_r, base_g, base_b = profile["base_rgb"]
                    star = NSColor.colorWithCalibratedRed_green_blue_alpha_(
                        float(base_r),
                        float(base_g),
                        float(base_b),
                        alpha,
                    )
                if not day and index % 19 == 0:
                    self._line(
                        x - 8.0,
                        y + 3.0,
                        x,
                        y,
                        star.colorWithAlphaComponent_(alpha * 0.22),
                        0.55,
                    )
                self._dot(x, y, radius, star)
                # A few slow-moving diffraction glints make the space field
                # recognizable even against the pale day cockpit.
                glint_period = 11 if day else 8
                if index % glint_period == 0:
                    glint = 4.2 if day else 7.2
                    glint_color = star.colorWithAlphaComponent_(
                        0.34 if day else 0.68
                    )
                    self._line(
                        x - glint,
                        y + radius,
                        x + glint,
                        y + radius,
                        glint_color,
                        0.55,
                    )
                if not day and index % 17 == 0:
                    self._circle(
                        x + radius,
                        y + radius,
                        radius + 2.8,
                        stroke=star.colorWithAlphaComponent_(0.24),
                        line_width=0.55,
                    )
                    self._line(
                        x + radius,
                        y - glint,
                        x + radius,
                        y + glint,
                        glint_color,
                        0.55,
                    )

        @objc.python_method
        def _section(self, x: float, y: float, w: float, h: float, title: str, accent, *, prominent=False, edge=""):
            section_code = {
                "核心服務": "SYS-01",
                "資源與模型": "AI-02",
                "即時紀錄": "LOG-03",
                "網路硬碟": "NET-04",
                "任務模組": "OPS-05",
                "業務待辦": "QUEUE-06",
                "背景監控": "WATCH-07",
            }.get(str(title), "NODE")
            contrast = _wallpaper_contrast_profile(getattr(self, "_theme", "cyber"))
            self._chamfered(
                x,
                y,
                w,
                h,
                14 if prominent else 8,
                fill=self._color("071117", contrast["panel"]),
                stroke=accent.colorWithAlphaComponent_(0.90 if prominent else 0.68),
                line_width=1.6 if prominent else 1.0,
            )
            if prominent:
                self._chamfered(
                    x + 7,
                    y + 7,
                    w - 14,
                    h - 14,
                    9,
                    stroke=self._color("35F5E8", 0.22),
                    line_width=0.7,
                )
            self._line(x + 10, y + 38, x + w - 10, y + 38, self._color("35F5E8", 0.26), 0.8)
            self._glow_line(x + 10, y + 38, x + 62, y + 38, accent, 1.35)
            self._line(x + 7, y + 10, x + 7, y + 30, accent, 2.2)
            self._draw_text(title, x + 18, y + 9, w - 94, 20, size=13.5, color=accent, weight=0.74)
            self._tech_tag(section_code, x + w - 72, y + 10, 58, accent)
            if edge == "left":
                self._line(x, y + 36, x, y + h - 36, self._color("35F5E8", 0.78), 2.0)
                self._line(x + 5, y + 50, x + 5, y + h - 50, self._color("35F5E8", 0.18), 0.8)
            elif edge == "right":
                self._line(x + w, y + 36, x + w, y + h - 36, self._color("35F5E8", 0.78), 2.0)
                self._line(x + w - 5, y + 50, x + w - 5, y + h - 50, self._color("35F5E8", 0.18), 0.8)

        @objc.python_method
        def _dashboard_state_color(self, state: str):
            state = _visual_state(str(state or "waiting"))
            return {
                "ok": self._color("67F58D"),
                "waiting": self._color("FFC857"),
                "attention": self._color("FF5F5F"),
            }.get(state, self._color("FF5F5F"))

        @objc.python_method
        def _compact(self, text, limit: int = 18) -> str:
            text = str(text or "").strip()
            if len(text) <= limit:
                return text
            return text[: max(0, limit - 3)] + "..."

        @objc.python_method
        def _draw_status_row(self, x, y, w, label, value, state="ok", value_w: float = 104, row_h: float = 29, detail: str = ""):
            visual_state = _visual_state(str(state or "waiting"))
            color = {
                "ok": self._color("67F58D"),
                "waiting": self._color("FFC857"),
                "attention": self._color("FF5F5F"),
            }.get(visual_state, self._color("FF5F5F"))
            text_y = y + max(2, (row_h - 18) / 2)
            tag_w = 38.0
            tag_x = x + w - tag_w - 7
            value_x = x + w - value_w - tag_w - 14
            label_w = max(36.0, value_x - (x + 28) - 6)
            contrast = _wallpaper_contrast_profile(getattr(self, "_theme", "cyber"))
            self._chamfered(
                x,
                y,
                w,
                row_h,
                4,
                fill=self._color("0A1B22", contrast["row"]),
                stroke=self._color("31505A", 0.60),
                line_width=0.72,
            )
            self._line(x + 3, y + 5, x + 3, y + row_h - 5, color, 1.9)
            self._hex(
                x + 11,
                y + max(6, (row_h - 10) / 2),
                10,
                fill=color.colorWithAlphaComponent_(0.16),
                stroke=color.colorWithAlphaComponent_(0.84),
                line_width=0.7,
            )
            self._hex(
                x + 14,
                y + max(9, (row_h - 4) / 2),
                4,
                fill=color,
            )
            self._draw_text(label, x + 28, text_y, label_w, 18, size=11.7, color=self._color("E9F5F1"), weight=0.48)
            self._draw_text(self._compact(value, 18), value_x, text_y, value_w, 18, size=10.9, color=color, weight=0.7, align=NSRightTextAlignment)
            self._tech_tag(_telemetry_state_code(state), tag_x, y + max(2, (row_h - 16) / 2), tag_w, color)
            self._line(
                x + 28,
                y + row_h - 3,
                x + w - 8,
                y + row_h - 3,
                color.colorWithAlphaComponent_(0.10),
                0.55,
            )
            self._status_regions.append(
                (
                    (x, y, w, row_h),
                    {
                        "title": str(label),
                        "state": str(state),
                        "value": str(value),
                        "detail": str(detail or f"目前顯示：{value}"),
                    },
                )
            )

        @objc.python_method
        def _draw_log_row(self, x, y, w, event, highlighted=False):
            state = str(event.get("state") or "waiting")
            color = self._dashboard_state_color(state)
            fill = self._color("0A252C", 0.96 if highlighted else 0.72)
            stroke = self._color("31F6E2", 0.78 if highlighted else 0.30)
            self._chamfered(x, y, w, 31, 4, fill=fill, stroke=stroke, line_width=1.0 if highlighted else 0.7)
            self._line(x + 5, y + 6, x + 5, y + 25, stroke, 1.5)
            self._draw_text(event.get("time", "--:--"), x + 14, y + 8, 48, 16, size=11.5, color=self._color("31F6E2"), mono=True)
            self._line(x + 64, y + 6, x + 64, y + 25, self._color("31F6E2", 0.28), 0.7)
            self._hex(x + 72, y + 10, 10, fill=color.colorWithAlphaComponent_(0.15), stroke=color, line_width=0.65)
            self._hex(x + 75, y + 13, 4, fill=color)
            self._draw_text(event.get("source", ""), x + 88, y + 7, 118, 17, size=11.8, color=self._color("E9F5F1"), weight=0.48)
            self._draw_text(event.get("label", CHECK_WAITING_TEXT), x + w - 142, y + 7, 92, 17, size=11.2, color=color, weight=0.65, align=NSRightTextAlignment)
            self._tech_tag("LIVE" if highlighted else "RX", x + w - 43, y + 7, 32, color)

        @objc.python_method
        def _draw_terminal_log(self, x: float, y: float, w: float, events) -> None:
            """Render the cognitive-core log as a compact green terminal."""
            terminal_green = self._color("67F58D")
            terminal_bright = self._color("31F6E2")
            contrast = _wallpaper_contrast_profile(getattr(self, "_theme", "cyber"))
            terminal_bg = NSColor.colorWithCalibratedRed_green_blue_alpha_(
                0.006,
                0.018,
                0.012,
                contrast["terminal"],
            )
            selected = list(events or [])[-3:]
            row_count = max(1, len(selected))
            row_h = 20.0
            header_h = 22.0
            box_h = header_h + row_count * row_h + 8.0
            self._chamfered(
                x,
                y,
                w,
                box_h,
                7,
                fill=terminal_bg,
                stroke=terminal_green.colorWithAlphaComponent_(0.72),
                line_width=1.0,
            )
            self._line(
                x + 9,
                y + header_h,
                x + w - 9,
                y + header_h,
                terminal_green.colorWithAlphaComponent_(0.30),
                0.65,
            )
            self._draw_text(
                "MAGI://LIVE/CONSOLE",
                x + 12,
                y + 6,
                w - 88,
                13,
                size=8.2,
                color=terminal_green,
                weight=0.70,
                mono=True,
            )
            self._draw_text(
                "● STREAM",
                x + w - 75,
                y + 6,
                62,
                13,
                size=7.8,
                color=terminal_green,
                weight=0.68,
                align=NSRightTextAlignment,
                mono=True,
            )
            for index in range(row_count):
                event = selected[index] if index < len(selected) else {}
                timestamp = str(event.get("time") or "--:--")
                source = re.sub(
                    r"\s+",
                    " ",
                    str(event.get("source") or "SYSTEM"),
                ).strip()
                label = re.sub(
                    r"\s+",
                    " ",
                    str(event.get("label") or "waiting for telemetry"),
                ).strip()
                source = self._compact(source, 12)
                label = self._compact(label, 30)
                prefix = ">" if index == len(selected) - 1 else "$"
                line = f"{prefix} {timestamp} [{source}] {label}"
                alpha = 1.0 if index == len(selected) - 1 else 0.72
                self._draw_text(
                    line,
                    x + 12,
                    y + header_h + 6 + index * row_h,
                    w - 24,
                    15,
                    size=9.4,
                    color=terminal_green.colorWithAlphaComponent_(alpha),
                    weight=0.62 if index == len(selected) - 1 else 0.46,
                    mono=True,
                )
            if int(time.time() * 2) % 2 == 0:
                cursor_y = y + header_h + 7 + min(2, max(0, len(selected) - 1)) * row_h
                terminal_bright.setFill()
                NSBezierPath.fillRect_(NSMakeRect(x + w - 18, cursor_y, 6, 11))

        @objc.python_method
        def _monitor_state_value(self, info: dict) -> tuple[str, str]:
            return _monitor_display_state(info, self._compact)

        @objc.python_method
        def _memory_rows(self, cache: dict) -> list[tuple[str, str, str]]:
            rows = []
            _total_gb, avail_gb, pct = cache.get("mem", (0, 0, 0)) if isinstance(cache, dict) else (0, 0, 0)
            if pct:
                state = "attention" if pct >= 92 else ("waiting" if pct >= 85 else "ok")
                rows.append(("系統記憶", f"{pct:.0f}%｜{avail_gb:.1f}G餘", state))
            else:
                rows.append(("系統記憶", CHECK_WAITING_TEXT, "waiting"))
            magi_mb = int(cache.get("magi_mb", 0) or 0) if isinstance(cache, dict) else 0
            engine_rss_mb = int(cache.get("engine_rss_mb", 0) or 0) if isinstance(cache, dict) else 0
            workload_rss_mb = int(cache.get("workload_rss_mb", 0) or 0) if isinstance(cache, dict) else 0
            model_memory = int(cache.get("omlx_model_memory_bytes", 0) or 0) if isinstance(cache, dict) else 0
            non_engine_mb = max(0, magi_mb - engine_rss_mb)
            state = "attention" if non_engine_mb >= 8192 else ("waiting" if non_engine_mb >= 4096 else "ok")
            rows.append(
                (
                    "MAGI 記憶",
                    _format_magi_memory(
                        magi_mb,
                        model_memory,
                        engine_rss_mb=engine_rss_mb,
                        workload_rss_mb=workload_rss_mb,
                    ),
                    state,
                )
            )
            return rows

        @objc.python_method
        def _engine_rows(self, cache: dict) -> list[tuple[str, str, str]]:
            engines = cache.get("engines", {}) if isinstance(cache, dict) else {}
            profile_info = cache.get("omlx_profile", {}) if isinstance(cache, dict) else {}
            text_status = profile_info.get("text_status", {}) if isinstance(profile_info, dict) else {}
            rows = []
            for name, _port in OMLX_ENGINES:
                model_id = str(engines.get(name, "") or "") if isinstance(engines, dict) else ""
                if name == "文字推理" and isinstance(text_status, dict) and text_status:
                    if text_status.get("mismatch"):
                        state = "attention"
                    elif text_status.get("degraded"):
                        state = "waiting"
                    elif text_status.get("ok"):
                        state = "ok"
                    else:
                        state = "attention"
                    rows.append((name, self._compact(text_status.get("label") or model_id or "離線", 17), state))
                    continue
                if not model_id and not _model_expected(name, profile_info):
                    rows.append((name, "未啟用", "idle"))
                else:
                    rows.append((name, self._compact(_short_model_id(model_id, 22) or "離線", 17), "ok" if model_id else "attention"))
            return rows

        @objc.python_method
        def _nas_summary(self, nas: dict) -> tuple[str, str]:
            state = _nas_state(nas)
            if state == "waiting":
                return CHECK_WAITING_TEXT, "waiting"
            if state == "attention":
                return "需處理", "attention"
            if nas.get("lan") and nas.get("mounted"):
                return "區網掛載", "ok"
            if nas.get("vpn") and nas.get("mounted"):
                return "遠端掛載", "ok"
            if nas.get("synology_drive"):
                return "同步可用", "ok"
            if nas.get("mounted"):
                return "連線不穩", "waiting"
            if nas.get("lan") or nas.get("vpn"):
                return "可達未掛載", "waiting"
            return "未掛載", "attention"

        @objc.python_method
        def _nas_rows(self, cache: dict) -> list[tuple[str, str, str]]:
            nas = cache.get("nas", {}) if isinstance(cache, dict) else {}
            summary, state = self._nas_summary(nas)
            rows = [("網路硬碟", summary, state)]
            shares = nas.get("shares", {}) if isinstance(nas, dict) else {}
            for share_name, _mount_path in NAS_SHARES:
                info = shares.get(share_name, {}) if isinstance(shares, dict) else {}
                if info.get("mounted"):
                    disk = info.get("disk")
                    mode_label = "同步可用" if info.get("mode") == "synology_drive" else "已掛載"
                    if disk:
                        used_gb, total_gb, pct = disk
                        rows.append((share_name, f"{pct:.0f}%｜{used_gb:.0f}/{total_gb:.0f}G", "ok"))
                    else:
                        rows.append((share_name, mode_label, "ok"))
                else:
                    rows.append((share_name, "未掛載", "attention"))
            return rows

        @objc.python_method
        def _business_modules(self):
            cache = getattr(self, "_status_cache", {}) or {}
            live = cache.get("business_live", {}) if isinstance(cache, dict) else {}
            modules = live.get("modules", {}) if isinstance(live, dict) else {}
            if isinstance(modules, dict) and modules:
                return modules
            return _business_module_status_from_payload({}).get("modules", {})

        @objc.python_method
        def _factory_checks(self):
            cache = getattr(self, "_status_cache", {}) or {}
            live = cache.get("business_live", {}) if isinstance(cache, dict) else {}
            factory = live.get("factory", {}) if isinstance(live, dict) else {}
            if isinstance(factory, dict) and factory:
                return factory
            return _business_module_status_from_payload({}).get("factory", {})

        @objc.python_method
        def _credential(self):
            cache = getattr(self, "_status_cache", {}) or {}
            live = cache.get("business_live", {}) if isinstance(cache, dict) else {}
            credential = live.get("credential", {}) if isinstance(live, dict) else {}
            return credential if isinstance(credential, dict) else {"state": "waiting", "label": CHECK_WAITING_TEXT}

        @objc.python_method
        def _circle(self, cx: float, cy: float, radius: float, *, fill=None, stroke=None, line_width: float = 1.0):
            path = NSBezierPath.bezierPathWithOvalInRect_(
                NSMakeRect(cx - radius, cy - radius, radius * 2, radius * 2)
            )
            if fill is not None:
                fill.setFill()
                path.fill()
            if stroke is not None:
                stroke.setStroke()
                path.setLineWidth_(line_width)
                path.stroke()

        @objc.python_method
        def _arc(self, cx: float, cy: float, radius: float, start: float, end: float, color, width: float = 1.0):
            path = NSBezierPath.bezierPath()
            path.appendBezierPathWithArcWithCenter_radius_startAngle_endAngle_(
                NSMakePoint(cx, cy),
                radius,
                start,
                end,
            )
            color.setStroke()
            path.setLineWidth_(width)
            path.stroke()

        @objc.python_method
        def _radial_tick_ring(self, cx: float, cy: float, radius: float, color, count: int = 96):
            for index in range(count):
                angle = (math.pi * 2 * index) / count
                major = index % 8 == 0
                inner = radius - (13 if major else 6)
                x1 = cx + math.cos(angle) * inner
                y1 = cy + math.sin(angle) * inner
                x2 = cx + math.cos(angle) * radius
                y2 = cy + math.sin(angle) * radius
                self._line(
                    x1,
                    y1,
                    x2,
                    y2,
                    color.colorWithAlphaComponent_(0.82 if major else 0.28),
                    1.25 if major else 0.55,
                )

        @objc.python_method
        def _radial_connector(self, x1: float, y1: float, x2: float, y2: float, color, *, rightward: bool):
            elbow = x1 + (34 if rightward else -34)
            self._glow_line(x1, y1, elbow, y1, color.colorWithAlphaComponent_(0.62), 0.85)
            self._glow_line(elbow, y1, x2, y2, color.colorWithAlphaComponent_(0.62), 0.85)
            self._hex(x1 - 3, y1 - 3, 6, fill=color.colorWithAlphaComponent_(0.86))
            self._circle(x2, y2, 4.0, fill=color.colorWithAlphaComponent_(0.20), stroke=color, line_width=0.8)

        @objc.python_method
        def _radial_panel_state(self, rows) -> str:
            states = [_visual_state(str(row[2] if len(row) > 2 else "waiting")) for row in rows]
            if any(state == "attention" for state in states):
                return "attention"
            if any(state == "waiting" for state in states):
                return "waiting"
            return "ok"

        @objc.python_method
        def _radial_panel_height(self, rows, maximum: float) -> float:
            """Size pods to their real row count instead of reserving six rows."""
            row_count = max(1, min(6, len(list(rows))))
            return min(maximum, max(104.0, 50.0 + row_count * 27.0))

        @objc.python_method
        def _radial_panel(self, x: float, y: float, w: float, h: float, title: str, code: str, rows, accent):
            state = self._radial_panel_state(rows)
            state_color = self._dashboard_state_color(state)
            contrast = _wallpaper_contrast_profile(getattr(self, "_theme", "cyber"))
            self._chamfered(
                x,
                y,
                w,
                h,
                18,
                fill=self._color("071117", contrast["panel"]),
                stroke=accent.colorWithAlphaComponent_(0.72),
                line_width=1.25,
            )
            self._chamfered(
                x + 7,
                y + 7,
                w - 14,
                h - 14,
                12,
                stroke=accent.colorWithAlphaComponent_(0.16),
                line_width=0.65,
            )
            self._arc(x + 24, y + 24, 15, 95, 265, state_color, 2.1)
            self._circle(x + 24, y + 24, 4, fill=state_color)
            self._draw_text(title, x + 48, y + 10, w - 132, 21, size=13.2, color=accent, weight=0.75)
            self._tech_tag(code, x + w - 72, y + 11, 57, accent)
            self._line(x + 16, y + 40, x + w - 16, y + 40, accent.colorWithAlphaComponent_(0.28), 0.75)
            selected = list(rows)[:6]
            available = max(24.0, h - 49.0)
            row_h = min(27.0, max(20.0, available / max(1, len(selected))))
            for index, row in enumerate(selected):
                label = str(row[0])
                value = str(row[1])
                row_state = str(row[2] if len(row) > 2 else "waiting")
                detail = str(row[3] if len(row) > 3 else f"{label}目前顯示：{value}")
                self._draw_status_row(
                    x + 13,
                    y + 44 + index * row_h,
                    w - 26,
                    label,
                    value,
                    row_state,
                    min(118.0, w * 0.34),
                    max(18.0, row_h - 2.0),
                    detail,
                )

        @objc.python_method
        def _draw_knowledge_radar(self, cx: float, cy: float, radius: float, knowledge: dict, cyan, green, amber, red):
            """Render live Obsidian/Wiki/vector/judgment relationships."""
            knowledge = knowledge if isinstance(knowledge, dict) else {}
            nodes = knowledge.get("nodes") if isinstance(knowledge.get("nodes"), list) else []
            score = max(0, min(100, int(knowledge.get("score") or 0)))
            graph_radius = min(182.0, max(128.0, radius * 0.60))
            metric_radius = min(radius - 72.0, graph_radius + 31.0)
            phase = (time.time() % 12.0) / 12.0
            pulse = 0.54 + 0.24 * math.sin(phase * math.pi * 2.0)

            self._circle(
                cx,
                cy - 34,
                graph_radius + 24,
                stroke=cyan.colorWithAlphaComponent_(0.13),
                line_width=0.75,
            )
            metrics = [
                float(knowledge.get("coverage_pct") or 0.0),
                float(knowledge.get("graph_pct") or 0.0),
                float(knowledge.get("obsidian_quality_pct") or 0.0),
                float(knowledge.get("insight_quality_pct") or 0.0),
                float(knowledge.get("judgment_quality_pct") or 0.0),
            ]
            segment = 58.0
            gap = 14.0
            start_angle = 103.0
            for index, metric in enumerate(metrics):
                segment_start = start_angle + index * (segment + gap)
                segment_end = segment_start + segment
                color = green if metric >= 85 else amber if metric >= 55 else red
                self._arc(
                    cx,
                    cy - 34,
                    metric_radius,
                    segment_start,
                    segment_end,
                    cyan.colorWithAlphaComponent_(0.12),
                    4.0,
                )
                self._arc(
                    cx,
                    cy - 34,
                    metric_radius,
                    segment_start,
                    segment_start + segment * max(0.0, min(100.0, metric)) / 100.0,
                    color.colorWithAlphaComponent_(0.78),
                    4.0,
                )

            if not nodes:
                self._draw_text(
                    "KNOWLEDGE RADAR // WAITING FOR INDEX",
                    cx - 180,
                    cy - graph_radius - 108,
                    360,
                    15,
                    size=8.8,
                    color=amber,
                    weight=0.62,
                    align=NSCenterTextAlignment,
                    mono=True,
                )
                return

            positions = []
            node_count = min(5, len(nodes))
            angle_step = 360.0 / max(1, node_count)
            # Equal-angle placement keeps every knowledge domain balanced.
            # A fixed north anchor makes the ring visually stable on refresh.
            base_angles = tuple(
                -90.0 + index * angle_step for index in range(node_count)
            )
            node_colors = []
            for index, node in enumerate(nodes[:5]):
                angle = math.radians(base_angles[index])
                node_x = cx + math.cos(angle) * graph_radius
                node_y = cy - 34 + math.sin(angle) * graph_radius
                positions.append((node_x, node_y))
                node_score = max(0.0, min(100.0, float(node.get("score") or 0.0)))
                node_color = green if node_score >= 85 else amber if node_score >= 55 else red
                node_colors.append(node_color)

            # The edges represent actual indexed-system relationships.
            for first, second in ((0, 1), (0, 2), (1, 2), (1, 4), (2, 3), (3, 4)):
                if first >= len(positions) or second >= len(positions):
                    continue
                x1, y1 = positions[first]
                x2, y2 = positions[second]
                relation_color = node_colors[first] if first < len(node_colors) else cyan
                self._line(
                    x1,
                    y1,
                    x2,
                    y2,
                    relation_color.colorWithAlphaComponent_(0.18 + pulse * 0.18),
                    0.7,
                )

            for index, node in enumerate(nodes[:5]):
                node_x, node_y = positions[index]
                node_color = node_colors[index]
                self._line(
                    cx,
                    cy - 34,
                    node_x,
                    node_y,
                    node_color.colorWithAlphaComponent_(0.13),
                    0.6,
                )
                self._circle(
                    node_x,
                    node_y,
                    16.0,
                    fill=self._color("071117", 0.92),
                    stroke=node_color.colorWithAlphaComponent_(0.78),
                    line_width=1.2,
                )
                self._circle(
                    node_x,
                    node_y,
                    7.0 + pulse * 2.0,
                    fill=node_color.colorWithAlphaComponent_(0.08 + pulse * 0.12),
                    stroke=node_color.colorWithAlphaComponent_(0.40),
                    line_width=0.7,
                )
                label = str(node.get("label") or "NODE")
                value = str(node.get("value") or "—")
                angle = math.radians(base_angles[index])
                radial_x = math.cos(angle)
                radial_y = math.sin(angle)
                label_w = 120.0
                label_h = 29.0
                if radial_y <= -0.72:
                    label_x = node_x - label_w / 2.0
                    label_y = node_y - 54.0
                else:
                    label_x = node_x + (
                        25.0 if radial_x > 0 else -label_w - 25.0
                    )
                    label_y = node_y - label_h / 2.0
                self._rounded(
                    label_x - 4,
                    label_y - 2,
                    label_w + 8,
                    label_h + 4,
                    6,
                    fill=self._color("050B0F", 0.88),
                    stroke=node_color.colorWithAlphaComponent_(0.28),
                    line_width=0.65,
                )
                self._draw_text(
                    label,
                    label_x,
                    label_y + 1,
                    label_w,
                    12,
                    size=8.0,
                    color=cyan,
                    weight=0.70,
                    align=NSCenterTextAlignment,
                    mono=True,
                )
                self._draw_text(
                    value,
                    label_x,
                    label_y + 13,
                    label_w,
                    13,
                    size=(
                        9.2
                        if len(value) <= 14
                        else 8.0
                        if len(value) <= 18
                        else 7.2
                    ),
                    color=node_color,
                    weight=0.72,
                    align=NSCenterTextAlignment,
                    mono=True,
                )
                region_x = min(node_x - 28, label_x - 4)
                region_y = min(node_y - 28, label_y - 2)
                region_right = max(node_x + 28, label_x + label_w + 4)
                region_top = max(node_y + 28, label_y + label_h + 2)
                self._status_regions.append(
                    (
                        (
                            region_x,
                            region_y,
                            region_right - region_x,
                            region_top - region_y,
                        ),
                        {
                            "title": label,
                            "state": str(
                                node.get("state")
                                or ("ok" if float(node.get("score") or 0.0) >= 70 else "waiting")
                            ),
                            "value": value,
                            "detail": str(node.get("detail") or "知識來源尚無說明。"),
                        },
                    )
                )

            level_color = green if score >= 85 else amber if score >= 55 else red
            self._draw_text(
                f"KNOWLEDGE RADAR // {knowledge.get('grade') or '--'} · {score:02d}",
                cx - 180,
                cy - graph_radius - 108,
                360,
                15,
                size=8.8,
                color=level_color,
                weight=0.72,
                align=NSCenterTextAlignment,
                mono=True,
            )

        @objc.python_method
        def _draw_radial_dashboard(self, width: float, height: float, cache: dict):
            cyan = self._color("35F5E8")
            green = self._color("67F58D")
            amber = self._color("FFC857")
            red = self._color("FF5F5F")
            muted = self._color("8AA3A4")
            day_mode = getattr(self, "_theme", "cyber") == "forest"
            self._status_regions = []
            self._button_regions = []
            self._overlay_regions = []

            contrast = _wallpaper_contrast_profile(getattr(self, "_theme", "cyber"))

            # Draw the cached Apple wallpaper in this exact graphics context.
            # Transparent NSColor fills then blend with the image instead of
            # an intermediate grey NSView backing store.
            wallpaper_drawn = False
            wallpaper_view = getattr(self, "_wallpaper_view", None)
            if bool(getattr(self, "_wallpaper_active", False)) and wallpaper_view is not None:
                try:
                    wallpaper_drawn = bool(
                        wallpaper_view.draw_current_wallpaper(
                            NSMakeRect(0, 0, width, height)
                        )
                    )
                except Exception:
                    wallpaper_drawn = False
            if not wallpaper_drawn:
                self._rounded(
                    0,
                    0,
                    width,
                    height,
                    0,
                    fill=self._color("030507", 1.0),
                )

            self._gradient(
                0,
                0,
                width,
                height,
                self._color("030507", contrast["scrim_start"]),
                self._color("181D21", contrast["scrim_end"]),
                -64,
            )
            self._starfield(width, height)
            self._scan_field(width, height)

            cx = width / 2.0
            cy = height / 2.0 - 12.0
            panel_w = min(356.0, max(292.0, width * 0.225))
            panel_h = min(205.0, max(166.0, (height - 200.0) / 3.0 - 14.0))
            margin = max(24.0, min(42.0, width * 0.025))
            top_y = 88.0
            left_x = margin
            right_x = width - margin - panel_w
            central_space = max(430.0, width - (panel_w + margin) * 2.0 - 72.0)
            radius = min(height * 0.286, central_space * 0.43)
            radius = max(205.0, radius)

            # Panoramic monitor perimeter and sensor horizon.
            self._circle(cx, cy, radius + 34, stroke=cyan.colorWithAlphaComponent_(0.10), line_width=1.0)
            self._circle(cx, cy, radius + 22, stroke=cyan.colorWithAlphaComponent_(0.26), line_width=0.8)
            self._radial_tick_ring(cx, cy, radius + 12, cyan, 120)
            self._arc(cx, cy, radius + 28, 200, 340, cyan.colorWithAlphaComponent_(0.76), 2.0)
            self._arc(cx, cy, radius + 28, 20, 160, cyan.colorWithAlphaComponent_(0.42), 1.1)
            self._arc(cx, cy, radius - 5, 215, 325, amber.colorWithAlphaComponent_(0.62), 1.25)
            self._arc(cx, cy, radius - 5, 35, 145, green.colorWithAlphaComponent_(0.52), 1.25)
            self._circle(
                cx,
                cy,
                radius - 35,
                fill=self._color("060C10", contrast["core"]),
                stroke=cyan.colorWithAlphaComponent_(0.52),
                line_width=1.25,
            )
            self._circle(cx, cy, radius - 58, stroke=cyan.colorWithAlphaComponent_(0.18), line_width=0.7)
            self._line(cx - radius + 20, cy, cx + radius - 20, cy, cyan.colorWithAlphaComponent_(0.15), 0.65)
            self._line(cx, cy - radius + 20, cx, cy + radius - 20, cyan.colorWithAlphaComponent_(0.15), 0.65)

            overall = _overall_state(cache)
            overall_color = self._dashboard_state_color(overall)
            readiness = cache.get("business_readiness", {}) if isinstance(cache, dict) else {}
            summary = readiness.get("summary", {}) if isinstance(readiness, dict) else {}
            attention_count = int(summary.get("attention") or 0)
            waiting_count = int(summary.get("waiting") or 0)
            release_id = str(os.environ.get("MAGI_V3_RELEASE_ID") or "V3").replace("v3-20260731-", "").replace("v3-20260730-", "")
            interface_mode = "DAY" if day_mode else "NIGHT"

            # Header. The macOS status item remains visible above this window
            # and is the primary toggle/close control.
            self._chamfered(
                24,
                17,
                width - 48,
                38,
                9,
                fill=self._color("071117", contrast["header"]),
                stroke=cyan.colorWithAlphaComponent_(0.28),
                line_width=0.75,
            )
            self._draw_text(
                "MAGI PANORAMIC SYSTEM // 360° SITUATIONAL FIELD",
                38,
                26,
                width * 0.56,
                17,
                size=10.2,
                color=cyan,
                weight=0.72,
                mono=True,
            )
            self._draw_text(
                f"V3/{release_id.upper()}　{interface_mode}　{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
                width * 0.55,
                26,
                width * 0.34,
                17,
                size=9.6,
                color=muted,
                weight=0.58,
                align=NSRightTextAlignment,
                mono=True,
            )
            # Six information pods radiate into the central circular display.
            db_state = "ok" if (cache.get("db", {}) or {}).get("local") else "attention"
            process_health = cache.get("process_health", {}) if isinstance(cache, dict) else {}
            process_summary = process_health.get("summary", {}) if isinstance(process_health, dict) else {}
            anomaly_count = int(process_summary.get("anomaly_count", 0) or 0)
            anomaly_detail = _process_anomaly_detail(process_health)
            process_state = "ok" if process_health.get("ok") is True and anomaly_count == 0 else "attention"
            core_rows = [
                (
                    label,
                    _label_for_state(_service_state(cache, label), OPERATIONAL_TEXT),
                    _service_state(cache, label),
                    str((cache.get("service_details") or {}).get(label) or f"{label}狀態正常。"),
                )
                for label, _patterns in SERVICES
            ]
            core_rows.extend(
                [
                    ("本機資料庫", OPERATIONAL_TEXT if db_state == "ok" else "離線", db_state, "本機 MariaDB 連線狀態。"),
                    ("程序異常", "無" if process_state == "ok" else f"{anomaly_count}項", process_state, anomaly_detail if process_health.get("ok") is True else "程序監控讀取失敗。"),
                ]
            )
            resource_rows = self._memory_rows(cache) + self._engine_rows(cache)
            modules = self._business_modules()
            module_rows = [
                (
                    label,
                    str(info.get("label") or CHECK_WAITING_TEXT),
                    str(info.get("state") or "waiting"),
                    str(info.get("detail") or f"{label}等待檢查。"),
                )
                for label, info in modules.items()
            ]
            readiness_items = readiness.get("items", {}) if isinstance(readiness, dict) else {}
            readiness_rows = [
                (
                    label,
                    str(info.get("label") or CHECK_WAITING_TEXT),
                    str(info.get("state") or "waiting"),
                    _business_readiness_detail(str(label), info),
                )
                for label, info in readiness_items.items()
            ]
            network_rows = self._nas_rows(cache)
            monitors = cache.get("monitors", {}) if isinstance(cache, dict) else {}
            cron_summary = cache.get("cron_summary", {}) if isinstance(cache, dict) else {}
            credential = self._credential()
            health = cache.get("health", {}) if isinstance(cache, dict) else {}
            agent = cache.get("agent_status", {}) if isinstance(cache, dict) else {}
            monitor_rows = [
                ("定時排程", str(cron_summary.get("label") or CHECK_WAITING_TEXT), str(cron_summary.get("state") or "waiting"), _cron_failure_detail(cache.get("cron_details", []))),
                ("憑證狀態", str(credential.get("label") or CHECK_WAITING_TEXT), str(credential.get("state") or "waiting"), _credential_detail(credential)),
                ("自我修復", _label_for_state(str((health.get("guardian") or {}).get("state") or "waiting"), OPERATIONAL_TEXT), str((health.get("guardian") or {}).get("state") or "waiting"), str((health.get("guardian") or {}).get("detail") or "健康報告未提供原因。")),
                ("功能健康", _label_for_state(str((health.get("function_health") or {}).get("state") or "waiting"), OPERATIONAL_TEXT), str((health.get("function_health") or {}).get("state") or "waiting"), str((health.get("function_health") or {}).get("detail") or "健康報告未提供原因。")),
                ("Agent 狀態", str(agent.get("label") or "系統正常待命"), str(agent.get("state") or "idle"), str(agent.get("detail") or "Agent 尚無公開活動摘要。")),
            ]

            core_h = self._radial_panel_height(core_rows, panel_h)
            resource_h = self._radial_panel_height(resource_rows, panel_h)
            network_h = self._radial_panel_height(network_rows, panel_h)
            module_h = self._radial_panel_height(module_rows, panel_h)
            readiness_h = self._radial_panel_height(readiness_rows, panel_h)
            monitor_h = self._radial_panel_height(monitor_rows, panel_h)
            panels = [
                (left_x, top_y, core_h, "核心系統", "CORE-01", core_rows, cyan),
                (left_x, cy - resource_h / 2.0, resource_h, "資源／模型", "AI-02", resource_rows, cyan),
                (left_x, height - network_h - 88.0, network_h, "網路／儲存", "NET-03", network_rows, cyan),
                (right_x, top_y, module_h, "業務模組", "OPS-04", module_rows, cyan),
                (right_x, cy - readiness_h / 2.0, readiness_h, "任務佇列", "QUEUE-05", readiness_rows, amber if waiting_count else green),
                (right_x, height - monitor_h - 88.0, monitor_h, "健康守門", "WATCH-06", monitor_rows, cyan),
            ]
            anchor_targets = [
                (cx - radius * 0.73, cy - radius * 0.68),
                (cx - radius - 6, cy),
                (cx - radius * 0.73, cy + radius * 0.68),
                (cx + radius * 0.73, cy - radius * 0.68),
                (cx + radius + 6, cy),
                (cx + radius * 0.73, cy + radius * 0.68),
            ]
            for index, panel in enumerate(panels):
                x, y, pod_h = panel[:3]
                rightward = index < 3
                start = (
                    x + panel_w if rightward else x,
                    y + pod_h / 2.0,
                )
                end = anchor_targets[index]
                self._radial_connector(start[0], start[1], end[0], end[1], panel[6], rightward=rightward)
            for x, y, pod_h, title, code, rows, accent in panels:
                self._radial_panel(x, y, panel_w, pod_h, title, code, rows, accent)

            # Dynamic knowledge graph feeds the cognitive core from real
            # Obsidian, Wiki, vector, judgment and insight indexes.
            knowledge = cache.get("knowledge_radar", {}) if isinstance(cache, dict) else {}
            self._draw_knowledge_radar(cx, cy, radius, knowledge, cyan, green, amber, red)

            # Central cognitive core and short live event tape.
            self._circle(cx, cy - 34, 72, fill=overall_color.colorWithAlphaComponent_(0.055), stroke=overall_color.colorWithAlphaComponent_(0.62), line_width=1.2)
            self._circle(cx, cy - 34, 54, stroke=cyan.colorWithAlphaComponent_(0.34), line_width=0.8)
            self._hex(cx - 22, cy - 56, 44, fill=cyan.colorWithAlphaComponent_(0.08), stroke=cyan.colorWithAlphaComponent_(0.72), line_width=1.0)
            self._draw_text("M A G I", cx - 145, cy - 52, 290, 32, size=28, color=cyan, weight=0.84, align=NSCenterTextAlignment, mono=True)
            knowledge_score = max(0, min(100, int(knowledge.get("score") or 0))) if isinstance(knowledge, dict) else 0
            knowledge_grade = str(knowledge.get("grade") or "--") if isinstance(knowledge, dict) else "--"
            knowledge_color = green if knowledge_score >= 85 else amber if knowledge_score >= 55 else red
            self._draw_text(
                f"KNOWLEDGE {knowledge_grade} / {knowledge_score:02d}",
                cx - 100,
                cy - 82,
                200,
                14,
                size=8.8,
                color=knowledge_color,
                weight=0.72,
                align=NSCenterTextAlignment,
                mono=True,
            )
            overall_label = OVERALL_WAITING_TEXT if overall == "waiting" else _label_for_state(overall, OPERATIONAL_TEXT)
            self._draw_text(overall_label, cx - 120, cy - 11, 240, 22, size=14.5, color=overall_color, weight=0.75, align=NSCenterTextAlignment)
            self._tech_tag(_telemetry_state_code(overall), cx - 31, cy + 17, 62, overall_color)
            self._draw_text(
                f"需處理 {attention_count}　待確認 {waiting_count}",
                cx - 140,
                cy + 41,
                280,
                18,
                size=10.2,
                color=red if attention_count else (amber if waiting_count else green),
                weight=0.68,
                align=NSCenterTextAlignment,
            )

            live_events = _format_live_events(cache, limit=3)
            event_w = min(352.0, radius * 1.58)
            event_x = cx - event_w / 2.0
            radar_graph_radius = min(182.0, max(128.0, radius * 0.60))
            # Dock the terminal below the complete node ring.  The former
            # in-ring position covered the southern knowledge domains.
            event_y = cy + radar_graph_radius + 40.0
            self._draw_terminal_log(event_x, event_y, event_w, live_events)

            # Circular command nodes on the lower panoramic arc.
            commands = [
                ("重新整理", "refresh"),
                ("首頁", "open_hub"),
                ("紀錄", "open_logs"),
                ("檢查", "run_check"),
                ("知識庫", "open_knowledge"),
                ("切換日" if getattr(self, "_theme", "cyber") == "cyber" else "切換夜", "toggle_theme"),
            ]
            group_w = min(720.0, width * 0.54)
            step = group_w / len(commands)
            start_x = cx - group_w / 2.0
            command_y = height - 48.0
            for index, (label, action) in enumerate(commands):
                node_x = start_x + step * index + 23.0
                state_color = cyan
                self._circle(node_x, command_y, 22, fill=self._color("0A1B22", 0.96), stroke=state_color.colorWithAlphaComponent_(0.76), line_width=1.2)
                self._circle(node_x, command_y, 15, stroke=state_color.colorWithAlphaComponent_(0.18), line_width=0.65)
                self._draw_text(f"{index + 1:02d}", node_x - 16, command_y - 8, 32, 16, size=9.2, color=state_color, weight=0.72, align=NSCenterTextAlignment, mono=True)
                self._draw_text(label, node_x + 28, command_y - 9, step - 35, 18, size=10.6, color=self._color("E9F5F1"), weight=0.62)
                self._button_regions.append(((node_x - 25, command_y - 25, step - 2, 50), action))
            self._draw_text(
                "LINEAR COMMAND RAIL // LOCAL SECURE CONTROL",
                cx - 230,
                height - 84,
                460,
                14,
                size=8.5,
                color=muted,
                weight=0.58,
                align=NSCenterTextAlignment,
                mono=True,
            )
            if self._action_notice:
                self._draw_text(
                    f"// {self._action_notice}",
                    cx - 190,
                    height - 102,
                    380,
                    15,
                    size=9.4,
                    color=cyan,
                    weight=0.62,
                    align=NSCenterTextAlignment,
                    mono=True,
                )
            self._draw_detail_overlay(width, height)

        @objc.python_method
        def _draw_detail_overlay(self, width: float, height: float) -> None:
            """Draw status details inside the cockpit instead of a native alert."""
            detail = getattr(self, "_detail_overlay", None)
            if not isinstance(detail, dict) or not detail:
                return
            cyan = self._color("35F5E8")
            muted = self._color("8AA3A4")
            state = str(detail.get("state") or "waiting")
            state_color = self._dashboard_state_color(state)
            state_label = {
                "ok": "功能正常",
                "alive": "功能正常",
                "idle": "待命",
                "attention": "需要處理",
                "down": "需要處理",
                "waiting": "等待確認",
                "processing": "處理中",
                "stale": "資料逾時",
            }.get(state, state)
            # Dim the star field but keep it visible to preserve spatial
            # continuity with the panoramic cockpit.
            self._rounded(
                0,
                0,
                width,
                height,
                0,
                fill=self._color("030507", 0.72),
            )
            panel_w = min(790.0, max(620.0, width - 260.0))
            panel_h = min(520.0, max(430.0, height - 260.0))
            x = (width - panel_w) / 2.0
            y = (height - panel_h) / 2.0
            self._chamfered(
                x,
                y,
                panel_w,
                panel_h,
                20,
                fill=self._color("060C10", 0.975),
                stroke=cyan.colorWithAlphaComponent_(0.88),
                line_width=1.6,
            )
            self._chamfered(
                x + 10,
                y + 10,
                panel_w - 20,
                panel_h - 20,
                13,
                stroke=cyan.colorWithAlphaComponent_(0.22),
                line_width=0.75,
            )
            self._hud_brackets(x + 18, y + 18, panel_w - 36, panel_h - 36, cyan.colorWithAlphaComponent_(0.54), 24)
            self._draw_text(
                "MAGI STATUS CHANNEL // CONTEXT PANEL",
                x + 34,
                y + 26,
                panel_w - 68,
                16,
                size=9.2,
                color=cyan,
                weight=0.70,
                mono=True,
            )
            self._draw_text(
                str(detail.get("title") or "狀態項目"),
                x + 34,
                y + 54,
                panel_w - 210,
                34,
                size=23.0,
                color=self._color("E9F5F1"),
                weight=0.78,
            )
            self._tech_tag(
                state_label,
                x + panel_w - 166,
                y + 60,
                124,
                state_color,
            )
            self._line(x + 34, y + 101, x + panel_w - 34, y + 101, cyan.colorWithAlphaComponent_(0.34), 0.9)
            self._draw_text(
                f"目前顯示 // {detail.get('value') or '未提供'}",
                x + 38,
                y + 119,
                panel_w - 76,
                20,
                size=12.2,
                color=state_color,
                weight=0.70,
                mono=True,
            )
            pages = self._detail_pages(detail)
            page_index = max(0, min(len(pages) - 1, int(getattr(self, "_detail_page", 0))))
            self._detail_page = page_index
            self._draw_text(
                pages[page_index],
                x + 38,
                y + 158,
                panel_w - 76,
                panel_h - 252,
                size=14.0,
                color=self._color("E6F7F4"),
                weight=0.46,
            )
            if len(pages) > 1:
                self._draw_text(
                    f"PAGE {page_index + 1:02d}/{len(pages):02d}",
                    x + panel_w - 166,
                    y + panel_h - 82,
                    124,
                    16,
                    size=9.0,
                    color=muted,
                    weight=0.60,
                    align=NSRightTextAlignment,
                    mono=True,
                )
            button_y = y + panel_h - 60
            buttons = [("複製內容", "overlay_copy")]
            if page_index > 0:
                buttons.append(("上一頁", "overlay_prev"))
            if page_index < len(pages) - 1:
                buttons.append(("下一頁", "overlay_next"))
            buttons.append(("關閉", "overlay_close"))
            button_w = 118.0
            gap = 12.0
            total_w = len(buttons) * button_w + (len(buttons) - 1) * gap
            button_x = x + (panel_w - total_w) / 2.0
            for label, action in buttons:
                self._chamfered(
                    button_x,
                    button_y,
                    button_w,
                    34,
                    7,
                    fill=self._color("0A1B22", 0.94),
                    stroke=cyan.colorWithAlphaComponent_(0.62),
                    line_width=0.9,
                )
                self._draw_text(
                    label,
                    button_x,
                    button_y + 8,
                    button_w,
                    18,
                    size=11.0,
                    color=cyan,
                    weight=0.68,
                    align=NSCenterTextAlignment,
                )
                self._overlay_regions.append(((button_x, button_y, button_w, 34), action))
                button_x += button_w + gap

        def drawRect_(self, rect):
            bounds = self.bounds()
            width = bounds.size.width
            height = bounds.size.height
            cache = getattr(self, "_status_cache", {}) or {}
            if getattr(self, "_fullscreen_mode", False):
                self._draw_radial_dashboard(width, height, cache)
                return
            cyan = self._color("35F5E8")
            green = self._color("67F58D")
            amber = self._color("FFC857")
            red = self._color("FF5F5F")
            muted = self._color("8AA3A4")
            self._status_regions = []

            day_mode = getattr(self, "_theme", "cyber") == "forest"
            self._gradient(
                0,
                0,
                width,
                height,
                self._color("030507", 1.0),
                self._color("181D21", 1.0),
                -72,
            )
            self._chamfered(
                7,
                7,
                width - 14,
                height - 14,
                26,
                fill=self._color("181D21", 0.90),
                stroke=self._color("71808A", 0.94),
                line_width=1.6,
            )
            self._chamfered(
                20,
                20,
                width - 40,
                height - 40,
                15,
                fill=self._color("060C10", 0.94),
                stroke=self._color("288A88", 0.68),
                line_width=1.1,
            )
            self._scan_field(width, height)
            self._hud_brackets(27, 27, width - 54, height - 54, self._color("35F5E8", 0.64), 28)
            for x in range(44, int(width) - 43, 48):
                tick_h = 7 if (x - 44) % 192 == 0 else 3
                self._line(x, 24, x, 24 + tick_h, self._color("31F6E2", 0.42), 0.7)
                self._line(x, height - 24 - tick_h, x, height - 24, self._color("31F6E2", 0.30), 0.7)

            self._line(17, 102, 17, height - 84, self._color("64747D", 0.72), 1.2)
            self._line(width - 17, 102, width - 17, height - 84, self._color("64747D", 0.72), 1.2)
            for y in range(126, int(height) - 98, 58):
                self._line(12, y, 23, y, self._color("35F5E8", 0.58), 1.1)
                self._line(width - 23, y, width - 12, y, self._color("35F5E8", 0.58), 1.1)

            overall = _overall_state(cache)
            overall_label = OVERALL_WAITING_TEXT if overall == "waiting" else _label_for_state(overall, OPERATIONAL_TEXT)
            readiness = cache.get("business_readiness", {}) if isinstance(cache, dict) else {}
            summary = readiness.get("summary", {}) if isinstance(readiness, dict) else {}
            attention_count = int(summary.get("attention") or 0)
            waiting_count = int(summary.get("waiting") or 0)
            summary_color = red if attention_count else (amber if waiting_count else green)

            # Command header: status pods feed a central orbital MAGI core.
            core_state_color = self._dashboard_state_color(overall)
            release_id = str(os.environ.get("MAGI_V3_RELEASE_ID") or "V3").replace("v3-20260730-", "")
            interface_mode = "DAY" if day_mode else "NIGHT"
            self._draw_text(
                "MAGI // AUTONOMOUS LEGAL OPERATIONS",
                40,
                27,
                270,
                12,
                size=8.6,
                color=muted,
                weight=0.58,
                mono=True,
            )
            self._draw_text(
                f"V3/{release_id.upper()}  {interface_mode}  {datetime.now().strftime('%H:%M:%S')}",
                width - 310,
                27,
                270,
                12,
                size=8.6,
                color=muted,
                weight=0.58,
                align=NSRightTextAlignment,
                mono=True,
            )

            self._chamfered(38, 43, 250, 54, 9, fill=self._color("0A141A", 0.92), stroke=self._color("4B626B", 0.86), line_width=1.0)
            self._glow_line(46, 51, 46, 89, core_state_color, 1.5)
            self._hex(58, 58, 16, fill=core_state_color.colorWithAlphaComponent_(0.13), stroke=core_state_color, line_width=0.8)
            self._hex(63, 63, 6, fill=core_state_color)
            self._draw_text("SYSTEM CORE // 01", 84, 51, 136, 13, size=8.4, color=muted, weight=0.62, mono=True)
            self._draw_text(overall_label, 84, 67, 128, 20, size=14.6, color=core_state_color, weight=0.74)
            self._tech_tag(_telemetry_state_code(overall), 228, 62, 46, core_state_color)

            self._orbit(width / 2, 69, 142, cyan)
            self._chamfered(width / 2 - 150, 39, 300, 62, 13, fill=self._color("081B22", 0.92), stroke=cyan.colorWithAlphaComponent_(0.88), line_width=1.35)
            self._chamfered(width / 2 - 140, 46, 280, 48, 8, stroke=cyan.colorWithAlphaComponent_(0.22), line_width=0.7)
            self._draw_text("M A G I", 0, 44, width, 30, size=28.5, color=cyan, weight=0.82, align=NSCenterTextAlignment, mono=True)
            self._draw_text("COGNITIVE OPERATIONS CORE", 0, 75, width, 12, size=8.0, color=self._color("D8F7F4", 0.76), weight=0.58, align=NSCenterTextAlignment, mono=True)
            self._glow_line(288, 70, width / 2 - 151, 70, cyan.colorWithAlphaComponent_(0.52), 0.7)
            self._glow_line(width / 2 + 151, 70, width - 288, 70, cyan.colorWithAlphaComponent_(0.52), 0.7)

            self._chamfered(width - 288, 43, 250, 54, 9, fill=self._color("0A141A", 0.92), stroke=self._color("4B626B", 0.86), line_width=1.0)
            self._glow_line(width - 46, 51, width - 46, 89, summary_color, 1.5)
            self._hex(width - 74, 58, 16, fill=summary_color.colorWithAlphaComponent_(0.13), stroke=summary_color, line_width=0.8)
            self._hex(width - 69, 63, 6, fill=summary_color)
            self._draw_text("OPERATIONS // 02", width - 274, 51, 156, 13, size=8.4, color=muted, weight=0.62, align=NSRightTextAlignment, mono=True)
            self._draw_text(f"需處理 {attention_count}・待確認 {waiting_count}", width - 274, 67, 158, 20, size=13.2, color=summary_color, weight=0.72, align=NSRightTextAlignment)
            self._tech_tag("QUEUE", width - 104, 62, 50, summary_color)
            self._glow_line(42, 110, width - 42, 110, self._color("31F6E2", 0.54), 0.75)
            self._hex(width / 2 - 3, 107, 6, fill=self._color("D8F7F4", 0.88))

            margin = 28
            gap = 14
            top_y = 120
            main_bottom = 676
            left_w = 260
            center_w = 416
            right_w = width - margin * 2 - left_w - center_w - gap * 2
            left_x = margin
            center_x = left_x + left_w + gap
            right_x = center_x + center_w + gap

            # 左翼：核心服務與資源模型。
            core_h = 260
            self._section(left_x, top_y, left_w, core_h, "核心服務", cyan, edge="left")
            db_state = "ok" if (cache.get("db", {}) or {}).get("local") else "attention"
            process_health = cache.get("process_health", {}) if isinstance(cache, dict) else {}
            process_summary = process_health.get("summary", {}) if isinstance(process_health, dict) else {}
            anomaly_count = int(process_summary.get("anomaly_count", 0) or 0)
            process_state = "ok" if process_health.get("ok") is True and anomaly_count == 0 else "attention"
            service_rows = [
                ("守護程式", _label_for_state(_service_state(cache, "守護程式", "守護程序"), OPERATIONAL_TEXT), _service_state(cache, "守護程式", "守護程序"), str((cache.get("service_details") or {}).get("守護程式") or "程序狀態正常。")),
                ("主伺服器", _label_for_state(_service_state(cache, "主伺服器"), OPERATIONAL_TEXT), _service_state(cache, "主伺服器"), str((cache.get("service_details") or {}).get("主伺服器") or "主伺服器狀態正常。")),
                ("通訊機器人", _label_for_state(_service_state(cache, "通訊機器人", "通訊機器"), OPERATIONAL_TEXT), _service_state(cache, "通訊機器人", "通訊機器"), str((cache.get("service_details") or {}).get("通訊機器人") or "通訊機器人狀態正常。")),
                ("工具介面", _label_for_state(_service_state(cache, "工具介面", "工具接口"), OPERATIONAL_TEXT), _service_state(cache, "工具介面", "工具接口"), str((cache.get("service_details") or {}).get("工具介面") or "工具介面狀態正常。")),
                ("本機資料庫", OPERATIONAL_TEXT if db_state == "ok" else "離線", db_state, "本機 MariaDB 連線正常。" if db_state == "ok" else "無法連線至本機 MariaDB 的 3306 連接埠。"),
                ("程序異常", "無" if process_state == "ok" else f"{anomaly_count}項", process_state, _process_anomaly_detail(process_health) if process_health.get("ok") is True else "程序監控讀取失敗。"),
            ]
            for i, row in enumerate(service_rows):
                self._draw_status_row(left_x + 14, top_y + 47 + i * 34, left_w - 28, row[0], row[1], row[2], 82, 28, row[3])

            resource_y = top_y + core_h + gap
            resource_h = main_bottom - resource_y
            self._section(left_x, resource_y, left_w, resource_h, "資源與模型", cyan, edge="left")
            resource_rows = self._memory_rows(cache) + self._engine_rows(cache)
            for i, row in enumerate(resource_rows[:6]):
                self._draw_status_row(left_x + 14, resource_y + 47 + i * 36, left_w - 28, row[0], row[1], row[2], 102, 29)

            # 中央主視窗：即時事件流與網路硬碟。
            live_h = 350
            self._section(center_x, top_y, center_w, live_h, "即時紀錄", cyan, prominent=True)
            live_events = _format_live_events(cache)
            for i, event in enumerate(live_events):
                self._draw_log_row(center_x + 16, top_y + 48 + i * 36, center_w - 32, event, highlighted=i == len(live_events) - 1)
            self._line(center_x + 24, top_y + 304, center_x + center_w - 24, top_y + 304, self._color("31F6E2", 0.58), 1.0)
            pulse_points = cache.get("live_pulse_points") if isinstance(cache, dict) else None
            if not isinstance(pulse_points, list):
                pulse_points = _live_pulse_points(live_events)
            base_x, base_y = center_x + 244, top_y + 312
            for idx, point in enumerate(pulse_points):
                self._rounded(base_x + idx * 9, base_y + 22 - point, 5, point, 1.5, fill=self._color("31F6E2", 0.70))
            checked_at = ""
            live = cache.get("business_live", {}) if isinstance(cache, dict) else {}
            if isinstance(live, dict):
                checked_at = live.get("checked_at", "")
            self._draw_text(f"事件流　最近檢查 {checked_at or '--:--'}", center_x + 24, top_y + 315, 200, 18, size=10.5, color=muted, mono=True)

            nas_y = top_y + live_h + gap
            nas_h = main_bottom - nas_y
            self._section(center_x, nas_y, center_w, nas_h, "網路硬碟", cyan, prominent=True)
            nas_rows = self._nas_rows(cache)
            nas_step = min(32.0, max(25.0, (nas_h - 50) / max(1, len(nas_rows[:5]))))
            for i, row in enumerate(nas_rows[:5]):
                self._draw_status_row(center_x + 16, nas_y + 45 + i * nas_step, center_w - 32, row[0], row[1], row[2], 172, min(27, nas_step - 3))

            # 右翼：任務模組、業務待辦與背景監控。
            task_h = TASK_MODULE_SECTION_HEIGHT
            self._section(right_x, top_y, right_w, task_h, "任務模組", cyan, edge="right")
            modules = self._business_modules()
            for i, (label, info) in enumerate(modules.items()):
                state = str(info.get("state") or "waiting")
                value = str(info.get("label") or _label_for_state(state, OPERATIONAL_TEXT))
                row_y, row_h = _task_module_row_geometry(i)
                detail = str(info.get("detail") or f"{label}目前顯示：{value}")
                self._draw_status_row(right_x + 14, top_y + row_y, right_w - 28, label, value, state, 96, row_h, detail)

            factory_y = top_y + task_h + gap
            factory_h = 174
            readiness_accent = red if attention_count else (amber if waiting_count else green)
            self._section(right_x, factory_y, right_w, factory_h, "業務待辦", readiness_accent, edge="right")
            readiness_items = readiness.get("items", {}) if isinstance(readiness, dict) else {}
            readiness_rows = list(readiness_items.items())
            readiness_step = min(25.0, max(20.0, (factory_h - 48) / max(1, len(readiness_rows))))
            for i, (label, info) in enumerate(readiness_rows):
                state = str(info.get("state") or "waiting")
                value = str(info.get("label") or CHECK_WAITING_TEXT)
                detail = _business_readiness_detail(str(label), info)
                self._draw_status_row(right_x + 14, factory_y + 43 + i * readiness_step, right_w - 28, label, value, state, 94, min(22, readiness_step - 2), detail)

            monitor_y = factory_y + factory_h + gap
            monitor_h = main_bottom - monitor_y
            self._section(right_x, monitor_y, right_w, monitor_h, "背景監控", cyan, edge="right")
            monitor_rows = []
            monitors = cache.get("monitors", {}) if isinstance(cache, dict) else {}
            for display_name, _pattern in MONITOR_THREADS:
                info = monitors.get(display_name, {}) if isinstance(monitors, dict) else {}
                state, value = self._monitor_state_value(info)
                monitor_rows.append((display_name, value, state, str(info.get("detail") or "監控狀態未提供進一步原因。")))
            cron_summary = cache.get("cron_summary", {}) if isinstance(cache, dict) else {}
            monitor_rows.append(("定時排程", cron_summary.get("label", CHECK_WAITING_TEXT), cron_summary.get("state", "waiting"), _cron_failure_detail(cache.get("cron_details", []))))
            credential = self._credential()
            monitor_rows.append(("憑證狀態", credential.get("label", CHECK_WAITING_TEXT), credential.get("state", "waiting"), _credential_detail(credential)))
            health = cache.get("health", {}) if isinstance(cache, dict) else {}
            for key in ("guardian", "function_health"):
                info = health.get(key, {}) if isinstance(health, dict) else {}
                title = str((HEALTH_ARTIFACTS.get(key) or {}).get("label") or key)
                monitor_rows.append((title, _label_for_state(str(info.get("state") or "waiting"), OPERATIONAL_TEXT), str(info.get("state") or "waiting"), str(info.get("detail") or "健康報告未提供原因。")))
            agent_status = cache.get("agent_status", {}) if isinstance(cache, dict) else {}
            monitor_rows.append(
                (
                    "Agent 狀態",
                    str(agent_status.get("label") or "尚無活動"),
                    str(agent_status.get("state") or "idle"),
                    str(agent_status.get("detail") or "Agent 尚無公開活動摘要。"),
                )
            )
            for i, row in enumerate(monitor_rows[:9]):
                detail = row[3] if len(row) > 3 else ""
                self._draw_status_row(right_x + 14, monitor_y + 42 + i * 18, right_w - 28, row[0], row[1], row[2], 92, 17, detail)

            # 操作列
            commands = [
                ("重新整理", "refresh"),
                ("開啟首頁", "open_hub"),
                ("查看紀錄", "open_logs"),
                ("執行檢查", "run_check"),
                (
                    "切換為日" if getattr(self, "_theme", "cyber") == "cyber" else "切換為夜",
                    "toggle_theme",
                ),
            ]
            button_y = height - 58
            button_w = (width - 92) / len(commands)
            self._button_regions = []
            for i, (text, action) in enumerate(commands):
                x = 30 + i * (button_w + 8)
                self._chamfered(
                    x,
                    button_y,
                    button_w,
                    38,
                    6,
                    fill=self._color("0A1B22", 0.94),
                    stroke=self._color("31F6E2", 0.62),
                    line_width=1.0,
                )
                self._glow_line(x + 10, button_y + 5, x + button_w - 10, button_y + 5, self._color("31F6E2", 0.34), 0.65)
                self._line(x + 6, button_y + 9, x + 6, button_y + 29, self._color("31F6E2", 0.74), 1.3)
                self._tech_tag(f"0{i + 1}", x + 13, button_y + 11, 27, self._color("31F6E2"))
                self._draw_text(text, x + 43, button_y + 10, button_w - 51, 18, size=11.8, color=self._color("E9F5F1"), weight=0.58, align=NSCenterTextAlignment)
                self._button_regions.append(((x, button_y, button_w, 38), action))
            if self._action_notice:
                self._draw_text(
                    f"// {self._action_notice}",
                    width - 320,
                    button_y - 18,
                    290,
                    14,
                    size=9.2,
                    color=self._color("31F6E2"),
                    weight=0.58,
                    align=NSRightTextAlignment,
                    mono=True,
                )
            self._draw_text(
                "COMMAND DECK / SECURE LOCAL CONTROL",
                30,
                button_y - 18,
                310,
                14,
                size=8.6,
                color=muted,
                weight=0.56,
                mono=True,
            )

else:
    _CockpitWallpaperView = None
    _CockpitDashboardView = None


# ── 工具函式 ─────────────────────────────────────────────────────

def _pgrep(pattern: str) -> str:
    try:
        r = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True, timeout=3)
        pids = r.stdout.strip().split("\n")
        return pids[0] if pids[0] else ""
    except Exception:
        return ""


def _pgrep_any(patterns) -> str:
    if isinstance(patterns, str):
        patterns = (patterns,)
    for pattern in patterns:
        pid = _pgrep(str(pattern))
        if pid:
            return pid
    return ""


def _check_omlx(port: int) -> str:
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/v1/models",
            headers={"User-Agent": "MAGI-MenuBar/3.0"},
        )
        with urllib.request.urlopen(req, timeout=2) as resp:
            data = json.loads(resp.read())
            models = data.get("data", [])
            if models:
                return models[0].get("id", "")
    except Exception:
        pass
    return ""


def _check_omlx_model_memory(port: int) -> int:
    """Return model memory reported by oMLX /health, in bytes."""
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/health",
            headers={"User-Agent": "MAGI-MenuBar/3.0"},
        )
        with urllib.request.urlopen(req, timeout=2) as resp:
            data = json.loads(resp.read())
        pool = data.get("engine_pool") if isinstance(data, dict) else None
        value = pool.get("current_model_memory", 0) if isinstance(pool, dict) else 0
        return max(0, int(value or 0))
    except Exception:
        return 0


def _tcp(host: str, port: int = 445, timeout: float = 2) -> bool:
    import socket
    try:
        with socket.create_connection((host, port), timeout=timeout) as s:
            return True
    except Exception:
        return False


def _http_health(url: str, timeout: float = 3) -> str:
    """GET url, return model/status string or empty on failure."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "MAGI-MenuBar/3.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
            # /v1/models response
            models = data.get("data", [])
            if models:
                return models[0].get("id", "Active")
            # /health response
            if data.get("status") == "ok":
                return "Active"
            return "Active"
    except Exception:
        return ""


def _http_liveness(url: str, timeout: float = 1.5) -> bool:
    """Bounded HTTP probe used to distinguish a process from a live service."""
    if not url:
        return False
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "MAGI-MenuBar/3.0"})
        with urllib.request.urlopen(req, timeout=timeout) as response:
            status = getattr(response, "status", None)
            if status is None:
                status = response.getcode()
            status = int(status)
            return 200 <= status < 400
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, ValueError):
        return False
    except Exception:
        return False


def _service_liveness(process_alive: bool, name: str) -> tuple[bool, str]:
    probe_urls = {"主伺服器": SERVER_HEALTH_URL, "工具介面": TOOLS_HEALTH_URL}
    url = probe_urls.get(name)
    if url:
        if _http_liveness(url):
            return True, OPERATIONAL_TEXT
        return False, "HTTP 無回應"
    if not process_alive:
        return False, "程序未執行"
    return True, OPERATIONAL_TEXT


_LOG_TS_RE = re.compile(r'"ts"\s*:\s*"([^"]+)"')
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*m")


def _read_log_tail(path: str, max_bytes: int = 60000) -> str:
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            sz = f.tell()
            f.seek(max(0, sz - max_bytes))
            return f.read().decode("utf-8", errors="replace")
    except Exception:
        return ""


def _live_log_message(raw: str) -> str:
    message = _ANSI_ESCAPE_RE.sub("", str(raw or "")).strip()
    request = re.search(r'"(?:GET|POST|PUT|PATCH|DELETE)\s+([^\s]+)\s+HTTP/[^\s]+"\s+(\d{3})', message)
    if request:
        return f"{request.group(1)}　{request.group(2)}"
    return re.sub(r"\s+", " ", message)[:72] or "服務事件"


def _live_log_state(raw: str, level: str) -> str:
    message = _ANSI_ESCAPE_RE.sub("", str(raw or ""))
    request = re.search(r'"(?:GET|POST|PUT|PATCH|DELETE)\s+[^\s]+\s+HTTP/[^\s]+"\s+(\d{3})', message)
    if request:
        status = int(request.group(1))
        if status >= 500:
            return "attention"
        if status >= 400:
            return "waiting"
    normalized_level = str(level or "INFO").upper()
    return "attention" if normalized_level in {"ERROR", "CRITICAL"} else ("waiting" if normalized_level == "WARNING" else "ok")


def _live_log_source(logger_name: str) -> str:
    name = str(logger_name or "").lower()
    if "discord" in name:
        return "通訊機器人"
    if "cron" in name:
        return "排程服務"
    if "laf" in name:
        return "法扶模組"
    if "file" in name or "review" in name:
        return "閱卷模組"
    if "transcript" in name:
        return "筆錄模組"
    return "主伺服器"


def _live_log_events(log_tail: str, limit: int = LIVE_LOG_DISPLAY_MAX) -> list[dict]:
    events: list[dict] = []
    for raw_line in reversed(str(log_tail or "").splitlines()):
        try:
            payload = json.loads(raw_line)
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        message = _live_log_message(payload.get("msg") or "")
        if not message:
            continue
        level = str(payload.get("level") or "INFO").upper()
        state = _live_log_state(payload.get("msg") or "", level)
        stamp = str(payload.get("ts") or "")
        events.append(
            {
                "time": stamp[11:16] if len(stamp) >= 16 else "--:--",
                "source": _live_log_source(payload.get("logger") or ""),
                "state": state,
                "label": message,
            }
        )
        if len(events) >= limit:
            break
    events.reverse()
    return events


def _live_pulse_points(events: list[dict], count: int = LIVE_PULSE_POINT_COUNT) -> list[int]:
    values = {"ok": 10, "waiting": 17, "attention": 24}
    points = [values.get(str(event.get("state") or "waiting"), 17) for event in events[-count:]]
    return ([0] * max(0, count - len(points))) + points


def _extract_log_time(line: str) -> tuple[float, str]:
    m = _LOG_TS_RE.search(line)
    if not m:
        return 0.0, ""
    raw = (m.group(1) or "").strip()
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return dt.timestamp(), dt.strftime("%H:%M:%S")
    except Exception:
        return 0.0, ""


def _find_latest_log_match(log_tail: str, markers: tuple[str, ...]) -> tuple[float, str]:
    if not log_tail:
        return 0.0, ""
    for line in reversed(log_tail.splitlines()):
        if any(marker in line for marker in markers):
            return _extract_log_time(line)
    return 0.0, ""


def _service_alive(services: dict, *aliases: str) -> bool:
    if not isinstance(services, dict):
        return False
    for name in aliases:
        if name and bool(services.get(name)):
            return True
    return False


def _active_omlx_profile() -> str:
    try:
        with open(os.path.expanduser("~/.omlx/active_profile"), encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return ""


def _short_model_id(model_id: str, limit: int = 28) -> str:
    model_id = str(model_id or "").strip()
    return model_id[:limit] if len(model_id) > limit else model_id


def _model_label(keyword: str) -> str:
    keyword = str(keyword or "").lower()
    if "26b" in keyword:
        return "26B"
    if "12b" in keyword:
        return "12B"
    if "e4b" in keyword or "4b" in keyword:
        return "4B"
    return keyword.upper() if keyword else "未知"


def _is_night_profile(profile: str) -> bool:
    """Treat degraded night profile names as the night topology."""
    return str(profile or "").strip().lower().split("-", 1)[0] == "night"


def _omlx_text_status(
    model_id: str,
    expected_profile: str,
    expected_keyword: str,
    active_profile: str,
    *,
    transitioning: bool = False,
) -> dict:
    """Return user-facing text-model status for the menubar."""
    model_low = (model_id or "").lower()
    expected_keyword = (expected_keyword or "").lower()
    fallback_keywords = []
    if expected_profile == "night":
        fallback_keywords.append(NIGHT_FALLBACK_MODEL_KEYWORD.lower())
    fallback_keywords.append(DAY_FALLBACK_MODEL_KEYWORD.lower())
    fallback_keywords = [item for item in dict.fromkeys(fallback_keywords) if item]
    expected_profile = expected_profile or "day"
    active_profile = active_profile or ""

    profile_zh = "日間" if expected_profile == "day" else "夜間"
    expected_label = _model_label(expected_keyword)

    if transitioning:
        current = _short_model_id(model_id) if model_id else "主模型重啟中"
        return {
            "icon": "🟡",
            "label": f"{profile_zh}模型切換中・{current}",
            "degraded": False,
            "ok": False,
            "mismatch": False,
            "transitioning": True,
        }

    allowed_active = {expected_profile}
    if expected_profile == "day":
        allowed_active.add("day-e4b-degraded")
    if expected_profile == "night":
        allowed_active.add("night-e4b-degraded")
        allowed_active.add("night-12b-degraded")
    profile_mismatch = bool(active_profile and active_profile not in allowed_active)

    if not model_id:
        return {
            "icon": "🔴",
            "label": f"{profile_zh}主模型離線（預期{expected_label}）",
            "degraded": False,
            "ok": False,
            "mismatch": True,
        }

    if expected_keyword and expected_keyword in model_low:
        label = f"{profile_zh}{expected_label}  {_short_model_id(model_id)}"
        if profile_mismatch:
            label += f"・profile={active_profile}"
        return {
            "icon": "🟡" if profile_mismatch else "🟢",
            "label": label,
            "degraded": False,
            "ok": not profile_mismatch,
            "mismatch": profile_mismatch,
        }

    fallback_keyword = next((item for item in fallback_keywords if item in model_low), "")
    if fallback_keyword:
        fallback_label = (
            "E4B"
            if fallback_keyword == DAY_FALLBACK_MODEL_KEYWORD.lower()
            else _model_label(fallback_keyword)
        )
        label = f"{profile_zh}降級{fallback_label}（預期{expected_label}）"
        if profile_mismatch:
            label += f"・profile={active_profile}"
        return {
            "icon": "🟡",
            "label": label,
            "degraded": True,
            "ok": not profile_mismatch,
            "mismatch": profile_mismatch,
        }

    label = f"{profile_zh}模型不符：{_short_model_id(model_id)}（預期{expected_label}）"
    if profile_mismatch:
        label += f"・profile={active_profile}"
    return {
        "icon": "🔴",
        "label": label,
        "degraded": False,
        "ok": False,
        "mismatch": True,
    }


_MENUBAR_PROCESS_ZOMBIE_TRACKER = ZombiePersistence()


def _collect_process_health() -> dict:
    """Use the same process snapshot and taxonomy as Web/Golem."""
    return _collect_shared_process_monitor(
        magi_root=Path(MAGI_ROOT),
        run_ps=subprocess.run,
        zombie_tracker=_MENUBAR_PROCESS_ZOMBIE_TRACKER,
    )


def _process_anomaly_detail(process_health: dict) -> str:
    summary = process_health.get("summary", {}) if isinstance(process_health, dict) else {}
    return (
        f"孤兒 {int(summary.get('orphan_count', 0) or 0)}／"
        f"殭屍 {int(summary.get('zombie_count', 0) or 0)}／"
        f"重複 {int(summary.get('duplicate_groups', 0) or 0)}"
    )


def _count_zombies() -> tuple:
    """Backward-compatible view backed by the unified process collector."""
    result = _collect_process_health()
    count = int((result.get("summary") or {}).get("zombie_count", 0) or 0)
    return count, _process_anomaly_detail(result)


_MEM_MODULES = [
    ("Server",        "api/server.py"),
    ("Discord Bot",   "api/discord_bot.py"),
    ("Tools API",     "api/tools_api.py"),
    ("oMLX Text",     "omlx serve.*--port 8080"),
    ("oMLX Embed",    "omlx serve.*--port 8081"),
    ("FAISS Rebuild", "MEMORY_ENABLE_FAISS"),
    ("File Review",   "file_review_auto_worker\\.py|file-review-orchestrator/action\\.py"),
    ("LAF Orch",      "laf_orchestrator\\.py|laf-portal-automation/action\\.py"),
    ("Autopilot",     "magi-autopilot/action\\.py"),
    ("Selenium",      "chromedriver"),
]
_MEM_ENGINE_MODULES = frozenset({"oMLX Text", "oMLX Embed"})
_MEM_WORKLOAD_MODULES = frozenset({"FAISS Rebuild", "File Review", "LAF Orch", "Autopilot", "Selenium"})


def _get_module_memory() -> list:
    import re
    results = []
    try:
        r = subprocess.run(["ps", "-eo", "pid=,ppid=,rss=,command="], capture_output=True, text=True, timeout=5)
        processes = []
        children: dict[int, list[int]] = {}
        for line in r.stdout.splitlines():
            parts = line.strip().split(None, 3)
            if len(parts) < 4:
                continue
            try:
                pid, ppid, rss_kb = int(parts[0]), int(parts[1]), int(parts[2])
            except ValueError:
                continue
            processes.append((pid, ppid, rss_kb, parts[3]))
            children.setdefault(ppid, []).append(pid)
        by_pid = {pid: (rss_kb, command) for pid, _ppid, rss_kb, command in processes}

        for mod_name, pattern in _MEM_MODULES:
            regex = re.compile(pattern)
            roots = {pid for pid, _ppid, _rss, command in processes if regex.search(command)}
            owned = set(roots)
            stack = list(roots)
            while stack:
                for child in children.get(stack.pop(), ()):
                    if child not in owned:
                        owned.add(child)
                        stack.append(child)
            if owned:
                total_rss = sum(by_pid.get(pid, (0, ""))[0] for pid in owned)
                results.append((mod_name, total_rss // 1024, len(owned)))
    except Exception:
        pass
    results.sort(key=lambda x: x[1], reverse=True)
    return results


def _memory_breakdown(module_memory: list) -> tuple[int, int, int, int]:
    """Return total/core/workload/engine RSS as distinct operational classes."""
    total = sum(max(0, int(item[1] or 0)) for item in module_memory if len(item) >= 2)
    engine = sum(
        max(0, int(item[1] or 0))
        for item in module_memory
        if len(item) >= 2 and str(item[0]) in _MEM_ENGINE_MODULES
    )
    workload = sum(
        max(0, int(item[1] or 0))
        for item in module_memory
        if len(item) >= 2 and str(item[0]) in _MEM_WORKLOAD_MODULES
    )
    core = max(0, total - engine - workload)
    return total, core, workload, engine


def _get_system_memory() -> tuple:
    try:
        import psutil
        m = psutil.virtual_memory()
        return m.total / (1024**3), m.available / (1024**3), m.percent
    except ImportError:
        return 0, 0, 0


def _get_node_ip(name: str) -> str:
    """Get node IP from registry with fallback."""
    try:
        from api.routing.node_registry import get_node_ip
        return get_node_ip(name) or ""
    except Exception:
        return ""


def _get_disk_usage(path: str) -> tuple:
    """Return (used_gb, total_gb, percent) for a mount point, or None."""
    try:
        if str(path or "").startswith(_USER_MOUNT_ROOT) and os.environ.get(
            "MAGI_MENUBAR_NAS_DISK_USAGE", ""
        ).strip().lower() not in {"1", "true", "yes", "on"}:
            return None
        if not (os.path.ismount(path) or os.path.isdir(path)):
            return None
        st = os.statvfs(path)
        total = st.f_blocks * st.f_frsize
        free = st.f_bavail * st.f_frsize
        used = total - free
        total_gb = total / (1024 ** 3)
        used_gb = used / (1024 ** 3)
        pct = (used / total * 100) if total > 0 else 0
        return (used_gb, total_gb, pct)
    except Exception:
        return None


def _synology_drive_fallback_path() -> str:
    for path in _SYNOLOGY_DRIVE_CANDIDATES:
        try:
            if os.path.isdir(path) and os.listdir(path):
                return path
        except OSError:
            continue
    return ""


def _load_cron_jobs() -> list:
    """Load cron_jobs.json and return list of job dicts."""
    try:
        path = _configured_path("MAGI_CRON_JOBS_FILE", os.path.join(MAGI_ROOT, "cron_jobs.json"))
        with open(path, "r", encoding="utf-8") as f:
            jobs = json.load(f)
        return [j for j in jobs if isinstance(j, dict)]
    except Exception:
        return []


def _runtime_dir_path() -> str:
    raw = os.environ.get("MAGI_RUNTIME_DIR", "").strip()
    return raw if raw else os.path.join(MAGI_ROOT, ".runtime")


def _menubar_theme_path() -> str:
    return os.path.join(_runtime_dir_path(), "menubar_theme.json")


def _load_menubar_theme() -> str:
    payload = _load_json_file(_menubar_theme_path())
    value = str(payload.get("theme") or "") if isinstance(payload, dict) else ""
    return _normalize_menubar_theme(value)


def _save_menubar_theme(theme: str) -> str:
    value = _normalize_menubar_theme(theme)
    path = _menubar_theme_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump({"theme": value, "updated_at": datetime.now().isoformat()}, handle, ensure_ascii=False)
    os.replace(tmp, path)
    return value


def _load_cron_state() -> dict:
    data = _load_json_file(os.path.join(_runtime_dir_path(), "cron_state.json"))
    return data if isinstance(data, dict) else {}


def _first_nonempty(mapping: dict, *keys: str) -> str:
    if not isinstance(mapping, dict):
        return ""
    for key in keys:
        value = mapping.get(key)
        if value not in (None, ""):
            return str(value)
    return ""


def _cron_returncode(value) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except Exception:
        return None


def _cron_resource_protection_active(job_id: str, state_item: dict) -> bool:
    if job_id != "job_resource_governor" or not isinstance(state_item, dict):
        return False
    returncode = _cron_returncode(state_item.get("returncode", state_item.get("last_returncode")))
    timed_out = bool(state_item.get("timed_out") or state_item.get("last_timed_out"))
    if timed_out or returncode != 2:
        return False
    state_text = "\n".join(
        str(state_item.get(key) or "")
        for key in (
            "last_error",
            "last_stdout_tail",
            "last_stderr_tail",
            "stdout",
            "stderr",
        )
    ).lower()
    return (
        ('"ok": false' in state_text or "'ok': false" in state_text)
        and (
            '"level": "critical"' in state_text
            or "'level': 'critical'" in state_text
        )
        and any(
            marker in state_text
            for marker in (
                "free_plus_inactive",
                "disk_free_gb",
                "swap_used_gb",
                "pause_heavy_backlog_jobs",
            )
        )
    )


def _cron_state_failed(state_item: dict, job_id: str = "") -> bool:
    if not isinstance(state_item, dict) or not state_item:
        return False
    returncode = _cron_returncode(state_item.get("returncode", state_item.get("last_returncode")))
    timed_out = bool(state_item.get("timed_out") or state_item.get("last_timed_out"))
    last_success = state_item.get("last_success")
    last_status = str(state_item.get("last_status") or "").strip().lower()
    last_error = str(state_item.get("last_error") or "").strip().lower()
    retry = state_item.get("v3_retry")
    if (
        isinstance(retry, dict)
        and str(retry.get("status") or "").strip().lower() in {"queued", "running"}
    ):
        return False
    if _cron_resource_protection_active(job_id, state_item):
        return False
    state_text = "\n".join(
        str(state_item.get(key) or "")
        for key in (
            "last_status",
            "last_error",
            "last_stdout_tail",
            "last_stderr_tail",
            "stdout",
            "stderr",
        )
    ).lower()
    returncode = _cron_returncode(state_item.get("returncode", state_item.get("last_returncode")))
    deferred = (
        last_status in {"deferred", "partial"}
        or "resource_guard_skipped" in last_error
        or '"status": "partial"' in state_text
        or "'status': 'partial'" in state_text
        or returncode == 75
    )
    upstream_waiting = any(
        marker in state_text
        for marker in (
            "upstream",
            "external service",
            "external_service",
            "http 500",
            "temporarily unavailable",
            "service unavailable",
        )
    )
    if (deferred or upstream_waiting) and not timed_out and returncode in (None, 0, 75):
        return False
    return timed_out or last_success is False or (returncode is not None and returncode != 0)


def _cron_safe_validation_rejection(job: dict, state_item: dict) -> bool:
    """A rejected distill candidate means the deployment guard worked."""
    job_id = str(job.get("id") or "")
    command = str(job.get("command") or "")
    if "distill_train_gemma" not in job_id and "nightly_distill_gemma.py" not in command:
        return False
    state_text = "\n".join(
        str(state_item.get(key) or "")
        for key in ("last_error", "last_stderr_tail", "last_stdout_tail", "error", "stderr", "stdout")
    ).lower()
    return any(
        marker in state_text
        for marker in (
            "validation gate",
            "candidate_rejected",
            "candidate rejected",
            "channel_marker_leak",
            "insufficient_traditional_chinese",
            "too_much_english",
            "blocked from deploy",
            "deploy_allowed=false",
        )
    ) or ("v011" in state_text and "reclaim" in state_text)


def _cron_display_timestamp(job: dict, state_item: dict) -> str:
    last_status = str(state_item.get("last_status") or "").strip().lower()
    if _cron_state_failed(state_item, str(job.get("id") or "")) or last_status == "deferred":
        return _first_nonempty(
            state_item,
            "last_result_at",
            "last_complete_at",
            "last_dispatch_at",
            "last_dispatch",
            "last_run",
        )
    state_ts = _first_nonempty(
        state_item,
        "last_success_at",
        "last_complete_at",
        "last_result_at",
        "last_dispatch_at",
        "last_dispatch",
        "last_run",
    )
    return state_ts or _first_nonempty(job, "last_success_at", "last_run")


def _cron_expected_interval_hours(expr: str) -> float | None:
    parts = str(expr or "").split()
    if len(parts) != 5:
        return None
    minute, hour, day_of_month, _month, day_of_week = parts
    if day_of_month not in {"*", "?"}:
        return 31 * 24.0
    if day_of_week not in {"*", "?"}:
        return 7 * 24.0
    if hour in {"*", "*/1"}:
        if minute.startswith("*/"):
            try:
                return max(1.0 / 60.0, int(minute[2:]) / 60.0)
            except Exception:
                return 1.0
        return 1.0
    if hour.startswith("*/"):
        try:
            return float(max(1, int(hour[2:])))
        except Exception:
            return 1.0
    if "," in hour:
        slots = [part for part in hour.split(",") if part.strip()]
        if slots:
            return max(1.0, 24.0 / len(slots))
    return 24.0


def _cron_stale_threshold_hours(expr: str) -> float:
    interval = _cron_expected_interval_hours(expr)
    if interval is None:
        return 72.0
    return max(2.0, round(interval * 2.5 + 6.0, 3))


def _active_release_cron_boundary() -> tuple[datetime | None, str]:
    """Return the current deployment boundary used to archive old failures."""

    path = _configured_path(
        "MAGI_V3_ACTIVE_RELEASE_MARKER",
        os.path.expanduser(
            "~/Library/Application Support/MAGI/runtime/active-release.json"
        ),
    )
    payload = _load_json_file(path)
    if not isinstance(payload, dict):
        return None, ""
    return (
        _parse_dt(str(payload.get("committed_at") or "")),
        str(payload.get("release_id") or "").strip(),
    )


def _cron_local_timezone():
    """Return the timezone used by legacy naive cron-state timestamps."""

    try:
        return ZoneInfo(os.environ.get("MAGI_TIMEZONE") or "Asia/Taipei")
    except (ValueError, ZoneInfoNotFoundError):
        return datetime.now().astimezone().tzinfo


def _cron_details_from_state(jobs: list, cron_state: dict, *, now: datetime | None = None) -> list[dict]:
    """Build all cron details before limiting display so failures cannot be hidden."""
    details = []
    release_committed_at, active_release_id = _active_release_cron_boundary()
    for job in jobs:
        if not isinstance(job, dict) or not job.get("enabled", True):
            continue
        job_id = str(job.get("id") or "").strip()
        state_item = cron_state.get(job_id) if isinstance(cron_state.get(job_id), dict) else {}
        desc = str(job.get("desc") or job.get("command", "")[:30]).strip()
        cron_expr = str(job.get("cron", "")).strip()
        last_run = _cron_display_timestamp(job, state_item)
        safe_rejection = _cron_safe_validation_rejection(job, state_item)
        last_status = str(state_item.get("last_status") or "").strip().lower()
        returncode = _cron_returncode(state_item.get("returncode", state_item.get("last_returncode")))
        semantic_text = "\n".join(
            str(state_item.get(key) or "")
            for key in ("last_status", "last_error", "last_stdout_tail", "last_stderr_tail")
        ).lower()
        pending_occurrence = (
            state_item.get("v3_pending_occurrence")
            if isinstance(state_item.get("v3_pending_occurrence"), dict)
            else {}
        )
        pending_status = str(pending_occurrence.get("status") or "").strip().lower()
        occurrence_pending = pending_status in {"queued", "running"}
        retry_occurrence = (
            state_item.get("v3_retry")
            if isinstance(state_item.get("v3_retry"), dict)
            else {}
        )
        retry_status = str(retry_occurrence.get("status") or "").strip().lower()
        automatic_retry_waiting = (
            retry_status in {"queued", "running"}
        )
        semantic_job_waiting = (
            job_id == "job_1770705679"
            and "all_judgment_reason_searches_failed" in semantic_text
            and last_status not in {"ok", "passed", "success"}
        )
        resource_waiting = (
            "resource_guard_skipped" in semantic_text
            or _cron_resource_protection_active(job_id, state_item)
        )
        large_offpeak_waiting = (
            job_id == "job_nas_pdf_ocr_worker_offpeak"
            and (
                "deferred_large" in semantic_text
                or "large ocr file to off-peak" in semantic_text
                or "large_files_waiting_for_offpeak_window" in semantic_text
            )
        )
        semantic_collision_waiting = (
            job_id == "job_drive_case_sync_all_files"
            and (
                "semantic_collision" in semantic_text
                or "semantic_path_collision" in semantic_text
            )
        )
        terminal_deferred = (
            last_status in {"deferred", "partial"}
            or (
                returncode == 75
                and last_status not in {"ok", "passed", "success"}
            )
        )
        partial_waiting = terminal_deferred and (
            returncode == 75
            or '"status": "partial"' in semantic_text
            or "'status': 'partial'" in semantic_text
        )
        storage_waiting = any(
            marker in semantic_text
            for marker in (
                "storage_unavailable",
                "device not configured",
                "socket is not connected",
                "stale file handle",
            )
        )
        review_required = bool(
            state_item.get("last_review_required")
            or state_item.get("last_candidate_rejected")
        )
        active_deferred = (
            safe_rejection
            or review_required
            or automatic_retry_waiting
            or semantic_job_waiting
            or (
                occurrence_pending
                and (
                    terminal_deferred
                    or resource_waiting
                    or partial_waiting
                )
            )
        )
        historical_deferred = terminal_deferred and not active_deferred
        wait_reason = ""
        if active_deferred:
            if safe_rejection:
                wait_reason = "candidate_rejected"
            elif automatic_retry_waiting:
                wait_reason = "auto_repair"
            elif semantic_collision_waiting:
                wait_reason = "semantic_collision"
            elif storage_waiting:
                wait_reason = "storage"
            elif semantic_job_waiting or any(
                marker in semantic_text
                for marker in (
                    "upstream",
                    "external service",
                    "external_service",
                    "http 500",
                    "temporarily unavailable",
                    "service unavailable",
                )
            ):
                wait_reason = "upstream"
            elif resource_waiting:
                wait_reason = "resource"
            elif large_offpeak_waiting:
                wait_reason = "large_offpeak"
            else:
                wait_reason = "partial"
        failed = (
            _cron_state_failed(state_item, job_id)
            and not safe_rejection
            and not semantic_job_waiting
        )
        result_at = _parse_dt(
            _first_nonempty(
                state_item,
                "last_result_at",
                "last_complete_at",
                "last_failure_at",
                "last_dispatch_at",
                "last_run",
            )
        )
        boundary_at = release_committed_at
        if result_at and boundary_at:
            if result_at.tzinfo and boundary_at.tzinfo is None:
                local_zone = _cron_local_timezone()
                boundary_at = boundary_at.replace(tzinfo=local_zone).astimezone(
                    result_at.tzinfo
                )
            elif boundary_at.tzinfo and result_at.tzinfo is None:
                boundary_at = boundary_at.astimezone(
                    _cron_local_timezone()
                ).replace(tzinfo=None)
        superseded_by_release = bool(
            (failed or semantic_collision_waiting)
            and result_at
            and boundary_at
            and result_at < boundary_at
            and active_release_id
            and active_release_id in str(job.get("command") or "")
        )
        if superseded_by_release:
            failed = False
        stale = False
        if last_run:
            try:
                dt = _parse_dt(last_run)
                if dt:
                    reference = now
                    if reference is None:
                        reference = datetime.now(dt.tzinfo) if dt.tzinfo else datetime.now()
                    elif dt.tzinfo and reference.tzinfo is None:
                        reference = reference.replace(tzinfo=dt.tzinfo)
                    stale = (
                        not occurrence_pending
                        and (reference - dt).total_seconds() / 3600 > _cron_stale_threshold_hours(cron_expr)
                    )
            except Exception:
                stale = False
        status = (
            "failed"
            if failed
            else (
                "deferred"
                if active_deferred
                else (
                    "stale"
                    if stale
                    else (
                        "queued"
                        if occurrence_pending
                        else (
                            "history"
                            if historical_deferred
                            else ("waiting" if not last_run else "ok")
                        )
                    )
                )
            )
        )
        if superseded_by_release:
            status = "waiting"
        details.append(
            {
                "id": job_id,
                "desc": desc[:25],
                "cron": cron_expr,
                "relative": _parse_last_run(last_run, now=now),
                "status": status,
                "stale": stale,
                "safe_rejection": safe_rejection,
                "historical_deferred": historical_deferred,
                "superseded_by_release": superseded_by_release,
                "wait_reason": wait_reason,
            }
        )
    priority = {
        "failed": 0,
        "stale": 1,
        "deferred": 2,
        "waiting": 3,
        "queued": 4,
        "history": 4,
        "ok": 4,
    }
    return sorted(details, key=lambda detail: (priority.get(str(detail.get("status")), 4), str(detail.get("id") or "")))


def _parse_dt(raw: str) -> datetime | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except Exception:
        return None


def _parse_last_run(iso_str: str, *, now: datetime | None = None) -> str:
    """Convert ISO timestamp to relative time string like '2小時前'."""
    if not iso_str:
        return "從未"
    try:
        dt = _parse_dt(iso_str)
        if dt is None:
            return iso_str[:16]
        reference = now
        if reference is None:
            reference = datetime.now(dt.tzinfo) if dt.tzinfo else datetime.now()
        elif dt.tzinfo and reference.tzinfo is None:
            reference = reference.replace(tzinfo=dt.tzinfo)
        delta = reference - dt
        secs = delta.total_seconds()
        if secs < 0:
            return "排程中"
        if secs < 60:
            return "剛剛"
        if secs < 3600:
            return f"{int(secs // 60)}分鐘前"
        if secs < 86400:
            return f"{int(secs // 3600)}小時前"
        return f"{int(secs // 86400)}天前"
    except Exception:
        return iso_str[:16] if iso_str else "從未"


def _load_json_file(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _knowledge_radar_snapshot(*, force: bool = False) -> dict:
    """Build a truthful, lightweight knowledge graph snapshot.

    The source indexes are sizeable, so the files are parsed only when their
    size or modification time changes.  The 5-second Menubar refresh therefore
    performs cheap stat calls instead of repeatedly loading the full vault
    index.
    """
    agent_paths = {
        "vault": _agent_path("obsidian_vault_config.json"),
        "obsidian": _agent_path("obsidian_index.json"),
        "wiki": _agent_path("wiki_synthesizer_state.json"),
        "vectors": _agent_path("doc_vector_index.json"),
        "quality": _mutable_static_path("knowledge_lint_latest.json"),
        "judgment_progress": os.path.join(
            _runtime_dir_path(),
            "legacy_judgment_resummary_latest.json",
        ),
    }
    signature = []
    for key, path in agent_paths.items():
        try:
            stat = os.stat(path)
            signature.append((key, int(stat.st_mtime_ns), int(stat.st_size)))
        except OSError:
            signature.append((key, 0, 0))
    signature = tuple(signature)

    with _KNOWLEDGE_RADAR_LOCK:
        if not force and _KNOWLEDGE_RADAR_MEMO.get("signature") == signature:
            return dict(_KNOWLEDGE_RADAR_MEMO.get("snapshot") or {})

        vault_config = _load_json_file(agent_paths["vault"])
        obsidian = _load_json_file(agent_paths["obsidian"])
        wiki = _load_json_file(agent_paths["wiki"])
        vectors = _load_json_file(agent_paths["vectors"])
        quality = _load_json_file(agent_paths["quality"])
        judgment_progress = _load_json_file(agent_paths["judgment_progress"])

        notes = obsidian.get("notes") if isinstance(obsidian.get("notes"), dict) else {}
        note_total = len(notes)
        vectorized_notes = 0
        obsidian_chunks = 0
        recent_notes = 0
        now = time.time()
        for info in notes.values():
            info = info if isinstance(info, dict) else {}
            chunks = max(0, int(info.get("chunks") or 0))
            obsidian_chunks += chunks
            vectorized_notes += int(chunks > 0)
            ingested_epoch = _epoch_from_iso(str(info.get("ingested_at") or ""))
            if ingested_epoch and 0 <= now - ingested_epoch <= 86400:
                recent_notes += 1

        cases = wiki.get("cases") if isinstance(wiki.get("cases"), dict) else {}
        wiki_cases = len(cases)
        wiki_pages = 0
        complete_cases = 0
        for info in cases.values():
            info = info if isinstance(info, dict) else {}
            pages = info.get("wiki_pages") if isinstance(info.get("wiki_pages"), dict) else {}
            required = info.get("required_pages") if isinstance(info.get("required_pages"), list) else []
            wiki_pages += len(pages)
            if required and all(str(name) in pages for name in required):
                complete_cases += 1

        vector_docs = len(vectors)
        vector_chunks = sum(
            max(0, int((info or {}).get("chunks_written") or 0))
            for info in vectors.values()
            if isinstance(info, dict)
        )
        checks = {
            str(item.get("check") or ""): item
            for item in quality.get("checks", [])
            if isinstance(item, dict)
        }
        obsidian_quality = checks.get("obsidian_summary_quality", {})
        quality_notes = max(0, int(obsidian_quality.get("total_notes") or 0))
        bad_notes = max(0, int(obsidian_quality.get("bad_notes") or 0))
        obsidian_quality_pct = (
            max(0.0, min(100.0, (quality_notes - bad_notes) * 100.0 / quality_notes))
            if quality_notes
            else 0.0
        )
        insight_quality = checks.get("insight_quality", {})
        insight_quality_pct = max(
            0.0,
            min(100.0, float(insight_quality.get("health_pct") or 0.0)),
        )
        judgment_quality = checks.get("judgment_summary_quality", {})
        judgment_tables = (
            judgment_quality.get("tables")
            if isinstance(judgment_quality.get("tables"), dict)
            else {}
        )
        primary_judgments = (
            judgment_tables.get("court_judgments")
            if isinstance(judgment_tables.get("court_judgments"), dict)
            else {}
        )
        judgment_total = max(0, int(primary_judgments.get("total") or 0))
        judgment_missing = max(0, int(primary_judgments.get("missing_summary") or 0))
        judgment_issues = max(0, int(primary_judgments.get("quality_issue_count") or 0))
        has_source_metrics = "source_ready" in primary_judgments
        judgment_source_ready = max(
            0,
            int(primary_judgments.get("source_ready") or judgment_total),
        )
        judgment_usable = max(
            0,
            int(
                primary_judgments.get("source_usable")
                if has_source_metrics
                else judgment_total - judgment_missing - judgment_issues
            ),
        )
        judgment_backlog = max(
            0,
            int(
                primary_judgments.get(
                    "pending_source_backlog",
                    primary_judgments.get("source_backlog"),
                )
                if has_source_metrics
                else judgment_missing + judgment_issues
            ),
        )
        judgment_reviewed_no_insight = max(
            0,
            int(primary_judgments.get("reviewed_no_usable_insight") or 0),
        )
        judgment_pending_nvidia = max(
            0,
            int(primary_judgments.get("pending_nvidia_review") or 0),
        )
        judgment_awaiting_source = max(
            0,
            int(primary_judgments.get("awaiting_source") or 0),
        )
        judgment_quality_pct = (
            max(0.0, min(100.0, judgment_usable * 100.0 / judgment_source_ready))
            if judgment_source_ready
            else 0.0
        )
        progress_status = str(judgment_progress.get("status") or "")
        progress_ok = bool(judgment_progress.get("ok", judgment_progress.get("success", False)))
        progress_epoch = _epoch_from_iso(str(judgment_progress.get("updated_at") or ""))
        progress_age = max(0.0, now - progress_epoch) if progress_epoch else None
        progress_recent = bool(
            progress_ok
            and progress_status in {
                "running",
                "completed",
                "completed_with_failures",
                "already_running",
            }
            and progress_age is not None
            and progress_age <= 2 * 3600
        )
        progress_updated = max(0, int(judgment_progress.get("updated") or 0))
        progress_local_updated = max(0, int(judgment_progress.get("local_updated") or 0))
        progress_nvidia_updated = max(0, int(judgment_progress.get("nvidia_updated") or 0))
        progress_queued = max(0, int(judgment_progress.get("local_queued") or 0))
        scheduled_daily_capacity = max(
            1,
            int(judgment_progress.get("first_pass_daily_capacity") or 240 * 96),
        )
        judgment_eta_days = (
            max(1, int(math.ceil(judgment_backlog / scheduled_daily_capacity)))
            if judgment_backlog
            else 0
        )
        judgment_node_state = (
            "ok" if not judgment_backlog else
            "processing" if progress_recent else
            "attention"
        )

        coverage_pct = vectorized_notes * 100.0 / note_total if note_total else 0.0
        graph_pct = complete_cases * 100.0 / wiki_cases if wiki_cases else 0.0
        update_epochs = [
            _epoch_from_iso(str(obsidian.get("updated_at") or "")),
            _epoch_from_iso(str(wiki.get("updated_at") or "")),
            _epoch_from_iso(str(quality.get("scan_time") or "")),
        ]
        latest_epoch = max(update_epochs or [0.0])
        age_sec = max(0.0, now - latest_epoch) if latest_epoch else None
        if age_sec is None:
            freshness_pct = 0.0
        elif age_sec <= 86400:
            freshness_pct = 100.0
        elif age_sec <= 3 * 86400:
            freshness_pct = 75.0
        elif age_sec <= 7 * 86400:
            freshness_pct = 45.0
        else:
            freshness_pct = 20.0

        available_sources = sum(bool(value) for value in (notes, cases, vectors, checks))
        if available_sources:
            score = int(
                round(
                    coverage_pct * 0.22
                    + graph_pct * 0.18
                    + obsidian_quality_pct * 0.20
                    + insight_quality_pct * 0.15
                    + judgment_quality_pct * 0.15
                    + freshness_pct * 0.10
                )
            )
        else:
            score = 0
        grade = (
            "S" if score >= 95 else
            "A" if score >= 85 else
            "B" if score >= 70 else
            "C" if score >= 55 else
            "D" if available_sources else "--"
        )
        state = (
            "waiting" if not available_sources else
            "attention" if score < 55 else
            "waiting" if score < 70 else
            "ok"
        )
        vault_path = str(vault_config.get("vault_path") or "")
        vault_name = str(vault_config.get("vault_name") or "Obsidian")
        updated_label = (
            datetime.fromtimestamp(latest_epoch).strftime("%m-%d %H:%M")
            if latest_epoch
            else CHECK_WAITING_TEXT
        )
        nodes = [
            {
                "key": "obsidian",
                "label": "OBSIDIAN",
                "value": f"{note_total:,}",
                "score": obsidian_quality_pct,
                "detail": (
                    f"{vault_name}：索引 {note_total:,} 筆，24 小時新增 {recent_notes:,} 筆；"
                    f"摘要品質 {obsidian_quality_pct:.0f}%。"
                ),
            },
            {
                "key": "wiki",
                "label": "WIKI GRAPH",
                "value": f"{wiki_cases:,}案",
                "score": graph_pct,
                "detail": (
                    f"關聯案件 {wiki_cases:,} 件、圖譜頁面 {wiki_pages:,} 頁；"
                    f"完整案件 {complete_cases:,} 件。"
                ),
            },
            {
                "key": "vector",
                "label": "VECTOR",
                "value": f"{vectorized_notes:,}/{note_total:,}",
                "score": coverage_pct,
                "detail": (
                    f"已向量化筆記 {vectorized_notes:,}/{note_total:,}；"
                    f"Obsidian chunks {obsidian_chunks:,}，向量文件 {vector_docs:,}／chunks {vector_chunks:,}。"
                ),
            },
            {
                "key": "judgment",
                "label": "JUDGMENT",
                "value": f"{judgment_quality_pct:.0f}%{'↗' if judgment_node_state == 'processing' else ''}",
                "score": judgment_quality_pct,
                "state": judgment_node_state,
                "detail": (
                    f"具備可核對全文 {judgment_source_ready:,} 筆，其中高品質摘要 {judgment_usable:,} 筆；"
                    f"待審查 {judgment_backlog:,}，已審查但無可用見解 {judgment_reviewed_no_insight:,}，"
                    f"等待完整來源 {judgment_awaiting_source:,}，NVIDIA 複核佇列 {judgment_pending_nvidia:,}。"
                    + (
                        f"兩階段補摘要管線正常：本輪本機高信心寫入 {progress_local_updated:,} 筆、"
                        f"NVIDIA 複核寫入 {progress_nvidia_updated:,} 筆、送交複核 {progress_queued:,} 筆"
                        f"（合計寫入 {progress_updated:,}）；估計約 {judgment_eta_days} 天完成本機首輪掃描。"
                        if progress_recent and judgment_backlog
                        else "補摘要管線尚無兩小時內的成功證據，需檢查排程。"
                        if judgment_backlog
                        else "目前沒有可處理 backlog。"
                    )
                ),
            },
            {
                "key": "insight",
                "label": "INSIGHT",
                "value": f"{insight_quality_pct:.0f}%",
                "score": insight_quality_pct,
                "detail": (
                    f"實務見解品質 {insight_quality_pct:.1f}%；"
                    f"健康 {int(insight_quality.get('healthy') or 0):,}/"
                    f"{int(insight_quality.get('total_insights') or 0):,}。"
                ),
            },
        ]
        snapshot = {
            "state": state,
            "score": score,
            "grade": grade,
            "label": f"知識等級 {grade}・{score}",
            "updated_label": updated_label,
            "age_sec": int(age_sec) if age_sec is not None else None,
            "vault_path": vault_path,
            "vault_name": vault_name,
            "sources": available_sources,
            "coverage_pct": round(coverage_pct, 1),
            "graph_pct": round(graph_pct, 1),
            "obsidian_quality_pct": round(obsidian_quality_pct, 1),
            "insight_quality_pct": round(insight_quality_pct, 1),
            "judgment_quality_pct": round(judgment_quality_pct, 1),
            "freshness_pct": round(freshness_pct, 1),
            "nodes": nodes,
            "detail": (
                f"知識等級 {grade}（{score}/100）。向量覆蓋 {coverage_pct:.1f}%、"
                f"Wiki 圖譜完整 {graph_pct:.1f}%、Obsidian 摘要品質 {obsidian_quality_pct:.1f}%、"
                f"實務見解品質 {insight_quality_pct:.1f}%、裁判高品質摘要覆蓋 {judgment_quality_pct:.1f}%。"
            ),
        }
        _KNOWLEDGE_RADAR_MEMO["signature"] = signature
        _KNOWLEDGE_RADAR_MEMO["snapshot"] = dict(snapshot)
        return snapshot


def _public_agent_percent(value) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "尚無活動"
    if 0.0 <= number <= 1.0:
        number *= 100.0
    if not 0.0 <= number <= 100.0:
        return "尚無活動"
    return f"{number:.0f}%"


def _public_agent_count(value) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return min(99, max(0, number))


def _agent_status_from_public_payload(payload: dict) -> dict:
    """Allowlist a public agent summary; never expose prompts, messages, or reasoning."""
    empty = {
        "state": "idle",
        "label": "系統正常待命",
        "intent": "尚無活動",
        "plan": "尚無活動",
        "tool": "尚無活動",
        "model": "尚無活動",
        "confirmation": "尚無活動",
        "retry": "尚無活動",
        "route_confidence": "尚無活動",
        "success_rate_7d": "尚無活動",
        "detail": "系統正常待命；目前沒有正在執行的公開長任務，背景排程不受影響。",
    }
    if not isinstance(payload, dict):
        return empty

    intent = _PUBLIC_AGENT_INTENTS.get(str(payload.get("intent_category") or "").strip().lower(), "")
    steps = []
    raw_steps = payload.get("plan_steps") if isinstance(payload.get("plan_steps"), list) else []
    for item in raw_steps[:5]:
        if not isinstance(item, dict):
            continue
        step = _PUBLIC_AGENT_PLAN_STEPS.get(str(item.get("id") or "").strip().lower())
        step_state = _PUBLIC_AGENT_STEP_STATES.get(str(item.get("state") or "").strip().lower())
        if step and step_state:
            steps.append(f"{step}：{step_state}")
    tool = _PUBLIC_AGENT_TOOL_CATEGORIES.get(str(payload.get("tool_category") or "").strip().lower(), "")
    model_id = str(payload.get("model_id") or "").strip()
    model = model_id if _PUBLIC_AGENT_MODEL_RE.fullmatch(model_id) else ""
    confirmation_known = "waiting_confirmation" in payload and isinstance(payload.get("waiting_confirmation"), bool)
    waiting_confirmation = bool(payload.get("waiting_confirmation")) if confirmation_known else False
    retry_count = _public_agent_count(payload.get("retry_count"))
    route_confidence = _public_agent_percent(payload.get("route_confidence"))
    success_rate = _public_agent_percent(payload.get("success_rate_7d"))
    has_activity = bool(intent or steps or tool or model or confirmation_known or retry_count is not None or route_confidence != "尚無活動")
    if not has_activity:
        return empty

    public_state = str(payload.get("status") or "").strip().lower()
    plan_state = str(payload.get("plan_status") or "").strip().lower()
    health = payload.get("health") if isinstance(payload.get("health"), dict) else {}
    health_state = str(health.get("status") or "").strip().lower()
    verification = payload.get("verification") if isinstance(payload.get("verification"), dict) else {}
    verification_state = str(verification.get("status") or "").strip().lower()
    failed = (
        public_state in {"blocked", "degraded"}
        or plan_state in {"failed", "blocked", "cancelled"}
        or health_state in {"degraded", "unhealthy"}
        or verification_state == "failed"
    )
    running = public_state == "running" or plan_state == "running"
    display_state = "attention" if failed else "waiting" if waiting_confirmation else "ok"

    status = {
        "state": display_state,
        "intent": intent or "其他公開作業",
        "plan": "；".join(steps) if steps else "尚無公開步驟",
        "tool": tool or "未使用公開工具",
        "model": model or "未提供公開模型",
        "confirmation": "等待確認" if waiting_confirmation else "無需確認",
        "retry": f"{retry_count} 次" if retry_count is not None else "0 次",
        "route_confidence": route_confidence,
        "success_rate_7d": success_rate,
    }
    if failed:
        status["label"] = f"{status['intent']} · 受阻"
        activity = "受阻或驗證失敗"
    elif waiting_confirmation:
        status["label"] = f"{status['intent']} · 等待確認"
        activity = "等待確認"
    elif running:
        status["label"] = f"{status['intent']} · 執行中"
        activity = "執行中"
    elif retry_count:
        status["label"] = f"{status['intent']} · 重試 {retry_count} 次"
        activity = "已完成，曾重試"
    else:
        status["label"] = status["intent"]
        activity = "已完成或待命"
    status["detail"] = "\n".join(
        [
            f"公開作業狀態：{activity}",
            f"最近意圖：{status['intent']}",
            f"計畫步驟：{status['plan']}",
            f"工具／模型：{status['tool']}／{status['model']}",
            f"等待確認：{status['confirmation']}",
            f"重試：{status['retry']}",
            f"路由信心：{status['route_confidence']}",
            f"七日成功率：{status['success_rate_7d']}",
            "此區只顯示公開作業摘要，不含訊息內容或內部推理。",
        ]
    )
    return status


def _agent_status_live(*, now: float | None = None) -> dict:
    path = _mutable_static_path(AGENT_STATUS_PUBLIC_FILENAME)
    payload = _load_json_file(path)
    status = _agent_status_from_public_payload(payload)
    try:
        age_sec = max(0.0, (time.time() if now is None else float(now)) - os.path.getmtime(path))
    except (OSError, TypeError, ValueError):
        age_sec = None
    public_state = str(payload.get("status") or "").strip().lower()
    plan_state = str(payload.get("plan_status") or "").strip().lower()
    active = public_state == "running" or plan_state == "running"
    if age_sec is not None and age_sec > AGENT_STATUS_ACTIVE_MAX_AGE_SEC:
        previous_label = str(status.get("label") or "尚無活動")
        if active:
            status.update(
                state="attention",
                label="Agent 狀態逾時",
                detail=(
                    f"公開狀態已 {int(age_sec // 60)} 分鐘未更新，上一狀態仍標示執行中；"
                    "這代表遙測中斷或作業未正常收尾，不代表目前仍有程序佔用。"
                ),
            )
        else:
            status = _agent_status_from_public_payload({})
            status["detail"] = (
                "系統正常待命；目前沒有正在執行的公開長任務，背景排程不受影響。"
                f"上一項「{previous_label}」已於 {int(age_sec // 60)} 分鐘前完成。"
            )
    if age_sec is not None:
        status["age_sec"] = int(age_sec)
    status["path"] = path
    return status


def _epoch_from_iso(raw: str) -> float:
    raw = str(raw or "").strip()
    if not raw:
        return 0.0
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def _mem_bar(pct: float, width: int = 8) -> str:
    filled = int(pct / 100 * width)
    return "▓" * filled + "░" * (width - filled)


def _system_memory_icon(pct: float) -> str:
    if pct >= 92:
        return "🔴"
    if pct >= 85:
        return "🟡"
    return "🟢"


def _magi_process_memory_icon(magi_mb: int, engine_rss_mb: int = 0) -> str:
    non_engine_mb = max(0, int(magi_mb or 0) - max(0, int(engine_rss_mb or 0)))
    if non_engine_mb >= 8192:
        return "🔴"
    if non_engine_mb >= 4096:
        return "🟡"
    return "🟢"


def _format_magi_memory(
    magi_rss_mb: int,
    model_memory_bytes: int,
    *,
    engine_rss_mb: int | None = None,
    workload_rss_mb: int | None = None,
) -> str:
    """Keep process RSS and oMLX model allocation visibly distinct."""
    model_gb = max(0, int(model_memory_bytes or 0)) / (1024 ** 3)
    model_text = f"{model_gb:.1f}G" if model_gb else "未回報"
    if engine_rss_mb is not None and workload_rss_mb is not None:
        engine_mb = max(0, int(engine_rss_mb or 0))
        workload_mb = max(0, int(workload_rss_mb or 0))
        core_mb = max(0, int(magi_rss_mb or 0) - engine_mb - workload_mb)
        engine_text = f"{engine_mb / 1024:.1f}G" if engine_mb >= 1024 else f"{engine_mb}MB"
        return f"核心 {core_mb}MB｜工作 {workload_mb}MB｜引擎 {engine_text}（模型 {model_text}）"
    return f"RSS {int(magi_rss_mb or 0)}MB｜模型 {model_text}"


# ── 主程式 ───────────────────────────────────────────────────────

class MAGIMenuBar(rumps.App):
    def __init__(self):
        super().__init__(" MAGI ", quit_button=None)
        self.icon = None
        self._action_lock = threading.Lock()
        self._cache_lock = threading.Lock()
        self._collection_lock = threading.Lock()
        self._live_check_failure = None
        self._status_cache = {}
        self._status_generation = 0
        self._applied_status_generation = -1
        self._last_collection_started = 0.0
        self._cockpit_window = None
        self._cockpit_container_view = None
        self._cockpit_host_view = None
        self._cockpit_wallpaper_view = None
        self._status_click_target = None
        self.cockpit_view = None
        self.cockpit_menu_item = self._create_cockpit_menu_item()

        # ── Header ──
        self.menu_header = rumps.MenuItem("  MAGI", callback=None)
        self.menu_header.set_callback(None)
        self.overall_status_item = rumps.MenuItem("  整體狀態：等待同步", callback=None)
        self.overall_status_item.set_callback(None)
        self.agent_status_item = rumps.MenuItem("  Agent 狀態：系統正常待命", callback=None)
        self.agent_status_item.set_callback(None)

        # ── 主控台 ──
        self.live_log_header = rumps.MenuItem("╭─ 即時紀錄 ─────────────────", callback=None)
        self.live_log_header.set_callback(None)
        self.live_log_items = []
        for _ in range(LIVE_LOG_DISPLAY_MAX):
            item = rumps.MenuItem("│  --:--  等待同步　等待檢查")
            item.set_callback(None)
            self.live_log_items.append(item)
        self.live_pulse_item = rumps.MenuItem("╰─ 資料脈衝：等待同步", callback=None)
        self.live_pulse_item.set_callback(None)

        # ── 任務模組 ──
        self.business_header = rumps.MenuItem("── 任務模組 ──", callback=None)
        self.business_header.set_callback(None)
        self.business_module_items = {}
        for label in BUSINESS_MODULE_CHECKS:
            item = rumps.MenuItem(f"  ◻ {label}  {CHECK_WAITING_TEXT}")
            item.set_callback(None)
            self.business_module_items[label] = item

        # ── 出廠檢查 ──
        self.factory_header = rumps.MenuItem("── 出廠檢查 ──", callback=None)
        self.factory_header.set_callback(None)
        self.factory_check_items = {}
        for label in FACTORY_CHECKS:
            item = rumps.MenuItem(f"  ◻ {label}  {CHECK_WAITING_TEXT}")
            item.set_callback(None)
            self.factory_check_items[label] = item

        # ── 核心服務 ──
        self.svc_header = rumps.MenuItem("── 系統狀態 ──", callback=None)
        self.svc_header.set_callback(None)
        self.service_items = {}
        for name, _ in SERVICES:
            item = rumps.MenuItem(f"  ◻ {name}")
            item.set_callback(None)
            self.service_items[name] = item

        # ── 推理引擎 ──
        self.omlx_header = rumps.MenuItem("── 推理引擎 ──", callback=None)
        self.omlx_header.set_callback(None)
        self.omlx_items = {}
        for name, _ in OMLX_ENGINES:
            item = rumps.MenuItem(f"  ◻ {name}")
            item.set_callback(None)
            self.omlx_items[name] = item
        # macOS Vision OCR (non-port-based, always-on if PyObjC installed)
        self.ocr_item = rumps.MenuItem("  ◻ OCR引擎")
        self.ocr_item.set_callback(None)

        # ── 遠端節點 ──
        self.nodes_header = rumps.MenuItem("── 遠端節點 ──", callback=None)
        self.nodes_header.set_callback(None)
        self.node_items = {}
        for display_name, _, role, _, _ in REMOTE_NODES:
            item = rumps.MenuItem(f"  ◻ {display_name}")
            item.set_callback(None)
            self.node_items[display_name] = item

        # ── 排程 ── (header + 逐條子項)
        self.cron_header = rumps.MenuItem("── 定時排程 ──", callback=None)
        self.cron_header.set_callback(None)
        self.cron_summary_item = rumps.MenuItem("  ◻ 排程總覽")
        self.cron_summary_item.set_callback(None)
        # 動態子項由 _apply_status 管理
        self._cron_job_items = []

        # ── 背景監控 ──
        self.monitor_header = rumps.MenuItem("── 背景監控 ──", callback=None)
        self.monitor_header.set_callback(None)
        self.monitor_items = {}
        for display_name, _ in MONITOR_THREADS:
            item = rumps.MenuItem(f"  ◻ {display_name}")
            item.set_callback(None)
            self.monitor_items[display_name] = item

        # ── 連線 ──
        self.conn_header = rumps.MenuItem("── 外部連線 ──", callback=None)
        self.conn_header.set_callback(None)
        self.nas_status_item = rumps.MenuItem("  ◻ 網路硬碟")
        self.nas_status_item.set_callback(None)
        # NAS 子項：各卷 + 容量
        self.nas_share_items = {}
        for share_name, _ in NAS_SHARES:
            item = rumps.MenuItem(f"    ◻ {share_name}")
            item.set_callback(None)
            self.nas_share_items[share_name] = item
        self.db_status_item = rumps.MenuItem("  ◻ 本機資料庫")
        self.db_status_item.set_callback(None)
        self.db_detail_item = rumps.MenuItem("    ◻ 詳細")
        self.db_detail_item.set_callback(None)

        # ── 系統 ──
        self.res_header = rumps.MenuItem("── 系統資源 ──", callback=None)
        self.res_header.set_callback(None)
        self.mem_system_item = rumps.MenuItem("  ◻ 系統記憶")
        self.mem_system_item.set_callback(None)
        self.mem_total_item = rumps.MenuItem("  ◻ MAGI 記憶（RSS｜模型）")
        self.mem_total_item.set_callback(None)
        self.zombie_item = rumps.MenuItem("  ◻ 程序異常")
        self.zombie_item.set_callback(None)

        # ── 操作 ──
        self.start_item = rumps.MenuItem("  ▶ 啟動系統", callback=self.on_start)
        self.stop_item = rumps.MenuItem("  ■ 停止系統", callback=self.on_stop)
        self.restart_item = rumps.MenuItem("  ↻ 重新啟動", callback=self.on_restart)
        self.clean_zombie_item = rumps.MenuItem("  ♻ 清除殭屍", callback=self.on_clean_zombies)
        self.quit_item = rumps.MenuItem("  ✕ 結束監控", callback=self.on_quit)

        fallback_menu = [
            self.menu_header,
            self.overall_status_item,
            self.agent_status_item,
            rumps.separator,
            # ── 主控台 ──
            self.live_log_header,
            *self.live_log_items,
            self.live_pulse_item,
            rumps.separator,
            # ── 任務模組 ──
            self.business_header,
            *self.business_module_items.values(),
            rumps.separator,
            # ── 出廠檢查 ──
            self.factory_header,
            *self.factory_check_items.values(),
            rumps.separator,
            # ── 核心服務 ──
            self.svc_header,
            *self.service_items.values(),
            rumps.separator,
            # ── 推理引擎 ──
            self.omlx_header,
            *self.omlx_items.values(),
            self.ocr_item,
            rumps.separator,
            # ── 排程 ──
            self.cron_header,
            self.cron_summary_item,
            rumps.separator,
            # ── 背景監控 ──
            self.monitor_header,
            *self.monitor_items.values(),
            rumps.separator,
            # ── 外部連線 ──
            self.conn_header,
            self.nas_status_item,
            *self.nas_share_items.values(),
            self.db_status_item,
            self.db_detail_item,
            rumps.separator,
            # ── 系統資源 ──
            self.res_header,
            self.mem_system_item,
            self.mem_total_item,
            self.zombie_item,
            rumps.separator,
            # ── 操作 ──
            self.start_item,
            self.stop_item,
            self.restart_item,
            self.clean_zombie_item,
            rumps.separator,
            self.quit_item,
        ]
        self.menu = [self.cockpit_menu_item] if self.cockpit_menu_item is not None else fallback_menu
        if _HAS_APPKIT and _HAS_RUMPS and hasattr(rumps, "events"):
            try:
                rumps.events.before_start.register(self._configure_status_item_action)
            except Exception:
                logging.getLogger("menubar").warning("status item full-screen hook unavailable", exc_info=True)

    def _create_cockpit_menu_item(self):
        if not (_HAS_APPKIT and _HAS_RUMPS and _CockpitDashboardView is not None):
            return None
        try:
            view = _CockpitDashboardView.alloc().initWithFrame_(NSMakeRect(0, 0, DASHBOARD_WIDTH, DASHBOARD_HEIGHT))
            view.set_controller(self)
            self.cockpit_view = view
            return rumps.MenuItem("  ◉ 開啟全螢幕 MAGI 駕駛艙", callback=self._toggle_cockpit_fullscreen)
        except Exception:
            logging.getLogger("menubar").warning("cockpit dashboard view unavailable", exc_info=True)
            self.cockpit_view = None
            return None

    def _configure_status_item_action(self) -> None:
        """Make a status-item click toggle the borderless panoramic cockpit."""
        try:
            status_item = self._nsapp.nsstatusitem
            target = _StatusItemClickTarget.alloc().initWithController_(self)
            self._status_click_target = target
            status_item.setMenu_(None)
            status_item.setTarget_(target)
            status_item.setAction_("toggleCockpit:")
            button = status_item.button()
            if button is not None:
                button.setTarget_(target)
                button.setAction_("toggleCockpit:")
        except Exception:
            logging.getLogger("menubar").warning(
                "failed to configure direct panoramic cockpit click",
                exc_info=True,
            )

    def _screen_for_cockpit(self):
        try:
            status_item = self._nsapp.nsstatusitem
            button = status_item.button()
            window = button.window() if button is not None else None
            screen = window.screen() if window is not None else None
            if screen is not None:
                return screen
        except Exception:
            pass
        return NSScreen.mainScreen()

    def _ensure_cockpit_window(self):
        if not (_HAS_APPKIT and self.cockpit_view is not None):
            return None
        screen = self._screen_for_cockpit()
        if screen is None:
            return None
        # Use the native visible work area so the panoramic cockpit remains
        # full-window while the macOS status bar stays visible and clickable.
        frame = screen.visibleFrame()
        window = self._cockpit_window
        if window is None:
            window = _CockpitFullscreenWindow.alloc().initWithContentRect_styleMask_backing_defer_(
                frame,
                NSWindowStyleMaskBorderless,
                NSBackingStoreBuffered,
                False,
            )
            window.setReleasedWhenClosed_(False)
            window.setOpaque_(False)
            window.setBackgroundColor_(NSColor.clearColor())
            window.setHasShadow_(False)
            window.setLevel_(NSMainMenuWindowLevel - 1)
            window.setCollectionBehavior_(
                NSWindowCollectionBehaviorCanJoinAllSpaces
                | NSWindowCollectionBehaviorFullScreenAuxiliary
                | NSWindowCollectionBehaviorStationary
            )
            container = NSView.alloc().initWithFrame_(
                NSMakeRect(0, 0, frame.size.width, frame.size.height)
            )
            wallpaper = _CockpitWallpaperView.alloc().initWithFrame_(
                NSMakeRect(0, 0, frame.size.width, frame.size.height)
            )
            wallpaper.set_dashboard_view(self.cockpit_view)
            container.addSubview_(wallpaper)
            host_class = NSView if wallpaper.has_wallpaper() else (_NS_VISUAL_EFFECT_VIEW or NSView)
            host = host_class.alloc().initWithFrame_(NSMakeRect(0, 0, frame.size.width, frame.size.height))
            if _NS_VISUAL_EFFECT_VIEW is not None and host_class is _NS_VISUAL_EFFECT_VIEW:
                host.setMaterial_(_NS_VISUAL_EFFECT_MATERIAL)
                host.setBlendingMode_(_NS_VISUAL_EFFECT_BEHIND)
                host.setState_(_NS_VISUAL_EFFECT_ACTIVE)
                host.setEmphasized_(False)
            host.addSubview_(self.cockpit_view)
            container.addSubview_(host)
            self.cockpit_view.set_wallpaper_view(wallpaper)
            self.cockpit_view.set_wallpaper_active(wallpaper.has_wallpaper())
            window.setContentView_(container)
            self._cockpit_container_view = container
            self._cockpit_host_view = host
            self._cockpit_wallpaper_view = wallpaper
            self._cockpit_window = window
        window.setFrame_display_(frame, True)
        if self._cockpit_container_view is not None:
            self._cockpit_container_view.setFrame_(
                NSMakeRect(0, 0, frame.size.width, frame.size.height)
            )
        if self._cockpit_wallpaper_view is not None:
            self._cockpit_wallpaper_view.setFrame_(
                NSMakeRect(0, 0, frame.size.width, frame.size.height)
            )
        if self._cockpit_host_view is not None:
            self._cockpit_host_view.setFrame_(NSMakeRect(0, 0, frame.size.width, frame.size.height))
        self.cockpit_view.setFrame_(NSMakeRect(0, 0, frame.size.width, frame.size.height))
        return window

    def _toggle_cockpit_fullscreen(self, _sender=None) -> None:
        window = self._cockpit_window
        if window is not None and bool(window.isVisible()):
            self._hide_cockpit_fullscreen()
            return
        self._show_cockpit_fullscreen()

    def _show_cockpit_fullscreen(self, _sender=None) -> None:
        if not _HAS_APPKIT:
            return
        try:
            window = self._ensure_cockpit_window()
            if window is None:
                return
            with self._cache_lock:
                snapshot = dict(self._status_cache) if self._status_cache else {}
            self.cockpit_view.update_status(snapshot)
            NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
            window.makeKeyAndOrderFront_(None)
            window.makeFirstResponder_(self.cockpit_view)
            if self._cockpit_wallpaper_view is not None:
                self._cockpit_wallpaper_view.start()
            # 畫面開啟時立即更新，無須等待下一個背景輪詢週期。
            self._last_collection_started = 0.0
            threading.Thread(target=self._collect_status, daemon=True).start()
        except Exception:
            logging.getLogger("menubar").warning("failed to show full-screen cockpit", exc_info=True)

    def _hide_cockpit_fullscreen(self) -> None:
        try:
            if self._cockpit_wallpaper_view is not None:
                self._cockpit_wallpaper_view.pause()
            if self._cockpit_window is not None:
                self._cockpit_window.orderOut_(None)
        except Exception:
            logging.getLogger("menubar").warning("failed to hide full-screen cockpit", exc_info=True)

    # ── 資料收集（背景執行緒）────────────────────────────────────

    def _cockpit_is_visible(self) -> bool:
        try:
            return bool(self._cockpit_window is not None and self._cockpit_window.isVisible())
        except Exception:
            return False

    @rumps.timer(CHECK_INTERVAL)
    def _periodic_check(self, _sender):
        with self._cache_lock:
            cache_snapshot = dict(self._status_cache) if self._status_cache else {}
            generation = int(self._status_generation)
        if cache_snapshot and generation != self._applied_status_generation:
            try:
                self._apply_status(cache_snapshot)
                self._applied_status_generation = generation
            except Exception as _apply_err:
                import traceback
                logging.getLogger("menubar").error("_apply_status error: %s\n%s", _apply_err, traceback.format_exc())
        collect_interval = (
            MENUBAR_VISIBLE_COLLECT_INTERVAL
            if self._cockpit_is_visible()
            else MENUBAR_HIDDEN_COLLECT_INTERVAL
        )
        if time.monotonic() - float(self._last_collection_started or 0.0) >= collect_interval:
            threading.Thread(target=self._collect_status, daemon=True).start()

    def _collect_status(self):
        """Collect once; concurrent timer/manual refresh calls share one flight."""
        collection_lock = getattr(self, "_collection_lock", None)
        acquired = False
        if collection_lock is not None:
            try:
                acquired = collection_lock.acquire(blocking=False)
            except Exception:
                return False
            if not acquired:
                return False
        try:
            self._last_collection_started = time.monotonic()
            MAGIMenuBar._collect_status_once(self)
            return True
        finally:
            if acquired:
                collection_lock.release()

    def _collect_status_once(self):
        """背景執行緒：收集所有 I/O 資料，存入 cache。"""
        cache = {}

        # ── 核心服務 ──
        svcs = {}
        service_details = {}
        for name, pattern in SERVICES:
            alive, detail = _service_liveness(bool(_pgrep_any(pattern)), name)
            svcs[name] = alive
            service_details[name] = detail
        cache["services"] = svcs
        cache["service_details"] = service_details

        server_log_path = _agent_path("server.log")
        cache["live_log_path"] = server_log_path if os.path.isfile(server_log_path) else "/tmp/magi-menubar.log"
        cache["live_events"] = _live_log_events(_read_log_tail(server_log_path))
        cache["live_pulse_points"] = _live_pulse_points(cache["live_events"])

        # ── 推理引擎 ──
        engines = {}
        for name, port in OMLX_ENGINES:
            engines[name] = _check_omlx(port)
        cache["engines"] = engines
        cache["omlx_model_memory_bytes"] = sum(
            _check_omlx_model_memory(port)
            for name, port in OMLX_ENGINES
            if engines.get(name)
        )
        expected_profile, expected_keyword = expected_omlx_profile_now()
        active_profile = _active_omlx_profile()
        cache["omlx_profile"] = {
            "expected_profile": expected_profile,
            "expected_keyword": expected_keyword,
            "active_profile": active_profile,
            "text_status": _omlx_text_status(
                engines.get("文字推理", ""),
                expected_profile,
                expected_keyword,
                active_profile,
                transitioning=profile_transition_in_progress(
                    active_profile,
                    expected_profile,
                ),
            ),
        }

        # ── 遠端節點 ──
        nodes = {}
        for display_name, reg_key, role, port, check_type in REMOTE_NODES:
            ip = _get_node_ip(reg_key)
            if not ip:
                # Hardcoded fallback
                _fb = {"melchior": "", "balthasar": "", "nas": ""}
                ip = _fb.get(reg_key, "")
            if not ip:
                nodes[display_name] = {"online": False, "ip": "", "detail": "無 IP"}
                continue
            online = _tcp(ip, port, timeout=3)
            detail = ""
            if online and check_type == "api":
                detail = _http_health(f"http://{ip}:{port}/v1/models", timeout=3)
            elif online and check_type == "flask":
                detail = _http_health(f"http://{ip}:{port}/health", timeout=3)
            elif online and check_type == "tcp":
                detail = "連線正常"
            nodes[display_name] = {"online": online, "ip": ip, "detail": detail or ""}
        cache["nodes"] = nodes

        # ── 排程任務 ──
        jobs = _load_cron_jobs()
        cron_state = _load_cron_state()
        enabled = [j for j in jobs if j.get("enabled", True)]
        cache["cron_enabled"] = len(enabled)
        cache["cron_bot"] = bool(_pgrep("discord_bot.py"))
        all_cron_details = _cron_details_from_state(enabled, cron_state)
        cache["cron_details"] = all_cron_details[:CRON_DISPLAY_MAX]
        cache["cron_summary"] = _cron_summary(len(enabled), cache["cron_bot"], all_cron_details)

        business_live = _business_module_status_live()
        live_check_failure = getattr(self, "_live_check_failure", None)
        if isinstance(live_check_failure, dict):
            failure_epoch = float(live_check_failure.get("at") or 0.0)
            checked_epoch = float(business_live.get("checked_epoch") or 0.0)
            if business_live.get("ok") and checked_epoch > failure_epoch:
                self._live_check_failure = None
            else:
                business_live = _business_module_status_failure(
                    str(live_check_failure.get("reason") or "live_check_failed"),
                    returncode=live_check_failure.get("returncode"),
                )
                business_live["checked_at"] = datetime.fromtimestamp(failure_epoch).strftime("%H:%M") if failure_epoch else ""
                cache["live_check_failure"] = dict(live_check_failure)
        cache["business_live"] = business_live
        cache["health"] = _runtime_health_states()
        cache["business_readiness"] = _business_readiness_live()
        cache["agent_status"] = _agent_status_live()

        # ── 背景監控 ──
        monitors = {}
        try:
            # 監控顯示以 thread / log 活動為主，避免被 /health 的重型檢查誤判。
            _server_up = _service_alive(cache.get("services", {}), "主伺服器", "Server")
            log_path = _agent_path("server.log")
            _log_tail = _read_log_tail(log_path)
            _now = time.time()

            def _fmt_recent(epoch: float, *, max_age_sec: int, live_text: str, stale_text: str):
                if not epoch:
                    return "down", ""
                age = _now - epoch
                hhmmss = datetime.fromtimestamp(epoch).strftime("%H:%M:%S")
                if age <= max_age_sec:
                    return "alive", f"{live_text} {hhmmss}"
                return "stale", f"{stale_text} {hhmmss}"

            # 法扶 Gmail monitor：優先看狀態檔；舊 log 只作為 fallback。
            _laf_state = _load_json_file(_mutable_static_path("laf_gmail_monitor_state.json"))
            _gmail_epoch = _epoch_from_iso(_laf_state.get("updated_at", ""))
            _gmail_status = str(_laf_state.get("status") or "").lower()
            if not _gmail_epoch:
                _gmail_epoch, _ = _find_latest_log_match(_log_tail, ("[Gmail]",))
            _gmail_state, _gmail_detail = _fmt_recent(
                _gmail_epoch,
                max_age_sec=900,
                live_text="最近",
                stale_text="心跳中斷",
            )
            if _gmail_status in {"error", "auth_failed", "exiting"}:
                _gmail_state = "down"
                _gmail_detail = f"{_laf_state.get('status')} {(_laf_state.get('error') or '')}".strip()[:60]
            if _gmail_state == "down" and _server_up and "LAF Gmail Monitor background thread started" in _log_tail:
                _gmail_state = "starting"
                _gmail_detail = "已啟動，待首輪活動"
            if _gmail_state in {"down", "stale"} and _server_up and "[Gmail] Gmail 監控已啟動" in _log_tail:
                _gmail_state = "alive"
                _gmail_detail = "已啟動"
            monitors["法扶信箱監控"] = {
                "alive": _gmail_state == "alive",
                "state": _gmail_state,
                "detail": _gmail_detail,
            }

            # Portal retry must prove its own heartbeat; a live server is not loop evidence.
            _retry_payload = _load_json_file(_mutable_static_path("laf_portal_retry_state.json"))
            _retry_epoch = _epoch_from_iso(_retry_payload.get("updated_at", ""))
            _retry_status = str(_retry_payload.get("status") or "").lower()
            try:
                _retry_interval = max(60, int(_retry_payload.get("interval_sec") or 300))
            except (TypeError, ValueError):
                _retry_interval = 300
            if not _retry_epoch:
                _retry_epoch, _ = _find_latest_log_match(_log_tail, ("[LAF-RETRY]", "portal retry loop started"))
            _retry_state, _retry_detail = _fmt_recent(
                _retry_epoch,
                max_age_sec=max(180, min(900, _retry_interval + 120)),
                live_text="最近",
                stale_text="心跳中斷",
            )
            if _retry_status in {"error", "stopped"}:
                _retry_state = "down"
                _retry_detail = f"重試程序{_retry_status}"
            elif _retry_state == "alive" and _retry_status == "starting":
                _retry_state = "starting"
                _retry_detail = "啟動中"
            elif _retry_state == "alive" and _retry_status in {"idle", "ok"}:
                _retry_detail = "等待下一輪" if _retry_status == "idle" else "運作正常"
            elif _retry_state == "alive" and _retry_status == "running":
                _retry_detail = "執行中"
            if not _retry_epoch and _server_up:
                _retry_state = "waiting"
                _retry_detail = "等待啟用"
            monitors["法扶附件重試"] = {
                "alive": _retry_state == "alive",
                "state": _retry_state,
                "detail": _retry_detail,
            }

            # 閱卷 email 由法扶 Gmail monitor 的同一個 poll cycle 順便掃描；
            # FileReviewAuto worker 只做補充入口網站檢查，不應拿來代表信箱監控心跳。
            _fr_email_state = _load_json_file(_mutable_static_path("file_review_email_monitor_state.json"))
            _fr_email_status = str(_fr_email_state.get("status") or "").lower()
            _fr_email_epoch = _epoch_from_iso(_fr_email_state.get("updated_at", ""))
            _integrated_review_email = (
                str(_fr_email_state.get("source") or "") == "laf_gmail_monitor_cycle"
                or "file review email scan integrated in LAF monitor cycle" in _log_tail
                or "File Review Email Monitor: integrated into LAF Gmail Monitor cycle" in _log_tail
            )
            _fr_state = _load_json_file(_mutable_static_path("file_review_auto_state.json"))
            _review_epoch = _fr_email_epoch or _epoch_from_iso(_fr_state.get("updated_at", ""))
            _fr_interval = int(_fr_state.get("interval_sec") or 3600)
            if not _review_epoch:
                _review_epoch, _ = _find_latest_log_match(
                    _log_tail,
                    (
                        "[閱卷]",
                        "Checking Gmail for file review notifications...",
                        "Checking Gmail for non-LAF/Judicial auto-drafts...",
                    ),
                )
            _review_state, _review_detail = _fmt_recent(
                _review_epoch,
                max_age_sec=max(900, _fr_interval + 900),
                live_text="最近",
                stale_text="心跳中斷",
            )
            if _integrated_review_email and monitors.get("法扶信箱監控", {}).get("state") == "alive":
                _review_state = "alive"
                _review_detail = _gmail_detail.replace("最近 ", "隨法扶 ", 1) if _gmail_detail else "隨法扶監控"
                if _fr_email_status == "running" and _fr_email_epoch:
                    _review_detail = f"掃描中 {datetime.fromtimestamp(_fr_email_epoch).strftime('%H:%M:%S')}"
            if _fr_email_status in {"error", "auth_failed"}:
                _review_state = "down"
                _review_detail = f"{_fr_email_state.get('status')} {(_fr_email_state.get('error') or '')}".strip()[:60]
            elif (not _integrated_review_email) and _fr_state and not bool((_fr_state.get("result") or {}).get("ok", True)):
                _review_state = "down"
                _review_detail = "worker 上輪失敗"
            if _review_state == "down" and _server_up and "File Review Email Monitor: integrated into LAF Gmail Monitor cycle" in _log_tail:
                _review_state = "alive"
                _review_detail = "已整合至法扶監控"
            if _review_state in {"down", "stale"} and _server_up and "background file review email scan disabled" in _log_tail:
                _review_state = "alive"
                _review_detail = "已整合至法扶監控"
            elif _review_state == "down" and monitors.get("法扶信箱監控", {}).get("state") == "alive":
                _review_state = "alive"
                _review_detail = "隨法扶監控"
            monitors["閱卷信箱監控"] = {
                "alive": _review_state == "alive",
                "state": _review_state,
                "detail": _review_detail,
            }

            # 閱卷入口掃描：FileReviewAuto worker 的入口網站補充檢查心跳。
            # 這和信箱監控分開顯示，避免補充掃描逾時被誤解為 Gmail 監控失效。
            _portal_epoch = _epoch_from_iso(_fr_state.get("updated_at", ""))
            _portal_interval = int(_fr_state.get("interval_sec") or 3600)
            _portal_phase = str(_fr_state.get("phase") or "").strip()
            _portal_result = _fr_state.get("result") if isinstance(_fr_state.get("result"), dict) else {}
            if _portal_phase.startswith("running_") and _portal_epoch:
                _portal_state = "starting"
                _portal_detail = f"掃描中 {datetime.fromtimestamp(_portal_epoch).strftime('%H:%M:%S')}"
            else:
                _portal_state, _portal_detail = _fmt_recent(
                    _portal_epoch,
                    max_age_sec=max(900, _portal_interval + 900),
                    live_text="最近",
                    stale_text="最後活動",
                )
                if _portal_result:
                    if not bool(_portal_result.get("ok", True)):
                        _portal_state = "down"
                        _portal_detail = "上輪失敗"
                    elif bool(_portal_result.get("degraded")):
                        _payment = _portal_result.get("payment_slips") if isinstance(_portal_result.get("payment_slips"), dict) else {}
                        _reason = "補充掃描逾時" if _payment.get("error") == "timeout" else "補充掃描降級"
                        if _portal_epoch:
                            _portal_detail = f"{_reason} {datetime.fromtimestamp(_portal_epoch).strftime('%H:%M:%S')}"
                        else:
                            _portal_detail = _reason
                        _portal_state = "idle"
                    elif str(_portal_result.get("reason") or "") == "auto_download_disabled":
                        # This worker is intentionally scan-only when the
                        # hourly scheduled_check owns downloads.  A completed
                        # scan is healthy, not a yellow/idle process warning.
                        _portal_state = "alive"
                        _portal_detail = "掃描正常・下載由排程"
            monitors["閱卷入口掃描"] = {
                "alive": _portal_state == "alive",
                "state": _portal_state,
                "detail": _portal_detail,
            }
        except Exception as exc:
            monitors["_error"] = {
                "alive": False,
                "state": "down",
                "detail": f"監控狀態收集失敗: {exc}",
            }
            logging.getLogger("menubar").warning("monitor status collection failed", exc_info=True)
        cache["monitors"] = monitors

        # ── NAS ──
        try:
            from api.routing.node_registry import get_node as _get_node
            _nas = _get_node("nas")
            _nas_lan = (_nas.lan_ip if _nas else None) or ""
            _nas_ts = (_nas.tailscale_ip if _nas else None) or ""
        except Exception:
            _nas_lan, _nas_ts = "", ""
        lan_ip = os.environ.get("MAGI_NAS_HOST", _nas_lan)
        ts_ip = os.environ.get("MAGI_NAS_TAILSCALE_HOST", _nas_ts)
        lan_ok = _tcp(lan_ip, 445, timeout=1)
        vpn_ok = _tcp(ts_ip, 445, timeout=2)
        # 各卷掛載 + 容量
        shares = {}
        any_mounted = False
        any_synology_drive = False
        for share_name, mount_path in NAS_SHARES:
            # 檢查 /Volumes/<share>, -1, -2, 以及 ~/.magi_mounts/<share>
            actual_path = mount_path
            mode = "smb"
            user_path = os.path.join(_USER_MOUNT_ROOT, share_name)
            for candidate in (mount_path, mount_path + "-1", mount_path + "-2", user_path):
                if os.path.ismount(candidate):
                    actual_path = candidate
                    break
            mounted = os.path.ismount(actual_path)
            if not mounted and share_name == "homes":
                fallback_path = _synology_drive_fallback_path()
                if fallback_path:
                    actual_path = fallback_path
                    mounted = True
                    mode = "synology_drive"
                    any_synology_drive = True
            if mounted:
                any_mounted = True
            disk = _get_disk_usage(actual_path) if mounted else None
            shares[share_name] = {"mounted": mounted, "path": actual_path, "disk": disk, "mode": mode}
        cache["nas"] = {
            "lan": lan_ok, "vpn": vpn_ok, "mounted": any_mounted,
            "synology_drive": any_synology_drive,
            "shares": shares,
        }

        # ── DB ──
        # 舊遠端 DB 已退役；MENUBAR 只呈現本機 MariaDB，避免再顯示雙活/回寫等舊架構狀態。
        cache["db"] = {
            "local": _tcp("127.0.0.1", 3306, 2),
            "backup_configured": False,
        }

        # ── 知識態勢雷達 ──
        # Uses stat-bound memoization so the large Obsidian/Wiki indexes are
        # parsed only after they actually change.
        cache["knowledge_radar"] = _knowledge_radar_snapshot()

        # ── 系統記憶體 ──
        cache["mem"] = _get_system_memory()
        module_memory = _get_module_memory()
        magi_mb, core_rss_mb, workload_rss_mb, engine_rss_mb = _memory_breakdown(module_memory)
        cache["module_memory"] = module_memory
        cache["magi_mb"] = magi_mb
        cache["core_rss_mb"] = core_rss_mb
        cache["workload_rss_mb"] = workload_rss_mb
        cache["engine_rss_mb"] = engine_rss_mb
        process_health = _collect_process_health()
        cache["process_health"] = process_health
        zombie_count = int((process_health.get("summary") or {}).get("zombie_count", 0) or 0)
        cache["zombies"] = (zombie_count, _process_anomaly_detail(process_health))

        with self._cache_lock:
            self._status_cache = cache
            self._status_generation += 1

    # ── UI 更新（主執行緒）───────────────���─────────────────────

    def _apply_status(self, c):
        """主執行緒：用 cache 更新 UI（無 I/O）。"""

        # ── 主控台 ──
        if self.cockpit_view is not None:
            try:
                self.cockpit_view.update_status(c)
            except Exception:
                logging.getLogger("menubar").warning("cockpit dashboard update failed", exc_info=True)

        overall = _overall_state(c)
        overall_label = OVERALL_WAITING_TEXT if overall == "waiting" else _label_for_state(overall, OPERATIONAL_TEXT)
        _set_colored_title(self.menu_header, "  MAGI", _CYAN, bold=True)
        _set_colored_title(
            self.overall_status_item,
            f"  整體狀態：{overall_label}",
            _state_color(overall),
            bold=True,
        )
        agent_status = c.get("agent_status", {}) if isinstance(c, dict) else {}
        agent_state = str(agent_status.get("state") or "idle") if isinstance(agent_status, dict) else "idle"
        agent_label = str(agent_status.get("label") or "尚無活動") if isinstance(agent_status, dict) else "尚無活動"
        _set_colored_title(
            self.agent_status_item,
            f"  {_state_icon(agent_state)} Agent 狀態：{agent_label}",
            _state_color(agent_state),
        )
        _set_colored_title(self.live_log_header, "╭─ 即時紀錄 ─────────────────", _CYAN, bold=True)

        live_events = _format_live_events(c)
        for idx, item in enumerate(self.live_log_items):
            if idx < len(live_events):
                event = live_events[idx]
                state = str(event.get("state") or "waiting")
                line = (
                    f"│ {event.get('time', '--:--')}  {_state_icon(state)} "
                    f"{event.get('source', '')}　{event.get('label', CHECK_WAITING_TEXT)}"
                )
                _set_colored_title(item, line, _state_color(state), small=True)
            else:
                _set_colored_title(item, "│ --:--  等待同步　等待檢查", _YELLOW, small=True)
        pulse = "▁▂▃▅▆▇▆▅▃▂▁"
        checked_at = ""
        business_live = c.get("business_live", {}) if isinstance(c, dict) else {}
        if isinstance(business_live, dict):
            checked_at = business_live.get("checked_at", "")
        pulse_suffix = f"最近檢查 {checked_at}" if checked_at else CHECK_WAITING_TEXT
        _set_colored_title(self.live_pulse_item, f"╰─ 資料脈衝：{pulse}　{pulse_suffix}", _CYAN, small=True)

        # ── 任務模組 / 出廠檢查 ──
        modules = business_live.get("modules", {}) if isinstance(business_live, dict) else {}
        for label, item in self.business_module_items.items():
            info = modules.get(label, {}) if isinstance(modules, dict) else {}
            state = str(info.get("state") or "waiting")
            status_label = str(info.get("label") or _label_for_state(state, OPERATIONAL_TEXT))
            _set_colored_title(item, f"  {_state_icon(state)} {label}  {status_label}", _state_color(state))

        factory = business_live.get("factory", {}) if isinstance(business_live, dict) else {}
        for label, item in self.factory_check_items.items():
            info = factory.get(label, {}) if isinstance(factory, dict) else {}
            state = str(info.get("state") or "waiting")
            status_label = str(info.get("label") or _label_for_state(state, CHECK_PASSED_TEXT))
            _set_colored_title(item, f"  {_state_icon(state)} {label}  {status_label}", _state_color(state))

        # ── 核心服��� ──
        core_up = 0
        svcs = c.get("services", {})
        service_details = c.get("service_details", {}) if isinstance(c, dict) else {}
        for name, _ in SERVICES:
            if svcs.get(name):
                _set_colored_title(self.service_items[name], f"  🟢 {name}  {OPERATIONAL_TEXT}", None)
                core_up += 1
            else:
                detail = str(service_details.get(name) or "已停止") if isinstance(service_details, dict) else "已停止"
                _set_colored_title(self.service_items[name], f"  🔴 {name}  {detail}", None)

        # ── 推理引擎 ──
        omlx_up = 0
        engines = c.get("engines", {})
        profile_info = c.get("omlx_profile", {})
        text_status = profile_info.get("text_status", {}) if isinstance(profile_info, dict) else {}
        expected_profile = profile_info.get("expected_profile", "") if isinstance(profile_info, dict) else ""
        expected_keyword = profile_info.get("expected_keyword", "") if isinstance(profile_info, dict) else ""
        active_profile = profile_info.get("active_profile", "") if isinstance(profile_info, dict) else ""
        expected_label = _model_label(expected_keyword)
        profile_label = "日間" if expected_profile == "day" else "夜間"
        if text_status.get("degraded"):
            _set_colored_title(self.omlx_header, f"── 推理引擎（{profile_label}{expected_label}→E4B降級）──", None)
        elif expected_profile:
            _set_colored_title(self.omlx_header, f"── 推理引擎（{profile_label}{expected_label}）──", None)
        else:
            _set_colored_title(self.omlx_header, "── 推理引擎 ──", None)
        for name, _ in OMLX_ENGINES:
            model_id = engines.get(name, "")
            if model_id:
                if name == "文字推理" and text_status:
                    _set_colored_title(
                        self.omlx_items[name],
                        f"  {text_status.get('icon', '🟢')} {name}  {text_status.get('label', _short_model_id(model_id))}",
                        None,
                    )
                else:
                    short = _short_model_id(model_id)
                    _set_colored_title(self.omlx_items[name], f"  🟢 {name}  {short}", None)
                omlx_up += 1
            else:
                if name == "文字推理" and text_status:
                    _set_colored_title(
                        self.omlx_items[name],
                        f"  {text_status.get('icon', '🔴')} {name}  {text_status.get('label', '離線')}",
                        None,
                    )
                elif not _model_expected(name, profile_info):
                    _set_colored_title(self.omlx_items[name], f"  ⚪ {name}  未啟用", None)
                else:
                    _set_colored_title(self.omlx_items[name], f"  🔴 {name}  離線", None)
        # macOS Vision OCR status
        try:
            from skills.apple.apple_intelligence import VISION_AVAILABLE
            if VISION_AVAILABLE:
                _set_colored_title(self.ocr_item, f"  🟢 OCR引擎  {OPERATIONAL_TEXT}", None)
            else:
                _set_colored_title(self.ocr_item, "  ⚪ OCR引擎  未安裝", None)
        except Exception as e:
            _set_colored_title(self.ocr_item, f"  ⚪ OCR引擎  未安裝 ({e})", None)

        # ── 遠端節點 ──
        nodes_up = 0
        nodes = c.get("nodes", {})
        for display_name, _, role, _, _ in REMOTE_NODES:
            info = nodes.get(display_name, {})
            if info.get("online"):
                detail = info.get("detail", "")
                if detail and detail not in ("Active", "連線正常"):
                    short = detail[:20] if len(detail) > 20 else detail
                    label = f"  🟢 {display_name}  {short}"
                else:
                    label = f"  🟢 {display_name}  在線"
                _set_colored_title(self.node_items[display_name], label, None)
                nodes_up += 1
            else:
                _set_colored_title(self.node_items[display_name], f"  🔴 {display_name}  離線", None)

        # ── 排程 ──
        cron_summary = c.get("cron_summary", {}) if isinstance(c, dict) else {}
        cron_state = str(cron_summary.get("state") or "waiting") if isinstance(cron_summary, dict) else "waiting"
        cron_label = str(cron_summary.get("label") or "讀取失敗") if isinstance(cron_summary, dict) else "讀取失敗"
        _set_colored_title(
            self.cron_summary_item,
            f"  {_state_icon(cron_state)} 排程總覽  {cron_label}",
            _state_color(cron_state),
        )

        # 排程逐條 — 動態增減子項
        cron_details = c.get("cron_details", [])
        # 確保有足夠的 menu item
        while len(self._cron_job_items) < len(cron_details):
            item = rumps.MenuItem(f"    ◻ --")
            item.set_callback(None)
            self._cron_job_items.append(item)
            # 插入到 cron_summary_item 之後
            try:
                self.menu.insert_after(
                    self.cron_summary_item.title if not self._cron_job_items[:-1]
                    else self._cron_job_items[-2].title,
                    item,
                )
            except Exception:
                pass
        # 更新內容
        for i, detail in enumerate(cron_details):
            item = self._cron_job_items[i]
            desc = detail["desc"]
            rel = detail["relative"]
            status = str(detail.get("status") or "")
            if status == "failed":
                _set_colored_title(item, f"    🔴 {desc}  {rel}", None, small=True)
            elif detail.get("stale"):
                _set_colored_title(item, f"    🟡 {desc}  {rel}", None, small=True)
            elif status == "waiting":
                _set_colored_title(item, f"    🟡 {desc}  {rel}", None, small=True)
            else:
                _set_colored_title(item, f"    ⚪ {desc}  {rel}", None, small=True)
        # 隱藏多餘的
        for i in range(len(cron_details), len(self._cron_job_items)):
            _set_colored_title(self._cron_job_items[i], "", None, small=True)

        # ── 背景監控 ──
        monitors = c.get("monitors", {})
        for display_name, _ in MONITOR_THREADS:
            info = monitors.get(display_name, {})
            item = self.monitor_items.get(display_name)
            if item is None:
                continue
            state = str(info.get("state") or ("alive" if info.get("alive") else "down"))
            detail = info.get("detail", "")
            if state == "alive":
                suffix = f"  {detail}" if detail else f"  {OPERATIONAL_TEXT}"
                _set_colored_title(item, f"  🟢 {display_name}{suffix}", None)
            elif state in {"starting", "stale", "idle", "waiting"}:
                suffix = f"  {detail}" if detail else "  已啟動但暫無新活動"
                _set_colored_title(item, f"  🟡 {display_name}{suffix}", None)
            else:
                suffix = f"  {detail}" if detail else "  未偵測到活動"
                _set_colored_title(item, f"  🔴 {display_name}{suffix}", None)

        # ── NAS ──
        nas = c.get("nas", {})
        if nas.get("lan") and nas.get("mounted"):
            _set_colored_title(self.nas_status_item, "  🟢 網路硬碟  區網掛載", None)
        elif nas.get("vpn") and nas.get("mounted"):
            _set_colored_title(self.nas_status_item, "  🟢 網路硬碟  VPN掛載", None)
        elif nas.get("synology_drive"):
            _set_colored_title(self.nas_status_item, "  🟢 網路硬碟  同步可用", None)
        elif nas.get("mounted"):
            _set_colored_title(self.nas_status_item, "  🟡 網路硬碟  連線不穩", None)
        elif nas.get("lan") or nas.get("vpn"):
            _set_colored_title(self.nas_status_item, "  🟡 網路硬碟  可達未掛載", None)
        else:
            _set_colored_title(self.nas_status_item, "  🔴 網路硬碟  未掛載", None)

        # NAS 各卷
        shares = nas.get("shares", {})
        for share_name, _ in NAS_SHARES:
            si = shares.get(share_name, {})
            item = self.nas_share_items[share_name]
            if si.get("mounted"):
                disk = si.get("disk")
                mode_label = "同步可用" if si.get("mode") == "synology_drive" else "已掛載"
                if disk:
                    used_gb, total_gb, pct = disk
                    bar = _mem_bar(pct, 6)
                    _set_colored_title(
                        item,
                        f"    {bar} {share_name}  {mode_label} {used_gb:.0f}/{total_gb:.0f}G ({pct:.0f}%)",
                        None,
                        small=True,
                    )
                else:
                    _set_colored_title(item, f"    🟢 {share_name}  {mode_label}", None, small=True)
            else:
                _set_colored_title(item, f"    🔴 {share_name}  未掛載", None, small=True)

        # ── DB ──
        db = c.get("db", {})
        if db.get("local"):
            _set_colored_title(self.db_status_item, f"  🟢 本機資料庫  {OPERATIONAL_TEXT}", None)
            _set_colored_title(self.db_detail_item, "    MariaDB 127.0.0.1・備份區待設定", None, small=True)
        else:
            _set_colored_title(self.db_status_item, "  🔴 本機資料庫  離線", None)
            _set_colored_title(self.db_detail_item, "    MariaDB 無法連線，請先恢復本機資料庫", None, small=True)

        # ── 記憶體 ──
        _, avail_gb, pct = c.get("mem", (0, 0, 0))
        if pct > 0:
            bar = _mem_bar(pct)
            mem_color = None
            icon = _system_memory_icon(pct)
            _set_colored_title(self.mem_system_item, f"  {icon} {bar} 系統記憶  {pct:.0f}% {avail_gb:.1f}G餘", None)

        magi_mb = c.get("magi_mb", 0)
        engine_rss_mb = int(c.get("engine_rss_mb", 0) or 0)
        workload_rss_mb = int(c.get("workload_rss_mb", 0) or 0)
        icon = _magi_process_memory_icon(int(magi_mb or 0), engine_rss_mb)
        memory_label = _format_magi_memory(
            magi_mb,
            c.get("omlx_model_memory_bytes", 0),
            engine_rss_mb=engine_rss_mb,
            workload_rss_mb=workload_rss_mb,
        )
        _set_colored_title(self.mem_total_item, f"  {icon} MAGI 記憶  {memory_label}", None)

        process_health = c.get("process_health", {}) if isinstance(c, dict) else {}
        process_summary = process_health.get("summary", {}) if isinstance(process_health, dict) else {}
        anomaly_count = int(process_summary.get("anomaly_count", 0) or 0)
        if process_health.get("ok") is True and anomaly_count == 0:
            _set_colored_title(self.zombie_item, "  🟢 程序異常  無（孤兒0／殭屍0／重複0）", None)
        else:
            detail = _process_anomaly_detail(process_health) if process_health.get("ok") is True else "監控讀取失敗"
            _set_colored_title(self.zombie_item, f"  🔴 程序異常  {anomaly_count}項（{detail}）", None)

        # ── 選單列圖示 ──
        _profile = active_profile or expected_profile or "day"

        total = core_up + omlx_up
        # 離峰模式 8082/8083 不啟動，預期只有 E4B + embed = 2 個 oMLX
        _night_mode = _is_night_profile(_profile)
        if _night_mode:
            expected = len(SERVICES) + 2  # 只有 8080 + 8081
        else:
            expected = len(SERVICES) + len(OMLX_ENGINES)
        nodes_ok = nodes_up >= 1 if REMOTE_NODES else True
        if total >= expected and process_health.get("ok") is True and anomaly_count == 0 and nodes_ok and not text_status.get("degraded") and not text_status.get("mismatch"):
            self.title = " MAGI " if not _night_mode else " MAGI \U0001f319"
        elif core_up >= 2:
            self.title = " MAGI \u26a0"
        else:
            self.title = " MAGI \u2715"

    # ── 操作按鈕 ──────────────────────────────────────────────

    def _run_action(self, menu_item, label, command, original_callback):
        if not self._action_lock.acquire(blocking=False):
            return
        original_title = menu_item.title
        def _worker():
            try:
                _set_colored_title(menu_item, f"  ⏳ {label}...", _YELLOW)
                proc = subprocess.run(command, capture_output=True, text=True, timeout=120)
                if proc.returncode == 0:
                    _set_colored_title(menu_item, f"  ✅ {label} 完成", _GREEN)
                else:
                    _set_colored_title(menu_item, f"  ⚠ {label} 異常", _RED)
            except Exception:
                _set_colored_title(menu_item, f"  ⚠ {label} 錯誤", _RED)
            finally:
                time.sleep(3)
                _set_colored_title(menu_item, original_title, None)
                menu_item.set_callback(original_callback)
                self._action_lock.release()
        threading.Thread(target=_worker, daemon=True).start()

    def _set_dashboard_notice(self, text: str) -> None:
        if self.cockpit_view is not None:
            try:
                self.cockpit_view.set_action_notice(text)
            except Exception:
                logging.getLogger("menubar").warning("dashboard notice update failed", exc_info=True)

    def _show_dashboard_status_detail(self, detail: dict) -> None:
        if not (_HAS_APPKIT and self.cockpit_view is not None):
            self._set_dashboard_notice("無法開啟狀態說明")
            return
        try:
            self.cockpit_view.show_status_detail(detail if isinstance(detail, dict) else {})
        except Exception:
            logging.getLogger("menubar").warning("dashboard status detail failed", exc_info=True)
            self._set_dashboard_notice("無法開啟狀態說明")

    def _record_live_check_failure(self, reason: str, *, returncode: int | None = None, report_path: str = "") -> dict:
        failure = {
            "at": time.time(),
            "reason": re.sub(r"\s+", " ", str(reason or "live_check_failed")).strip()[:160],
            "returncode": returncode,
            "report_path": str(report_path or ""),
        }
        self._live_check_failure = failure
        failed_status = _business_module_status_failure(failure["reason"], returncode=returncode)
        failed_status["checked_at"] = datetime.fromtimestamp(failure["at"]).strftime("%H:%M")
        with self._cache_lock:
            cache = dict(self._status_cache) if self._status_cache else {}
            cache["business_live"] = failed_status
            cache["live_check_failure"] = dict(failure)
            self._status_cache = cache
            self._status_generation += 1
            return dict(cache)

    def _handle_dashboard_action(self, action: str) -> None:
        action = str(action or "")
        if action == "close":
            self._hide_cockpit_fullscreen()
            return
        if action == "refresh":
            self._set_dashboard_notice("正在重新整理狀態")

            def _worker():
                self._collect_status()
                with self._cache_lock:
                    snapshot = dict(self._status_cache) if self._status_cache else {}
                if snapshot:
                    self._apply_status(snapshot)
                self._set_dashboard_notice("狀態已重新整理")

            threading.Thread(target=_worker, daemon=True).start()
            return

        if action == "open_hub":
            self._set_dashboard_notice("正在開啟首頁")
            subprocess.Popen(["open", MAGI_HOME_URL], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return

        if action == "open_knowledge":
            self._set_dashboard_notice("正在開啟 Obsidian 知識庫")
            with self._cache_lock:
                knowledge = self._status_cache.get("knowledge_radar", {}) if self._status_cache else {}
            vault_path = str(knowledge.get("vault_path") or "") if isinstance(knowledge, dict) else ""
            if vault_path and os.path.isdir(vault_path):
                subprocess.Popen(
                    ["open", "-a", "Obsidian", vault_path],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            else:
                subprocess.Popen(["open", MAGI_HOME_URL], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                self._set_dashboard_notice("找不到 Obsidian Vault，已開啟首頁")
            return

        if action == "open_logs":
            self._set_dashboard_notice("正在開啟紀錄")
            with self._cache_lock:
                log_path = str(self._status_cache.get("live_log_path") or "/tmp/magi-menubar.log")
            subprocess.Popen(["open", log_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return

        if action == "run_check":
            if not self._action_lock.acquire(blocking=False):
                self._set_dashboard_notice("已有檢查正在執行")
                return
            self._set_dashboard_notice("正在執行三模組檢查")

            def _worker():
                try:
                    out_path = os.path.join(_runtime_dir_path(), "business_module_live_check_latest.json")
                    command = [
                        sys.executable,
                        os.path.join(MAGI_ROOT, "scripts", "ops", "business_module_live_check.py"),
                        "--json-out",
                        out_path,
                    ]
                    proc = subprocess.run(
                        command,
                        cwd=MAGI_ROOT,
                        capture_output=True,
                        text=True,
                        timeout=BUSINESS_LIVE_TIMEOUT_SEC,
                    )
                    if proc.returncode == 0:
                        self._live_check_failure = None
                        self._set_dashboard_notice("三模組檢查完成")
                    else:
                        snapshot = self._record_live_check_failure(
                            proc.stderr or proc.stdout or f"exit={proc.returncode}",
                            returncode=proc.returncode,
                            report_path=out_path,
                        )
                        self._set_dashboard_notice("三模組檢查需處理")
                        self._apply_status(snapshot)
                    self._collect_status()
                    with self._cache_lock:
                        snapshot = dict(self._status_cache) if self._status_cache else {}
                    if snapshot:
                        self._apply_status(snapshot)
                except Exception as exc:
                    logging.getLogger("menubar").warning("dashboard live check failed", exc_info=True)
                    snapshot = self._record_live_check_failure(str(exc), report_path=out_path if "out_path" in locals() else "")
                    self._apply_status(snapshot)
                    self._set_dashboard_notice("三模組檢查失敗")
                finally:
                    self._action_lock.release()

            threading.Thread(target=_worker, daemon=True).start()
            return

        if action == "toggle_theme":
            current = _load_menubar_theme()
            next_theme = _save_menubar_theme("forest" if current == "cyber" else "cyber")
            if self.cockpit_view is not None:
                self.cockpit_view.set_theme(next_theme)
            self._set_dashboard_notice(
                "已切換為日" if next_theme == "forest" else "已切換為夜"
            )
            return

    def on_start(self, _):
        self._run_action(self.start_item, "啟動", ["/opt/homebrew/bin/magi", "start"], self.on_start)

    def on_stop(self, _):
        self._run_action(self.stop_item, "停止", ["/opt/homebrew/bin/magi", "stop"], self.on_stop)

    def on_restart(self, _):
        self._run_action(self.restart_item, "重啟", ["/opt/homebrew/bin/magi", "restart"], self.on_restart)

    def on_clean_zombies(self, _):
        self._run_action(self.clean_zombie_item, "清殭屍", ["/opt/homebrew/bin/magi", "zombie"], self.on_clean_zombies)

    def on_quit(self, _):
        self._hide_cockpit_fullscreen()
        rumps.quit_application()


if __name__ == "__main__":
    _acquire_menubar_singleton()
    _initialize_menubar_application()
    MAGIMenuBar().run()
