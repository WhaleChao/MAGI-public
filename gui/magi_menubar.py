#!/usr/bin/env python3
"""
MAGI 選單列狀態監控
在 macOS 選單列顯示 MAGI 系統健康狀態。
"""

import os
import subprocess
import json
import threading
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
CHECK_INTERVAL = 5  # 拉長間隔減少主執行緒阻塞

SERVICES = [
    ("守護程序", "daemon.py"),
    ("主伺服器", "api/server.py"),
    ("通訊機器", "api/discord_bot.py"),
    ("工具接口", "api/tools_api.py"),
]

OMLX_ENGINES = [
    ("文字視覺", int(os.environ.get("MAGI_OMLX_PORT", "8080"))),
    ("向量嵌入", 8081),
]

# ── 顏色 ──
if _HAS_APPKIT:
    _GREEN  = NSColor.colorWithSRGBRed_green_blue_alpha_(0.34, 0.85, 0.47, 1.0)
    _YELLOW = NSColor.colorWithSRGBRed_green_blue_alpha_(1.0, 0.75, 0.0, 1.0)
    _RED    = NSColor.colorWithSRGBRed_green_blue_alpha_(1.0, 0.33, 0.33, 1.0)
    _GRAY   = NSColor.colorWithSRGBRed_green_blue_alpha_(0.55, 0.55, 0.55, 1.0)
    _CYAN   = NSColor.colorWithSRGBRed_green_blue_alpha_(0.3, 0.85, 0.9, 1.0)
    _FONT   = NSFont.monospacedSystemFontOfSize_weight_(12.0, 0.0)
    _FONT_B = NSFont.monospacedSystemFontOfSize_weight_(12.0, 0.5)
else:
    _GREEN = _YELLOW = _RED = _GRAY = _CYAN = _FONT = _FONT_B = None


def _set_colored_title(menu_item, text: str, color=None, bold=False):
    if _HAS_APPKIT and color and hasattr(menu_item, '_menuitem'):
        attrs = {
            NSForegroundColorAttributeName: color,
            NSFontAttributeName: _FONT_B if bold else _FONT,
        }
        astr = NSAttributedString.alloc().initWithString_attributes_(text, attrs)
        menu_item._menuitem.setAttributedTitle_(astr)
    else:
        menu_item.title = text


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
            headers={"User-Agent": "MAGI-MenuBar/2.0"},
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


_MAGI_ZOMBIE_PARENTS = {
    "daemon.py", "server.py", "discord_bot.py", "tools_api.py",
    "action.py", "heartbeat.py", "Python", "python3", "python3.14",
    "omlx", "chromedriver", "caddy", "socat", "bash",
}


def _count_zombies() -> tuple[int, str]:
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
    ("oMLX Text+Vis", "omlx serve.*--port 8080"),
    ("oMLX Embed",    "omlx serve.*--port 8081"),
    ("FAISS Rebuild", "MEMORY_ENABLE_FAISS"),
    ("File Review",   "file_review_auto_worker\\.py|file-review-orchestrator/action\\.py"),
    ("LAF Orch",      "laf_orchestrator\\.py|laf-portal-automation/action\\.py"),
    ("Autopilot",     "magi-autopilot/action\\.py"),
    ("Selenium",      "chromedriver"),
]


def _get_module_memory() -> list[tuple[str, int, int]]:
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


def _get_system_memory() -> tuple[float, float, float]:
    try:
        import psutil
        m = psutil.virtual_memory()
        return m.total / (1024**3), m.available / (1024**3), m.percent
    except ImportError:
        return 0, 0, 0


class MAGIMenuBar(rumps.App):
    def __init__(self):
        super().__init__(" MAGI ", quit_button=None)
        self.icon = None
        self._action_lock = threading.Lock()
        self._status_cache = {}  # 背景執行緒寫，主執行緒讀

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
        self._tier_item = rumps.MenuItem("  ◻ 推理分層")
        self._tier_item.set_callback(None)

        # ── 排程 ──
        self.cron_status_item = rumps.MenuItem("  ◻ 定時排程")
        self.cron_status_item.set_callback(None)

        # ── 連線 ──
        self.conn_header = rumps.MenuItem("── 外部連線 ──", callback=None)
        self.conn_header.set_callback(None)
        self.nas_status_item = rumps.MenuItem("  ◻ 網路硬碟")
        self.nas_status_item.set_callback(None)
        self.db_status_item = rumps.MenuItem("  ◻ 資料庫群")
        self.db_status_item.set_callback(None)

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
            self.svc_header,
            *self.service_items.values(),
            rumps.separator,
            self.omlx_header,
            *self.omlx_items.values(),
            self._tier_item,
            rumps.separator,
            self.cron_status_item,
            rumps.separator,
            self.conn_header,
            self.nas_status_item,
            self.db_status_item,
            rumps.separator,
            self.res_header,
            self.mem_system_item,
            self.mem_total_item,
            self.zombie_item,
            rumps.separator,
            self.start_item,
            self.stop_item,
            self.restart_item,
            self.clean_zombie_item,
            rumps.separator,
            self.quit_item,
        ]

    @rumps.timer(CHECK_INTERVAL)
    def _periodic_check(self, _sender):
        # 主執行緒只讀 cache 做 UI 更新（不 block）
        if self._status_cache:
            try:
                self._apply_status(self._status_cache)
            except Exception:
                pass
        # 背景執行緒收集資料（I/O 不阻塞選單點擊）
        threading.Thread(target=self._collect_status, daemon=True).start()

    def _collect_status(self):
        """背景執行緒：收集所有 I/O 資料，存入 cache。"""
        import socket
        cache = {}
        # 服務
        svcs = {}
        for name, pattern in SERVICES:
            svcs[name] = bool(_pgrep(pattern))
        cache["services"] = svcs
        # 推理
        engines = {}
        for name, port in OMLX_ENGINES:
            engines[name] = _check_omlx(port)
        cache["engines"] = engines
        # Tier
        try:
            from skills.bridge.tier_router import get_status as _ts
            cache["tier"] = _ts()
        except Exception:
            cache["tier"] = None
        # 排程
        try:
            cron_path = os.path.join(MAGI_ROOT, "cron_jobs.json")
            with open(cron_path, "r", encoding="utf-8") as f:
                jobs = json.load(f)
            cache["cron_enabled"] = len([j for j in jobs if j.get("enabled", True)])
            cache["cron_bot"] = bool(_pgrep("discord_bot.py"))
        except Exception:
            cache["cron_enabled"] = -1
            cache["cron_bot"] = False
        # NAS
        def _tcp(host, port=445, timeout=2):
            try:
                s = socket.create_connection((host, port), timeout=timeout)
                s.close()
                return True
            except Exception:
                return False
        lan_ip = os.environ.get("MAGI_NAS_HOST", "192.168.1.3")
        ts_ip = os.environ.get("MAGI_NAS_TAILSCALE_HOST", "100.111.10.126")
        mounted = os.path.ismount("/Volumes/homes") and (
            os.path.ismount("/Volumes/lumi") or os.path.ismount("/Volumes/lumi-1")
        )
        cache["nas"] = {"lan": _tcp(lan_ip, timeout=1), "vpn": _tcp(ts_ip, timeout=2), "mounted": mounted}
        # DB
        remote_host = os.environ.get("MAGI_REMOTE_DB_HOST", "100.121.61.74")
        cache["db"] = {"remote": _tcp(remote_host, 3306, 2), "local": _tcp("127.0.0.1", 3306, 2)}
        # Memory
        cache["mem"] = _get_system_memory()
        cache["magi_mb"] = sum(m[1] for m in _get_module_memory())
        cache["zombies"] = _count_zombies()
        self._status_cache = cache

    def _apply_status(self, c):
        """主執行緒：用 cache 更新 UI（無 I/O）。"""
        core_up = 0
        svcs = c.get("services", {})
        for name, _ in SERVICES:
            if svcs.get(name):
                _set_colored_title(self.service_items[name], f"  ● {name}  運行中", _GREEN)
                core_up += 1
            else:
                _set_colored_title(self.service_items[name], f"  ✗ {name}  已停止", _RED)

        omlx_up = 0
        engines = c.get("engines", {})
        for name, _ in OMLX_ENGINES:
            if engines.get(name):
                _set_colored_title(self.omlx_items[name], f"  ● {name}  就緒", _GREEN)
                omlx_up += 1
            else:
                _set_colored_title(self.omlx_items[name], f"  ✗ {name}  離線", _RED)

        # ── Tier ──
        ts = c.get("tier")
        if ts:
            mode_label = {"auto": "自動分層", "e4b": "輕型固定", "26b": "重型固定"}.get(ts.get("mode", ""), "")
            _set_colored_title(self._tier_item, f"  ● 推理分層  {mode_label}", _CYAN)
        else:
            _set_colored_title(self._tier_item, f"  ◻ 推理分層  --", _GRAY)

        # ── 排程 ──
        cron_n = c.get("cron_enabled", -1)
        if cron_n >= 0 and c.get("cron_bot"):
            _set_colored_title(self.cron_status_item, f"  ● 定時排程  {cron_n}個運行", _GREEN)
        elif cron_n > 0:
            _set_colored_title(self.cron_status_item, f"  ✗ 定時排程  Bot停止", _RED)
        else:
            _set_colored_title(self.cron_status_item, "  ⚠ 定時排程  錯誤", _YELLOW)

        # ── NAS ──
        nas = c.get("nas", {})
        if nas.get("lan") and nas.get("mounted"):
            _set_colored_title(self.nas_status_item, f"  ● 網路硬碟  區網掛載", _GREEN)
        elif nas.get("vpn") and nas.get("mounted"):
            _set_colored_title(self.nas_status_item, f"  ● 網路硬碟  VPN掛載", _GREEN)
        elif nas.get("mounted"):
            _set_colored_title(self.nas_status_item, f"  ⚠ 網路硬碟  連線不穩", _YELLOW)
        else:
            _set_colored_title(self.nas_status_item, "  ✗ 網路硬碟  未掛載", _RED)

        # ── DB ──
        db = c.get("db", {})
        if db.get("remote") and db.get("local"):
            _set_colored_title(self.db_status_item, f"  ● 資料庫群  雙活同步", _GREEN)
        elif db.get("local"):
            _set_colored_title(self.db_status_item, f"  ⚠ 資料庫群  使用備份", _YELLOW)
        elif db.get("remote"):
            _set_colored_title(self.db_status_item, f"  ● 資料庫群  遠端直連", _GREEN)
        else:
            _set_colored_title(self.db_status_item, f"  ✗ 資料庫群  全部離線", _RED)

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
            _set_colored_title(self.zombie_item, f"  ⚠ 殭屍程序  {zombies}個", _RED)

        # ── 選單列圖示 ──
        total = core_up + omlx_up
        expected = len(SERVICES) + len(OMLX_ENGINES)
        if total == expected and zombies == 0:
            self.title = " MAGI "
        elif core_up >= 2:
            self.title = " MAGI ⚠"
        else:
            self.title = " MAGI ✕"

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
                import time; time.sleep(3)
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


def _mem_bar(pct: float, width: int = 8) -> str:
    filled = int(pct / 100 * width)
    return "▓" * filled + "░" * (width - filled)


if __name__ == "__main__":
    MAGIMenuBar().run()
