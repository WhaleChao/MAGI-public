"""
Command handling pipeline extracted from Orchestrator.

Contains: handle_command (the massive dispatch method) and list_skills.

All functions accept an `orch` parameter (the Orchestrator instance)
instead of `self`.
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import threading
import time

logger = logging.getLogger("Orchestrator")

_MAGI_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


# Lazy-loaded modules
def _get_handler(name: str):
    from api.orchestrator import _get_handler as _orig
    return _orig(name)


def list_skills(orch) -> str:
    """Dynamically lists available skills by parsing SKILL.md frontmatter."""
    from skills.catalog import iter_top_level_skill_dirs

    skill_roots = [
        (f"{_MAGI_ROOT}/skills", "magi"),
        (os.path.join(os.path.expanduser("~"), ".openclaw", "skills"), "openclaw"),
    ]
    skills_found = []

    try:
        for skills_dir, source in skill_roots:
            if not os.path.isdir(skills_dir):
                continue
            for entry in iter_top_level_skill_dirs(skills_dir):
                skill_path = os.path.join(entry.path, "SKILL.md")
                try:
                    with open(skill_path, "r", encoding="utf-8") as f:
                        content = f.read()
                    name = entry.name
                    desc = "No description"
                    if content.startswith("---"):
                        parts = content.split("---", 2)
                        if len(parts) >= 3:
                            for line in parts[1].strip().split("\n"):
                                line = line.strip()
                                if line.startswith("name:"):
                                    name = line.split(":", 1)[1].strip().strip("'\"")
                                elif line.startswith("description:"):
                                    desc = line.split(":", 1)[1].strip().strip("'\"")
                    if len(desc) > 80:
                        desc = desc[:77] + "..."
                    skills_found.append({"name": name, "desc": desc, "source": source})
                except Exception:
                    skills_found.append({"name": entry.name, "desc": "(Unable to parse)", "source": source})
    except Exception as e:
        logger.error(f"Error scanning skills: {e}")
        return "\u274c \u7121\u6cd5\u8b80\u53d6\u6280\u80fd\u5217\u8868\u3002"

    response = f"\U0001f9e9 **MAGI \u6280\u80fd\u5217\u8868 (Skill Matrix)**\n"
    response += f"\U0001f4e6 \u5df2\u5b89\u88dd **{len(skills_found)}** \u500b\u6280\u80fd\u6a21\u7d44\n\n"

    emoji_map = {
        "bridge": "\U0001f309", "memory": "\U0001f9e0", "research": "\U0001f310",
        "law-firm": "\u2696\ufe0f", "browser": "\U0001f5a5\ufe0f", "identity": "\U0001faaa",
        "evolution": "\U0001f9ec", "apple": "\U0001f34e", "ops": "\u2699\ufe0f",
        "maintenance": "\U0001f527", "source_control": "\U0001f4c2", "synology": "\U0001f4be",
        "brain_manager": "\U0001f9e0"
    }

    for skill in sorted(skills_found, key=lambda s: s["name"]):
        emoji = emoji_map.get(skill["name"], "\U0001f4cc")
        src = str(skill.get("source") or "magi")
        response += f"{emoji} **{skill['name']}** [{src}]\n"
        response += f"  _{skill['desc']}_\n\n"

    response += "\U0001f4a1 *\u60a8\u53ef\u4ee5\u76f4\u63a5\u5c0d\u6211\u4e0b\u9054\u76f8\u95dc\u6307\u4ee4\uff0c\u4f8b\u5982\u300c\u67e5\u8a62\u884c\u7a0b\u300d\u3001\u300c\u5206\u6790\u7a0b\u5f0f\u78bc\u300d\u7b49\u3002*"
    return response


def handle_command(orch, user_id, message, role="user", platform="LINE") -> str:
    """
    Routes commands to skills / system functions.
    Uses CommandRegistry for extensible dispatch, falls back to legacy if-elif.

    This is a direct extraction of the massive _handle_command method.
    It delegates back to orch for methods that remain on the Orchestrator class.
    """
    # The full _handle_command body is enormous (~2100 lines).
    # We delegate to the original method on the orchestrator instance
    # to avoid duplicating all that logic during the initial split.
    # Future refactoring passes should break this further into sub-dispatchers.
    return orch._handle_command(user_id, message, role=role, platform=platform)
