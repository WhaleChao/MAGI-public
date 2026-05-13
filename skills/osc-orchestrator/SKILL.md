---
name: osc-orchestrator
description: OSC 核心自動化（headless）：把「PDF/檔名→待辦解析→寫入本機/主 DB」做成可對話呼叫的流程型 skill，並可與 pdf-namer 歸檔後自動同步待辦（不刪檔、可降級佇列）。
author: CASPER
created: 2026-02-16
---

# osc-orchestrator

## 目標
1. 不啟動 OSC GUI（可在沒有 `_tkinter` 的環境跑）。
2. 以檔名為主做待辦解析，尤其相對期限一律以檔名前綴 `YYYYMMDD`（收文日/文到日）當基準。
3. 主 DB 掛掉時可降級：把待辦寫入 pending queue，等 DB 恢復再補寫。
4. **絕不刪除任何 Synology Drive 檔案**（只讀、只新增 sidecar/queue）。

## 指令（可對話呼叫）
1. `help`
1. `self_test`
1. `db_smoke`（預設連 Casper 本機 MariaDB：`127.0.0.1:3307 law_firm_data`）
1. `待辦預覽 {"path":"/abs/file.pdf"}`（或 `todo_preview {...}`）
1. `待辦入庫 {"path":"/abs/file.pdf","case_number":"2025-0088"}`（或 `todo_sync {...}`）
1. `待辦清單 {"case_number":"2025-0088","status":"pending","limit":50}`（或 `todo_list {...}`）
1. `掃描資料夾待辦 {"root":"/abs/folder","max_files":200}`（或 `scan_folder {...}`）
1. `關鍵詞健檢 {"limit":200}`（或 `keyword_sanity {...}`）
1. `關鍵詞修補 {"dry_run":true,"limit":500}`（或 `keyword_fix {...}`）
1. `掃描案件待辦 {"max_cases":50,"max_files_per_case":50}`（或 `scan_cases {...}`）
1. `佇列狀態`（或 `queue_status`）
1. `佇列補寫 {"limit":50}`（或 `queue_flush {...}`）

## 備註
- `case_number` 若沒給，會嘗試從 `case_folder_name` 或路徑父層資料夾（`YYYY-NNNN-...`）推回。
- DB 連線可用環境變數覆寫：`OSC_DB_HOST/OSC_DB_PORT/OSC_DB_USER/OSC_DB_PASSWORD/OSC_DB_NAME`。
- 若 `case_number` 與 `case_folder_name` 推回的編號不一致，會自動改走佇列，避免寫錯案件。

## 呼叫格式
觸發詞：案件、待辦、掃描案件
參數：action=動作(scan/flush/status), case=案號(選填)

## 呼叫範例
使用者：掃描案件待辦
→ 案件 action=scan

使用者：待辦佇列狀態
→ 案件 action=status
