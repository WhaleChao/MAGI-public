# -*- coding: utf-8 -*-
"""
System Monitor Skill (系統監控)
Based on ClawHub community skill: system-info / system-monitor
Iron Dome Audit: ✅ SAFE — Read-only, no external data transmission

Provides: CPU, RAM, Disk, Network status
"""

import os
import logging
import platform
import subprocess

logger = logging.getLogger("SystemMonitor")

try:
    import psutil
    PSUTIL_AVAILABLE = True
except ImportError:
    PSUTIL_AVAILABLE = False
    logger.warning("⚠️ psutil not installed — system monitor limited")


def get_system_status():
    """
    Returns a formatted system status report.
    """
    if not PSUTIL_AVAILABLE:
        return _basic_status()
    
    try:
        # CPU
        cpu_percent = psutil.cpu_percent(interval=1)
        cpu_count = psutil.cpu_count()
        
        # Memory
        mem = psutil.virtual_memory()
        mem_total_gb = round(mem.total / (1024**3), 1)
        mem_used_gb = round(mem.used / (1024**3), 1)
        mem_percent = mem.percent
        
        # Disk
        disk = psutil.disk_usage('/')
        disk_total_gb = round(disk.total / (1024**3), 1)
        disk_used_gb = round(disk.used / (1024**3), 1)
        disk_percent = disk.percent
        
        # Uptime
        import time
        boot_time = psutil.boot_time()
        uptime_seconds = time.time() - boot_time
        uptime_hours = int(uptime_seconds // 3600)
        uptime_mins = int((uptime_seconds % 3600) // 60)
        
        # Top processes by memory
        top_procs = []
        for proc in sorted(psutil.process_iter(['name', 'memory_percent']), 
                          key=lambda p: p.info.get('memory_percent', 0) or 0, 
                          reverse=True)[:5]:
            name = proc.info.get('name', 'Unknown')
            mem_pct = proc.info.get('memory_percent', 0) or 0
            top_procs.append(f"  - `{name}`: {mem_pct:.1f}%")
        
        # Network
        net = psutil.net_io_counters()
        net_sent_mb = round(net.bytes_sent / (1024**2), 1)
        net_recv_mb = round(net.bytes_recv / (1024**2), 1)
        
        report = f"""📊 **CASPER 系統狀態報告**

🖥️ **處理器 (CPU)**
- 使用率: {cpu_percent}%
- 核心數: {cpu_count}
- 架構: {platform.machine()}

🧠 **記憶體 (RAM)**
- 使用: {mem_used_gb}GB / {mem_total_gb}GB ({mem_percent}%)
- {'🟢' if mem_percent < 70 else '🟡' if mem_percent < 90 else '🔴'} {'正常' if mem_percent < 70 else '偏高' if mem_percent < 90 else '⚠️ 危險'}

💾 **磁碟 (Disk)**
- 使用: {disk_used_gb}GB / {disk_total_gb}GB ({disk_percent}%)
- {'🟢' if disk_percent < 80 else '🟡' if disk_percent < 95 else '🔴'} {'正常' if disk_percent < 80 else '偏高' if disk_percent < 95 else '⚠️ 空間不足'}

🌐 **網路 (Network)**
- 已發送: {net_sent_mb} MB
- 已接收: {net_recv_mb} MB

⏱️ **運行時間**: {uptime_hours}h {uptime_mins}m

📈 **記憶體佔用 Top 5**
{chr(10).join(top_procs)}

🤖 **系統**: {platform.system()} {platform.release()}
"""
        return report.strip()
        
    except Exception as e:
        logger.error(f"System monitor error: {e}")
        return f"❌ 系統監控失敗: {e}"


def _basic_status():
    """Fallback without psutil."""
    try:
        result = subprocess.run(
            ["top", "-l", "1", "-n", "0", "-s", "0"],
            capture_output=True, text=True, timeout=5
        )
        lines = result.stdout.strip().split("\n")[:5]
        return "📊 **基本系統狀態**\n```\n" + "\n".join(lines) + "\n```"
    except Exception:
        return "❌ psutil 未安裝，無法取得系統狀態"


def check_service_health():
    """
    Check MAGI-related services.
    """
    services = {
        "Daemon (Supervisor)": "daemon.py",
        "Discord Bot": "api/discord_bot.py",
        "API Server": "api/server.py",
        "Tools API": "api/tools_api.py",
        "File Review Worker": "skills/ops/file_review_auto_worker.py",
        "Ollama (LLM)": "ollama",
        "Dashboard": "http.server",
    }

    report = "🏥 **服務健康檢查**\n\n"

    for name, keyword in services.items():
        try:
            # simple pgrep check
            cmd = ["pgrep", "-f", keyword]
            result = subprocess.run(
                cmd,
                capture_output=True, text=True, timeout=3
            )
            if result.returncode == 0:
                pids = result.stdout.strip().split("\n")
                # Filter out empty strings
                pids = [p for p in pids if p]
                pid_str = ", ".join(pids[:2])
                if len(pids) > 1 and name != "Ollama (LLM)": # Ollama can have multiple threads/processes
                     if name == "Discord Bot":
                         report += f"⚠️ {name}: 重複運行 (PID: {pid_str}) - 可能導致多重回應\n"
                     else:
                         report += f"✅ {name}: 運行中 (PID: {pid_str})\n"
                else:
                    report += f"✅ {name}: 運行中 (PID: {pid_str})\n"
            else:
                report += f"❌ {name}: 未運行\n"
        except Exception as e:
            report += f"⚠️ {name}: 無法檢測 ({e})\n"

    # --- OCR Engine (macOS Vision — GLM-OCR retired) ---
    try:
        from skills.apple.apple_intelligence import VISION_AVAILABLE
        if VISION_AVAILABLE:
            report += "✅ OCR 引擎: macOS Vision Framework（零 GPU、零額外記憶體）\n"
        else:
            report += "⚠️ OCR 引擎: macOS Vision 不可用（PyObjC 未安裝）\n"
    except Exception:
        report += "⚠️ OCR 引擎: macOS Vision 無法載入\n"

    return report.strip()


if __name__ == "__main__":
    print(get_system_status())
    print()
    print(check_service_health())
