#!/usr/bin/env python3
"""
Iron Dome: Protocol Override Mechanism
=======================================
Intercepts attempts to modify core SKILL files.
Saves the proposed changes as a pending override and sends a LINE notification.
Requires human approval to commit the changes to disk.
"""

import os
import sys
import json
from datetime import datetime
import logging

from pathlib import Path

_MAGI_ROOT = Path(__file__).resolve().parents[2]
if str(_MAGI_ROOT) not in sys.path:
    sys.path.insert(0, str(_MAGI_ROOT))

from api.runtime_paths import ensure_orch_on_sys_path

ensure_orch_on_sys_path()

from line_notifier import LAFNotifier

logger = logging.getLogger("protocol-override")
PENDING_FILE = f"{_MAGI_ROOT}/.agent/iron_dome_pending_override.json"
SKILLS_DIR = f"{_MAGI_ROOT}/skills"

def _load_pending() -> dict:
    if os.path.exists(PENDING_FILE):
        try:
            with open(PENDING_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 37, exc_info=True)
    return {}

def _save_pending(data: dict):
    os.makedirs(os.path.dirname(PENDING_FILE), exist_ok=True)
    with open(PENDING_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def clear_override():
    if os.path.exists(PENDING_FILE):
        os.remove(PENDING_FILE)

def request_override(skill_name: str, files: dict, reason: str = "") -> dict:
    """
    Called when a system agent attempts to overwrite a skill's files.
    files: {"action.py": "new content...", "SKILL.md": "new content..."}
    """
    skill_dir = os.path.join(SKILLS_DIR, skill_name)
    
    # If the skill does NOT exist yet, it's a creation, not an override.
    # Protocol Overrides only apply to modifying EXISTING core capabilities.
    if not os.path.exists(skill_dir):
        return {"blocked": False, "message": "New skill creation allowed."}
        
    logger.warning(f"Iron Dome Intercept: Attempting to modify existing skill: {skill_name}")
    
    payload = {
        "timestamp": datetime.now().isoformat(),
        "skill_name": skill_name,
        "files": files,
        "reason": reason
    }
    _save_pending(payload)
    
    # Send LINE Notification
    notifier = LAFNotifier()
    msg = (
        f"🚨 [Iron Dome 警報] 檢測到核心腳本修改企圖\n\n"
        f"技能名稱: {skill_name}\n"
        f"修改原因: {reason}\n\n"
        f"為保護系統（Protocol Override 卡控），修改已遭攔截並暫存。\n\n"
        f"請審核後回覆：「同意修改 {skill_name}」來正式套用，或回覆「拒絕」拋棄變更。"
    )
    notifier.notify_admin(msg, topic_key="alert", source="iron_dome")
    
    return {
        "blocked": True, 
        "message": f"IRON DOME PROTOCOL OVERRIDE: Modification to '{skill_name}' requires human consent. LINE notification sent."
    }

def approve_override() -> dict:
    """
    Called by the chat agent/bot when the user replies "同意修改".
    Commits the pending files to disk.
    """
    pending = _load_pending()
    if not pending or not pending.get("skill_name"):
        return {"success": False, "message": "目前沒有等待審核的修改請求。"}
        
    skill_name = pending["skill_name"]
    files = pending.get("files", {})
    
    skill_dir = os.path.join(SKILLS_DIR, skill_name)
    os.makedirs(skill_dir, exist_ok=True)
    
    committed = []
    for filename, content in files.items():
        # Prevent directory traversal attacks
        safe_filename = os.path.basename(filename) 
        file_path = os.path.join(skill_dir, safe_filename)
        try:
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(content)
            committed.append(safe_filename)
        except Exception as e:
            logger.error(f"Failed to write {safe_filename}: {e}")
            
    clear_override()
    
    notifier = LAFNotifier()
    notifier.notify_admin(
        f"✅ [Iron Dome] 已授權覆寫技能 `{skill_name}`。",
        topic_key="alert",
        source="iron_dome",
    )
    
    return {
        "success": True, 
        "message": f"成功覆寫技能 {skill_name} 的檔案: {', '.join(committed)}"
    }

if __name__ == "__main__":
    # Simple CLI for testing
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--approve", action="store_true")
    args = parser.parse_args()
    
    if args.approve:
        res = approve_override()
        print(res)
