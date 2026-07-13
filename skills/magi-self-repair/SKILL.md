---
name: magi-self-repair
description: Compatibility wrapper for targeted MAGI Doctor repairs and conservative self-repair guardian audits.
created: 2026-03-20
---

# MAGI Self Repair

相容層技能，兼提供保守自主維修入口。

舊版 UI 與 API 仍會尋找 `skills/magi-self-repair/action.py`。指定目標的修復仍會將
`repair_targets()` 呼叫轉發給 MAGI Doctor。

新增的 guardian 入口使用 `scripts/ops/magi_self_repair_guardian.py`：

- `guardian` / `guardian:audit`：彙整 Doctor、function health 與暫存殘留，只產生診斷。
- `guardian:repair-propose`：列出可修復項目與需要人工確認的高風險項目。
- `guardian:repair-safe`：只自動清除過期 `/tmp/magi_*` 殘留；daemon、cron、憑證、NAS、DB、模型與業務資料一律只提出建議。
