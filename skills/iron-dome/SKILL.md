---
name: iron-dome
description: 鐵穹安全核心與同步工具。提供規則掃描、動態規則管理、節點同步與自我測試，供 CASPER 在執行或安裝技能前進行安全過濾。
author: CASPER
created: 2026-02-17
triggers:
  - "鐵穹"
  - "Iron Dome"
  - "安全掃描"
  - "安全規則"
  - "iron dome sync"
---

# iron-dome

鐵穹安全核心與同步工具，用於：
- 內容安全掃描（prompt injection / destructive commands / secrets）
- 動態規則管理（新增/列出）
- 節點同步（broadcast/status）
- 自我測試

## 使用方式（CLI）

### 掃描文字
```bash
python3 action.py scan "rm -rf /"
```

### 列出規則
```bash
python3 action.py list --all
```

### 新增規則（可選同步）
```bash
python3 action.py add "rm\\s+-rf\\s+/Users" --reason "protect local" --broadcast
```

### 同步狀態 / 廣播
```bash
python3 action.py sync status
python3 action.py sync broadcast
```

### 自我測試
```bash
python3 action.py self_test
```

