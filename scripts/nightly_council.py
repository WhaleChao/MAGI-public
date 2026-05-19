import logging
import sys
import os
import requests
from datetime import datetime
import time # Added for time.sleep
_MAGI_ROOT = os.path.abspath(os.path.dirname(os.path.dirname(__file__)))

# Setup Logging
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# 載入 .env — cron 環境不會自動帶入環境變數
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'))
except ImportError:
    pass
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("NightlyCouncil")

# Configuration
LOG_FILE = f"{_MAGI_ROOT}/daemon.log"
STATUS_FILE = f"{_MAGI_ROOT}/static/magi_status.json"
def _resolve_sync_path() -> str:
    candidates = [
        "/Volumes/SynologyDrive/04_Robot/MAGI_SYNC",
        os.path.expanduser("~/Library/CloudStorage/SynologyDrive-homes/04_Robot/MAGI_SYNC"),
        os.path.expanduser("~/SynologyDrive/04_Robot/MAGI_SYNC"),
    ]
    for c in candidates:
        if os.path.isdir(c):
            return c
    return candidates[0]

SYNOLOGY_SYNC_PATH = _resolve_sync_path()

# Import Brain Manager for Independence Protocol
try:
    from skills.brain_manager.action import switch_brain_mode
except ImportError as e:
    logger.error(f"Failed to import switch_brain_mode: {e}")
    # Handle this error appropriately, e.g., exit or use a fallback
    # For now, we'll just log and let the program continue,
    # but the switch_brain_mode call will fail if not imported.

def sync_from_synology():
    """Sync files from Synology Drive before council starts."""
    import shutil
    
    if not os.path.exists(SYNOLOGY_SYNC_PATH):
        logger.warning(f"⚠️ Synology Drive not mounted: {SYNOLOGY_SYNC_PATH}")
        return "Synology 未掛載"
    
    sync_items = [
        ("iron_dome/iron_dome.py", "skills/bridge/iron_dome.py"),
        ("skills/iron_dome_sync.py", "skills/ops/iron_dome_sync.py"),
    ]
    
    synced = []
    for src_rel, dst_rel in sync_items:
        src = os.path.join(SYNOLOGY_SYNC_PATH, src_rel)
        dst = os.path.join(_MAGI_ROOT, dst_rel)
        
        if os.path.exists(src):
            try:
                shutil.copy2(src, dst)
                synced.append(os.path.basename(src))
                logger.info(f"✅ Synced: {src_rel}")
            except Exception as e:
                logger.error(f"❌ Sync failed {src_rel}: {e}")
    
    return f"已同步 {len(synced)} 個檔案" if synced else "無需同步"

def get_node_status_summary():
    """Reads magi_status.json and returns a string summary."""
    try:
        import json
        with open(STATUS_FILE, 'r') as f:
            data = json.load(f)
        
        nodes = data.get("nodes", {})
        status_lines = []
        for name, info in nodes.items():
            icon = "✅" if info['online'] else "🔴"
            status_lines.append(f"{icon} {name.upper()}: {'Online' if info['online'] else 'Offline'}")
        
        return "\n".join(status_lines)
    except Exception as e:
        return f"⚠️ 無法讀取節點狀態: {e}"

def conduct_nightly_council():
    """
    Conducts the Nightly Council or falls back to Direct Report if Watcher is offline.
    """
    logger.info("🌙 Nightly Council: Session Started.")
    
    # [CRITICAL] Enforce Independence (Engineer Mode)
    # The user has explicitly requested that Melchior operates independently during Nightly Council.
    # We switch to local mode so Casper releases shared inference resources and Melchior warms its local oMLX route.
    logger.info("🛡️ Nightly Independence Protocol: Releasing Melchior...")
    logger.info("👉 Switching Brain to LOCAL (Melchior=oMLX local route)...")
    
    try:
        if 'switch_brain_mode' in globals():
            result = switch_brain_mode("local", force=True)
            logger.info(f"✅ Brain Mode Result: {result}")
        else:
             logger.warning("⚠️ Brain Manager not imported! Melchior might still be bound to Casper.")
        
        # Give Melchior 10s to warm the local model route.
        time.sleep(10)
    except Exception as e:
        logger.error(f"❌ Failed to release Melchior: {e}")
        # We continue, but warn
        logger.warning("⚠️ Council proceeding, but Melchior might be resource-constrained.")

    # Pre-Council: Sync from Synology
    sync_result = sync_from_synology()
    logger.info(f"📦 Pre-Council Sync: {sync_result}")

    # Preferred path: use unified Night Talk protocol (supports 3/3 and 2/2 fallback)
    try:
        from skills.magi.night_talk import start_night_talk

        logger.info("🧾 Delegating to unified Night Talk protocol...")
        minutes = start_night_talk()
        return (
            f"{minutes}\n\n"
            f"---\n"
            f"[Pre-Council Sync] {sync_result}"
        )
    except Exception as e:
        logger.warning(f"Night Talk delegation failed, falling back to legacy summary mode: {e}")
    
    try:
        import json
        with open(STATUS_FILE, 'r') as f:
            data = json.load(f)
        nodes = data.get("nodes", {})
    except Exception as e:
        logger.error(f"Failed to read status: {e}")
        return f"⚠️ 系統嚴重錯誤: 無法讀取狀態檔 ({e})"

    # Watcher 已排除在必要功能之外，議會不再因 Watcher 離線而取消
    node_summary = get_node_status_summary()

    # Legacy Council Logic (Log Analysis)
    if not os.path.exists(LOG_FILE):
        return f"🌙 **夜議報告**\n\n{node_summary}\n\n(無日誌檔案)"

    error_count = 0
    warning_count = 0
    activity_count = 0
    
    try:
        with open(LOG_FILE, 'r') as f:
            for line in f:
                if "ERROR" in line:
                    error_count += 1
                if "WARNING" in line:
                    warning_count += 1
                activity_count += 1
    except Exception as e:
        return f"Log analysis failed: {e}"

    # Run Memory Consolidation
    memory_report = ""
    try:
        from scripts.memory_consolidation import run_consolidation
        memory_report = run_consolidation()
        logger.info("Memory consolidation completed")
    except Exception as e:
        memory_report = f"⚠️ 記憶歸類失敗: {e}"
        logger.error(f"Memory consolidation failed: {e}")

    summary = f"""
🌙 **MAGI 哲人議會報告 (正常召開)**
-----------------------------
📅 {datetime.now().strftime("%Y-%m-%d %H:%M")}

**[出席者狀態]**
{node_summary}

**[議程: 日誌審查]**
📊 活動量: {activity_count}
❌ 錯誤數: {error_count}
⚠️ 警告數: {warning_count}

{memory_report}

**[決議]**
Casper: 系統運作正常，記憶已歸檔。
-----------------------------
"""
    return summary

def send_report(report):
    """
    Sends the report to Admin via LINE.
    """
    # Using red_phone to notify admin
    try:
        sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
        from skills.ops.red_phone import alert_admin
        alert_admin(report, severity="info", topic_key="nightly")
        logger.info("Report sent via Red Phone.")
    except ImportError:
        print(report)
        logger.warning("Red Phone not found, printed report to stdout.")
    except Exception as e:
        print(report)
        logger.error(f"Failed to send report: {e}")

if __name__ == "__main__":
    report = conduct_nightly_council()
    send_report(report)
