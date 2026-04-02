# 🧩 MAGI Skill Architecture (v2.0)

> **Purpose**: Extend MAGI's capabilities through modular, secure, and specialized skill packages.

## 1. Skill Directory Structure
Each skill lives in `MAGI/skills/<skill_name>/` and MUST have a `SKILL.md`.

```
MAGI/
└── skills/
    ├── browser/
    │   ├── SKILL.md           <-- Manifest & Usage Guide
    │   └── browser_control.py <-- Implementation (Playwright)
    ├── ops/
    │   ├── SKILL.md
    │   ├── system_monitor.py
    │   └── file_manager.py
    └── ...
```

## 2. Skill Manifest (SKILL.md)
The `SKILL.md` file defines the skill's metadata, capabilities, and usage instructions for the AI.

```yaml
---
name: browser
description: Browser automation and web scraping.
compatibility: Requires Playwright
metadata:
  iron_dome: true  # Subject to security filtering
---
```

## 3. Iron Dome Security Policy
All skills must adhere to the **Iron Dome** non-proliferation treaty:

1.  **Read-Only Default**: Skills should prefer read-only operations unless explicitly authorized.
2.  **Sandboxing**: File operations are restricted to `Desktop/MAGI` and `Desktop/code`.
3.  **URL Filtering**: Browser skill blocks local network access (localhost, 127.0.0.1) to prevent SSRF.
4.  **No Shell**: Direct shell execution is banned; use `subprocess` with hardcoded commands only.

## 4. Integration Pattern
Skills are imported dynamically by the **Orchestrator** based on trigger keywords.

```python
# orchestrator.py
if "screenshot" in message:
    from skills.browser.browser_control import take_screenshot
    return take_screenshot(url)
```

## 5. Available Skills (as of 2026-02)

| Skill | Module | Capabilities |
|-------|--------|--------------|
| **Browser** | `skills.browser` | Headless navigation, screenshot, text extraction (Playwright) |
| **Ops** | `skills.ops` | System monitoring (CPU/RAM), File management, Service health |
| **Smart Summary** | `skills.ops` | URL summarization, Key point extraction |
| **Bridge** | `skills.bridge` | Node communication (Melchior, Balthasar) |
