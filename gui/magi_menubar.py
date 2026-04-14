#!/usr/bin/env python3
"""
MAGI 選單列狀態監控
在 macOS 選單列顯示 MAGI 系統健康狀態。

v3 — 增強版：
  - 遠端節點狀態（Melchior / Balthasar / Keeper）
  - DB failover 細節（雙活/備份/同步中）
  - NAS 分卷掛載狀態 + 容量
  - 排程任務逐條顯示 + 最後執行時間
  - 移除已廢棄的推理分層 tier
"""

import os
import subprocess
import json
import threading
import time
import urllib.request
import urllib.error

import rumps

# PyObjC: 強制上色 + 隱藏 Dock 圖示
try:
    from AppKit import (
        NSAttributedString,
        NSForegroundColorAttributeName,
        NSColor,
        NSFont,
        NSFontAttributeName,
        NSApplication,
        NSApplicationActivationPolicyAccessory,
    )
    _HAS_APPKIT = True
    NSApplication.sharedApplication().setActivationPolicy_(
        NSApplicationActivationPolicyAccessory
    )
except ImportError:
    _HAS_APPKIT = False

# ── 設定 ──────────────────────────────────────────────────────────
MAGI_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import sys
if MAGI_ROOT not in sys.path:
    sys.path.insert(0, MAGI_ROOT)

CHECK_INTERVAL = 5  # 秒

SERVICES = [
    ("守護程序", "daemon.py"),
    ("主伺服器", "api/server.py"),
    ("通訊機器", "api/discord_bot.py"),
    ("工具接口", "api/tools_api.py"),
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

# NAS 掛載卷
NAS_SHARES = [
    ("homes", "/Volumes/homes"),
    ("lumi",  "/Volumes/lumi"),
]
_USER_MOUNT_ROOT = os.path.expanduser("~/.magi_mounts")

# 背景監控 thread 名稱（用於偵測是否在線）
MONITOR_THREADS = [
    ("法扶 Gmail 監控", "laf-gmail-monitor"),
    ("法扶 Portal 重試", "laf-portal-retry-loop"),
    ("閱卷 Email 監控", "file-review-email"),
]

# 排程任務最多顯示的條數
CRON_DISPLAY_MAX = 15

# ── 顏色 ──
if _HAS_APPKIT:
    _GREEN  = NSColor.colorWithSRGBRed_green_blue_alpha_(0.34, 0.85, 0.47, 1.0)
    _YELLOW = NSColor.colorWithSRGBRed_green_blue_alpha_(1.0, 0.75, 0.0, 1.0)
    _RED    = NSColor.colorWithSRGBRed_green_blue_alpha_(1.0, 0.33, 0.33, 1.0)
    _GRAY   = NSColor.colorWithSRGBRed_green_blue_alpha_(0.55, 0.55, 0.55, 1.0)
    _CYAN   = NSColor.colorWithSRGBRed_green_blue_alpha_(0.3, 0.85, 0.9, 1.0)
    _FONT   = NSFont.monospacedSystemFontOfSize_weight_(12.0, 0.0)
    _FONT_S = NSFont.monospacedSystemFontOfSize_weight_(11.0, 0.0)
    _FONT_B = NSFont.monospacedSystemFontOfSize_weight_(12.0, 0.5)
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


# ── 工具函式 ─────────────────────────────────────────────────────

def _pgrep(pattern: str) -> str:
    try:
        r = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True, timeout=3)
        pids = r.stdout.strip().split("\n")
        return pids[0] if pids[0] else ""
    except Exception:
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
                if port in (8080, 11434):
                    main_kw = os.environ.get("MAGI_MAIN_MODEL", "gemma").lower().split("-")[0]
                    for m in models:
                        if main_kw in m.get("id", "").lower():
                            return m["id"]
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
        if not os.path.ismount(path):
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


def _load_cron_jobs() -> list:
    """Load cron_jobs.json and return list of job dicts."""
    try:
        path = os.path.join(MAGI_ROOT, "cron_jobs.json")
        with open(path, "r", encoding="utf-8") as f:
            jobs = json.load(f)
        return [j for j in jobs if isinstance(j, dict)]
    except Exception:
        return []


def _parse_last_run(iso_str: str) -> str:
    """Convert ISO timestamp to relative time string like '2小時前'."""
    if not iso_str:
        return "從未"
    try:
        from datetime import datetime
        # Handle both formats
        for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
            try:
                dt = datetime.fromisoformat(iso_str)
                break
            except (ValueError, TypeError):
                continue
        else:
            return iso_str[:16]
        delta = datetime.now() - dt
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


def _mem_bar(pct: float, width: int = 8) -> str:
    filled = int(pct / 100 * width)
    return "▓" * filled + "░" * (width - filled)


# ── 主程式 ───────────────────────────────────────────────────────

class MAGIMenuBar(rumps.App):
    def __init__(self):
        super().__init__(" MAGI ", quit_button=None)
        self.icon = None
        self._action_lock = threading.Lock()
        self._cache_lock = threading.Lock()
        self._status_cache = {}

        # ── Header ──
        self.menu_header = rumps.MenuItem("  MAGI v2", callback=None)
        self.menu_header.set_callback(None)

        # ── 核心服務 ──
        self.svc_header = rumps.MenuItem("── 核心服務 ──", callback=None)
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
        self.db_status_item = rumps.MenuItem("  ◻ 資料庫群")
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

        self.menu = [
            self.menu_header,
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

    # ── 資料收集（背景執行緒）────────────────────────────────────

    @rumps.timer(CHECK_INTERVAL)
    def _periodic_check(self, _sender):
        with self._cache_lock:
            cache_snapshot = dict(self._status_cache) if self._status_cache else {}
        if cache_snapshot:
            try:
                self._apply_status(cache_snapshot)
            except Exception:
                pass
        threading.Thread(target=self._collect_status, daemon=True).start()

    def _collect_status(self):
        """背景執行緒：收集所有 I/O 資料，存入 cache。"""
        cache = {}

        # ── 核心服務 ──
        svcs = {}
        for name, pattern in SERVICES:
            svcs[name] = bool(_pgrep(pattern))
        cache["services"] = svcs

        # ── 推理引擎 ──
        engines = {}
        for name, port in OMLX_ENGINES:
            engines[name] = _check_omlx(port)
        cache["engines"] = engines

        # ── 遠端節點 ──
        nodes = {}
        for display_name, reg_key, role, port, check_type in REMOTE_NODES:
            ip = _get_node_ip(reg_key)
            if not ip:
                # Hardcoded fallback
                _fb = {"melchior": "100.116.54.16", "balthasar": "100.118.235.126", "nas": "100.121.61.74"}
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
        enabled = [j for j in jobs if j.get("enabled", True)]
        cache["cron_enabled"] = len(enabled)
        cache["cron_bot"] = bool(_pgrep("discord_bot.py"))
        # 逐條資訊
        cron_details = []
        now_ts = time.time()
        for j in enabled[:CRON_DISPLAY_MAX]:
            desc = str(j.get("desc") or j.get("command", "")[:30]).strip()
            cron_expr = str(j.get("cron", "")).strip()
            last_run = str(j.get("last_run", "")).strip()
            relative = _parse_last_run(last_run)
            # 超過 26 小時未執行的 daily job → 可能異常
            stale = False
            if last_run:
                try:
                    from datetime import datetime
                    dt = datetime.fromisoformat(last_run)
                    age_h = (datetime.now() - dt).total_seconds() / 3600
                    # 對每2小時的 job，超過3小時算異常；daily 超過26小時
                    if "*/2" in cron_expr and age_h > 3:
                        stale = True
                    elif age_h > 26:
                        stale = True
                except Exception:
                    pass
            cron_details.append({
                "desc": desc[:25],
                "cron": cron_expr,
                "relative": relative,
                "stale": stale,
            })
        cache["cron_details"] = cron_details

        # ── 背景監控 thread ──
        monitors = {}
        try:
            log_path = os.path.join(MAGI_ROOT, ".agent", "server.log")
            tail = ""
            if os.path.isfile(log_path):
                with open(log_path, "rb") as f:
                    f.seek(0, 2)
                    sz = f.tell()
                    # 讀最後 100KB 確保涵蓋到啟動訊息
                    f.seek(max(0, sz - 102400))
                    tail = f.read().decode("utf-8", errors="replace")
            for display_name, thread_name in MONITOR_THREADS:
                alive = False
                detail = ""
                if thread_name == "laf-gmail-monitor":
                    alive = "[Gmail]" in tail and ("掃描" in tail or "檢查信件" in tail or "✅" in tail or "Gmail 監控已啟動" in tail)
                    for line in reversed(tail.splitlines()):
                        if "[Gmail]" in line and '"ts"' in line:
                            try:
                                detail = line.split('"ts": "')[1].split('"')[0][11:19]
                            except Exception:
                                pass
                            break
                elif thread_name == "laf-portal-retry-loop":
                    alive = "LAF portal retry loop started" in tail or "portal:retry" in tail or "retry loop started" in tail
                    for line in reversed(tail.splitlines()):
                        if "retry loop" in line and '"ts"' in line:
                            try:
                                detail = line.split('"ts": "')[1].split('"')[0][11:19]
                            except Exception:
                                pass
                            break
                elif thread_name == "file-review-email":
                    # 閱卷 email 監控是 cron job（job_file_review_check），不是常駐 thread
                    # 檢查 cron_jobs.json 裡 file_review_check 是否 enabled
                    try:
                        cron_path = os.path.join(MAGI_ROOT, "cron_jobs.json")
                        if os.path.isfile(cron_path):
                            with open(cron_path, "r") as _cf:
                                _cjobs = json.load(_cf)
                            for _cj in _cjobs:
                                if isinstance(_cj, dict) and "file_review" in str(_cj.get("command", "")).lower():
                                    alive = _cj.get("enabled", True)
                                    lr = str(_cj.get("last_run", "")).strip()
                                    if lr:
                                        detail = lr[11:19] if len(lr) > 18 else lr
                                    break
                    except Exception:
                        pass
                monitors[display_name] = {"alive": alive, "detail": detail}
        except Exception:
            pass
        cache["monitors"] = monitors

        # ── NAS ──
        try:
            from api.routing.node_registry import get_node as _get_node
            _nas = _get_node("nas")
            _nas_lan = (_nas.lan_ip if _nas else None) or "192.168.1.3"
            _nas_ts = (_nas.tailscale_ip if _nas else None) or "100.111.10.126"
        except Exception:
            _nas_lan, _nas_ts = "192.168.1.3", "100.111.10.126"
        lan_ip = os.environ.get("MAGI_NAS_HOST", _nas_lan)
        ts_ip = os.environ.get("MAGI_NAS_TAILSCALE_HOST", _nas_ts)
        lan_ok = _tcp(lan_ip, 445, timeout=1)
        vpn_ok = _tcp(ts_ip, 445, timeout=2)
        # 各卷掛載 + 容量
        shares = {}
        any_mounted = False
        for share_name, mount_path in NAS_SHARES:
            # 檢查 /Volumes/<share>, -1, -2, 以及 ~/.magi_mounts/<share>
            actual_path = mount_path
            user_path = os.path.join(_USER_MOUNT_ROOT, share_name)
            for candidate in (mount_path, mount_path + "-1", mount_path + "-2", user_path):
                if os.path.ismount(candidate):
                    actual_path = candidate
                    break
            mounted = os.path.ismount(actual_path)
            if mounted:
                any_mounted = True
            disk = _get_disk_usage(actual_path) if mounted else None
            shares[share_name] = {"mounted": mounted, "path": actual_path, "disk": disk}
        cache["nas"] = {
            "lan": lan_ok, "vpn": vpn_ok, "mounted": any_mounted,
            "shares": shares,
        }

        # ── DB (with failover detail) ──
        try:
            from api.db_failover import get_failover_status
            fo = get_failover_status()
            cache["db"] = {
                "remote": fo.get("remote_ok") if fo.get("remote_ok") is not None else _tcp(
                    os.environ.get("MAGI_REMOTE_DB_HOST", "100.121.61.74"), 3306, 2),
                "local": _tcp("127.0.0.1", 3306, 2),
                "failover_active": fo.get("failover_active", False),
                "syncing": fo.get("syncing", False),
                "active_host": fo.get("active_host", ""),
            }
        except Exception:
            # Fallback: raw TCP check
            remote_host = os.environ.get("MAGI_REMOTE_DB_HOST", "100.121.61.74")
            cache["db"] = {
                "remote": _tcp(remote_host, 3306, 2),
                "local": _tcp("127.0.0.1", 3306, 2),
                "failover_active": False,
                "syncing": False,
                "active_host": remote_host,
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

        # ── 核心服��� ──
        core_up = 0
        svcs = c.get("services", {})
        for name, _ in SERVICES:
            if svcs.get(name):
                _set_colored_title(self.service_items[name], f"  ● {name}  運行中", _GREEN)
                core_up += 1
            else:
                _set_colored_title(self.service_items[name], f"  ✗ {name}  已停止", _RED)

        # ── 推理引擎 ──
        omlx_up = 0
        engines = c.get("engines", {})
        for name, _ in OMLX_ENGINES:
            model_id = engines.get(name, "")
            if model_id:
                short = model_id[:28] if len(model_id) > 28 else model_id
                _set_colored_title(self.omlx_items[name], f"  ● {name}  {short}", _GREEN)
                omlx_up += 1
            else:
                _set_colored_title(self.omlx_items[name], f"  ✗ {name}  離線", _RED)
        # macOS Vision OCR status
        try:
            from skills.apple.apple_intelligence import VISION_AVAILABLE
            if VISION_AVAILABLE:
                _set_colored_title(self.ocr_item, "  ● OCR引擎  macOS Vision", _GREEN)
            else:
                _set_colored_title(self.ocr_item, "  ✗ OCR引擎  未安裝", _GRAY)
        except Exception as e:
            _set_colored_title(self.ocr_item, f"  ✗ OCR引擎  未安裝 ({e})", _GRAY)

        # ── 遠端節點 ──
        nodes_up = 0
        nodes = c.get("nodes", {})
        for display_name, _, role, _, _ in REMOTE_NODES:
            info = nodes.get(display_name, {})
            if info.get("online"):
                detail = info.get("detail", "")
                if detail and detail not in ("Active", "連線正常"):
                    short = detail[:20] if len(detail) > 20 else detail
                    label = f"  ● {display_name}  {short}"
                else:
                    label = f"  ● {display_name}  在線"
                _set_colored_title(self.node_items[display_name], label, _GREEN)
                nodes_up += 1
            else:
                _set_colored_title(self.node_items[display_name], f"  ✗ {display_name}  離線", _RED)

        # ── 排程 ──
        cron_n = c.get("cron_enabled", -1)
        cron_bot = c.get("cron_bot", False)
        if cron_n >= 0 and cron_bot:
            _set_colored_title(self.cron_summary_item, f"  ● 排程總覽  {cron_n}個啟用・Bot運行", _GREEN)
        elif cron_n > 0:
            _set_colored_title(self.cron_summary_item, f"  ✗ 排程總覽  {cron_n}個啟用・Bot停止", _RED)
        else:
            _set_colored_title(self.cron_summary_item, "  ⚠ 排程總覽  讀取失敗", _YELLOW)

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
            if detail["stale"]:
                _set_colored_title(item, f"    ⚠ {desc}  {rel}", _YELLOW, small=True)
            else:
                _set_colored_title(item, f"    ● {desc}  {rel}", _GRAY, small=True)
        # 隱藏多餘的
        for i in range(len(cron_details), len(self._cron_job_items)):
            _set_colored_title(self._cron_job_items[i], "", _GRAY, small=True)

        # ── 背景監控 ──
        monitors = c.get("monitors", {})
        for display_name, _ in MONITOR_THREADS:
            info = monitors.get(display_name, {})
            item = self.monitor_items.get(display_name)
            if not item:
                continue
            if info.get("alive"):
                detail = info.get("detail", "")
                suffix = f"  最近 {detail}" if detail else "  運行中"
                _set_colored_title(item, f"  ● {display_name}{suffix}", _GREEN)
            else:
                _set_colored_title(item, f"  ✗ {display_name}  未偵測到活動", _RED)

        # ── NAS ──
        nas = c.get("nas", {})
        if nas.get("lan") and nas.get("mounted"):
            _set_colored_title(self.nas_status_item, "  ● 網路硬碟  區網掛載", _GREEN)
        elif nas.get("vpn") and nas.get("mounted"):
            _set_colored_title(self.nas_status_item, "  ● 網路硬碟  VPN掛載", _GREEN)
        elif nas.get("mounted"):
            _set_colored_title(self.nas_status_item, "  ⚠ 網路硬碟  連線不穩", _YELLOW)
        elif nas.get("lan") or nas.get("vpn"):
            _set_colored_title(self.nas_status_item, "  ⚠ 網路硬碟  可達未掛載", _YELLOW)
        else:
            _set_colored_title(self.nas_status_item, "  ✗ 網路硬碟  未掛載", _RED)

        # NAS 各卷
        shares = nas.get("shares", {})
        for share_name, _ in NAS_SHARES:
            si = shares.get(share_name, {})
            item = self.nas_share_items[share_name]
            if si.get("mounted"):
                disk = si.get("disk")
                if disk:
                    used_gb, total_gb, pct = disk
                    bar = _mem_bar(pct, 6)
                    _set_colored_title(
                        item,
                        f"    {bar} {share_name}  {used_gb:.0f}/{total_gb:.0f}G ({pct:.0f}%)",
                        _GREEN if pct < 80 else (_YELLOW if pct < 90 else _RED),
                        small=True,
                    )
                else:
                    _set_colored_title(item, f"    ● {share_name}  已掛載", _GREEN, small=True)
            else:
                _set_colored_title(item, f"    ✗ {share_name}  未掛載", _RED, small=True)

        # ── DB ──
        db = c.get("db", {})
        syncing = db.get("syncing", False)
        failover = db.get("failover_active", False)
        if syncing:
            _set_colored_title(self.db_status_item, "  ⟳ 資料庫群  同步中", _CYAN)
            _set_colored_title(self.db_detail_item, "    本機→遠端資料回寫中...", _CYAN, small=True)
        elif db.get("remote") and db.get("local") and not failover:
            _set_colored_title(self.db_status_item, "  ● 資料庫群  雙活同步", _GREEN)
            _set_colored_title(self.db_detail_item, f"    主={db.get('active_host', '?')}  備=127.0.0.1", _GRAY, small=True)
        elif failover and db.get("local"):
            _set_colored_title(self.db_status_item, "  ⚠ 資料庫群  使用備份", _YELLOW)
            _set_colored_title(self.db_detail_item, "    遠端不可達・已切換至本機", _YELLOW, small=True)
        elif db.get("remote"):
            _set_colored_title(self.db_status_item, "  ● 資料庫群  遠端直連", _GREEN)
            _set_colored_title(self.db_detail_item, f"    主={db.get('active_host', '?')}", _GRAY, small=True)
        elif db.get("local"):
            _set_colored_title(self.db_status_item, "  ⚠ 資料庫群  僅本機", _YELLOW)
            _set_colored_title(self.db_detail_item, "    遠端離線・本機獨立運行", _YELLOW, small=True)
        else:
            _set_colored_title(self.db_status_item, "  ✗ 資料庫群  全部離線", _RED)
            _set_colored_title(self.db_detail_item, "    遠端+本機皆不可達", _RED, small=True)

        # ── 記憶體 ──
        _, avail_gb, pct = c.get("mem", (0, 0, 0))
        if pct > 0:
            bar = _mem_bar(pct)
            mem_color = _GREEN if pct < 70 else (_YELLOW if pct < 85 else _RED)
            _set_colored_title(self.mem_system_item, f"  {bar} 系統記憶  {pct:.0f}% {avail_gb:.1f}G餘", mem_color)

        magi_mb = c.get("magi_mb", 0)
        _set_colored_title(self.mem_total_item, f"  ● 程序佔用  {magi_mb}MB", _YELLOW if magi_mb > 2000 else _GREEN)

        zombies, z_detail = c.get("zombies", (0, ""))
        if zombies == 0:
            _set_colored_title(self.zombie_item, "  ● 殭屍程序  無", _GREEN)
        else:
            _set_colored_title(self.zombie_item, f"  ⚠ 殭屍程序  {zombies}個 {z_detail}", _RED)

        # ── 選單列圖示 ──
        try:
            _profile = open("/Users/ai/.omlx/active_profile").read().strip()
        except Exception:
            _profile = "day"

        total = core_up + omlx_up
        # 離峰模式 8082/8083 不啟動，預期只有 E4B + embed = 2 個 oMLX
        _night_mode = _profile == "night"
        if _night_mode:
            expected = len(SERVICES) + 2  # 只有 8080 + 8081
        else:
            expected = len(SERVICES) + len(OMLX_ENGINES)
        nodes_ok = nodes_up >= 1 if REMOTE_NODES else True
        if total == expected and zombies == 0 and nodes_ok:
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
    MAGIMenuBar().run()
