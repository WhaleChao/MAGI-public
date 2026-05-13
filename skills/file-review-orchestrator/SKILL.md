---
name: file-review-orchestrator
description: 閱卷系統協調器 — 自動聲請閱卷、下載已核准卷證、繳費單處理、Gmail 通知、歸檔到案件資料夾。支援 LINE/DC 指令與每夜自動巡檢。
author: CASPER
created: 2026-02-16
updated: 2026-03-09
---

# file-review-orchestrator

## 指令

| # | 指令 | 說明 |
|---|------|------|
| 1 | `help` | 顯示說明 |
| 2 | `self_test` | 自我檢查（import / DB / Gmail） |
| 3 | `db_smoke` | 連線 DB 冒煙測試（只讀） |
| 4 | `probe {…}` | 閱卷查核（只查核不送件） |
| 5 | `apply {…}` | 線上聲請閱卷 |
| 6 | `download` | 下載所有已核准閱卷資料（背景） |
| 7 | `download {"case_number":"…"}` | 下載特定案號 |
| 8 | `download_sync` | 強制同步下載（阻塞，除錯用） |
| 9 | `download_status {"job_id":"latest"}` | 查詢背景任務狀態 |
| 10 | `download_payment_slips` | 批次下載繳費單 PDF |
| 11 | `check_emails` | 掃描 Gmail 繳費單 + 交付通知 |
| 12 | `preview_emails` | 信件掃描預覽（不下載、不通知） |
| 13 | `downloadable_probe` | 可下載判定（入口列表 + Gmail） |
| 14 | `check_stale` | 檢查超過 90 天未更新案件 |
| 15 | `reauth_gmail` | 重新授權閱卷 Gmail |

## apply / probe payload（JSON）

- `court_code`（必填）: 法院代碼（TPD/SLD/ILD 等）
- `year`（必填）: 民國年（如 114）
- `case_type`（必填）: 字別（如 訴、簡、易）
- `case_number`（必填）: 案號數字
- `client_name`（可選）: 當事人姓名
- `auto_submit`（可選，預設 false）: 是否直接送出

## LINE/DC 指令格式

- `閱卷查核 基隆 114訴1`
- `查核閱卷 台北 114訴123`
- `閱卷聲請 台北 114訴123 民事`
- `下載閱卷` / `下載閱卷 114年度訴字第123號`
- `下載繳費單`
- `檢查閱卷信箱`
- `預覽閱卷通知`
- `閱卷可下載判定`
- `閱卷到期檢查`
- `重新授權閱卷信箱`

## 流程

1. 從 config.json 讀取 `judicial.eefile_username/password`
2. SSO 登入 portal.ezlawyer.com.tw（含 OCR 驗證碼）
3. 導航到「線上閱卷作業」
4. 執行聲請 / 下載 / 繳費 / 信箱檢查
5. 歸檔到 Synology Drive 案件資料夾
6. TG/DC 通知結果

## 智慧處理規則（2026-03-09）

### 繳費單

- **逾期過濾**：超過 14 天的逾期案件自動跳過（`_is_payment_overdue`）
- **去重**：`payment_registry.json` 有記錄即跳過，避免重複點擊繳費
- **指定辯護免繳費**：查詢 DB `case_category`，僅「指定辯護案件」免繳費（`_is_fee_exempt_case`）
- **PDF 重命名**：僅重命名 Chrome 預設名「繳費單.pdf」→「繳費單_{當事人}_{案號}.pdf」

### 聲請

- **義務辯護自動勾選**：指定辯護案件聲請時自動勾選 `isobligation=Y`
- **委任狀上傳**：首次聲請需上傳有收文章的委任狀
  - 法扶案件搜尋開辦資料夾，一般/無償案件搜尋整個案件資料夾
  - 收文章判斷使用視覺模組（`_has_stamp_on_document`），非看檔名

### 下載

- **閱卷資料去重**：下載前檢查案件「閱卷資料」子資料夾是否已有檔案（`_case_review_folder_has_files`）

### 通知

- **法扶信件排除**：Gmail 掃描排除 `@laf.org.tw` 來源，避免與法扶通知系統重疊
- **TG 路由**：通知走 `red_phone` topic_key `filereview`
- **繳費單通知**（2026-03-25 修正）：
  - 文字通知走 `red_phone.send_telegram_push_with_status()`（TG 推送 + DC mirror），**不用 Discord webhook**
  - PDF 附件：TG 走 `LAFNotifier._push_telegram_document()`，DC 走 `red_phone.send_discord_bot_file()`（Bot API multipart upload）
  - 繳費通知 key（`web_payment:*`）永久去重；非繳費通知才套用 `PAYMENT_NOTIFY_COOLDOWN_HOURS`
  - 已繳費跳過：`_has_payment_proof_uploaded` 檢查 `paystatus`/`p_status`/`payment`/`statusnm`/`result` 多欄位

## 背景執行策略

- `download` 預設為背景任務，避免 Selenium timeout
- 同步模式用 `download_sync`
- `download_status {"job_id":"latest"}` 追蹤進度
- 狀態檔位於 `_bg_jobs/`

## 安全判斷規則（必須遵守）

1. 歸檔必須做「法院案號 + 當事人 + DB 案件」一致性比對
2. 任一訊號衝突時，停在 `_待歸檔`，不得自動搬移
3. 指定案號下載找不到匹配時中止，不退回全部下載
4. 禁用檔名啟發式硬分派（除非明確開啟風險旗標）

## 依賴

- `file_review_automation.FileReviewManager`（MAGI/casper_ecosystem/law_firm_orchestrators/）
- `config.json`（judicial 區塊：eefile_username/password/download_folder）
- `payment_registry.json`（閱卷下載/，繳費單去重）
- `apply_registry.json`（聲請次數追蹤）
- `vision_parser`（skills/pdf-namer/，收文章判斷）
- Selenium + ChromeDriver（headless）
- Gmail API（OAuth token）

## 呼叫格式
觸發詞：閱卷、下載卷證、繳費、聲請閱卷
參數：action=動作(check/download/apply), case=案號(選填)

## 呼叫範例
使用者：檢查閱卷信箱
→ 閱卷 action=check

使用者：下載 2025-0004 的卷證
→ 閱卷 action=download case=2025-0004

使用者：可下載案件
→ 閱卷 action=downloadable
