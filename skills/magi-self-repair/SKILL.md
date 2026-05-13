---
name: magi-self-repair
description: Legacy compatibility wrapper that delegates self-repair calls to MAGI Doctor.
created: 2026-03-20
---

# MAGI Self Repair

相容層技能。

舊版 UI 與 API 仍會尋找 `skills/magi-self-repair/action.py`，實際修復邏輯已整合到 `skills/magi-doctor/action.py`。
此技能僅保留舊介面，將 `repair_targets()` 呼叫轉發給 MAGI Doctor。
