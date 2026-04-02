# LAF 結案報結表單 vs MAGI 實測對照報告 — 2026-03-26

> 測試方式：直接登入正式站 lawyer.laf.org.tw 開啟 張蘭心 (1131017-W-001) 結案表單
> 對照 MAGI `laf_automation_v2.py` fill_closing_report() 邏輯

---

## 一、測試結果摘要

| 項目 | 狀態 |
|------|------|
| 身分查詢 (identity lookup) | ✅ 修復後正常（needs_manual_confirm=false） |
| Page 1 基本欄位 | ⚠️ 大部分正確，缺少結案類型 |
| Page 1 結案類型級聯選單 | ❌ 完全缺失 |
| Page 1 disabled/readonly 欄位 | ⚠️ 部分需要處理 |
| Page 2 次數欄位 | ⚠️ 欄位映射正確，但 readonly 可能阻擋 |
| Page 2 select 欄位 | ✅ 有 disabled 處理 |
| 密碼提醒彈窗 | ❌ 尚未處理 |

---

## 二、Page 1 欄位逐項對照

### ✅ 正確對應的欄位

| Portal 欄位 | name | MAGI 來源 | Portal 值域 | MAGI 值 | 備註 |
|-----------|------|----------|-----------|--------|------|
| 受扶助人身分 | aidcdds | 預設 | SELECT (聲請人/原告/被告...) | 聲請人 | Portal 自動帶入 |
| 准予扶助種類 | aidtype | 預設 | TEXT (readonly) | 訴訟代理及辯護 | Portal 自動帶入 |
| 扶助內容-程序 | aidproc | 預設 | TEXT (readonly) | 消費者債務清理事件 | Portal 自動帶入 |
| 國民法官案件 | is_citizen_judge | 固定 "否" | SELECT (是/否) | 否 | ✅ |
| 繫屬機關類型 | rel_court1 | court_kind | SELECT (法院/檢察署/其他) | 法院 | ✅ 值直接匹配 |
| 繫屬機關名稱 | rel_court2 | court_name | TEXT | 臺灣花蓮地方法院 | ✅ |
| 裁判日期 | judg_dt | judg_dt | TEXT, 7碼台灣日期 | 1150316 | ✅ maxlength=7 |
| 案號-年 | year | court_case_year | TEXT | 114 | ✅ |
| 案號-字別 | relcode | court_case_code | TEXT | 消債更 | ✅ |
| 案號-號 | relno | court_case_no | TEXT | 93 | ✅ |
| 對造姓名 | appellee | 固定 "無" | TEXT | 無 | ✅ |
| 對受扶助人影響 | judg_eff | judg_eff | SELECT | 對受扶助人較不利 | ✅ 值直接匹配 |

### ❌ 缺失的欄位

| Portal 欄位 | name | 類型 | 問題 |
|-----------|------|------|------|
| **結案類型** | **casekd** | HIDDEN SELECT | MAGI 完全沒有設定此級聯選單 |
| 結案類型 Level 1-9 | level1~level9 | HIDDEN SELECT | 同上，級聯子選單 |
| 結案類型顯示文字 | **clcate** | VISIBLE TEXT | 由 setClcate() 從級聯選單組合而成 |
| 結案文件名稱 | cl_docnm | TEXT | 由結案類型選擇過程自動帶入 |
| 訴訟參與 | is_litigation_participation | SELECT | 未設定（可能導致驗證失敗） |
| 調解類型 | medi_type | SELECT | 未設定（消債案件可能不需要） |

### ⚠️ 可能需要處理的欄位

| Portal 欄位 | name | 問題 |
|-----------|------|------|
| judg_dt | judg_dt | 某些狀態下 disabled=true，需 JS 移除 |
| 刑期 | terms | 非刑事案件不需要 |
| 緩刑 | reprieve | 非刑事案件不需要 |

---

## 三、結案類型級聯選單機制（核心缺失）

### 3.1 機制說明

Portal 使用一組隱藏的級聯下拉選單來選擇結案類型：

```
casekd → level1 → level2 → level3 → ... → level8
```

每次選擇後觸發 AJAX：
```
GET /lafcsp/getPLS12ByFnode?category=L41A_1&fnode=<selected_value>
```

返回 JSON 陣列：
```json
[{"seq": "0005", "nodenm": "民/家事案件", "is_enode": "N"}, ...]
```

最終使用者點擊「確定」按鈕，觸發 `setClcate()`，將所有層級文字以「、」串接寫入 `clcate`：
```
例：扶助種類為訴訟代理或辯護、民/家事案件、消債事件程序、更生程序、...
```

### 3.2 張蘭心案件的預期路徑

```
casekd  = "0001" (扶助種類為訴訟代理或辯護)
level1  = "????" (民/家事案件) ← 需要 AJAX 查詢確切 value
level2  = "????" (消債事件程序)
level3  = "????" (更生程序 → 聲請駁回)
```

### 3.3 MAGI 修復方案

**方案 A（推薦）：直接 JS 設值**
```javascript
// 1. 設定隱藏 select 值
document.querySelector('[name="casekd"]').value = "0001";
document.querySelector('[name="level1"]').value = level1_value;
// ...

// 2. 直接設定顯示文字
document.querySelector('[name="clcate"]').value =
  "扶助種類為訴訟代理或辯護、民/家事案件、消債事件程序、更生程序、聲請駁回";
```

**方案 B：觸發 AJAX 級聯**
```javascript
// 模擬完整的級聯選擇流程
$('#casekd').val('0001').trigger('change');
// 等待 AJAX 完成後
$('#level1').val(xxx).trigger('change');
// 重複直到最終層級
```

---

## 四、Page 2 欄位對照

### ✅ 正確映射的欄位

| Portal 欄位 | ID/name | MAGI 來源 | 方法 | 備註 |
|-----------|---------|----------|------|------|
| 面談次數 | meet_times | meeting_count | send_keys | maxlength=2, ⚠️ readonly |
| 電話討論次數 | tel_times | contact_count | send_keys | maxlength=2, ⚠️ readonly |
| 律見次數 | inq_times | inq_count | send_keys | maxlength=2, ⚠️ readonly |
| 書狀次數 | wc_times | document_count | send_keys | maxlength=2, ⚠️ readonly |
| 律師開庭次數 | lawyerap_times | court_count | JS injection | ✅ 正確 |
| 開庭總次數(hidden) | ap_times | court_count | JS injection | ✅ 正確 |
| 閱卷次數(hidden) | viewsheet_times | review_count | JS injection | ✅ 正確 |
| 費用已請領完畢 | islgfee | 固定 "是" | JS (disabled=false) | ✅ 正確 |
| 調解/和解達成 | is_med_by_ly | has_mediation_success | JS (disabled=false) | ✅ 正確 |
| 特別說明 | noarrivereason | zero_reasons | send_keys | ⚠️ readonly |

### ⚠️ readonly 欄位問題

Page 2 的 `meet_times`, `tel_times`, `inq_times`, `wc_times`, `noarrivereason` 在 HTML snapshot 中有 `readonly` 屬性。

MAGI 使用 `elm.clear()` + `elm.send_keys()` 填寫這些欄位，但 Selenium 的 send_keys 在 readonly 欄位上**可能不生效**。

**修復方案：**
```javascript
// 在 send_keys 前移除 readonly
var el = document.getElementById('meet_times');
if (el) { el.readOnly = false; el.removeAttribute('readonly'); }
```

或改為全部使用 JS injection（像 court_count 那樣）。

---

## 五、其他發現

### 5.1 密碼變更提醒彈窗
登入後 Portal 會跳出密碼變更提醒 Modal，MAGI 的 Selenium login 流程需要加入關閉此彈窗的邏輯。

### 5.2 Frameset 架構
Portal 使用 `<frameset>` 架構，主 URL 始終是 `toMainPage`。MAGI 的 Selenium 需要正確切換到內容 frame。

### 5.3 checkData() 表單驗證
Page 1 暫存/下一頁前會觸發 `checkData()` 驗證。如果 `clcate` 為空，驗證會失敗，導致無法存檔。

---

## 六、修復優先順序

1. **P0（必修）**: 加入結案類型 casekd 級聯選單填寫邏輯
2. **P0（必修）**: Page 2 readonly 欄位的 JS 移除處理
3. **P1（重要）**: 密碼變更提醒彈窗的自動關閉
4. **P1（重要）**: judg_dt disabled 狀態的 JS 移除
5. **P2（改善）**: is_litigation_participation 設值（消債案件：否）
