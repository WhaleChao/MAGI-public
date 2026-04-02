# LAF 模組程式碼修改紀錄 — 2026-03-26

> 修改目的：對齊 MAGI LAF 模組與實際法扶律師線上操作系統 (lawyer.laf.org.tw)
> 驗證方式：登入實際網站比對 HTML + 保存的 HTML snapshot 交叉確認
> 涉及檔案：`laf_orchestrator.py`、`laf_automation_v2.py`

---

## 一、修改總覽

| # | 類別 | 修改內容 | 影響檔案 | 依據來源 |
|---|------|---------|---------|---------|
| 1 | 欄位名修正 | Radio `pb_lawyer_status` → `lawy_status` | automation_v2 | withdrawal HTML snapshot |
| 2 | 欄位名修正 | Radio `rsm_lawyer_status` → `lawy_status` | automation_v2 | inquiry HTML snapshot |
| 3 | 欄位名修正 | Fee 表單無 `lawy_status` radio | automation_v2 | fee HTML snapshot |
| 4 | Option 值修正 | `_match_fee_type` 回傳值改為 portal 實際 option value | orchestrator | fee HTML snapshot |
| 5 | 新增欄位 | Fee reqsubj3（第三層 select） | orchestrator + automation_v2 | fee HTML snapshot |
| 6 | 新增 mapping | Inquiry 新增管轄 `0010` | orchestrator | inquiry HTML snapshot |
| 7 | 新增欄位 | Inquiry `comments` textarea | automation_v2 | inquiry HTML snapshot |
| 8 | Identity Bug | 衝突過濾器誤殺空 DB 欄位 | orchestrator | 測試案例：[當事人N] |
| 9 | Identity Bug | 前綴名字比對（外籍姓名後綴） | orchestrator | 測試案例：[當事人N]Ayka lku |
| 10 | Identity Bug | Sole-candidate 放寬 auto-proceed | orchestrator | 測試案例：張蘭心 |
| 11 | Identity Bug | 早期 flag 未清除 | orchestrator | 測試案例：張蘭心 |

---

## 二、各項修改依據

### 修改 1: Withdrawal radio 名稱

**問題：** MAGI 用 `pb_lawyer_status`，portal 實際 name 為 `lawy_status`

**依據 HTML snapshot：**
```
檔案：laf_official_visual_smoke_20260222_084116/pages/withdrawal_084137_report_detail.html
```

HTML 中的 radio：
```html
<input type="radio" name="lawy_status" value="N"> 尚未辦理或本次回報事項與辦理情形無關
<input type="radio" name="lawy_status" value="P"> 辦理中
<input type="radio" name="lawy_status" value="F"> 訴訟程序已結案或律師已辦理完成
```

**修改位置：** `laf_automation_v2.py` ~line 3772
```python
# 修改前：
self._set_radio_value("pb_lawyer_status", _pb_status)

# 修改後：先嘗試 lawy_status，失敗再 fallback
if not self._set_radio_value("lawy_status", _pb_status):
    self._set_radio_value("pb_lawyer_status", _pb_status)
```

---

### 修改 2: Inquiry radio 名稱

**問題：** MAGI 用 `rsm_lawyer_status`，portal 實際 name 為 `lawy_status`

**依據 HTML snapshot：**
```
檔案：laf_official_visual_smoke_20260219_120326/pages/inquiry_120550_report_detail.html
```

HTML 中的 radio 同 withdrawal — 三個表單共用 `lawy_status` name。

**修改位置：** `laf_automation_v2.py` ~line 3814
```python
# 修改前：
self._set_radio_value("rsm_lawyer_status", status)

# 修改後：
if not self._set_radio_value("lawy_status", status):
    if not self._set_radio_value("rsm_lawyer_status", status):
        _set_any(["#rsm_lawyer_status", "select[name='rsm_lawyer_status']"], status)
```

---

### 修改 3: Fee 表單無 lawy_status

**問題：** MAGI 嘗試設定 `lgfee_lawyer_status` radio，但 fee 表單根本沒有此 radio

**依據 HTML snapshot：**
```
檔案：laf_official_visual_smoke_20260219_120326/pages/fee_120742_report_detail.html
```

搜尋整個 HTML 無任何 `lawy_status` 或 `lgfee_lawyer_status` radio。Fee 表單只有 reqsubj select 和 reqdesc textarea。

**修改位置：** `laf_automation_v2.py` ~line 3859
```python
# 修改後：加 defensive check，有才設
_fee_status = data.get("lgfee_lawyer_status") or data.get("lawy_status")
if _fee_status:
    if not self._set_radio_value("lawy_status", str(_fee_status)):
        self._set_radio_value("lgfee_lawyer_status", str(_fee_status))
```

---

### 修改 4: Fee _match_fee_type 回傳值

**問題：** 原程式回傳 `'其他'` 和 `'新鑑費用及必要費用之處理'`，這些不是 portal 的 option value

**依據 HTML snapshot：**
```
檔案：laf_official_visual_smoke_20260219_120326/pages/fee_120742_report_detail.html
```

Portal 實際 select option values：
```html
<select name="reqsubj1">
  <option value="0116">訴訟費用及必要費用之處理</option>
</select>
<select name="reqsubj2">
  <option value="0120">支付裁判費</option>
  <option value="0121">支付裁判費以外之費用</option>
</select>
```

**修改位置：** `laf_orchestrator.py` ~line 1520
```python
# 修改前：
return subj1, '新鑑費用及必要費用之處理'
return subj1, '其他'

# 修改後：
return subj1, '0121'  # 支付裁判費以外之費用
```

---

### 修改 5: Fee reqsubj3（第三層 select）

**問題：** 當 reqsubj2 = 0120（支付裁判費）時，portal 會顯示第三層 reqsubj3 select，MAGI 完全沒有處理

**依據 HTML snapshot：**
```
檔案：laf_official_visual_smoke_20260219_120326/pages/fee_120742_report_detail.html
```

Portal 第三層 select：
```html
<select name="reqsubj3">
  <option value="0132">三千元以下之訴訟費用律師聲請本會墊付</option>
  <option value="0133">訴訟救助或訴訟救助之抗告被駁</option>
  <option value="0134">法院不駁回訴訟救助直接命補繳訴訟費用</option>
  <option value="0135">原已經准予訴訟救助法院仍命補繳訴訟費用</option>
  <option value="0136">律師未聲請訴訟救助導致法院命補繳訴訟費用</option>
</select>
```

**修改位置：**
- `laf_orchestrator.py` ~line 3165：自動補 reqsubj3 預設值
- `laf_automation_v2.py` ~line 3859：新增 reqsubj3 填入邏輯

---

### 修改 6: Inquiry 新增管轄 0010

**問題：** Portal 的 reqsubj2 有 5 個選項，MAGI 只 mapping 了 4 個（缺 0010 管轄）

**依據 HTML snapshot：**
```
檔案：laf_official_visual_smoke_20260219_120326/pages/inquiry_120550_report_detail.html
```

Portal 完整 reqsubj2 options：
```html
<select name="reqsubj2">
  <option value="0007">資力不合標準</option>
  <option value="0008">案件顯無理由或其他不應扶助者</option>
  <option value="0009">有終止事由</option>
  <option value="0010">本案管轄有問題</option>
  <option value="0117">其他</option>
</select>
```

**修改位置：** `laf_orchestrator.py` ~line 1508
```python
# 新增：
if any(k in r for k in ('管轄', '移轉管轄', '移送')):
    return '0010'
```

---

### 修改 7: Inquiry comments textarea

**問題：** Portal 有 `comments` textarea（律師意見），MAGI 沒有填入

**依據 HTML snapshot：**
```
檔案：laf_official_visual_smoke_20260219_120326/pages/inquiry_120550_report_detail.html
```

```html
<textarea name="comments" id="comments" ...></textarea>
```

**修改位置：** `laf_automation_v2.py` ~line 3804

---

### 修改 8-11: Identity Lookup Bugs

這些不是 portal 欄位問題，而是 `_lookup_case_identity` 的邏輯 bug，在實際測試兩個案件時發現：

#### Bug 8: 衝突過濾器誤殺空 DB 欄位

**問題：** 如果 DB 中 `legal_aid_number` 是空的，原邏輯 `c_laf != req_laf` 會判定為「不符」而拒絕該候選人。但空值應該是「未知」而非「衝突」。

**測試案例：** [當事人N] — DB 可能未儲存法扶案號，但 candidate 本身是正確的

**修改位置：** `laf_orchestrator.py` ~line 2241
```python
# 修改前：
if req_laf and c_laf != req_laf:
# 修改後：
if req_laf and c_laf and c_laf != req_laf:
```

同理 case_number 和 client_name 的比對也加了空值判斷。

#### Bug 9: 前綴名字比對

**問題：** [當事人N]在 DB 存的是 `[當事人N]Ayka lku`，用 `--client [當事人N]` 查詢時精確比對失敗

**修改位置：** `laf_orchestrator.py` ~line 2214（新增 LIKE prefix 查詢）+ line 2246/2266（衝突過濾和計分支援 startswith）

#### Bug 10: Sole-candidate 放寬

**問題：** 張蘭心只有 1 筆案件，DB 唯一比對成功，但因 `require_case_signal_for_auto` 要求必須有 LAF 或 case number signal 才能 auto-proceed

**修改位置：** `laf_orchestrator.py` ~line 2334
```python
_sole_candidate_client_match = (
    len(filtered) == 1
    and "client_name" in matched
    and top.get("laf_case_number")
)
```

#### Bug 11: 早期 flag 未清除

**問題：** Line 2162 在 DB 查詢前就設了 `needs_manual_confirm = True`（因沒有 --laf-case-no），即使後面 DB 唯一比對成功也不會清除

**修改位置：** `laf_orchestrator.py` ~line 2355
```python
elif _sole_candidate_client_match:
    out["needs_manual_confirm"] = False
    out["manual_reason"] = ""
```

---

## 三、HTML Snapshot 來源索引

供模擬站使用的 HTML 原始檔案：

| 表單類型 | Snapshot 路徑 | 說明 |
|---------|-------------|------|
| Closing Page 1 | `laf_official_visual_smoke_20260222_025537/pages/closing_025612_closing_page1_toCR.html` | 結案第一頁完整 HTML |
| Closing Page 2 | `laf_official_visual_smoke_20260222_025537/pages/closing_025620_closing_page2_toClosedSummaryLawyer.html` | 結案第二頁 |
| Inquiry | `laf_official_visual_smoke_20260219_120326/pages/inquiry_120550_report_detail.html` | 疑義回報 |
| Fee | `laf_official_visual_smoke_20260219_120326/pages/fee_120742_report_detail.html` | 費用回報 |
| Withdrawal | `laf_official_visual_smoke_20260222_084116/pages/withdrawal_084137_report_detail.html` | 撤回 |
| Go Live | `laf_official_visual_smoke_20260219_120326/pages/go_live_120338_report_detail.html` | 遵期開辦 |
| Condition | `laf_official_visual_smoke_20260219_120326/pages/condition_120358_report_detail.html` | 條件成就 |

這些 snapshot 位於：
```
casper_ecosystem/law_firm_orchestrators/laf_official_visual_smoke_*/pages/
```

---

## 四、結構化欄位提取資料

供模擬站程式使用的完整欄位定義：

| 檔案 | 格式 | 用途 |
|-----|------|------|
| `docs/laf_portal_evidence/laf_form_fields.json` | JSON | 完整欄位資料（按表單分類） |
| `docs/laf_portal_evidence/laf_form_fields_structured.json` | JSON | 按欄位類型重組 |
| `docs/laf_portal_evidence/laf_form_fields.csv` | CSV | 扁平表格（426 筆欄位） |
| `docs/LAF_PORTAL_FIELD_MAP_20260326.md` | Markdown | 人類可讀欄位對照表 |

---

## 五、Patch 檔案

| 檔案 | 涉及程式 |
|-----|---------|
| `patch_orchestrator.diff` | `laf_orchestrator.py` 的所有修改 |
| `patch_automation.diff` | `laf_automation_v2.py` 的所有修改 |

套用方式：
```bash
cd ~/Desktop/MAGI
git apply patch_orchestrator.diff
git apply patch_automation.diff
```
