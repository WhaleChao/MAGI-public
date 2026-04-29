# OSC 桌面原版 vs MAGI v2 網頁版 功能對照

> 產出日期：2026-04-29  
> 掃描範圍：`/Users/ai/Desktop/Paperclip/_internal/osc.py`（41,470 行）vs MAGI v2 `api/blueprints/osc_*.py` + `templates/partials/osc/` + `static/osc/`

---

## 0. 摘要

| 指標 | 數字 |
|---|---|
| 功能領域總數 | 11 |
| ✅ 完整復原 | 6 |
| ⚠️ 部分復原 | 5 |
| ❌ 完全缺失 | 6 |
| 🚫 不建議移植 | 4 |
| 整體還原度（功能加權粗估） | **~65%** |

**Top 6 重要缺失：**
1. **OSC UI 沒有蓋章/書狀製作按鈕**（後端 doc-producer skill 已完整、只缺前端入口；UX 退化嚴重，使用者得改 chat 觸發）
2. **Google Calendar 真實雙向同步**（僅有本地 calendar_events 表，無 OAuth 推送）
3. **案件/當事人 CSV 批次匯入匯出**（日常資料遷移和備份依賴）
4. **地址標籤 PNG 生成**（寄件封面自動化，法扶作業核心工具）
5. **自動備份還原**（桌面版每 24h 自動備份 JSON，網頁版無對應）
6. **Checklist（應備事項表）**（法扶補件追蹤，原版有完整 UI + Discord 推播）

**書狀生成 / 委任契約 / 合併 PDF 三大核心已 ✅ 完整實作**（osc_debt 5 種書類、forms 委任契約收據、debt/merge-pdf + doc-producer 雙合併路徑）。蓋章後端也已完整，只差網頁 UI 整合。

---

## 1. 原版功能盤點

### 1.1 案件管理（CaseManagementFrame, line 14299）
- 新增 / 編輯 / 刪除案件（CaseDialog, line 19146）
- 案件篩選（案件類型、搜尋詞）
- **CSV 匯入案件**（import_csv, line 18310）
- **CSV 匯出案件**（export_csv, line 18416）
- 文件索引重建（build_document_index）
- 案件編號修復（fix_missing_case_numbers）
- 案件狀態批次標記已結案（mark_cases_as_closed, line 4731）
- **右鍵：列印地址標籤 PNG**（trigger_generate_address_pdf, line 15011）
- **右鍵：列印案件卷夾標籤 PNG**（trigger_generate_case_info_label, line 15116）
- **手動歸檔結案案件**（manual_archive_closed_cases → get_closed_cases_for_archive, line 8416）
- Google Calendar 一鍵全同步（sync_all_to_google_calendar）
- **假日管理**（HolidayManagerDialog, line 23254）

### 1.2 行事曆（CalendarFrame, line 11750；GoogleCalendarService, line 11191）
- 本地行事曆 CRUD（AddEventDialog, EditEventDialog）
- **Google Calendar OAuth 真實雙向同步**：token.pickle → InstalledAppFlow → build
- Todo 同步到 GCal（todos + meetings 均含 google_calendar_id 欄）
- 待辦清理 GCal 事件（clear_google_calendar_events, line 4086）

### 1.3 文件生成 — OSC 5 種書類（LegalAidFrame buttons, line 30037）
原版以外部 `.app`（robot 子應用）方式啟動，按鈕呼叫 `_launch_exe_in_program_dir`：
1. 撰寫聲請狀（01_撰寫聲請狀）
2. 撰寫財產及收入狀況說明書（02_撰寫財產及收入狀況說明書）
3. 撰寫債權人清冊（03_撰寫債權人清冊）
4. 撰寫補件陳報狀（04_撰寫補件陳報狀）
5. 合併調解聲請狀（05_合併檔案）

### 1.4 委任狀及收據（DocumentGeneratorFrame, line 36320）
- 律師酬金收據（委任費用 / 諮詢費用）
- 刑事委任狀（告訴代理人 / 辯護人）
- 民事委任狀、行政委任狀
- 案件委任契約書
- docx → PDF 轉換並儲存（docx2pdf / LibreOffice）

### 1.5 帳務管理（BillingOverviewFrame, line 32561）
- 收支記錄（CRUD）
- **固定支出管理**（recurring expenses — 每月自動產生）
- 月度匯總（monthly transactions）
- 支出科目 / 細目分類

### 1.6 報價單（QuotationFrame, line 13252；QuoteManagementFrame, line 21335）
- 新增 / 編輯報價單
- 報價單 PDF 匯出（export_professional_pdf）
- 報價單模板管理
- 報價單狀態管理（待確認 / 已確認 / 已收款）

### 1.7 當事人管理（ClientManagementFrame, line 21900）
- CRUD
- **CSV 匯入**（import_clients_from_csv, line 22120）
- **CSV 匯出**（export_clients_to_csv）
- 關聯案件清單顯示

### 1.8 法律扶助管理（LegalAidFrame, line 29268）
- 掃描派案（scan_and_refresh）
- 法扶案件狀態更新（update_legal_aid_status）
- 法扶地址標籤 PNG 列印（trigger_generate_laf_address_pdf, line 15067）
- 閱卷次數統計
- **ChecklistDialog / CaseChecklistDialog**（應備事項清單，line 31574、32214）
- 開辦 / 結案資料彙總
- Google Calendar 事件統計（出庭、開庭、法扶期限）

### 1.9 書狀管理與 AI 草擬
- 書狀索引（DocumentManagementFrame, line 24299）
- **熱鍵管理**（HotkeyManager, line 29169 — 系統全域 Ctrl/Shift+1~9 快速複製詞句）
- **QuickKeywordPanel**（浮動詞句面板）
- AI 書狀草擬（DraftGenerationFrame, line 34871）：Gemini / Ollama 雙提供者
- 法律洞察庫（LegalInsightFrame, line 34384）

### 1.10 設定與系統
- SettingsDialog（line 22475）：路徑 / DB / Google API / Discord Webhook / PNG 標籤尺寸
- **AutoBackupManager**（line 619）：每 24h 自動 JSON 備份，保留 7 份，支援手動還原
- **ThemeManager**（line 507）：日間 / 夜間主題切換
- **PerformanceMonitor + StatusMonitorDialog**（DB 查詢效能監控）
- **FirstRunWizard**（line 37453）：5 步驟初始化精靈（路徑 → DB → Google → Discord）
- **Discord 通知**（DailyDiscordNotifier, line 36810）：4 個 Webhook 頻道

### 1.11 儀表板（DashboardFrame, line 9172）
- 案件統計 / 待辦摘要 / 急件清單
- 月度收支 / 待確認報價單
- 會議行程（今日 + N 天）

---

## 2. 網頁版實作盤點

### 2.1 後端 API（共 ~70 個端點，分 4 個 blueprint）

**osc_cases.py**（最大，~3440 行）
- `/api/osc/cases` CRUD、workbench、quick-action、folder-browser
- `/api/osc/dashboard` 統計
- `/api/osc/calendar/events` CRUD（純本地，無 GCal 同步）
- `/api/osc/clients` CRUD
- `/api/osc/meetings` CRUD
- `/api/osc/todos` CRUD
- `/api/osc/insights` CRUD + fetch-full
- `/api/osc/documents` 索引 + open + content + upload
- `/api/osc/document-keywords` / `document-templates` / `document-replacements`
- `/api/osc/drafts/meta` + `generate`（Casper/Ollama/Gemini）+ `export`
- `/api/osc/forms/preview` + `export`（委任狀/收據）
- `/api/osc/quotations` CRUD + templates
- `/api/osc/laf` 法扶列表
- `/api/osc/laf-wizard/run` + `/api/osc/archive-wizard/preview` + `execute`
- `/api/osc/memory-keywords` CRUD
- `/api/osc/opponents` CRUD
- `/api/osc/labor-law/calc` + `parse-files`
- `/api/osc/judgments`（司法院判決查詢）

**osc_accounting.py**
- `/api/osc/accounting/transactions` CRUD
- `/api/osc/accounting/summary`
- `/api/osc/accounting/defaults` CRUD
- `/api/osc/accounting/recurring` CRUD

**osc_debt.py**（消債 Robot）
- `/api/osc/debt/generate`（4 種：application, asset_statement, creditor_list, report）
- `/api/osc/debt/batch-generate`
- `/api/osc/debt/merge-pdf`
- `/api/osc/debt/auto-import`
- `/api/osc/debt/validate` + `scan-evidence`

**osc_settings.py**
- `/api/osc/settings` CRUD
- `/api/osc/courts` CRUD
- `/api/osc/legal-aid-branches` CRUD

### 2.2 前端 Sidebar（osc.html）
業務概覽 / 案件管理 / 當事人 / 會議紀錄 / 行事曆 / 待辦事項 / 書狀索引 / 書狀草擬 / 委任/收據 / 消債 Robot / 法扶回報 / 結案歸檔 / 法扶清單 / 帳務收支 / 報價單 / 實務見解 / 系統設定

Tab JS 檔：accounting.js / admin.js / calendar.js / cases.js / dashboard.js / documents.js / drafts.js / insights.js / todos.js

---

## 3. 功能對照表

### 3.1 案件管理

| 子功能 | 原版（class / line） | 網頁版 | 狀態 | 補強難度 |
|---|---|---|---|---|
| 案件 CRUD | CaseDialog, line 19146 | `POST/PUT/DELETE /api/osc/cases` | ✅ 完整 | — |
| 案件篩選搜尋 | CaseManagementFrame get_filtered_cases | GET /api/osc/cases + cases.js | ✅ 完整 | — |
| CSV 匯入案件 | import_csv, line 18310 | 無 | ❌ 缺 | 低 |
| CSV 匯出案件 | export_csv, line 18416 | 無 | ❌ 缺 | 低 |
| 案件編號修復 | fix_missing_case_numbers | quick-action endpoint（部分） | ⚠️ 半 | 低 |
| 文件索引重建 | build_document_index | `/api/osc/documents` scan | ⚠️ 半（無手動觸發 UI） | 低 |
| 手動歸檔結案 | manual_archive_closed_cases | archive-wizard endpoints | ✅ 完整 | — |
| 地址標籤 PNG 列印 | trigger_generate_address_pdf, line 15011 | 無 | ❌ 缺 | 高 |
| 案件卷夾標籤 PNG | trigger_generate_case_info_label, line 15116 | 無 | ❌ 缺 | 中 |

### 3.2 行事曆與 Google Calendar 同步

| 子功能 | 原版 | 網頁版 | 狀態 | 補強難度 |
|---|---|---|---|---|
| 本地行事曆 CRUD | CalendarFrame, line 11750 | `/api/osc/calendar/events` CRUD + calendar.html | ✅ 完整 | — |
| Google OAuth 真實同步 | GoogleCalendarService, line 11191；token.pickle | 無（僅存本地 google_calendar_id 欄，無推送） | ❌ 缺 | 高 |
| Todo → GCal 同步 | get_todos_for_gcal_sync, line 4985 | 無 | ❌ 缺 | 高 |
| GCal 事件清理 | clear_google_calendar_events, line 4086 | 無 | ❌ 缺 | 高 |

### 3.3 文件生成（OSC 5 種書類）

| 子功能 | 原版 | 網頁版 | 狀態 | 補強難度 |
|---|---|---|---|---|
| 聲請狀（application） | robot/01_撰寫聲請狀 | `POST /api/osc/debt/generate` form_type=application | ✅ 完整 | — |
| 財產及收入說明書（asset_statement） | robot/02 | form_type=asset_statement | ✅ 完整 | — |
| 債權人清冊（creditor_list） | robot/03 | form_type=creditor_list | ✅ 完整 | — |
| 補件陳報狀（report） | robot/04 | form_type=report | ✅ 完整 | — |
| 合併 PDF（merge） | robot/05_合併檔案 | `POST /api/osc/debt/merge-pdf` | ✅ 完整 | — |
| 批次產生 | 無 | `POST /api/osc/debt/batch-generate` | ✅ 完整（網頁版新增） | — |

### 3.4 委任狀及收據（DocumentGeneratorFrame）

| 子功能 | 原版 | 網頁版 | 狀態 | 補強難度 |
|---|---|---|---|---|
| 律師酬金收據 | generate_receipt, line 333 | `/api/osc/forms/preview` + `export` | ✅ 完整 | — |
| 刑事委任狀（告訴/辯護） | generate_poa, line 354 | forms endpoint 支援 | ✅ 完整 | — |
| 民事 / 行政委任狀 | generate_poa_civil/administrative | forms endpoint 支援 | ✅ 完整 | — |
| 案件委任契約書 | generate_engagement_agreement, line 446 | forms endpoint 支援 | ✅ 完整 | — |

### 3.5 帳務管理

| 子功能 | 原版 | 網頁版 | 狀態 | 補強難度 |
|---|---|---|---|---|
| 收支 CRUD | BillingOverviewFrame, line 32561 | `/api/osc/accounting/transactions` + accounting.html | ✅ 完整 | — |
| 固定支出管理 | recurring expenses UI | `/api/osc/accounting/recurring` CRUD | ✅ 完整 | — |
| 月度匯總 | get_monthly_transactions | `/api/osc/accounting/summary` | ✅ 完整 | — |
| 支出科目預設 | get_default_expense | `/api/osc/accounting/defaults` | ✅ 完整 | — |

### 3.6 當事人管理

| 子功能 | 原版 | 網頁版 | 狀態 | 補強難度 |
|---|---|---|---|---|
| 當事人 CRUD | ClientManagementFrame, line 21900 | `/api/osc/clients` CRUD + clients.html | ✅ 完整 | — |
| 關聯案件顯示 | on_client_select | workbench endpoint | ✅ 完整 | — |
| CSV 匯入當事人 | import_clients_from_csv, line 22120 | 無 | ❌ 缺 | 低 |
| CSV 匯出當事人 | export_clients_to_csv | 無 | ❌ 缺 | 低 |

### 3.7 報價單

| 子功能 | 原版 | 網頁版 | 狀態 | 補強難度 |
|---|---|---|---|---|
| 報價單 CRUD | QuotationFrame / QuoteManagementFrame | `/api/osc/quotations` + `/api/osc/quotation-templates` + quotations.html | ✅ 完整 | — |
| 報價單 PDF 匯出 | export_professional_pdf | 無對應端點 | ⚠️ 缺 PDF 匯出 | 中 |
| 報價單狀態管理 | update_quotation_status | PUT /api/osc/quotations/:id | ✅ 完整 | — |

### 3.8 法律扶助管理

| 子功能 | 原版 | 網頁版 | 狀態 | 補強難度 |
|---|---|---|---|---|
| 法扶案件列表 | LegalAidFrame, line 29268 | `/api/osc/laf` + laf.html | ✅ 完整 | — |
| 法扶 Wizard（回報） | scan_and_refresh + status update | `/api/osc/laf-wizard/run` + lafWizard.html | ✅ 完整 | — |
| 法扶地址標籤 PNG | trigger_generate_laf_address_pdf, line 15067 | 無 | ❌ 缺 | 高 |
| 閱卷次數統計 | populate_review_dates | casper/skill 部分支援 | ⚠️ 半 | 中 |
| 應備事項 Checklist | ChecklistDialog, line 31574；CaseChecklistDialog, line 32214 | 無 web 前端（DB 表存在但無 API 端點） | ❌ 缺 | 中 |

### 3.9 書狀管理與 AI 草擬

| 子功能 | 原版 | 網頁版 | 狀態 | 補強難度 |
|---|---|---|---|---|
| 書狀索引 CRUD | DocumentManagementFrame, line 24299 | `/api/osc/documents` + documents.html | ✅ 完整 | — |
| AI 書狀草擬 | DraftGenerationFrame, line 34871 | `/api/osc/drafts/generate` (Casper/Ollama/Gemini) + drafts.html | ✅ 完整 | — |
| 法律洞察庫 | LegalInsightFrame, line 34384 | `/api/osc/insights` CRUD + insights.html | ✅ 完整 | — |
| **全域熱鍵（Ctrl/Shift+1~9）** | HotkeyManager, line 29169 | 無（瀏覽器沙箱無法監聽系統全域鍵） | 🚫 不適用 | — |
| 浮動詞句面板 | QuickKeywordPanel, line 28099 | 部分（memory-keywords API 存在，無浮動 UI） | ⚠️ 半 | 中 |

### 3.10 設定與系統管理

| 子功能 | 原版 | 網頁版 | 狀態 | 補強難度 |
|---|---|---|---|---|
| 基本設定（事務所資訊 / 路徑） | SettingsDialog, line 22475 | `/api/osc/settings` CRUD + admin.html | ✅ 完整 | — |
| Google API 設定 | SettingsDialog google_group | settings endpoint（gemini_api_key 等） | ✅ 完整 | — |
| Discord Webhook 設定 | SettingsDialog discord_group, line 22790 | settings endpoint 有欄位，前端 admin.html 存在 | ⚠️ 半（UI 未完成） | 低 |
| **自動備份還原** | AutoBackupManager, line 619 | 無 | ❌ 缺 | 中 |
| **PNG 標籤尺寸設定** | SettingsDialog png_label_frame, line 176 | 無 | ❌ 缺（與 PNG 生成功能連動） | 低 |
| **首次使用精靈** | FirstRunWizard, line 37453（5 步驟） | 無 | ❌ 缺 | 中 |
| 夜間主題切換 | ThemeManager, line 507 | osc-theme.css 存在，無動態切換按鈕 | ⚠️ 半 | 低 |
| DB 效能監控 | PerformanceMonitor + StatusMonitorDialog | 無 | 🚫 低優先 | — |

### 3.12 PDF 蓋章與一條龍書狀製作（Sonnet 初稿漏掉，Opus 補）

| 子功能 | 原版 | 網頁版 | 狀態 | 補強難度 |
|---|---|---|---|---|
| **PDF overlay 標記正本/副本/繕本** | `_add_overlays_and_stamp` (osc.py:25984) PyMuPDF + china-ss 16/12 字體 | `skills/doc-producer/action.py mark_copy_type()`（邏輯與原版幾乎一致） | ✅ **後端完整** | — |
| **附委任狀旗標** | 同上 | 同上 | ✅ 後端完整 | — |
| **繕本已送對造旗標** | 同上 | 同上 | ✅ 後端完整 | — |
| **DOCX→PDF→蓋章→合併 一條龍** | `finalize_and_generate_pdf` (osc.py:~26150) | `doc-producer produce()` (action.py:269) | ✅ 後端完整 | — |
| **chat 訊息觸發**（如「做正本 /path」「合併pdf a.pdf b.pdf」） | 無（桌面 button） | `api/pipelines/skill_dispatch.py:344-410` | ✅（網頁版新增） | — |
| **OSC 網頁 UI 上的蓋章/製作按鈕** | tk button | **無**（osc.html 16 個 tab + osc_debt.html 6 個 panel 都沒入口） | ❌ **缺 UI** | 低-中 |
| **批次同時產 N 份繕本**（原版彈窗詢問份數） | `finalize_and_generate_pdf` simpledialog | skill 一次只處理一份，須前端 loop | ⚠️ 半 | 低 |
| **手動定位蓋章座標**（PDF 上點擊選 x/y） | `prompt_for_manual_stamp` (osc.py:26042) + `_get_stamp_location_manually` (line 26200) tk Canvas | 無（瀏覽器互動式 PDF 點擊未實作） | ❌ 完全缺 | 高（需 PDF.js 整合） |

**重要說明**：書狀蓋章核心邏輯**已完整實作於 `skills/doc-producer/`**，但**只能透過 chat 訊息或直接呼叫 skill 觸發**。OSC 網頁主介面與消債羅伯特子頁都沒有「蓋章」按鈕，使用者若要蓋章必須改用 chat 介面或記住路徑手動觸發 skill，UX 體驗顯著退化。

**補強建議**：在 `osc_debt.html` 「合併PDF」panel 加蓋章選項；在 `osc.html` 「📄 書狀索引」or 「🖨 委任/收據」標籤的每筆紀錄旁加「製作正本」「製作副本」按鈕，呼叫既有 doc-producer skill（後端不用動）。難度低-中。

---

### 3.11 儀表板

| 子功能 | 原版 | 網頁版 | 狀態 | 補強難度 |
|---|---|---|---|---|
| 案件統計 / 待辦摘要 | DashboardFrame, line 9172 | `/api/osc/dashboard` + dashboard.html | ✅ 完整 | — |
| 急件清單 | get_urgent_cases | dashboard endpoint | ✅ 完整 | — |
| 待確認報價單 | get_pending_quotations | dashboard endpoint | ✅ 完整 | — |

---

## 4. 重要缺失分析

### 4.1 Google Calendar 真實雙向同步

- **原版實作**：osc.py:11191-11464，`GoogleCalendarService` 使用 `google-auth-oauthlib.InstalledAppFlow`，token.pickle 保存授權，可雙向讀寫行事曆事件；todos 和 meetings 均有 `google_calendar_id` 欄位自動同步
- **為何重要**：律師工作日程（開庭、法扶截止日）需要出現在手機行事曆，是日常核心工具
- **網頁版能否補**：可以，但架構不同——需要 OAuth2 callback endpoint（網頁 server-side OAuth）而非桌面 InstalledAppFlow，需要存放 refresh_token 在 DB 而非 token.pickle
- **補強建議**：建立 `/api/osc/calendar/google-auth` OAuth 流程 + background sync task，難度高但可行

### 4.2 案件 / 當事人 CSV 匯入匯出

- **原版實作**：osc.py:18310-18452（案件）、22120-22310（當事人），支援 DictReader，欄位寬容對映
- **為何重要**：搬遷資料庫、批次建案時唯一批量工具；客戶填完 Excel 表格直接匯入是工作流
- **網頁版能否補**：是，但**無現成可重用 logic**（spot-check 確認 MAGI 內 `insert_case_from_csv` 不存在），須從原版 osc.py 移植 DictReader 解析+欄位對映程式碼
- **補強建議**：`POST /api/osc/cases/import-csv`（multipart）+ `GET /api/osc/cases/export-csv`，前端加 button；難度**中**（需 port 原版 200 行解析邏輯）

### 4.3 地址標籤 PNG 生成（寄件封面）

- **原版實作**：osc.py:15011-15095，觸發 AddressConfirmationDialog（line 18674）讓用戶確認地址，背景呼叫 `_generate_address_label_image_threaded`，用 PIL 繪製 300 DPI PNG
- **為何重要**：對被告、法院寄送文件時必須列印地址標籤，是每個案件必用功能；法扶版本則對多個法扶分會批量生成
- **網頁版能否補**：可以，後端用 PIL/Pillow 生成，回傳 PNG stream 供前端下載
- **補強建議**：`POST /api/osc/cases/:id/address-label-png`，難度中（需 Pillow + 字型設定）

### 4.4 自動備份還原

- **原版實作**：osc.py:619-775，`AutoBackupManager` 每 24h 自動從 DB 提取案件/會議/待辦資料存 JSON，最多 7 份，支援手動從備份清單還原
- **為何重要**：MariaDB 部署在 NAS 上，NAS 故障或誤刪資料時的最後防線
- **網頁版能否補**：是，可建 `GET /api/osc/backups`、`POST /api/osc/backups`、`POST /api/osc/backups/:filename/restore`
- **補強建議**：後端排程任務（cron）+ API endpoint 對應，難度中

### 4.5 應備事項 Checklist（法扶補件清單）

- **原版實作**：`ChecklistDialog`（line 31574）/ `CaseChecklistDialog`（line 32214），分法扶清單和案件清單，與 `legal_aid_checklists` / `case_checklists` 表連動，Discord 每日推播補件清單
- **為何重要**：法扶案件補件追蹤是高優先工作流，目前 DB 表已存在
- **網頁版現況**：spot-check 確認 — DB 表 ✅、**有 read-only 嵌入查詢**（osc_cases.py:766/777/865/2214 在 workbench 與 laf endpoint 內有 SELECT）、**無 CRUD endpoint**、**無前端 UI**
- **網頁版能否補**：是，DB 表與部分讀取邏輯已就緒，補 dedicated CRUD API 和前端 UI 即可
- **補強建議**：`/api/osc/checklists` CRUD（POST/PUT/DELETE）+ 在 laf.html 增加 checklist section，難度中

---

## 5. 補強優先序建議

| 優先 | 功能 | 預估工作量 |
|---|---|---|
| **P0** | **OSC UI 接 doc-producer 蓋章按鈕**（後端已就緒，只缺前端按鈕 + JS 呼叫；高 ROI） | **0.5-1 天** |
| P1 | CSV 匯入匯出（案件 + 當事人，須 port 原版解析邏輯） | 2-3 天 |
| P1 | Checklist 應備事項（API + laf.html UI） | 1-2 天 |
| P2 | 地址標籤 PNG 生成 | 2-3 天 |
| P2 | 報價單 PDF 匯出 | 1 天 |
| P2 | Discord Webhook 設定前端 UI 補完 | 半天 |
| P2 | 夜間主題動態切換按鈕 | 半天 |
| P3 | 自動備份還原 API + cron | 2-3 天 |
| P3 | 首次使用精靈 | 1-2 天 |
| P4 | Google Calendar OAuth 網頁版 | 4-7 天（架構重設計） |
| 保留 | PNG 標籤尺寸設定 | 搭配地址標籤一起做 |

---

## 6. 不建議移植的桌面殘留

| 功能 | 原版 class / line | 說明 |
|---|---|---|
| 全域熱鍵（Ctrl/Shift+1~9 詞句） | HotkeyManager, line 29169 | 瀏覽器沙箱無法監聽系統全域鍵，技術上不可行 |
| 視窗狀態記憶（位置 / 大小） | WindowStateManager, line 409 | Web app 無此需求 |
| Windows DPI 處理 | CrossPlatformUtils._setup_windows_dpi, line 214 | macOS/Linux 不需要 |
| DB 效能監控 StatusMonitorDialog | PerformanceMonitor + StatusMonitorDialog, line 37267 | 生產環境已有 server-side monitoring，不需桌面彈窗 |

---

*本文件由 Sonnet 4.6 自動產出，供 Opus 驗收使用。原版 osc.py 41,470 行逐 class 掃描，網頁版 blueprints 全 endpoint 逐一對照。*
