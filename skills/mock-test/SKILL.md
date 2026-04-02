---
name: mock-test
description: 模擬站全套技能測試 — 對 eefile_mock (port 17001) + laf_mock (port 17002) 執行完整功能測試（file-review-orchestrator 閱卷 / laf-orchestrator 法扶 / laf-portal-automation）。
license: MIT
compatibility: Python 3.10+
metadata:
  author: MAGI
  version: "1.0"
  sage: casper
  updated: "2026-03-09"
---

# mock-test

模擬站技能全套測試。在不影響正式站的前提下，對本機 mock server 執行完整功能驗證。

## 指令

```bash
# 執行所有測試
python action.py --task all

# 只測閱卷流程
python action.py --task file_review

# 只測法扶流程
python action.py --task laf

# 只測 portal 自動化
python action.py --task portal

# 發送通知測試
python action.py --task notify
```

## 測試範圍

| 測試項目 | Mock Server | 說明 |
|----------|-------------|------|
| file_review | eefile_mock (17001) | 閱卷流程完整驗證 |
| laf | laf_mock (17002) | 法扶結案報結流程 |
| portal | laf_mock (17002) | 法扶 portal 自動化 |
| notify | — | Telegram/Discord 通知 |

## 觸發方式

- CLI: `python action.py --task all`
- Telegram: `@MAGI 模擬測試`
- Discord: `@MAGI mock test`

## Dependencies

- `/Users/ai/Desktop/MAGI_v2/skills/file-review-orchestrator/` — 閱卷技能
- `/Users/ai/Desktop/MAGI_v2/skills/laf-orchestrator/` — 法扶技能
- `/Users/ai/Desktop/MAGI_v2/skills/laf-portal-automation/` — Portal 自動化
