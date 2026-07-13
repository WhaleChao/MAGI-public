from __future__ import annotations

import os
from typing import Any


def _legacy_openclaw_enabled() -> bool:
    value = str(os.environ.get("MAGI_INCLUDE_RETIRED_OPENCLAW_SKILLS", "")).strip().lower()
    return value in {"1", "true", "yes", "on"}


def iter_skill_roots(magi_root: str) -> list[tuple[str, str]]:
    roots = [(os.path.join(os.path.abspath(magi_root), "skills"), "magi")]
    if _legacy_openclaw_enabled():
        roots.append((os.path.join(os.path.expanduser("~"), ".openclaw", "skills"), "openclaw-retired"))
    return roots


def build_skill_list_response(magi_root: str, logger: Any = None) -> str:
    """Build the human-facing skill list from one canonical root policy."""
    from skills.catalog import iter_top_level_skill_dirs

    skills_found: list[dict[str, str]] = []
    try:
        for skills_dir, source in iter_skill_roots(magi_root):
            if not os.path.isdir(skills_dir):
                continue
            for entry in iter_top_level_skill_dirs(skills_dir):
                entry_path = os.fspath(entry)
                skill_path = os.path.join(entry_path, "SKILL.md")
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
    except Exception as exc:
        if logger is not None:
            logger.error("Error scanning skills: %s", exc)
        return "❌ 無法讀取技能列表。"

    response = "🧩 **MAGI 技能列表 (Skill Matrix)**\n"
    response += f"📦 已安裝 **{len(skills_found)}** 個技能模組\n\n"

    emoji_map = {
        "bridge": "🌉",
        "memory": "🧠",
        "research": "🌐",
        "law-firm": "⚖️",
        "browser": "🖥️",
        "identity": "🪪",
        "evolution": "🧬",
        "apple": "🍎",
        "ops": "⚙️",
        "maintenance": "🔧",
        "source_control": "📂",
        "synology": "💾",
        "brain_manager": "🧠",
    }

    for skill in sorted(skills_found, key=lambda s: s["name"]):
        emoji = emoji_map.get(skill["name"], "📌")
        src = str(skill.get("source") or "magi")
        response += f"{emoji} **{skill['name']}** [{src}]\n"
        response += f"  _{skill['desc']}_\n\n"

    response += "💡 *您可以直接對我下達相關指令，例如「查詢行程」、「分析程式碼」等。*"
    return response
