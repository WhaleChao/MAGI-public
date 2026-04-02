# -*- coding: utf-8 -*-
"""
Issue Tracker Skill (自我改進)
Iron Dome Audit: ✅ SAFE — Local file logging only

Provides: Automatic error logging to Nightly Council agenda
"""

import os
_MAGI_ROOT = os.path.abspath(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
import logging
from datetime import datetime

logger = logging.getLogger("IssueTracker")

AGENDA_FILE = f"{_MAGI_ROOT}/nightly_council_agenda.md"

def log_issue(command, error_msg, context=None, severity="Normal"):
    """
    Log a failed command or system error to the Nightly Council agenda.
    """
    try:
        # Ensure directory exists
        os.makedirs(os.path.dirname(AGENDA_FILE), exist_ok=True)
        
        # Initialize file if not exists
        if not os.path.exists(AGENDA_FILE):
            with open(AGENDA_FILE, "w", encoding="utf-8") as f:
                f.write("# 🌙 Nightly Council Agenda (夜議議程)\n\n")
                f.write("此文件由 Issue Tracker 自動維護，記錄系統運行時發生的錯誤與改進建議。\n\n")

        # Format the entry
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        entry = f"\n## ⚠️ Issue Report ({timestamp})\n"
        entry += f"- **Severity**: {severity}\n"
        entry += f"- **Command**: `{command}`\n"
        entry += f"- **Error**: `{error_msg}`\n"
        if context:
            entry += f"- **Context**: {context}\n"
        entry += "\n> **Action Item**: Please review this error during the Nightly Council and propose a fix.\n"
        entry += "---\n"

        # Append to file
        with open(AGENDA_FILE, "a", encoding="utf-8") as f:
            f.write(entry)
            
        logger.info(f"📝 Issue logged to agenda: {error_msg}")
        return True

    except Exception as e:
        logger.error(f"Failed to log issue: {e}")
        return False
