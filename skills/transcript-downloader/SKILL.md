---
name: transcript-downloader
description: 電子筆錄調閱協調器 — 從司法院 ezlawyer 下載筆錄 PDF、自動解析日期/類型、重命名、歸檔到案件資料夾。支援 LINE/DC 指令與每夜自動同步。
author: CASPER
created: 2026-02-16
---

# transcript-downloader

## 指令
1. `help`
2. `self_test`
3. `download {"case_number":"114年度訴字第123號"}` — 下載特定案號的筆錄
4. `download {"case_number":"114年度原易字第000168號","court_name":"臺灣臺東地方法院","case_type":"刑事"}` — 無 DB 對照時可直接指定法院與類別
5. `download_all` — 從 DB 取所有進行中案件 → 批次下載 + 歸檔
6. `sync` — 全同步：掃描 MD5 → 下載新筆錄 → 統一更名
7. `rename` — 對所有已下載筆錄統一更名（日期+類型格式）

## download payload（JSON）
- `case_number`（必填）: 法院案號（如 `114年度訴字第123號`）
- `court_name`（可選）: 法院名稱（DB 無法對照時建議提供，如 `臺灣臺東地方法院`）
- `case_type`（可選）: 類別（如 `刑事`/`民事`）
- `headless`（可選，預設 true）
- `timeout_sec`（可選，預設 600）

## LINE/DC 指令格式
- `下載筆錄 花蓮 114訴123`
- `下載筆錄 114訴123`
- `筆錄同步`
- `筆錄下載 114年度訴字第123號`

## 流程
1. 從 config.json 讀取 `judicial.record_username/password`
2. SSO 登入 portal.ezlawyer.com.tw（含驗證碼 OCR）
3. 查詢案號 → 下載 PDF
4. 解析 PDF 表頭（日期、類型、時段）
5. 重新命名（如 `20251221 審理程序筆錄(下午0230).pdf`）
6. 歸檔到 Synology Drive 案件資料夾
7. LINE/DC 通知完成

## 依賴
- `judicial_automation_v2.CourtRecordDownloader`（MAGI 正式版：`casper_ecosystem/law_firm_orchestrators/judicial_automation_v2.py`）
- `config.json`（judicial 區塊）

## 呼叫格式
觸發詞：筆錄、同步筆錄、下載筆錄
參數：action=動作(sync/download/rename), case=案號(選填)

## 呼叫範例
使用者：同步筆錄
→ 筆錄 action=sync

使用者：下載 2025-0004 的筆錄
→ 筆錄 action=download case=2025-0004

使用者：重新命名筆錄
→ 筆錄 action=rename
