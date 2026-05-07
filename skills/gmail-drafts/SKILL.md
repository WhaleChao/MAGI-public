---
name: gmail-drafts
description: 透過 Gmail API 建立「草稿」（drafts.create），用於 CASPER 擬好回信但不直接寄出。支援 OAuth（必要時才互動授權），不會自動刪除或寄出。
---

# gmail-drafts

## 目標

- 把 CASPER 草擬的回信，直接存到 Gmail 的「草稿」資料夾。
- **只建立草稿，不寄出**。

## 指令

- `help`
- `self_test`（不會真的建立草稿，只檢查憑證檔案是否存在）
- `authorize`（互動授權一次，產生/更新 token 檔；白天執行即可）
- `create {json}`：
  - `to`：收件人（可空字串；可先存草稿不指定）
  - `subject`：主旨
  - `body`：內文（純文字）
  - `thread_id`（可選）

## OAuth 檔案

預設會找：
- `credentials.json`: `/Users/ai/Desktop/code/json/credentials.json`
- `token.json`: `/Users/ai/Desktop/code/json/gmail_compose_token.json`

若 token 不存在或失效，會回傳 `need_interactive_oauth=true`（由你在白天完成一次授權即可）。
