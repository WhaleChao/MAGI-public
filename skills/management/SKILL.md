---
name: management
description: Self-improvement and system management skills.
metadata:
  iron_dome: true
  role: internal
---

# Management Skills

## 1. Issue Tracker (`issue_tracker.py`)
- **Capabilities**: Log system errors and failed commands to the Nightly Council agenda.
- **Safety**: Local file append only (`nightly_council_agenda.md`).
- **Trigger**: Automatically called by Orchestrator on exception.

## 2. Auto Skill (`auto_skill.py`)
- **Capabilities**: Teach/remember/recall user knowledge, learn from files, and internalize knowledge into runnable skills.
- **Safety**: Restricts file-teach roots to MAGI and Desktop `code` folder.

## 3. Code Auto-Fix (`code_autofix.py`)
- **Capabilities**: Scan Python files, run syntax repair loop, validate compile, and optionally internalize learned repair patterns.
- **Safety**: Restricts target roots and blocks forbidden destructive patch patterns.
