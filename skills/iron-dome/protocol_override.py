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
import shutil
import tempfile
from datetime import datetime
import logging

from pathlib import Path

_MAGI_ROOT = Path(__file__).resolve().parents[2]
if str(_MAGI_ROOT) not in sys.path:
    sys.path.insert(0, str(_MAGI_ROOT))

from api.runtime_paths import ensure_orch_on_sys_path
from skills.overlay import effective_skill_dir, ensure_overlay_skill, skill_overlay_dir

ensure_orch_on_sys_path()

from line_notifier import LAFNotifier

logger = logging.getLogger("protocol-override")
_AGENT_DIR = Path(os.environ.get("MAGI_AGENT_DIR", str(_MAGI_ROOT / ".agent"))).expanduser()
PENDING_FILE = str(_AGENT_DIR / "iron_dome_pending_override.json")
SKILLS_DIR = str(skill_overlay_dir())

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


def _write_override_file(file_path: Path, content: str) -> None:
    with file_path.open("w", encoding="utf-8") as handle:
        handle.write(content)

def request_override(skill_name: str, files: dict, reason: str = "") -> dict:
    """
    Called when a system agent attempts to overwrite a skill's files.
    files: {"action.py": "new content...", "SKILL.md": "new content..."}
    """
    try:
        skill_dir = str(effective_skill_dir(skill_name))
    except ValueError as exc:
        return {"blocked": True, "message": str(exc)}
    
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

    if not isinstance(files, dict) or not files:
        return {"success": False, "message": "待審核內容沒有可寫入檔案。"}
    validated: list[tuple[str, str]] = []
    for filename, content in files.items():
        safe_filename = os.path.basename(str(filename or ""))
        if (
            safe_filename != filename
            or safe_filename in {"", ".", "..", ".overlay-seed.json"}
            or not isinstance(content, str)
        ):
            return {"success": False, "message": f"不安全的覆寫檔案：{filename}"}
        validated.append((safe_filename, content))

    try:
        skill_path = ensure_overlay_skill(skill_name)
    except ValueError as exc:
        return {"success": False, "message": str(exc)}

    overlay_root = skill_path.parent
    try:
        stage = Path(tempfile.mkdtemp(prefix=f".{skill_name}.approval.", dir=str(overlay_root)))
        backup = Path(tempfile.mkdtemp(prefix=f".{skill_name}.approval-backup.", dir=str(overlay_root)))
        backup.rmdir()
    except Exception as exc:
        if "stage" in locals():
            shutil.rmtree(stage, ignore_errors=True)
        return {"success": False, "message": f"無法建立安全覆寫區，提案仍保留：{exc}"}
    committed = [filename for filename, _content in validated]
    swapped = False
    try:
        shutil.copytree(skill_path, stage, dirs_exist_ok=True, symlinks=False)
        for safe_filename, content in validated:
            _write_override_file(stage / safe_filename, content)
        if backup.exists() or backup.is_symlink():
            raise ValueError("stale_override_backup")
        skill_path.replace(backup)
        try:
            stage.replace(skill_path)
            swapped = True
        except Exception:
            backup.replace(skill_path)
            raise
    except Exception as exc:
        logger.error("Iron Dome override transaction failed for %s: %s", skill_name, exc)
        if swapped and backup.exists():
            failed_tree = overlay_root / f".{skill_name}.failed-approval"
            if failed_tree.exists():
                shutil.rmtree(failed_tree, ignore_errors=True)
            if skill_path.exists():
                skill_path.replace(failed_tree)
            backup.replace(skill_path)
            shutil.rmtree(failed_tree, ignore_errors=True)
        elif backup.exists() and not skill_path.exists():
            backup.replace(skill_path)
        shutil.rmtree(stage, ignore_errors=True)
        return {
            "success": False,
            "message": f"覆寫失敗，提案仍保留：{exc}",
            "committed": [],
        }
    finally:
        if stage.exists():
            shutil.rmtree(stage, ignore_errors=True)

    # The swap is the commit point. Backup cleanup must not turn a complete
    # write into a reported partial failure or trigger a destructive rollback.
    try:
        shutil.rmtree(backup)
    except Exception as exc:
        logger.warning("Iron Dome committed override but backup cleanup failed: %s", exc)

    try:
        clear_override()
    except Exception as exc:
        logger.error("Iron Dome override committed but proposal cleanup failed: %s", exc)
        return {
            "success": False,
            "message": f"覆寫已套用但提案清除失敗，未發送成功通知：{exc}",
            "committed": committed,
        }

    try:
        notifier = LAFNotifier()
        notifier.notify_admin(
            f"✅ [Iron Dome] 已授權覆寫技能 `{skill_name}`。",
            topic_key="alert",
            source="iron_dome",
        )
    except Exception as exc:
        logger.error("Iron Dome success notification failed: %s", exc)

    return {
        "success": True, 
        "message": f"成功覆寫技能 {skill_name} 的檔案: {', '.join(committed)}",
        "committed": committed,
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
