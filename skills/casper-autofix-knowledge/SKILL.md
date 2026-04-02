---
name: casper-autofix-knowledge
description: Auto-learned code repair patterns from CASPER.
author: CASPER-AUTOSKILL
created: 2026-04-02
---

# casper-autofix-knowledge

This skill serves learned operational knowledge from CASPER's AutoSkill KB.

## Runtime Contract
- Execute with `python3 action.py --task "<user request>"`.
- Fallback invoke: `python3 action.py "<user request>"`.

## Examples
- `python3 action.py --task "line invalid signature"`
- `python3 action.py --task "port 5002 already in use"`

## Safety Constraints
- Read-only lookup and stdout output only.
- No destructive commands.
