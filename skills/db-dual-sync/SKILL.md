---
name: db-dual-sync
description: 遠端 Keeper DB 與本地 fallback DB 的雙向同步與備份控制（只 upsert、不刪除），提供 status/sync/backup/list/restore/test。
author: CASPER
created: 2026-02-28
---

# db-dual-sync

## 目的

- 以「遠端優先、本地備援」運作 law_firm_data。
- 遠端可連線時，將離線期間本地新增/更新資料安全回補到遠端。
- 僅做 upsert，**不做刪除**。

## 指令

1. `help`
2. `self_test`
3. `status`
4. `sync`
5. `sync {json}`
6. `backup`
7. `backup {json}`
8. `list_backups`
9. `list_backups {json}`

### sync payload（可選）

- `tables`: 逗號字串或陣列
- `chunk_size`: 預設 800
- `update_window_days`: 預設 21
- `recent_limit`: 預設 5000

### backup payload（可選）

- `target`: `remote|local|both`（預設 `both`）
- `keep_days`: 預設 30

