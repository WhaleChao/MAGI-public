# 法扶 Portal — 未結案件進度 (progress) 欄位實測對照

> 建立日期：2026-04-17  
> 來源：`scratch/explore_laf_progress_page.py` 實際登入律師帳號讀取 DOM（read-only，無填表、無送出）  
> 快照：`.runtime/debug_screenshots/progress_explore_*.html|png`

---

## 一、路徑

| 項目 | 值 |
|-----|----|
| URL (搜尋頁) | `/lafcsp/toNotClosedCase` |
| 表單 action | `/lafcsp/caseClosedReply` (POST，與結案共用 endpoint) |
| 表單容器 | Modal `#dialog-notClosedReply` (`modal-xl`) |
| 選單上一級 | 線上回報 → 未結案件進度 |
| 對應動作 | `progress` (新增到 `_workflow_meta`) |

---

## 二、搜尋頁

### 輸入欄位（與其他回報共通）

| 欄位 | input name/id | 型別 | 備註 |
|-----|--------------|------|-----|
| 申請編號 | `applyno` / `#applyno` | text | 13 碼格式如 `1150320-E-014` |
| 受扶助人姓名 | `applynm` / `#applynm` | text | maxlength=50 |
| 識別證號 | `applyid` / `#applyid` | text | 可選 |

### 搜尋觸發

- 按鈕：`<a class="btn btn-primary" onclick="showList()">開始搜尋</a>`
- JS：`showList()`（三欄位必須擇一填入，案號若填則必須 13 碼）

### 結果列表

- 表格 id：`#dataTables-example`
- 每列「回報」按鈕 onclick：`goReply(dataIdx, crtnm, applyno, applynm, aidproc, aidcontent, clerkInfo, finddt, replyDt, replyStatus)`
- 點擊後 jQuery 將參數填入 modal 的 span/input，然後 `$('#dialog-notClosedReply').modal('show')`。

---

## 三、未結案件進度回報 Modal（核心表單）

### 表單 id / action

- `<form method="post" id="form2" name="form2" action="/lafcsp/caseClosedReply">`
- 送出 JS：`doUpdate()` → 內部驗 `selResult` 與 `selRemark` 非空 + 字數 ≤ 500 bytes (~250 中文字) → `$('#form2').submit()`。

### 顯示欄位（唯讀，由 goReply 填入 span）

| 欄位 | span id |
|-----|---------|
| 分會別 | `#selCrtnm` |
| 申請編號 | `#selApplynoStr` |
| 受扶助人姓名 | `#selApplynm` |
| 承辦人電話與分機 | `#selClerkInfo` |
| 扶助程序 | `#selAidproc` |
| 扶助事項 | `#selAidcontent` |
| 接案日期 | `#selFinddt` |
| 傳送日期 | `#selReplyDt` |

### 隱藏欄位

| 欄位 | id / name |
|-----|-----------|
| 當前案號 | `#selApplyno` / `selApplyno` |
| 查詢條件回填 | `#queryApplyno` / `#queryApplynm` / `#queryApplyid` |
| reply_id（後端帶回） | `#reply_id` |

### 可寫欄位

#### 回報狀態（**必填**，紅字標 required）

| input | type | 選項 |
|-------|------|------|
| `selResult` / `#selResult` | select | `""`=請選擇；**`"1"`=已辦理完成**；**`"2"`=尚未辦理完成**；**`"3"`=應行終止、撤銷、撤回、換律師、移轉並換律師** |

> **MAGI 約束**：本任務是「進度回報」，**必須固定選 `"2"` 尚未辦理完成**。  
> `"1"` 已辦理完成應改走 `closing` workflow；`"3"` 終止/撤回走 `withdrawal` workflow。

#### 說明（**必填**，紅字標 required）

| input | type | 限制 |
|-------|------|------|
| `selRemark` / `#selRemark` | textarea 4×80 | ≤ 500 bytes（~250 中文字） |

> portal 顯示的舉例：「9/12 與受扶助人實質討論案情、9/13 遞送委任狀至繫屬機關、9/14 遞出相關書狀。」  
> MAGI 組 remark 格式：`{民國日期} 收受最後一份裁定，{民國日期} 提出書狀`  
> （若兩件事都存在，可直接字串相接；若僅其一，省略另一句）

### 檔案上傳

| 元素 | 值 |
|-----|---|
| 觸發按鈕 | `<input id="uploadBtn" onclick="linkUpload('NOT_CLOSE')">上傳文件` |
| context key | `'NOT_CLOSE'`（與其他 workflow 的 context 鍵不同：go_live 是 `'NOT_OPENED'`，結案是 `'CLOSED'` 之類） |
| 反映清單 | `<div id="uploadDocnms">`（已上傳檔名顯示於此） |
| AJAX post 到 | 與 go_live 相同的 upload endpoint（`fd.append('applyno', uploadApplyno.value)` 後 POST） |

> 上傳單次 ≤ 8MB（portal 規則），本任務兩個 PDF 視檔案大小可能要分次上傳。

### 送出按鈕

| 按鈕 | onclick | 備註 |
|-----|---------|-----|
| 確定 | `doUpdate()` | **僅有此按鈕。無暫存/存檔/儲存**。 |
| 關閉 (modal footer) | `data-dismiss="modal"` | 關閉 modal 不送出 |

> **重要**：本頁面與 `go_live` 相同，**無 draft 存檔機制**。MAGI 若要「先預覽再送出」必須採用 go_live 的 `confirm_token + screenshot` 兩階段確認流程（**不是** close/condition/inquiry 那種直接 `doSave()` 暫存）。

---

## 四、實作摘要（供 Sonnet 接線 `_workflow_meta`）

```python
"progress": {
    "name": "未結案件進度",
    "url_path": "/lafcsp/toNotClosedCase",
    "apply_selectors": ["#applyno", "input[name='applyno']"],
    "name_selectors": ["#applynm", "input[name='applynm']"],
    "search_js": ["showList()"],
    "report_onclick": ["goReply("],        # row 按鈕 onclick pattern
    "expected_token": "dialog-notClosedReply",  # 出現即表示 modal 已開
    # 本表單無 draft，保持與 go_live 相同：不提供 draft_js。
    "draft_js": [],
    "draft_buttons": [],
    # 送出函數 — draft-only / confirm_token 模式下「絕不呼叫」
    "submit_js": ["doUpdate()"],
    "submit_buttons": ["確定"],
    # 表單欄位（供 save_progress_draft 填入）
    "form_fields": {
        "result_select": "#selResult",       # 固定填 "2" 尚未辦理完成
        "remark_textarea": "#selRemark",     # 組 remark 塞入
        "upload_js": "linkUpload('NOT_CLOSE')",
    },
    # 需要的兩階段確認碼模式（同 go_live）
    "requires_confirm_token": True,
    "confirm_token_ttl_sec_env": "MAGI_LAF_PROGRESS_CONFIRM_TTL_SEC",  # 預設沿用 1800s
}
```

### Draft-mode 偽暫存流程（因無 draft，只填不送）

1. `open_workflow_report_page("progress", laf_case_no, client_name)` — 登入、搜尋、點 `goReply(...)`、modal 打開。
2. 填 `#selResult` → value `"2"`。
3. 填 `#selRemark` → 組好的 remark 字串。
4. 開啟 upload modal（`linkUpload('NOT_CLOSE')`）→ 逐一 `set_input_files` → 按「上傳」→ 等 `uploadDocnms` 出現檔名。
5. **截圖整個 modal** → 存 `.runtime/debug_screenshots/progress_{case_no}_{ts}.png`。
6. **絕不呼叫 `doUpdate()`**；直接 `driver.execute_script("$('#dialog-notClosedReply').modal('hide');")` 關 modal 離開。
7. 回傳 `{ success: True, screenshot: ..., payload: ... }` 給 orchestrator，orchestrator 註冊 `register_laf_progress_submit_pending()` 產生 confirm_token，通知律師。

### Portal-submit 模式（確認後真送出）

1. 同上步驟 1-4（重新登入 + 打開 modal + 填欄位 + 上傳檔案）。
2. 這次直接 `execute_script("doUpdate();")`。
3. 等 modal `#dialog-msg` 出現「存檔成功」或類似訊息；解析 `#msgPart` 文字。
4. 回傳結果給 orchestrator → update `legal_aid_status`（或新欄位 `last_progress_report_at`）。

---

## 五、發現清單（必讀）

1. **表單 action 與結案共用** `/lafcsp/caseClosedReply` — 後端以 `selResult` 值 + 路徑 token 區分。勿以 action URL 判斷回報類型。
2. **`selRemark` 字數上限 500 bytes**（`getBLen2()` 中文算 2 bytes）。MAGI 組 remark 超過 250 中文字要切。
3. **`replyStatus==1` 時 `#selReplyStatus` 會被 disabled** — 表示案件已曾回報「已辦理完成」，這種狀態下不應再用 progress 流程；應在 dispatch 前檢查該案是否可回報。
4. **`suspendPart1` 區塊包著 `selRemark`** — `changeStatus()` 在部分狀態下可能隱藏，Sonnet 實作時必須先選 `selResult` 再填 remark，確保區塊可見。
5. **同一案件多次回報**：portal 允許；每次新增 `reply_id`。MAGI 可以針對同一案件每月定期觸發，不需去重。

---

## 六、未觸及（留給 Sonnet 實作驗證）

本次探勘未執行「實際選 selResult + 填 remark + 送出」，以下細節需 Sonnet 在 `portal-submit` 模式第一次 live 驗收時確認：

- `changeStatus()` 在 selResult="2" 下是否有額外欄位出現（`suspendPart1` 是 show 還是 hide）？— 從 code 路徑看應該 show。
- 送出成功後的回應頁面結構（是否跳 modal `#dialog-msg` with `存檔成功`、或 redirect 到列表）。
- 上傳檔案成功後 `uploadDocnms` 顯示格式。
- 同一案已有 pending reply 時重開 modal 是否會載入既有 remark（goReply 中 `$("#selRemark").val(document.forms[0].remark2[dataIdx].value)` 暗示會）。

---

（完）
