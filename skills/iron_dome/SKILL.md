---
name: iron_dome
description: Internal Python import shim — delegates all calls to skills/iron-dome/action.py. Python cannot import modules with hyphens, so this underscore alias exists purely for `import skills.iron_dome` compatibility.
license: MIT
compatibility: Python 3.10+
metadata:
  author: MAGI
  version: "1.0"
  type: internal-alias
  alias_of: iron-dome
  updated: "2026-03-09"
---

# iron_dome (internal alias)

此模組不是獨立 skill，而是 `iron-dome` 的 Python import 相容層。

Python 無法 `import iron-dome`（hyphen 不是合法識別字元），所以透過此 shim 將所有呼叫轉發至 `skills/iron-dome/action.py`。

**不應獨立維護或列為正式 skill。**
