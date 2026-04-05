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
    # 設為 Accessory app：不出現在 Dock、不出現在 App Switcher
    NSApplication.sharedApplication().setActivationPolicy_(
        NSApplicationActivationPolicyAccessory
    )
except ImportError:
    _HAS_APPKIT = False

# ── 設定 ──────────────────────────────────────────────────────────
MAGI_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHECK_INTERVAL = 3  # 秒

SERVICES = [
    ("守護程序",       "daemon.py"),
    ("主伺服器",       "api/server.py"),
    ("Discord 機器人", "api/discord_bot.py"),
    ("工具 API",       "api/tools_api.py"),
]

OMLX_ENGINES = [
    ("文字+視覺 Gemma4", int(os.environ.get("MAGI_OMLX_PORT", "8080"))),
    ("向量嵌入 BERT",    8081),
]

# ── 顏色 ──
if _HAS_APPKIT:
    _GREEN  = NSColor.colorWithSRGBRed_green_blue_alpha_(0.0, 1.0, 0.5, 1.0)
    _YELLOW = NSColor.colorWithSRGBRed_green_blue_alpha_(1.0, 0.75, 0.0, 1.0)
    _RED    = NSColor.colorWithSRGBRed_green_blue_alpha_(1.0, 0.3, 0.3, 1.0)
    _GRAY   = NSColor.colorWithSRGBRed_green_blue_alpha_(0.6, 0.6, 0.6, 1.0)
    _FONT   = NSFont.menuFontOfSize_(13.0)
else:
    _GREEN = _YELLOW = _RED = _GRAY = _FONT = None


def _set_colored_title(menu_item, text: str, color=None):
    """Set menu item title with explicit color via NSAttributedString."""
    if _HAS_APPKIT and color and hasattr(menu_item, '_menuitem'):
        attrs = {
            NSForegroundColorAttributeName: color,
            NSFontAttributeName: _FONT,
        }
        astr = NSAttributedString.alloc().initWithString_attributes_(text, attrs)
        menu_item._menuitem.setAttributedTitle_(astr)
    else:
        menu_item.title = text


def _pgrep(pattern: str) -> str:
    try:
        r = subprocess.run(
            ["pgrep", "-f", pattern],
            capture_output=True, text=True, timeout=3,
        )
        pids = r.stdout.strip().split("\n")
        return pids[0] if pids[0] else ""
    except Exception:
        return ""


def _check_omlx(port: int) -> str:
    """Query oMLX /v1/models — returns model name if loaded, '' if not."""
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/v1/models",
            headers={"User-Agent": "MAGI-MenuBar/1.0"},
        )
        with urllib.request.urlopen(req, timeout=2) as resp:
            data = json.loads(resp.read())
            models = data.get("data", [])
            if models:
                # 主推理 port (8080)：優先顯示 TAIDE（主對話模型），Qwen 只負責 code
                if port in (8080, 11434):
                    main_kw = os.environ.get("MAGI_MAIN_MODEL", "TAIDE").lower().split("-")[0]
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
    """只計算 MAGI 相關的殭屍程序，回傳 (count, detail)。"""
    try:
        r = subprocess.run(
            ["ps", "-eo", "pid=,ppid=,stat=,command="],
            capture_output=True, text=True, timeout=3,
        )
        magi_zombies = 0
        parent_names = []
        for line in r.stdout.splitlines():
            parts = line.split(None, 3)
            if len(parts) < 3:
                continue
            if not parts[2].startswith("Z"):
                continue
            # 找父程序，判斷是否 MAGI 相關
            ppid = parts[1]
            try:
                r2 = subprocess.run(
                    ["ps", "-p", ppid, "-o", "command="],
                    capture_output=True, text=True, timeout=2,
                )
                pcmd = r2.stdout.strip()
            except Exception:
                pcmd = ""
            is_magi = (
                "MAGI" in pcmd or "magi" in pcmd
                or "Desktop/MAGI" in pcmd
                or any(kw in pcmd for kw in _MAGI_ZOMBIE_PARENTS)
            )
            if is_magi:
                magi_zombies += 1
                name = pcmd.split("/")[-1].split()[0][:20] if pcmd else "?"
                if name and name not in parent_names:
                    parent_names.append(name)

        detail = f"(父: {', '.join(parent_names[:3])})" if parent_names else ""
        return magi_zombies, detail
    except Exception:
        return 0, ""


# ── 記憶體佔用 ──────────────────────────────────────────────────
_MEM_MODULES = [
    ("Server",        "api/server.py"),
    ("Discord Bot",   "api/discord_bot.py"),
    ("Tools API",     "api/tools_api.py"),
    ("oMLX Text+Vis", "omlx serve.*--port 8080"),
    ("oMLX Embed",    "omlx serve.*--port 8081"),
    ("FAISS Rebuild", "MEMORY_ENABLE_FAISS"),
    ("File Review",   "file_review_auto_worker\\.py|file-review-orchestrator/action\\.py"),
    ("LAF Orch",      "laf_orchestrator\\.py|laf-portal-automation/action\\.py"),
    ("OC Gateway",    "openclaw-gateway|openclaw.*gateway"),
    ("OC Cron",       "openclaw_cron_runner\\.py"),
    ("Autopilot",     "magi-autopilot/action\\.py"),
    ("Selenium",      "chromedriver"),
    ("Chrome Headless","Google Chrome.*/MacOS/Google Chrome --.*headless|Google Chrome Helper"),
]


def _get_module_memory() -> list[tuple[str, int, int]]:
    """Return [(module_name, rss_mb, process_count), ...] sorted by RSS desc."""
    import re
    results = []
    try:
        r = subprocess.run(
            ["ps", "-eo", "pid,rss,command"],
            capture_output=True, text=True, timeout=5,
        )
        lines = r.stdout.strip().splitlines()[1:]  # skip header
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
                cmd = parts[2]
                if regex.search(cmd):
                    total_rss += rss_kb
                    count += 1
            if count > 0:
                results.append((mod_name, total_rss // 1024, count))
    except Exception:
        pass
    results.sort(key=lambda x: x[1], reverse=True)
    return results


def _get_system_memory() -> tuple[float, float, float]:
    """Return (total_gb, available_gb, percent_used)."""
    try:
        import psutil
        m = psutil.virtual_memory()
        return m.total / (1024**3), m.available / (1024**3), m.percent
    except ImportError:
        return 0, 0, 0


# ── 動作轉圈動畫 ──
_SPINNER = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]


class MAGIMenuBar(rumps.App):
    def __init__(self):
        super().__init__("Ⓜ", quit_button=None)
        self.icon = None
        self._action_lock = threading.Lock()

        # ── 選單項目 ──
        self.menu_header = rumps.MenuItem("MAGI 系統狀態", callback=None)
        self.menu_header.set_callback(None)
        self.menu_sep1 = rumps.separator

        # ── 服務狀態 ──
        self.svc_header = rumps.MenuItem("— 核心服務 —", callback=None)
        self.svc_header.set_callback(None)

        self.service_items = {}
        for name, _ in SERVICES:
            item = rumps.MenuItem(f"  ◻ {name}  檢查中...")
            item.set_callback(None)
            self.service_items[name] = item

        self.menu_sep2 = rumps.separator
        self.omlx_header = rumps.MenuItem("— 推理引擎 —", callback=None)
        self.omlx_header.set_callback(None)

        self.omlx_items = {}
        for name, _ in OMLX_ENGINES:
            item = rumps.MenuItem(f"  ◻ {name}  檢查中...")
            item.set_callback(None)
            self.omlx_items[name] = item

        self._tier_item = rumps.MenuItem("  🧠 分層: 檢查中...")
        self._tier_item.set_callback(None)

        self.menu_sep3 = rumps.separator

        # ── 排程（精簡一行）──
        self.cron_status_item = rumps.MenuItem("  排程  檢查中...")
        self.cron_status_item.set_callback(None)

        self.menu_sep3_cron = rumps.separator

        # ── 連線狀態（NAS + DB 合併）──
        self.conn_header = rumps.MenuItem("— 連線 —", callback=None)
        self.conn_header.set_callback(None)
        self.nas_status_item = rumps.MenuItem("  ◻ NAS  檢查中...")
        self.nas_status_item.set_callback(None)
        self.db_status_item = rumps.MenuItem("  ◻ DB  檢查中...")
        self.db_status_item.set_callback(None)

        self.menu_sep3_conn = rumps.separator

        # ── 系統資源 ──
        self.res_header = rumps.MenuItem("— 系統 —", callback=None)
        self.res_header.set_callback(None)
        self.mem_system_item = rumps.MenuItem("  記憶體  檢查中...")
        self.mem_system_item.set_callback(None)
        self.mem_total_item = rumps.MenuItem("  MAGI 佔用  --")
        self.mem_total_item.set_callback(None)
        self.zombie_item = rumps.MenuItem("  殭屍  0")
        self.zombie_item.set_callback(None)

        self.menu_sep4 = rumps.separator
        self.start_item = rumps.MenuItem("▶  啟動 MAGI", callback=self.on_start)
        self.stop_item = rumps.MenuItem("■  停止 MAGI", callback=self.on_stop)
        self.restart_item = rumps.MenuItem("↻  重新啟動", callback=self.on_restart)
        self.clean_zombie_item = rumps.MenuItem("🧹 清除殭屍程序", callback=self.on_clean_zombies)

        self.menu_sep5 = rumps.separator
        self.quit_item = rumps.MenuItem("結束狀態監控", callback=self.on_quit)

        # 建構選單
        self.menu = [
            self.menu_header,
            self.menu_sep1,
            self.svc_header,
            *self.service_items.values(),
            self.menu_sep2,
            self.omlx_header,
            *self.omlx_items.values(),
            self._tier_item,
            self.menu_sep3,
            self.cron_status_item,
            self.menu_sep3_cron,
            self.conn_header,
            self.nas_status_item,
            self.db_status_item,
            self.menu_sep3_conn,
            self.res_header,
            self.mem_system_item,
            self.mem_total_item,
            self.zombie_item,
            self.menu_sep4,
            self.start_item,
            self.stop_item,
            self.restart_item,
            self.clean_zombie_item,
            self.menu_sep5,
            self.quit_item,
        ]

    # ── 用 rumps.timer 在主執行緒定時更新（避免 AppKit 背景執行緒 crash）──
    @rumps.timer(CHECK_INTERVAL)
    def _periodic_check(self, _sender):
        try:
            self._update_status()
        except Exception:
            pass

    def _update_status(self):
        core_up = 0

        for name, pattern in SERVICES:
            pid = _pgrep(pattern)
            if pid:
                _set_colored_title(
                    self.service_items[name],
                    f"  ● {name}    PID {pid}",
                    _GREEN,
                )
                core_up += 1
            else:
                _set_colored_title(
                    self.service_items[name],
                    f"  ✗ {name}    停止",
                    _RED,
                )

        omlx_up = 0
        for name, port in OMLX_ENGINES:
            model = _check_omlx(port)
            if model:
                _set_colored_title(
                    self.omlx_items[name],
                    f"  ● {name} :{port}    [{model}]",
                    _GREEN,
                )
                omlx_up += 1
            else:
                _set_colored_title(
                    self.omlx_items[name],
                    f"  ✗ {name} :{port}    停止",
                    _RED,
                )

        # ── Tier Router 狀態 ──
        try:
            from skills.bridge.tier_router import get_status as _tier_status
            ts = _tier_status()
            mode_label = {"auto": "自動", "e4b": "E4B固定", "26b": "26B固定"}.get(ts["mode"], ts["mode"])
            loaded = "🟢" if ts.get("model_26b_loaded") else "🟡"
            tier_text = f"  🧠 分層: {mode_label} | 26B {loaded}"
            if hasattr(self, "_tier_item"):
                _set_colored_title(self._tier_item, tier_text, _GREEN if ts["mode"] == "auto" else _YELLOW)
        except Exception:
            pass

        # ── 夜間排程狀態 ──
        self._update_cron_status()

        # ── NAS 連線狀態 ──
        self._update_nas_status()

        # ── DB 連線狀態 ──
        self._update_db_status()

        # ── 系統資源（精簡）──
        total_gb, avail_gb, pct = _get_system_memory()
        if pct > 0:
            mem_color = _GREEN if pct < 70 else (_YELLOW if pct < 85 else _RED)
            _set_colored_title(self.mem_system_item,
                               f"  記憶體  {pct:.0f}% ({avail_gb:.1f}GB 可用)", mem_color)
        modules = _get_module_memory()
        magi_total_mb = sum(m[1] for m in modules)
        _set_colored_title(self.mem_total_item,
                           f"  MAGI 佔用  {magi_total_mb} MB",
                           _YELLOW if magi_total_mb > 2000 else _GREEN)

        zombies, z_detail = _count_zombies()
        if zombies == 0:
            _set_colored_title(self.zombie_item, "  殭屍  0", _GREEN)
        else:
            _set_colored_title(self.zombie_item, f"  ⚠ 殭屍  {zombies} 個  {z_detail}", _RED)

        # 更新選單列圖示
        total = core_up + omlx_up
        expected = len(SERVICES) + len(OMLX_ENGINES)
        if total == expected and zombies == 0:
            self.title = "Ⓜ"   # 全部正常
        elif core_up >= 2:
            self.title = "Ⓜ!"  # 部分異常
        else:
            self.title = "Ⓜ✕"  # 停止

    def _update_cron_status(self):
        """排程狀態（精簡一行）。"""
        try:
            cron_path = os.path.join(MAGI_ROOT, "cron_jobs.json")
            if not os.path.exists(cron_path):
                _set_colored_title(self.cron_status_item, "  ✗ 排程  設定檔遺失", _RED)
                return
            with open(cron_path, "r", encoding="utf-8") as f:
                jobs = json.load(f)
            enabled = [j for j in jobs if j.get("enabled", True)]
            bot_pid = _pgrep("discord_bot.py")
            if bot_pid and enabled:
                _set_colored_title(self.cron_status_item,
                                   f"  ● 排程  {len(enabled)} 個任務運行中", _GREEN)
            elif enabled:
                _set_colored_title(self.cron_status_item,
                                   f"  ✗ 排程  Bot 停止（{len(enabled)} 個待執行）", _RED)
            else:
                _set_colored_title(self.cron_status_item, "  ⚠ 排程  無任務", _YELLOW)
        except Exception:
            _set_colored_title(self.cron_status_item, "  ⚠ 排程  檢查失敗", _YELLOW)

    def _update_nas_status(self):
        """NAS 連線（合併為一行：模式 + 掛載）。"""
        import socket

        def _tcp_ok(host, port=445, timeout=2):
            try:
                s = socket.create_connection((host, port), timeout=timeout)
                s.close()
                return True
            except Exception:
                return False

        try:
            lan_ip = os.environ.get("MAGI_NAS_HOST", "192.168.1.3")
            ts_ip = os.environ.get("MAGI_NAS_TAILSCALE_HOST", "100.111.10.126")
            mounted = os.path.ismount("/Volumes/homes") and (
                os.path.ismount("/Volumes/lumi") or os.path.ismount("/Volumes/lumi-1")
            )
            mount_tag = " 已掛載" if mounted else " 未掛載"

            if _tcp_ok(lan_ip, timeout=1):
                _set_colored_title(self.nas_status_item,
                                   f"  ● NAS  LAN {lan_ip}{mount_tag}",
                                   _GREEN if mounted else _YELLOW)
            elif _tcp_ok(ts_ip, timeout=3):
                _set_colored_title(self.nas_status_item,
                                   f"  ● NAS  Tailscale {ts_ip}{mount_tag}",
                                   _YELLOW)
            else:
                _set_colored_title(self.nas_status_item,
                                   "  ✗ NAS  離線", _RED)
        except Exception:
            _set_colored_title(self.nas_status_item, "  ⚠ NAS  檢查失敗", _YELLOW)

    def _update_db_status(self):
        """DB 連線（合併為一行：模式 + 狀態）。"""
        import socket

        def _db_ok(host, port):
            try:
                s = socket.create_connection((host, port), timeout=3)
                s.close()
                return True
            except Exception:
                return False

        try:
            remote_host = os.environ.get("MAGI_REMOTE_DB_HOST", "100.121.61.74")
            remote_ok = _db_ok(remote_host, 3306)
            active = os.environ.get("OSC_DB_HOST", remote_host)
            is_local = active in ("127.0.0.1", "localhost")

            local_ok = _db_ok("127.0.0.1", 3306)
            if remote_ok and not is_local:
                _set_colored_title(self.db_status_item,
                                   f"  ● DB   遠端直連 {remote_host}", _GREEN)
            elif is_local and local_ok and not remote_ok:
                _set_colored_title(self.db_status_item,
                                   f"  ⚠ DB   本機墊檔中（遠端離線）", _YELLOW)
            elif is_local and local_ok and remote_ok:
                _set_colored_title(self.db_status_item,
                                   f"  ↻ DB   本機墊檔（遠端恢復，待同步）", _YELLOW)
            elif not remote_ok and local_ok:
                _set_colored_title(self.db_status_item,
                                   f"  ⚠ DB   本機運作中（遠端離線）", _YELLOW)
            else:
                _set_colored_title(self.db_status_item,
                                   f"  ✗ DB   本機和遠端皆異常", _RED)
        except Exception:
            _set_colored_title(self.db_status_item, "  ⚠ DB   檢查失敗", _YELLOW)

    # ── 動作按鈕（帶進度 + 完成回饋）──
    def _run_action(self, menu_item, label, command, original_callback):
        """在背景執行 command，menu_item 顯示進度 → 完成/失敗 → 恢復。"""
        if not self._action_lock.acquire(blocking=False):
            return  # 已有動作在跑，忽略

        original_title = menu_item.title

        def _worker():
            try:
                _set_colored_title(menu_item, f"  ⏳ {label} 執行中...", _YELLOW)
                proc = subprocess.run(
                    command,
                    capture_output=True, text=True, timeout=120,
                )
                if proc.returncode == 0:
                    _set_colored_title(menu_item, f"  ✅ {label} 完成", _GREEN)
                else:
                    _set_colored_title(menu_item, f"  ⚠ {label} 異常 (exit {proc.returncode})", _RED)
            except subprocess.TimeoutExpired:
                _set_colored_title(menu_item, f"  ⚠ {label} 逾時", _RED)
            except Exception:
                _set_colored_title(menu_item, f"  ⚠ {label} 錯誤", _RED)
            finally:
                import time
                time.sleep(3)
                _set_colored_title(menu_item, original_title, None)
                menu_item.set_callback(original_callback)
                self._action_lock.release()

        t = threading.Thread(target=_worker, daemon=True)
        t.start()

    def on_start(self, _):
        self._run_action(
            self.start_item, "啟動 MAGI",
            ["/opt/homebrew/bin/magi", "start"],
            self.on_start,
        )

    def on_stop(self, _):
        self._run_action(
            self.stop_item, "停止 MAGI",
            ["/opt/homebrew/bin/magi", "stop"],
            self.on_stop,
        )

    def on_restart(self, _):
        self._run_action(
            self.restart_item, "重新啟動",
            ["/opt/homebrew/bin/magi", "restart"],
            self.on_restart,
        )

    def on_clean_zombies(self, _):
        self._run_action(
            self.clean_zombie_item, "清除殭屍",
            ["/opt/homebrew/bin/magi", "zombie"],
            self.on_clean_zombies,
        )

    def on_quit(self, _):
        rumps.quit_application()


if __name__ == "__main__":
    # 取代舊實例：殺掉已在跑的 magi_menubar，讓新版生效
    try:
        _r = subprocess.run(
            ["pgrep", "-f", "magi_menubar.py"],
            capture_output=True, text=True, timeout=5,
        )
        for _pid in _r.stdout.strip().split("\n"):
            if _pid and _pid != str(os.getpid()):
                os.kill(int(_pid), 15)  # SIGTERM
    except Exception:
        pass

    MAGIMenuBar().run()
