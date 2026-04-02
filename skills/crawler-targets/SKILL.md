---
name: crawler-targets
description: 管理「使用者指定的每日爬蟲 URL 清單」並將內容寫入向量資料庫（通過鐵穹後抓取）。支援 list/add/remove/run_daily（best-effort，不刪資料）。
---

# crawler-targets

## 功能

- 讓你用 LINE/DC 指定「想每天也一起爬」的網址。
- URL 會被持久化到本機狀態檔（不會因重啟消失）。
- 夜間任務可呼叫 `run_daily` 將內容抓下來並寫入向量資料庫，方便後續對話查詢。

## 狀態檔

- 預設：`/Users/ai/Desktop/code/_crawl_targets.json`
- 內容只做增修，不做刪除檔案（remove 只移除清單項目，不會刪任何下載檔）。

## 指令

- `list`
- `add {json}`：`{"url":"https://...","note":"可選"}`
- `remove {json}`：`{"url":"https://..."}`
- `run_daily {json}`：`{"max_targets":20,"max_sections":10}`
- `self_test`


## 呼叫格式
觸發詞：爬蟲、新增爬蟲、移除爬蟲
參數：action=動作(list/add/remove), url=網址(選填)

## 呼叫範例
使用者：爬蟲清單
→ 爬蟲 action=list

使用者：新增爬蟲 https://example.com
→ 爬蟲 action=add url=https://example.com
