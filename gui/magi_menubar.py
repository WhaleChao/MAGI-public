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
import threading
import time
import re
import logging
import urllib.request
import urllib.error
import atexit
import sys
from datetime import datetime

try:
    import fcntl
except ImportError:  # pragma: no cover - macOS runtime path
    fcntl = None


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
        NSAlert,
        NSAlertFirstButtonReturn,
        NSScrollView,
        NSTextView,
        NSPasteboard,
        NSPasteboardTypeString,
    )
    _HAS_APPKIT = True
    NSApplication.sharedApplication().setActivationPolicy_(
        NSApplicationActivationPolicyAccessory
    )
except ImportError:
    objc = None
    _HAS_APPKIT = False

# ── 設定 ──────────────────────────────────────────────────────────
MAGI_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if MAGI_ROOT not in sys.path:
    sys.path.insert(0, MAGI_ROOT)

try:
    from scripts.ops.omlx_profile_policy import (
        DAY_FALLBACK_MODEL_KEYWORD,
        DAY_MODEL_KEYWORD,
        NIGHT_MODEL_KEYWORD,
        expected_profile_now as expected_omlx_profile_now,
    )
except Exception:
    DAY_MODEL_KEYWORD = "e4b"
    DAY_FALLBACK_MODEL_KEYWORD = "e4b"
    NIGHT_MODEL_KEYWORD = "26b"

    def expected_omlx_profile_now():
        now = datetime.now()
        minutes = now.hour * 60 + now.minute
        return ("day", DAY_MODEL_KEYWORD) if 395 <= minutes < 1310 else ("night", NIGHT_MODEL_KEYWORD)


def _load_local_env_keys(keys: set[str]) -> None:
    """Load non-secret menubar settings when LaunchAgent lacks shell env."""
    env_path = os.path.join(MAGI_ROOT, ".env")
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

CHECK_INTERVAL = 5  # 秒

SERVICES = [
    ("守護程式", ("daemon.py", "run_daemon_no_site.py")),
    ("主伺服器", ("api/server.py",)),
    ("通訊機器人", ("api/discord_bot.py",)),
    ("工具介面", ("api/tools_api.py",)),
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
        (name.strip(), f"/Volumes/{name.strip()}")
        for name in _NAS_SHARES_ENV.split(",")
        if name.strip()
    ]
else:
    NAS_SHARES = [
        ("homes", "/Volumes/homes"),
        ("lumi",  "/Volumes/lumi"),
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


def _state_icon(state: str) -> str:
    if state == "ok":
        return "🟢"
    if state in {"waiting", "idle"}:
        return "🟡"
    return "🔴"


def _state_color(state: str):
    if state == "ok":
        return _GREEN
    if state in {"waiting", "idle"}:
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
    if payload.get("ok") is not True or requires_human or errors or warnings:
        reasons = []
        for item in payload.get("unresolved_issue_ids") or []:
            reasons.append(str(item))
        for item in payload.get("failed") or []:
            if isinstance(item, dict):
                reasons.append(f"{item.get('path') or item.get('name') or '檢查項目'}：{item.get('reason') or item.get('detail') or '失敗'}")
        if requires_human:
            reasons.extend(str(item.get("reason") or item) if isinstance(item, dict) else str(item) for item in requires_human)
        detail = "\n".join(dict.fromkeys(reason for reason in reasons if reason))
        if not detail:
            detail = f"錯誤 {errors} 項、警告 {warnings} 項；請開啟健康報告查看。"
        return {"state": "attention", "label": f"{label}需處理", "detail": detail}
    return {"state": "ok", "label": f"{label}通過", "detail": "最近一次健康檢查通過。"}


def _cron_failure_detail(details: list[dict]) -> str:
    lines = []
    for item in details or []:
        status = str(item.get("status") or "")
        if status not in {"failed", "stale"}:
            continue
        state = "執行失敗" if status == "failed" else "超過預期時間未執行"
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
        lines = []
        if blocked:
            lines.append(f"目前有 {blocked} 件案件的結案回報受阻，需補齊資料或排除錯誤。")
        pending_items = info.get("pending_items") if isinstance(info.get("pending_items"), list) else []
        review_items = info.get("review_items") if isinstance(info.get("review_items"), list) else []
        if pending:
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
            lines.append(f"附件重試清單（{pending_retry + manual_review} 件）：")
            for index, item in enumerate(retry_items, 1):
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
            if pending_retry:
                lines.append("")
                lines.append("MAGI 下一步：每小時自動重試；附件出現後會下載並歸檔，再更新本清單。")
            if manual_review:
                lines.append("標示「需人工確認」的案件不會繼續盲目重試，需先修正案件資料或登入問題。")
        elif pending_retry or manual_review:
            lines.append("案件明細尚在同步，請稍後重新整理。")
        return "\n".join(lines) or "目前法扶附件均已取得。"

    if label == "閱卷下載":
        ready = int(info.get("ready_to_download") or 0)
        ready_items = info.get("ready_items") if isinstance(info.get("ready_items"), list) else []
        if not bool(info.get("auto_download")):
            return "閱卷入口目前只會掃描，不會自動下載檔案。"
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

    if label == "錄音轉文字":
        provider = str(info.get("provider") or "")
        if provider == "mlx_whisper":
            return "目前使用 Apple MLX Whisper 高品質模型處理錄音轉文字。"
        if provider:
            return f"目前使用 {provider} 處理錄音轉文字。"
        return "尚未偵測到可用的錄音轉文字引擎。"

    if label == "NVIDIA重型":
        model = str(info.get("model") or "")
        if model:
            return f"重型指令目前會交由 NVIDIA 高階模型處理。\n目前模型：{model}"
        return "尚未偵測到可供重型指令使用的 NVIDIA 高階模型。"

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
    path = os.path.join(MAGI_ROOT, "static", "business_readiness_latest.json")
    payload = _load_json_file(path)
    items = payload.get("items") if isinstance(payload.get("items"), dict) else {}
    generated_epoch = _epoch_from_iso(payload.get("generated_at", ""))
    if not items:
        return {
            "state": "waiting",
            "summary": {"attention": 0, "waiting": 5, "ok": 0},
            "items": {
                label: {"state": "waiting", "label": CHECK_WAITING_TEXT}
                for label in ("案件回報", "法扶附件", "閱卷下載", "錄音轉文字", "NVIDIA重型")
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
    if enabled_count <= 0:
        return {"state": "waiting", "label": "讀取失敗", "failed": failed, "stale": stale}
    parts = [f"{enabled_count}個啟用"]
    if failed:
        parts.append(f"{failed}個失敗")
    if stale:
        parts.append(f"{stale}個逾時")
    if failed:
        state = "attention"
    elif stale:
        state = "waiting"
    elif not cron_bot:
        parts.append("Bot停止")
        state = "attention"
    else:
        parts.append(OPERATIONAL_TEXT)
        state = "ok"
    return {"state": state, "label": "・".join(parts), "failed": failed, "stale": stale}


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

    zombies, _detail = c.get("zombies", (0, ""))
    if int(zombies or 0) > 0:
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
        _state_from_infos((c.get("business_readiness", {}).get("items", {}) or {}).values()),
    ]
    if any(state in {"attention", "down", "failed"} for state in states):
        return "attention"
    if any(state != "ok" for state in states):
        return "waiting"
    return "ok"


if _HAS_APPKIT:
    class _CockpitDashboardView(NSView):
        """Custom first menu item: a drawn cockpit-style status dashboard."""

        def initWithFrame_(self, frame):
            self = objc.super(_CockpitDashboardView, self).initWithFrame_(frame)
            if self is None:
                return None
            self._status_cache = {}
            self._controller = None
            self._button_regions = []
            self._status_regions = []
            self._action_notice = ""
            return self

        def isFlipped(self):
            return True

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
        def _point_in_rect(self, point, rect) -> bool:
            x, y, w, h = rect
            return x <= point.x <= x + w and y <= point.y <= y + h

        def mouseDown_(self, event):
            point = self.convertPoint_fromView_(event.locationInWindow(), None)
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

        @objc.python_method
        def _color(self, value: str, alpha: float = 1.0):
            value = value.strip().lstrip("#")
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
        def _line(self, x1: float, y1: float, x2: float, y2: float, color, width: float = 1.0):
            path = NSBezierPath.bezierPath()
            path.moveToPoint_(NSMakePoint(x1, y1))
            path.lineToPoint_(NSMakePoint(x2, y2))
            color.setStroke()
            path.setLineWidth_(width)
            path.stroke()

        @objc.python_method
        def _dot(self, x: float, y: float, r: float, color):
            NSBezierPath.bezierPathWithOvalInRect_(NSMakeRect(x, y, r * 2, r * 2)).fill()
            color.setFill()

        @objc.python_method
        def _section(self, x: float, y: float, w: float, h: float, title: str, accent):
            self._rounded(
                x,
                y,
                w,
                h,
                8,
                fill=self._color("071619", 0.82),
                stroke=self._color("1F8F86", 0.72),
                line_width=1.1,
            )
            self._rounded(
                x + 8,
                y + 8,
                w - 16,
                30,
                5,
                fill=self._color("08292C", 0.72),
                stroke=accent,
                line_width=0.8,
            )
            self._draw_text(title, x + 18, y + 12, w - 36, 20, size=14, color=accent, weight=0.7)

        @objc.python_method
        def _dashboard_state_color(self, state: str):
            return {
                "ok": self._color("67F58D"),
                "alive": self._color("67F58D"),
                "waiting": self._color("FFC857"),
                "idle": self._color("FFC857"),
                "starting": self._color("FFC857"),
                "stale": self._color("FFC857"),
                "attention": self._color("FF5F5F"),
                "down": self._color("FF5F5F"),
                "failed": self._color("FF5F5F"),
            }.get(str(state or "waiting"), self._color("FFC857"))

        @objc.python_method
        def _compact(self, text, limit: int = 18) -> str:
            text = str(text or "").strip()
            if len(text) <= limit:
                return text
            return text[: max(0, limit - 3)] + "..."

        @objc.python_method
        def _draw_status_row(self, x, y, w, label, value, state="ok", value_w: float = 104, row_h: float = 29, detail: str = ""):
            color = {
                "ok": self._color("67F58D"),
                "alive": self._color("67F58D"),
                "waiting": self._color("FFC857"),
                "idle": self._color("FFC857"),
                "starting": self._color("FFC857"),
                "stale": self._color("FFC857"),
                "attention": self._color("FF5F5F"),
                "down": self._color("FF5F5F"),
                "failed": self._color("FF5F5F"),
            }.get(state, self._color("FFC857"))
            text_y = y + max(2, (row_h - 18) / 2)
            self._rounded(x, y, w, row_h, 4, fill=self._color("0B2024", 0.78), stroke=self._color("215A5B", 0.54))
            self._rounded(x + 10, y + max(6, (row_h - 9) / 2), 9, 9, 4.5, fill=color)
            self._draw_text(label, x + 28, text_y, w - value_w - 38, 18, size=12.0, color=self._color("E9F5F1"), weight=0.45)
            self._draw_text(self._compact(value, 18), x + w - value_w - 10, text_y, value_w, 18, size=11.3, color=color, weight=0.7, align=NSRightTextAlignment)
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
            fill = self._color("0B3034", 0.92 if highlighted else 0.62)
            stroke = self._color("31F6E2", 0.72 if highlighted else 0.25)
            self._rounded(x, y, w, 31, 4, fill=fill, stroke=stroke, line_width=1.0)
            self._draw_text(event.get("time", "--:--"), x + 12, y + 8, 48, 16, size=11.5, color=self._color("31F6E2"), mono=True)
            self._rounded(x + 68, y + 10, 10, 10, 5, fill=color)
            self._draw_text(event.get("source", ""), x + 88, y + 7, 126, 17, size=12.2, color=self._color("E9F5F1"), weight=0.45)
            self._draw_text(event.get("label", CHECK_WAITING_TEXT), x + w - 114, y + 7, 96, 17, size=11.8, color=color, weight=0.65, align=NSRightTextAlignment)

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
            state = "attention" if magi_mb >= 8192 else ("waiting" if magi_mb >= 4096 else "ok")
            rows.append(("程序佔用", f"{magi_mb}MB", state))
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
            self._status_regions = []
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

        def drawRect_(self, rect):
            bounds = self.bounds()
            width = bounds.size.width
            height = bounds.size.height
            cache = getattr(self, "_status_cache", {}) or {}
            cyan = self._color("35F5E8")
            green = self._color("67F58D")
            amber = self._color("FFC857")
            red = self._color("FF5F5F")
            muted = self._color("8AA3A4")

            self._rounded(0, 0, width, height, 0, fill=self._color("040B10", 0.98))
            self._rounded(10, 10, width - 20, height - 20, 16, fill=self._color("071217", 0.98), stroke=self._color("3D5963", 0.95), line_width=1.5)
            self._rounded(24, 24, width - 48, height - 48, 10, fill=self._color("06191D", 0.92), stroke=self._color("1F8F86", 0.42), line_width=1.1)

            for i, color in enumerate((amber, amber, self._color("FF5F5F"), self._color("FF5F5F"))):
                self._rounded(42 + i * 16, 42, 10, 5, 2, fill=color)
            for i in range(6):
                self._rounded(width - 124 + i * 13, 42, 8, 5, 2, fill=amber if i < 5 else self._color("FF5F5F"))

            overall = _overall_state(cache)
            overall_label = OVERALL_WAITING_TEXT if overall == "waiting" else _label_for_state(overall, OPERATIONAL_TEXT)
            self._draw_text("MAGI", 0, 32, width, 42, size=34, color=cyan, weight=0.78, align=NSCenterTextAlignment, mono=True)
            self._draw_text(f"整體狀態：{overall_label}", 0, 74, width, 22, size=14, color=self._dashboard_state_color(overall), weight=0.65, align=NSCenterTextAlignment)
            readiness = cache.get("business_readiness", {}) if isinstance(cache, dict) else {}
            summary = readiness.get("summary", {}) if isinstance(readiness, dict) else {}
            attention_count = int(summary.get("attention") or 0)
            waiting_count = int(summary.get("waiting") or 0)
            summary_color = red if attention_count else (amber if waiting_count else green)
            summary_text = f"業務需處理 {attention_count} 項　待確認 {waiting_count} 項"
            self._draw_text(summary_text, 0, 96, width, 18, size=11.5, color=summary_color, weight=0.6, align=NSCenterTextAlignment)
            self._line(40, 118, width - 40, 118, self._color("31F6E2", 0.52), 1.0)

            margin = 28
            gap = 18
            top_y = 124
            top_h = 318
            left_w = 272
            center_w = 390
            right_w = width - margin * 2 - left_w - center_w - gap * 2
            left_x = margin
            center_x = left_x + left_w + gap
            right_x = center_x + center_w + gap

            # 核心服務
            self._section(left_x, top_y, left_w, top_h, "核心服務", cyan)
            db_state = "ok" if (cache.get("db", {}) or {}).get("local") else "attention"
            zombies, _z_detail = cache.get("zombies", (0, "")) if isinstance(cache, dict) else (0, "")
            zombie_state = "ok" if int(zombies or 0) == 0 else "attention"
            service_rows = [
                ("守護程式", _label_for_state(_service_state(cache, "守護程式", "守護程序"), OPERATIONAL_TEXT), _service_state(cache, "守護程式", "守護程序"), str((cache.get("service_details") or {}).get("守護程式") or "程序狀態正常。")),
                ("主伺服器", _label_for_state(_service_state(cache, "主伺服器"), OPERATIONAL_TEXT), _service_state(cache, "主伺服器"), str((cache.get("service_details") or {}).get("主伺服器") or "主伺服器狀態正常。")),
                ("通訊機器人", _label_for_state(_service_state(cache, "通訊機器人", "通訊機器"), OPERATIONAL_TEXT), _service_state(cache, "通訊機器人", "通訊機器"), str((cache.get("service_details") or {}).get("通訊機器人") or "通訊機器人狀態正常。")),
                ("工具介面", _label_for_state(_service_state(cache, "工具介面", "工具接口"), OPERATIONAL_TEXT), _service_state(cache, "工具介面", "工具接口"), str((cache.get("service_details") or {}).get("工具介面") or "工具介面狀態正常。")),
                ("本機資料庫", OPERATIONAL_TEXT if db_state == "ok" else "離線", db_state, "本機 MariaDB 連線正常。" if db_state == "ok" else "無法連線至本機 MariaDB 的 3306 連接埠。"),
                ("殭屍程序", "無" if zombie_state == "ok" else f"{zombies}個", zombie_state, "未偵測到殭屍程序。" if zombie_state == "ok" else str(_z_detail or f"偵測到 {zombies} 個殭屍程序。")),
            ]
            for i, row in enumerate(service_rows):
                self._draw_status_row(left_x + 14, top_y + 52 + i * 36, left_w - 28, row[0], row[1], row[2], 104, 30, row[3])

            # 即時紀錄
            self._section(center_x, top_y, center_w, top_h, "即時紀錄", cyan)
            live_events = _format_live_events(cache)
            for i, event in enumerate(live_events):
                self._draw_log_row(center_x + 16, top_y + 48 + i * 31, center_w - 32, event, highlighted=i == len(live_events) - 1)
            self._line(center_x + 24, top_y + 284, center_x + center_w - 24, top_y + 284, self._color("31F6E2", 0.58), 1.0)
            live_events = _format_live_events(cache)
            pulse_points = cache.get("live_pulse_points") if isinstance(cache, dict) else None
            if not isinstance(pulse_points, list):
                pulse_points = _live_pulse_points(live_events)
            base_x, base_y = center_x + 190, top_y + 294
            for idx, point in enumerate(pulse_points):
                self._rounded(base_x + idx * 9, base_y + 22 - point, 5, point, 1.5, fill=self._color("31F6E2", 0.70))
            checked_at = ""
            live = cache.get("business_live", {}) if isinstance(cache, dict) else {}
            if isinstance(live, dict):
                checked_at = live.get("checked_at", "")
            self._draw_text(f"資料脈衝　最近檢查 {checked_at or '--:--'}", center_x + 24, top_y + 296, 156, 18, size=10.5, color=muted, mono=True)

            # 任務模組與目前業務待辦
            self._section(right_x, top_y, right_w, TASK_MODULE_SECTION_HEIGHT, "任務模組", cyan)
            modules = self._business_modules()
            for i, (label, info) in enumerate(modules.items()):
                state = str(info.get("state") or "waiting")
                value = str(info.get("label") or _label_for_state(state, OPERATIONAL_TEXT))
                row_y, row_h = _task_module_row_geometry(i)
                self._draw_status_row(right_x + 14, top_y + row_y, right_w - 28, label, value, state, 96, row_h)

            factory_y = top_y + 164
            self._section(right_x, factory_y, right_w, top_h - 164, "業務待辦", amber)
            readiness_items = readiness.get("items", {}) if isinstance(readiness, dict) else {}
            for i, (label, info) in enumerate(readiness_items.items()):
                state = str(info.get("state") or "waiting")
                value = str(info.get("label") or CHECK_WAITING_TEXT)
                detail = _business_readiness_detail(str(label), info)
                self._draw_status_row(right_x + 14, factory_y + 44 + i * 22, right_w - 28, label, value, state, 86, 21, detail)

            # 資源、模型、NAS、背景監控
            bottom_y = 454
            bottom_h = 230
            resource_w = 356
            nas_w = 300
            monitor_w = width - margin * 2 - resource_w - nas_w - gap * 2
            resource_x = margin
            nas_x = resource_x + resource_w + gap
            monitor_x = nas_x + nas_w + gap

            self._section(resource_x, bottom_y, resource_w, bottom_h, "資源與模型", cyan)
            resource_rows = self._memory_rows(cache) + self._engine_rows(cache)
            for i, row in enumerate(resource_rows[:6]):
                self._draw_status_row(resource_x + 14, bottom_y + 42 + i * 22, resource_w - 28, row[0], row[1], row[2], 150, 21)

            self._section(nas_x, bottom_y, nas_w, bottom_h, "網路硬碟", cyan)
            nas_rows = self._nas_rows(cache)
            for i, row in enumerate(nas_rows[:5]):
                self._draw_status_row(nas_x + 14, bottom_y + 44 + i * 27, nas_w - 28, row[0], row[1], row[2], 116, 24)
            if len(nas_rows) > 5:
                self._draw_text(f"其餘分卷 {len(nas_rows) - 5} 個", nas_x + 18, bottom_y + 150, nas_w - 36, 16, size=10.5, color=muted, align=NSCenterTextAlignment)

            self._section(monitor_x, bottom_y, monitor_w, bottom_h, "背景監控", cyan)
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
                self._draw_status_row(monitor_x + 14, bottom_y + 42 + i * 21, monitor_w - 28, row[0], row[1], row[2], 92, 18, detail)

            # 操作列
            commands = [
                ("重新整理", "refresh"),
                ("開啟首頁", "open_hub"),
                ("查看紀錄", "open_logs"),
                ("執行檢查", "run_check"),
            ]
            button_y = height - 58
            button_w = (width - 84) / 4
            self._button_regions = []
            for i, (text, action) in enumerate(commands):
                x = 30 + i * (button_w + 8)
                self._rounded(x, button_y, button_w, 38, 6, fill=self._color("0B2024", 0.88), stroke=self._color("31F6E2", 0.52), line_width=1.0)
                self._draw_text(text, x, button_y + 10, button_w, 18, size=12.5, color=self._color("E9F5F1"), weight=0.55, align=NSCenterTextAlignment)
                self._button_regions.append(((x, button_y, button_w, 38), action))
            if self._action_notice:
                self._draw_text(
                    self._action_notice,
                    30,
                    button_y - 20,
                    width - 60,
                    16,
                    size=10.5,
                    color=muted,
                    align=NSCenterTextAlignment,
                )

else:
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
    if not process_alive:
        return False, "程序未執行"
    probe_urls = {"主伺服器": SERVER_HEALTH_URL, "工具介面": TOOLS_HEALTH_URL}
    url = probe_urls.get(name)
    if url and not _http_liveness(url):
        return False, "HTTP 無回應"
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


def _omlx_text_status(model_id: str, expected_profile: str, expected_keyword: str, active_profile: str) -> dict:
    """Return user-facing text-model status for the menubar."""
    model_low = (model_id or "").lower()
    expected_keyword = (expected_keyword or "").lower()
    fallback_keyword = DAY_FALLBACK_MODEL_KEYWORD.lower()
    expected_profile = expected_profile or "day"
    active_profile = active_profile or ""

    profile_zh = "日間" if expected_profile == "day" else "夜間"
    expected_label = _model_label(expected_keyword)

    allowed_active = {expected_profile}
    if expected_profile == "day":
        allowed_active.add("day-e4b-degraded")
    if expected_profile == "night":
        allowed_active.add("night-e4b-degraded")
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

    if fallback_keyword and fallback_keyword in model_low:
        label = f"{profile_zh}降級E4B（預期{expected_label}）"
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


_MAGI_ZOMBIE_PARENTS = {
    "daemon.py", "server.py", "discord_bot.py", "tools_api.py",
    "action.py", "heartbeat.py", "Python", "python3", "python3.14",
    "omlx", "chromedriver", "caddy", "socat", "bash",
}


def _count_zombies() -> tuple:
    try:
        r = subprocess.run(["ps", "-eo", "pid=,ppid=,stat=,command="], capture_output=True, text=True, timeout=3)
        magi_zombies = 0
        parent_names = []
        for line in r.stdout.splitlines():
            parts = line.split(None, 3)
            if len(parts) < 3 or not parts[2].startswith("Z"):
                continue
            ppid = parts[1]
            try:
                r2 = subprocess.run(["ps", "-p", ppid, "-o", "command="], capture_output=True, text=True, timeout=2)
                pcmd = r2.stdout.strip()
            except Exception:
                pcmd = ""
            if "MAGI" in pcmd or "magi" in pcmd or "Desktop/MAGI" in pcmd or any(kw in pcmd for kw in _MAGI_ZOMBIE_PARENTS):
                magi_zombies += 1
                name = pcmd.split("/")[-1].split()[0][:20] if pcmd else "?"
                if name and name not in parent_names:
                    parent_names.append(name)
        detail = f"({', '.join(parent_names[:3])})" if parent_names else ""
        return magi_zombies, detail
    except Exception:
        return 0, ""


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


def _get_module_memory() -> list:
    import re
    results = []
    try:
        r = subprocess.run(["ps", "-eo", "pid,rss,command"], capture_output=True, text=True, timeout=5)
        lines = r.stdout.strip().splitlines()[1:]
        for mod_name, pattern in _MEM_MODULES:
            total_rss = 0
            count = 0
            regex = re.compile(pattern)
            for line in lines:
                parts = line.strip().split(None, 2)
                if len(parts) < 3:
                    continue
                try:
                    rss_kb = int(parts[1])
                except ValueError:
                    continue
                if regex.search(parts[2]):
                    total_rss += rss_kb
                    count += 1
            if count > 0:
                results.append((mod_name, total_rss // 1024, count))
    except Exception:
        pass
    results.sort(key=lambda x: x[1], reverse=True)
    return results


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
        path = os.path.join(MAGI_ROOT, "cron_jobs.json")
        with open(path, "r", encoding="utf-8") as f:
            jobs = json.load(f)
        return [j for j in jobs if isinstance(j, dict)]
    except Exception:
        return []


def _runtime_dir_path() -> str:
    raw = os.environ.get("MAGI_RUNTIME_DIR", "").strip()
    return raw if raw else os.path.join(MAGI_ROOT, ".runtime")


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


def _cron_state_failed(state_item: dict) -> bool:
    if not isinstance(state_item, dict) or not state_item:
        return False
    returncode = _cron_returncode(state_item.get("returncode", state_item.get("last_returncode")))
    timed_out = bool(state_item.get("timed_out") or state_item.get("last_timed_out"))
    last_success = state_item.get("last_success")
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
            "channel_marker_leak",
            "insufficient_traditional_chinese",
            "too_much_english",
            "blocked from deploy",
            "deploy_allowed=false",
        )
    )


def _cron_display_timestamp(job: dict, state_item: dict) -> str:
    if _cron_state_failed(state_item):
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


def _cron_details_from_state(jobs: list, cron_state: dict, *, now: datetime | None = None) -> list[dict]:
    """Build all cron details before limiting display so failures cannot be hidden."""
    details = []
    for job in jobs:
        if not isinstance(job, dict) or not job.get("enabled", True):
            continue
        job_id = str(job.get("id") or "").strip()
        state_item = cron_state.get(job_id) if isinstance(cron_state.get(job_id), dict) else {}
        desc = str(job.get("desc") or job.get("command", "")[:30]).strip()
        cron_expr = str(job.get("cron", "")).strip()
        last_run = _cron_display_timestamp(job, state_item)
        safe_rejection = _cron_safe_validation_rejection(job, state_item)
        failed = _cron_state_failed(state_item) and not safe_rejection
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
                    stale = (reference - dt).total_seconds() / 3600 > _cron_stale_threshold_hours(cron_expr)
            except Exception:
                stale = False
        status = "failed" if failed else ("stale" if stale else ("waiting" if not last_run else "ok"))
        details.append(
            {
                "id": job_id,
                "desc": desc[:25],
                "cron": cron_expr,
                "relative": _parse_last_run(last_run),
                "status": status,
                "stale": stale,
                "safe_rejection": safe_rejection,
            }
        )
    priority = {"failed": 0, "stale": 1, "waiting": 2, "ok": 3}
    return sorted(details, key=lambda detail: (priority.get(str(detail.get("status")), 4), str(detail.get("id") or "")))


def _parse_dt(raw: str) -> datetime | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except Exception:
        return None


def _parse_last_run(iso_str: str) -> str:
    """Convert ISO timestamp to relative time string like '2小時前'."""
    if not iso_str:
        return "從未"
    try:
        dt = _parse_dt(iso_str)
        if dt is None:
            return iso_str[:16]
        now = datetime.now(dt.tzinfo) if dt.tzinfo else datetime.now()
        delta = now - dt
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
        "label": "尚無活動",
        "intent": "尚無活動",
        "plan": "尚無活動",
        "tool": "尚無活動",
        "model": "尚無活動",
        "confirmation": "尚無活動",
        "retry": "尚無活動",
        "route_confidence": "尚無活動",
        "success_rate_7d": "尚無活動",
        "detail": "Agent 尚無公開活動摘要。",
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

    status = {
        "state": "waiting" if waiting_confirmation else "ok",
        "intent": intent or "其他公開作業",
        "plan": "；".join(steps) if steps else "尚無公開步驟",
        "tool": tool or "未使用公開工具",
        "model": model or "未提供公開模型",
        "confirmation": "等待確認" if waiting_confirmation else "無需確認",
        "retry": f"{retry_count} 次" if retry_count is not None else "0 次",
        "route_confidence": route_confidence,
        "success_rate_7d": success_rate,
    }
    if waiting_confirmation:
        status["label"] = f"{status['intent']} · 等待確認"
    elif retry_count:
        status["label"] = f"{status['intent']} · 重試 {retry_count} 次"
    else:
        status["label"] = status["intent"]
    status["detail"] = "\n".join(
        [
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


def _agent_status_live() -> dict:
    path = os.path.join(MAGI_ROOT, "static", AGENT_STATUS_PUBLIC_FILENAME)
    status = _agent_status_from_public_payload(_load_json_file(path))
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


def _magi_process_memory_icon(magi_mb: int) -> str:
    if magi_mb >= 8192:
        return "🔴"
    if magi_mb >= 4096:
        return "🟡"
    return "🟢"


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
        self.cockpit_view = None
        self.cockpit_menu_item = self._create_cockpit_menu_item()

        # ── Header ──
        self.menu_header = rumps.MenuItem("  MAGI", callback=None)
        self.menu_header.set_callback(None)
        self.overall_status_item = rumps.MenuItem("  整體狀態：等待同步", callback=None)
        self.overall_status_item.set_callback(None)
        self.agent_status_item = rumps.MenuItem("  Agent 狀態：尚無活動", callback=None)
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
        self.mem_total_item = rumps.MenuItem("  ◻ 程序佔用")
        self.mem_total_item.set_callback(None)
        self.zombie_item = rumps.MenuItem("  ◻ 殭屍程序")
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
        if self.cockpit_menu_item is not None and self.cockpit_view is not None:
            try:
                self.cockpit_menu_item._menuitem.setView_(self.cockpit_view)
            except Exception:
                logging.getLogger("menubar").warning("cockpit dashboard view attach failed", exc_info=True)

    def _create_cockpit_menu_item(self):
        if not (_HAS_APPKIT and _HAS_RUMPS and _CockpitDashboardView is not None):
            return None
        try:
            item = rumps.MenuItem("")
            view = _CockpitDashboardView.alloc().initWithFrame_(NSMakeRect(0, 0, DASHBOARD_WIDTH, DASHBOARD_HEIGHT))
            view.set_controller(self)
            item._menuitem.setView_(view)
            item._menuitem.setEnabled_(True)
            self.cockpit_view = view
            return item
        except Exception:
            logging.getLogger("menubar").warning("cockpit dashboard view unavailable", exc_info=True)
            self.cockpit_view = None
            return None

    # ── 資料收集（背景執行緒）────────────────────────────────────

    @rumps.timer(CHECK_INTERVAL)
    def _periodic_check(self, _sender):
        with self._cache_lock:
            cache_snapshot = dict(self._status_cache) if self._status_cache else {}
        if cache_snapshot:
            try:
                self._apply_status(cache_snapshot)
            except Exception as _apply_err:
                import traceback
                logging.getLogger("menubar").error("_apply_status error: %s\n%s", _apply_err, traceback.format_exc())
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

        server_log_path = os.path.join(MAGI_ROOT, ".agent", "server.log")
        cache["live_log_path"] = server_log_path if os.path.isfile(server_log_path) else "/tmp/magi-menubar.log"
        cache["live_events"] = _live_log_events(_read_log_tail(server_log_path))
        cache["live_pulse_points"] = _live_pulse_points(cache["live_events"])

        # ── 推理引擎 ──
        engines = {}
        for name, port in OMLX_ENGINES:
            engines[name] = _check_omlx(port)
        cache["engines"] = engines
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
            log_path = os.path.join(MAGI_ROOT, ".agent", "server.log")
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
            _laf_state = _load_json_file(os.path.join(MAGI_ROOT, "static", "laf_gmail_monitor_state.json"))
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
            _retry_payload = _load_json_file(os.path.join(MAGI_ROOT, "static", "laf_portal_retry_state.json"))
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
            _fr_email_state = _load_json_file(os.path.join(MAGI_ROOT, "static", "file_review_email_monitor_state.json"))
            _fr_email_status = str(_fr_email_state.get("status") or "").lower()
            _fr_email_epoch = _epoch_from_iso(_fr_email_state.get("updated_at", ""))
            _integrated_review_email = (
                str(_fr_email_state.get("source") or "") == "laf_gmail_monitor_cycle"
                or "file review email scan integrated in LAF monitor cycle" in _log_tail
                or "File Review Email Monitor: integrated into LAF Gmail Monitor cycle" in _log_tail
            )
            _fr_state = _load_json_file(os.path.join(MAGI_ROOT, "static", "file_review_auto_state.json"))
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
                        _portal_state = "idle"
                        _portal_detail = "僅掃描未下載"
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

        # ── 系統記憶體 ──
        cache["mem"] = _get_system_memory()
        cache["magi_mb"] = sum(m[1] for m in _get_module_memory())
        cache["zombies"] = _count_zombies()

        with self._cache_lock:
            self._status_cache = cache

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
        icon = _magi_process_memory_icon(int(magi_mb or 0))
        _set_colored_title(self.mem_total_item, f"  {icon} 程序佔用  {magi_mb}MB", None)

        zombies, z_detail = c.get("zombies", (0, ""))
        if zombies == 0:
            _set_colored_title(self.zombie_item, "  🟢 殭屍程序  無", None)
        else:
            _set_colored_title(self.zombie_item, f"  🔴 殭屍程序  {zombies}個 {z_detail}", None)

        # ── 選單列圖示 ──
        _profile = active_profile or expected_profile or "day"

        total = core_up + omlx_up
        # 離峰模式 8082/8083 不啟動，預期只有 E4B + embed = 2 個 oMLX
        _night_mode = _profile == "night"
        if _night_mode:
            expected = len(SERVICES) + 2  # 只有 8080 + 8081
        else:
            expected = len(SERVICES) + len(OMLX_ENGINES)
        nodes_ok = nodes_up >= 1 if REMOTE_NODES else True
        if total >= expected and zombies == 0 and nodes_ok and not text_status.get("degraded") and not text_status.get("mismatch"):
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
        text = _status_detail_text(detail if isinstance(detail, dict) else {})
        if not _HAS_APPKIT:
            self._set_dashboard_notice("無法開啟狀態說明")
            return
        try:
            cockpit_view = getattr(self, "cockpit_view", None)
            cockpit_window = cockpit_view.window() if cockpit_view is not None else None
            if cockpit_window is not None:
                cockpit_window.orderOut_(None)
            NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
            alert = NSAlert.alloc().init()
            title = str(detail.get("title") or "狀態項目") if isinstance(detail, dict) else "狀態項目"
            state = str(detail.get("state") or "waiting") if isinstance(detail, dict) else "waiting"
            alert.setMessageText_(f"{title}－{'紅燈原因' if state in {'attention', 'down'} else '狀態說明'}")
            alert.setInformativeText_("可直接選取下方文字，或按「複製原因」。")

            scroll = NSScrollView.alloc().initWithFrame_(NSMakeRect(0, 0, 560, 230))
            scroll.setHasVerticalScroller_(True)
            scroll.setAutohidesScrollers_(True)
            text_view = NSTextView.alloc().initWithFrame_(NSMakeRect(0, 0, 540, 220))
            text_view.setEditable_(False)
            text_view.setSelectable_(True)
            text_view.setFont_(NSFont.systemFontOfSize_(13.0))
            text_view.setString_(text)
            scroll.setDocumentView_(text_view)
            alert.setAccessoryView_(scroll)
            alert.addButtonWithTitle_("複製原因")
            alert.addButtonWithTitle_("關閉")
            if alert.runModal() == NSAlertFirstButtonReturn:
                pasteboard = NSPasteboard.generalPasteboard()
                pasteboard.clearContents()
                pasteboard.setString_forType_(text, NSPasteboardTypeString)
                self._set_dashboard_notice("原因已複製")
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
            return dict(cache)

    def _handle_dashboard_action(self, action: str) -> None:
        action = str(action or "")
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

    def on_start(self, _):
        self._run_action(self.start_item, "啟動", ["/opt/homebrew/bin/magi", "start"], self.on_start)

    def on_stop(self, _):
        self._run_action(self.stop_item, "停止", ["/opt/homebrew/bin/magi", "stop"], self.on_stop)

    def on_restart(self, _):
        self._run_action(self.restart_item, "重啟", ["/opt/homebrew/bin/magi", "restart"], self.on_restart)

    def on_clean_zombies(self, _):
        self._run_action(self.clean_zombie_item, "清殭屍", ["/opt/homebrew/bin/magi", "zombie"], self.on_clean_zombies)

    def on_quit(self, _):
        rumps.quit_application()


if __name__ == "__main__":
    _acquire_menubar_singleton()
    MAGIMenuBar().run()
