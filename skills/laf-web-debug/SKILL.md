---
name: laf-web-debug
description: LAF 法扶律師線上操作系統 (lawyer.laf.org.tw) 自動化除錯指南。涵蓋 Bootstrap modal 陷阱、上傳流程、驗證碼、Selenium 最佳實踐。
author: CASPER
created: 2026-03-29
version: 1.0.0
---

# LAF Web Debug SKILL

法扶律師線上操作系統 (lawyer.laf.org.tw) 的 Selenium 自動化除錯手冊。
適用於開辦、結案、疑義、費用、二階段等所有 workflow。

## 核心檔案

| 檔案 | 用途 |
|------|------|
| `casper_ecosystem/law_firm_orchestrators/laf_automation_v2.py` | Selenium 自動化主體 |
| `casper_ecosystem/law_firm_orchestrators/laf_orchestrator.py` | 業務邏輯/案件比對/欄位推算 |
| `api/orchestrator.py` | DC/TG 指令入口、子程序啟動、通知回調 |
| `api/discord_channel_router.py` | DC 頻道路由（topic_key → channel_id） |
| `casper_ecosystem/law_firm_orchestrators/line_notifier.py` | TG 通知（不送 DC） |

## 已知陷阱與解法

### 1. Bootstrap Modal 堆疊（最常見）

**現象**：上傳成功後 `#dialog-msg` 彈出，底層表單 modal（如 `#dialog-notOpenedReply`）被 Bootstrap 自動隱藏。關閉 `#dialog-msg` 後表單 modal 不會自動恢復。

**後果**：按鈕找不到、截圖空白、送出失敗。

**解法**：
```javascript
// 1. 關閉訊息 dialog
try { $('#dialog-msg').modal('hide'); } catch(e) {}
// 2. 移除殘留 backdrop（可能有多層）
document.querySelectorAll('.modal-backdrop').forEach(bd => bd.remove());
// 3. 恢復表單 modal
var formModals = ['#dialog-notOpenedReply', '#dialog-closingReply',
                  '#dialog-conditionReply', '#dialog-inquiryReply'];
for (var sel of formModals) {
    var fm = document.querySelector(sel);
    if (fm && (fm.style.display === 'none' || fm.style.display === '')) {
        try { $(sel).modal('show'); } catch(e) {}
        break;
    }
}
```

**禁止**：
- 不要呼叫 `closeModal1()`：它會觸發 `$('#dialog-processing').modal('show')` 的副作用
- 不要用 `_click_button_by_text(["關閉"])` 來關 dialog-msg：可能匹配到表單的「確定」按鈕

### 2. 上傳按鈕找不到

**現象**：go_live 頁面沒有 `#upload-button`，closing 頁面沒有 `#btnUpload`。

**解法**：`_click_upload_confirm()` 使用多層策略：
1. CSS 選擇器嘗試：`#upload-button`, `#uploadBtn2`, `#btnUpload`, `button.upload-btn`, `input[type='submit'][value*='上傳']`
2. JS 函數嘗試：`doUpload()`, `startUpload()`, `fileUpload()`, `uploadFile()`
3. 表單提交：`form[action*="upload"].submit()`
4. 文字搜尋：`_click_button_by_text(["上傳", "確認上傳"])`
5. onchange 觸發：`file_input.dispatchEvent(new Event('change', {bubbles:true}))`

### 3. 送出按鈕找不到

**現象**：`_click_button_by_text(["確定"])` 可能匹配到導航選單中含「確定」的連結（如「終止撤銷確定酬金」）。

**解法**：`submit_workflow` 使用三層策略：
1. **JS 直接呼叫**（最可靠）：go_live 用 `doUpdate()`、closing 用 `doSave('toCR')`
2. **按鈕 ID**：`#save_btn`、`#submitBtn`
3. 文字搜尋（最後手段）

**設定方式**：在 `_workflow_meta` 中加入 `submit_js` 清單：
```python
"submit_js": ["doUpdate()"],  # go_live
"submit_js": ["doSave('toCR')"],  # closing
```

### 4. 上傳完成判定

**現象**：go_live 頁面上傳表格沒有 `a[href*='downloadFile']` 連結，導致 `rows > 0` 判定永遠失敗。

**解法**：`_wait_upload_settled` 中，若 alert 包含「上傳成功」則直接判定成功：
```python
if "上傳成功" in alert_msg:
    return {"ok": True, "reason": "alert_success", ...}
```

### 5. 驗證碼 (CAPTCHA)

**機制**：
- ddddocr 辨識 → 第一次登入
- 若失敗，自動重試（最多 3 次）
- 若仍失敗，嘗試沿用既有 session（cookie 未過期時可直接進入）

**除錯**：
- 驗證碼圖片選擇器：`img#kaptchaImage[src*='captcha-image']`
- 登入後 popup 需 dismiss：尋找 `×` 按鈕或 `#closeBtn`
- DOM 摘要保存在 `laf_downloads/login_dom_summary.json`

### 6. file input 隱藏元素

**現象**：法扶網頁隱藏 `<input type="file">`，`send_keys` 無法生效。

**解法**：
```javascript
arguments[0].style.display = 'block';
arguments[0].style.visibility = 'visible';
arguments[0].style.opacity = '1';
arguments[0].style.height = 'auto';
arguments[0].style.width = 'auto';
arguments[0].style.position = 'relative';
```

### 7. 伺服器驗證

**機制**：上傳後用 fetch API 呼叫 `/lafcsp/genUploadFilesView` 確認檔案是否到達伺服器。
- `server_verified: true` → 完全確認
- `server_verified: false` → 可能檔案已到位但 API 回應格式不同，不代表失敗

### 8. 通知重複發送

**原因**：截圖附帶 caption 送出後，`notification_callback` 又送一次文字。

**解法**：
- 截圖送出成功後設 `_screenshot_sent = True`
- `notification_callback` 檢查此旗標，已送則跳過

**平台判定**：`platform_name` 必須 `.lower()` 後比較，否則 `"Discord"` ≠ `"discord"` 會走 else 分支（TG+DC 都送）。

## 除錯工具

### Debug 截圖與 HTML
每個關鍵步驟都會保存：
- `laf_downloads/debug_{workflow}_{tag}_{timestamp}.html` — 完整 DOM
- `laf_downloads/debug_{workflow}_{tag}_{timestamp}.png` — 瀏覽器截圖
- 匯出到 `static/exports/` 供 TG/DC 傳送

### 診斷日誌
Modal restore 會輸出詳細狀態：
```
🔧 Modal restore (submit): msg_hidden;backdrop_removed(1);restored:#dialog-notOpenedReply;
```

### 手動測試指令
```bash
# 開辦暫存（不送出）
cd /Users/ai/Desktop/MAGI
MAGI_PREFER_LOCAL_DB=1 python3 casper_ecosystem/law_firm_orchestrators/laf_orchestrator.py \
  --mode portal-draft --action go_live --client 邱衣萱

# 結案暫存
MAGI_PREFER_LOCAL_DB=1 python3 casper_ecosystem/law_firm_orchestrators/laf_orchestrator.py \
  --mode portal-draft --action closing --client 張蘭心

# 開辦送出（需環境變數）
MAGI_LAF_ALLOW_GO_LIVE_SUBMIT=1 MAGI_PREFER_LOCAL_DB=1 python3 \
  casper_ecosystem/law_firm_orchestrators/laf_orchestrator.py \
  --mode portal-submit --action go_live --client 邱衣萱
```

## 頻道路由對照表

| topic_key | DC 頻道 | 用途 |
|-----------|---------|------|
| `laf_go_live` | #法扶-開辦 | 開辦預覽/確認碼/送出結果 |
| `laf_closing` | #法扶-結案 | 結案暫存/報結確認 |
| `laf_dispatch` | #法扶-派案 | 派案通知 |
| `general` | #一般 | 預設（不應該用到） |

## Workflow Meta 結構

每個 workflow 在 `_workflow_meta()` 中有以下設定：
- `name`：中文名稱
- `url_path`：清單頁路徑
- `apply_selectors`：案號輸入框選擇器
- `name_selectors`：姓名輸入框選擇器
- `search_js`：搜尋按鈕 JS 呼叫
- `report_onclick`：報告按鈕 onclick 前綴
- `draft_js`：暫存按鈕 JS（空 = 不能暫存）
- `draft_buttons`：暫存按鈕文字
- `submit_js`：送出按鈕 JS（最可靠）
- `submit_buttons`：送出按鈕文字（備用）

## 新增 Workflow 檢查清單

1. [ ] 在 `_workflow_meta()` 加入新 workflow 設定
2. [ ] 在 `fill_workflow_fields()` 加入欄位填寫邏輯
3. [ ] 測試 modal 堆疊：上傳後 dialog-msg 是否蓋住表單
4. [ ] 測試按鈕選擇器：draft/submit 按鈕是否正確匹配
5. [ ] 測試通知路由：topic_key 是否正確、是否重複發送
6. [ ] 測試截圖品質：modal restore 後截圖是否包含完整表單
7. [ ] 加入 `discord_channel_map.json` 頻道對應
8. [ ] 加入 `discord_channel_router.py` 關鍵字偵測
