---
name: laf-portal-automation
description: Automates LAF Portal operations (Go-Live, Phase 2, Closing, Fee, Inquiry, Withdrawal) using case data from Synology Drive.
author: CASPER
created: 2026-02-15
updated: 2026-02-22
---

# LAF Portal Automation

This skill allows CASPER to automate reporting tasks on the LAF Portal by reading case files from Synology Drive.

> [!IMPORTANT]
> **「已轉入」** 是法扶官方系統自動設定的狀態，並非律師回報後才出現。
> 律師回報後顯示的是 **「已開辦」**（WF1）或 **「已回報」**（WF5-8）。

## Login

| Element | Selector | Notes |
|---|---|---|
| Username | `#user_id` | e.g. `G122156768` |
| Password | `#user_pass` | |
| Captcha Input | `#capText` | Read captcha from adjacent canvas/image |
| Login Button | `.btn-login` | |

## Navigation Map

| Page | NavigateTo ID | Search Input | Search Action |
|---|---|---|---|
| 遵期開辦 | `toNotOpenedCase` | `#toNotOpenedCase_applynm` | `doSearch('toNotOpenedCase')` |
| 條件是否成就 | `toCndQuery` | `#toCndQuery_applynm` | `doSearch('toCndQuery')` (部分版本也可能是 `searchCndCases()`) |
| 結案 | `toCR` | via `searchCaseList()` | — |
| 費用支付 | `toLGFEEQuery` | `#toLGFEEQuery_applynm` | `doSearch('toLGFEEQuery')` |
| 疑義 | `toReqSubj1Query` | `#toReqSubj1Query_applynm` | `doSearch('toReqSubj1Query')` |
| 撤回 | `toPBQuery` | `#toPBQuery_applynm` | `doSearch('toPBQuery')` |

## Simulator (Offline Training Site)

- Entry: `file:///Users/ai/Desktop/code/laf_training_simulator/index.html`
- Optional server: `/Users/ai/Desktop/code/laf_training_simulator/server.py` (default port `8080`)
- Latest QA screenshots captured by automation: `/Users/ai/Desktop/code/laf_training_simulator/_qa/`
- Snapshot simulator (built from official smoke HTML/PNG):  
  `file:///Users/ai/Desktop/code/laf_training_simulator/snapshot_simulator.html`
- Build snapshot dataset:
  - `python3 /Users/ai/Desktop/code/laf_training_simulator/build_snapshot_simulator.py`
  - `python3 /Users/ai/Desktop/code/laf_training_simulator/train_casper_from_snapshots.py`
- Training payload for CASPER:
  - `/Users/ai/Desktop/code/laf_training_simulator/snapshot_data/casper_laf_training.json`
  - `/Users/ai/Desktop/MAGI/skills/laf-portal-automation/references/snapshot_training.json`

## Natural Language Commands (zh-TW)

- `幫我做開案回報，當事人是蕭仁俊（只填寫不送出）`
- `幫我做二階段回報，當事人是[當事人L]（只暫存不送出）`
- `幫我做疑義回報，當事人是[當事人F]（只暫存不送出）`
- `幫我做訴訟中費用支付，當事人是[當事人E]（只暫存不送出）`
- `幫我做結案回報，當事人是蔡旭欽（只暫存不送出）`
- `打開法扶回報流程，先給我每一步按鈕與欄位，不要送出`

> 預設安全策略：**CASPER 不得直接送出**。若流程中有 `送出/提交/最終送出`，一律停下來詢問你。
> 開案（遵期開辦）例外流程：因正式站無暫存鈕，CASPER 會先截圖回傳，等待你或同事回覆 `正確送出 <確認碼>` 後才送出。

## 案件識別守門（必須遵守）
1. 執行任何 Workflow 前，必須先做「法扶案號 / OSC 案號 / 當事人」多訊號比對。
2. 僅姓名命中不得直接自動化；若缺少案號訊號，必須要求人工確認。
3. 多筆候選同分、或訊號互相衝突（例如案號對上但姓名不符）時，必須阻斷自動化並回報 `identity_needs_manual_confirmation`。
4. 不得以「最近更新資料夾」作為預設 fallback 自動選案；只能在唯一且可驗證時使用。

## Router CLI（訓練匹配）

- 列出已載入樣本：
  - `python /Users/ai/Desktop/MAGI/skills/laf-portal-automation/action.py --list`
- 以自然語言匹配回報流程：
  - `python /Users/ai/Desktop/MAGI/skills/laf-portal-automation/action.py --query "幫我做開案回報，當事人是蕭仁俊（只填寫不送出）"`

## Capability: Case Type & Document Validation
- **Input**: Case Directory in `01_案件/法扶案件/`.
- **Logic**:
  1. **Identify Case Type**:
     - IF Path contains `消費者債務清理`: **Type 1 (Debt)**.
     - ELSE: **Type 2 (General)**.

  2. **Retrieve & Validate Documents**:
     - **Type 1 (Debt)**:
       - **Required**: `*開辦通知書*.pdf` (Opening Notice).
       - **Validation**: Check for **Handwritten Date** (手寫日期) on the Notice.
       - **Date Source**: Use the **Handwritten Date**.
       - *Note*: Appointment Letter (`委任狀`) is NOT required.

     - **Type 2 (General)**:
       - **Required**: `*開辦通知書*.pdf` AND `*委任狀*.pdf` (Appointment Letter).
       - **Validation**:
         - Notice must have **Handwritten Date**.
         - Appointment Letter must have **Court Round Seal** (法院圓戳章).
       - **Date Source**: Use the **Round Seal Date** from the Appointment Letter.

  3. **Escalation (Human-in-the-Loop)**:
     - IF documents missing OR validation fails OR dates ambiguous:
     - **STOP** automation.
     - **Notify ADMIN via LINE**: `[ALERT] {CaseID}: Validation Failed ({Reason}). Requesting confirmation.`

---

## Workflow 1: Go-Live (遵期開辦)

**Trigger**: 自動 — by email or orchestrator
**Page**: `navigateTo('toNotOpenedCase')`

| Step | Action | Selector / Function |
|---|---|---|
| 1 | Search | `#toNotOpenedCase_applynm` → `doSearch('toNotOpenedCase')` |
| 2 | Click 回報 | `showNotOpenedDialog('{applyno}')` |
| 3 | Set 回報狀態 | `#toNotOpenedCase_selResult` → `1` (已開辦) or `3` (暫緩) |
| 4 | Upload 文件 | Click `＋ 上傳文件` → attach file |
| 5 | Enter 說明 | `#noc_remark` |
| 6 | Save policy | 遵期開辦頁沒有正式「暫存」按鈕；僅允許填寫/上傳到可驗證狀態，**不得按送出** |

**Modal fields (read-only)**: 分會別, 申請編號, 受扶助人姓名, 承辦人電話與分機, 扶助程序, 扶助事項, 接案日期, 傳送日期.

---

## Workflow 5: Two-Stage (消費者債務 — 附條件回報)

**Page**: `navigateTo('toCndQuery')`

| Step | Action | Selector / Function |
|---|---|---|
| 1 | Search | `#toCndQuery_applynm` (or `#cnd_applynm`) → `searchCndCases()` |
| 2 | Open Detail | `showCndDetail('{applyno}')` |
| 3 | Set 啟動是否達體現法建議 | `name="at_ctype"` → e.g. `附條件審查` |
| 4 | Enter 附條件原因 | `name="conditionrsn"` → e.g. `調解不成立，對方不同意` |
| 5 | Upload 文件 | Click `上傳` button |
| 6 | Save | 只可使用 `doSave('toCnd')` / 存檔類按鈕；**不得 `doFinalSave`** |

---

## Workflow 6: Fee Payment (訴訟中費用支付)

> [!IMPORTANT]
> **由律師主動告知 CASPER 處理**，非自動觸發。
> CASPER 需先詢問律師「原因 / 費用說明」，再自動判斷主旨選項。

**Trigger**: 律師主動告知 CASPER（如 LINE 訊息 `[當事人E] 費用支付 裁判費2000元`）
**Page**: `navigateTo('toLGFEEQuery')`

### CASPER 判斷邏輯

律師提供原因 → CASPER auto-select:

| 律師關鍵字 | `#lgfee_reqsubj1` 值 | `#lgfee_reqsubj2` 值 |
|---|---|---|
| 裁判費 | `0116` (訴訟費用及必要費用之處理) | `0120` (支付裁判費) |
| 鑑定費 | `0116` | 新鑑費用及必要費用之處理 |
| 其他 / 未明 | `0116` | `其他` |

| Step | Action | Selector / Function |
|---|---|---|
| 1 | Ask 律師: 需要填寫的費用說明 | LINE/DC |
| 2 | Search | `#toLGFEEQuery_applynm` → `doSearch('toLGFEEQuery')` |
| 3 | Click 回報 | `toReport('toLGFEEQuery', '{applyno}')` |
| 4 | Auto-select 主旨 | `#lgfee_reqsubj1`, `#lgfee_reqsubj2` (by keyword) |
| 5 | Enter 費用說明 | `#lgfee_desc` ← 律師提供的原因 |
| 6 | Upload 憑證 | Click `上傳憑證/單據` |
| 7 | Save | 只可用存檔/暫存；**不得 `doFinalSave('toLgfee')`** |

---

## Workflow 7: Inquiry (對扶助案件有疑義)

> [!IMPORTANT]
> **由律師主動告知 CASPER 處理**，非自動觸發。
> CASPER 需先詢問律師「疑義原因」，再自動判斷主旨選項。

**Trigger**: 律師主動告知 CASPER（如 `[當事人F] 有疑義 資力不合`）
**Page**: `navigateTo('toReqSubj1Query')`

### CASPER 判斷邏輯

律師提供原因 → CASPER auto-select:

| 律師關鍵字 | `#rsm_reqsubj2` 值 |
|---|---|
| 資力不合 / 經濟能力 | `0007` (資力不合標準) |
| 顯無理由 / 案件不可能 | `0008` (案件顯無理由) |
| 終止 / 撤止 | `0009` (有終止事由) |
| 其他 / 未明 | `0117` (其他) |

`#rsm_reqsubj1` 固定 → `0001` (對案件之扶助有疑義)

| Step | Action | Selector / Function |
|---|---|---|
| 1 | Ask 律師: 疑義原因和詳細描述 | LINE/DC |
| 2 | Search | `#toReqSubj1Query_applynm` → `doSearch('toReqSubj1Query')` |
| 3 | Click 回報 | `toReport('toReqSubj1Query', '{applyno}')` |
| 4 | Auto-select 主旨 | `#rsm_reqsubj1` = `0001`, `#rsm_reqsubj2` (by keyword) |
| 5 | Enter 問題概述 | `#rsm_desc` ← 律師提供的原因 |
| 6 | Set 辦理情形 | `name="rsm_lawyer_status"` radio: `N`/`P`/`F` |
| 7 | Upload | Click upload button |
| 8 | Save | 只可用存檔/暫存；**不得 `doFinalSave('toRSM')`** |

---

## Workflow 8: Withdrawal (受扶助人撤回)

> [!IMPORTANT]
> **由律師主動告知 CASPER 處理**，非自動觸發。
> CASPER 需先詢問律師「撤回原因」，再自動判斷選項。

**Trigger**: 律師主動告知 CASPER（如 `蕭月玲 撤回 申請人自行撤回`）
**Page**: `navigateTo('toPBQuery')`

### CASPER 判斷邏輯

律師提供原因 → CASPER auto-select:

| 律師關鍵字 | `#pb_reason` 值 |
|---|---|
| 自行委任 / 自己請律師 | `自行委任律師` |
| 不配合 / 不處理 | `不願配合辦理` |
| 撤回 / 申請人撤 | `申請人撤回申請` |
| 其他 / 未明 | `其他` |

| Step | Action | Selector / Function |
|---|---|---|
| 1 | Ask 律師: 撤回原因 | LINE/DC |
| 2 | Search | `#toPBQuery_applynm` → `doSearch('toPBQuery')` |
| 3 | Click 回報 | `toReport('toPBQuery', '{applyno}')` |
| 4 | Auto-select 撤回原因 | `#pb_reason` (by keyword) |
| 5 | Enter 撤回日期 | `#pb_date` |
| 6 | Set 辦理情形 | `name="pb_lawyer_status"` radio: `N`/`P`/`F` |
| 7 | Upload 撤回書 | Click upload button |
| 8 | Save | 只可用存檔/暫存；**不得 `doFinalSave('toPB')`** |

---

## Workflow 9: Closing Report with Admin Confirmation (報結確認流程)

> [!CAUTION]
> **報結一律使用「暫存」(`doSave`)；CASPER 不執行 `doFinalSave`。**
> 暫存後由律師本人在正式站手動送出。

> [!IMPORTANT]
> 當任何一項次數 < 1 時（如閱卷 0 次、開庭 0 次），在律師確認「沒錯，就是 0 次」後，
> CASPER 必須追問：**「請問空白欄位要填寫什麼理由？（如：未閱卷 / 未開庭）」**，
> 以律師回覆的理由文字填入頁面上的空白欄位。

**Trigger**: `python laf_orchestrator.py --mode closing`

| Step | Action | Tool |
|---|---|---|
| 1 | Query DB for `已結案` LAF cases | `_get_pending_closing_cases()` |
| 2 | Gather counts: 開會/聯繫/開庭/書狀/閱卷 | `_gather_case_counts()` |
| 3 | Send counts to 律師 | `LAFNotifier.send_closing_confirmation()` |
| 4 | If count < 1 and admin confirms 0 → **追問理由** | `on_admin_response()` |
| 5 | Parse: `OK`→暫存; `聯繫 2`→update; `暫停`→skip | Admin response |
| 6 | Fill portal form + **暫存** (not 送出) | `doSave('toCR')` ← 只暫存！ |
| 7 | Notify 律師: 已暫存，請確認後送出 | LINE/DC |
| 8 | 律師最終送出 | 由律師本人在正式站手動送出（CASPER 僅保留草稿/暫存） |
| 9 | Log result | `INSERT INTO laf_lifecycle_log` |

### 自動檢附文件（辦理進度/結案）

CASPER 依 workflow 套用不同上傳策略：

1. `inquiry / withdrawal / closing`
   - `04_我方歷次書狀`（含子資料夾）所有檔案轉 PDF 後上傳
   - `10_判決書`（含子資料夾）所有 PDF 上傳
2. `condition`（二階段）
   - **只上傳**「調解不成立證明書」
3. `fee`（訴訟中費用支付）
   - **只上傳**法院收據（粉紅收據）
4. 若頁面顯示「目前已有回報資料正在處理中」
   - 視為既有草稿進行中，CASPER 只截圖/留 HTML，**不重複送出新草稿**

> [!IMPORTANT]
> 法扶各流程的草稿按鈕名稱統一視為「**存檔**」（可相容舊頁面「暫存/保存/儲存」文字），
> 但安全策略仍是：**只存檔，不送出**。

### Admin Response Flow (count < 1)
```
CASPER: 📋 [當事人E] (1140910-E-010) 報結確認
        開會: 2  聯繫: 3  開庭: 1  書狀: 4  閱卷: 0 ⚠️
        請確認以上資料是否正確

律師:   OK

CASPER: ⚠️ 閱卷次數為 0，請問空白欄位要填寫什麼理由？
        （範例：未閱卷 / 本案無閱卷必要 / 僅閱電子卷宗）

律師:   未閱卷

CASPER: ✅ 收到。已暫存報結資料（閱卷: 0，理由: 未閱卷）。
        請至法扶平台確認後送出，或回覆「送出」由 CASPER 代為送出。

律師:   我自行送出

CASPER: ✅ 已記錄為「待你手動送出後完成」。
```

---

## Test Cases (Simulator Mock Data)

| Case | Client | Workflow | Page |
|---|---|---|---|
| 1150206-A-042 | 蕭仁俊 | WF1 Go-Live | `toNotOpenedCase` |
| 1131224-T-022 | [當事人L] | WF5 Two-Stage | `toCndQuery` |
| 1140910-E-010 | [當事人E] | WF6 Fee Payment | `toLGFEEQuery` |
| 1140909-W-007 | [當事人F] | WF7 Inquiry | `toReqSubj1Query` |
| 1140728-K-002 | 蕭月玲 | WF8 Withdrawal | `toPBQuery` |
