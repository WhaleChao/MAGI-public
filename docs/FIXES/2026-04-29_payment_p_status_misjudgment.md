# 2026-04-29 — 閱卷繳費通知 `p_status='Y'` 誤判已繳費 → DC 不上傳 PDF

## 表象
- 使用者：「閱卷雖然會通知，但不會在 DC 上傳繳費單給我」
- 4/10 後 OLA 偵測到的所有新待繳費案件 PDF 都沒送到 TG/DC
- 受影響案件（log 證實）：黃珊珊 114.原訴.000084（4/10 13:10）、陳文明 115.原訴.000036（4/29 10:57）

## 根因
`casper_ecosystem/law_firm_orchestrators/file_review_automation.py` 的 `_has_payment_proof_uploaded()`（commit 471ada7, 2026-04-08 加入）把 `p_status == 'Y'` 當作「已繳費」判定條件之一：

```python
if paystatus == "1" or p_status == "Y" or payment == "Y":
    paid = True
```

**真實 OLA 語義**：
- `p_status == 'Y'` → 「**繳費單已產生**」（書記官把 `p_payid` / `barcode1-3` 填上後該欄位即變 Y）
- `payment == 'Y'` → 「**繳費 flag 已設**」（律師繳費後）
- `paystatus == '1'` → 「**已繳**」

`p_status='Y'` + `payment='N'` + `barcode1-3` 都填上 = OLA 已產生繳費單可下載，但律師還沒繳費。**這正是 MAGI 應該送 PDF 通知的時機**，反而被誤判為「已繳費」立刻跳過。

## 證據
| 案件 | 日期 | `paystatus` | `p_status` | `payment` | `_has_payment_proof_uploaded` 修前 | 結果 |
|------|------|-------------|-----------|-----------|---|---|
| 余春香 115.原金訴.000044 | 4/10 11:10 | 2 | `''`(空) | N | False | ✅ 送 PDF |
| 吳志炳 114.原交易.000049 | 4/10 14:42 | 2 | (空) | N | False | ✅ 送 PDF |
| 黃珊珊 114.原訴.000084 | 4/10 13:10 | 2 | **Y** | N | True | ❌ 跳過 |
| 陳文明 115.原訴.000036 | 4/29 10:57 | 2 | **Y** | N | True | ❌ 跳過 |

## 修法
`_has_payment_proof_uploaded()` 移除 `p_status == 'Y'` 條件 + docstring 補警告：

```python
# 修前
if paystatus == "1" or p_status == "Y" or payment == "Y":
    paid = True

# 修後
if paystatus == "1" or payment == "Y":
    paid = True
```

docstring 加入：
```
⚠️ 注意：`p_status == 'Y'` 不是「已繳費」，而是「繳費單已產生可下載」
（OLA 把 p_payid / barcode 欄位填上即會將 p_status 設為 'Y'）。
```

## 驗證

### 單元（已通過）
餵 4/29 陳文明真實 row_json：
```
_has_payment_proof_uploaded(陳文明 row) = False  ✅ (修前: True)
```

### Live（待下次 auto_worker 觸發 ~1 小時內）
file_review_auto_worker 每小時跑一次 download，下次跑時：
- 陳文明 115.原訴.000036：未在 notified_cases / dismissed / proof_registry → **應收 DC PDF**
- 黃珊珊 114.原訴.000084：同上 → **應補收 4/10 漏掉的 DC PDF**
- 吳志炳：已在 notified_cases (4/10) → 仍 dedup 攔，不重發 ✓
- 張志雄：在 proof_registry → 仍 dedup 攔 ✓
- 蘇建和：在 dismissed_payments → 仍 dedup 攔 ✓

PDF 都在硬碟 `閱卷下載/20260429/`，registry 路徑可解析，`_resolve_payment_registry_files` 會找到。

## 守則更新
- **`p_status` 不可作為「已繳費」判定** — 它只表示繳費單已生成（OLA 系統側）
- 真正的繳費判定欄位：`payment == 'Y'` 或 `paystatus == '1'` 或 statusnm/result 含「已繳」「繳費完成」「收據」「繳訖」
- 此修補違反「§5.1 閱卷三模組不要再動」守則但屬「緊急 bug 例外」（影響核心功能、明確錯誤、修補範圍嚴格限縮在判定函數）

## 驗收層級
- 測試：`py_compile` 通過 + 單元驗證 `_has_payment_proof_uploaded(陳文明 row) = False`
- Live（待觸發）：下次 auto_worker run 時 DC 應收到陳文明 + 黃珊珊 PDF

---

# 連帶修復（同案 2026-04-29）：電子閱卷 applytype + 繳費憑證上傳目標 row

陳文明案踩出兩個額外 bug：(1) 電子閱卷 cmd_apply 只選「聲請本審」漏選「合併聲請」→ 同案需申請兩次；(2) 第二次申請後，上傳繳費憑證時 OLA 同案多 row（已繳 + 未繳），原始搜尋會 match 第一列（可能是已繳的）。

## Bug B：電子閱卷「聲請範圍」選錯 — radio name 已從 `applytype` 改為 `dossier_radio`

### 表象
- 陳文明 4/22 申請的 row：`dossier='聲請本審之電子卷證'`（只本審）
- 對比 4/10 余春香、吳志炳成功案例：`dossier='合併聲請本審及本審以外之電子卷證'`（合併）
- 結果：陳文明只下載本審卷，須再申請第二次才拿到完整卷證

### 根因（三層）
1. **OLA portal radio name 已改名**：原本 `name="applytype"` value="0/1/2"，現改為 `name="dossier_radio"`，value 為完整文字「聲請本審之電子卷證」/「聲請本審以外之電子卷證」/「合併聲請本審及本審以外之電子卷證」。原 JS query `input[name="applytype"]` **完全找不到任何元素** → 不選 → 用預設值送出。
2. **OLA 還有一個 hidden input `name="dossier"`**：實際送出時讀的是這個 hidden 欄位的 value（預設「聲請本審之電子卷證」）。光點 radio 不夠，還要同步更新 hidden `dossier.value`。
3. **Playwright wrapper 把 IIFE 結果吃掉**：`_convert_script_for_playwright` 把 script 包成 `() => { ... }`（statement block），原 script 是 `(function(){...})()` IIFE，內部 return 的值沒被外層 arrow function 回傳 → Python 收到 `None` → 一直 log「選項已預設或不需變更」（誤導性訊息，看似無事）。
4. **2026-04-29 19:54 audit 發現實際 DOM**：
    ```
    所有 radio name: applyway, dossier_radio (7 個), getway, isobligation, condition, sendway, nextdudt_radio
    matched「合併」: name="dossier_radio" value="合併聲請本審及本審以外之電子卷證"
    hidden: name="dossier" type="hidden" value="聲請本審之電子卷證"  ← 預設
    ```

### 修法
1. **JS 開頭加 `return`**：`return (function(){...})();` 讓 IIFE 回傳值傳到 Python。
2. **多層 selector**：依序嘗試 `dossier_radio` → `applytype`（兼容舊版）；value match 「合併聲請本審及本審以外之電子卷證」/`"2"`/「合併聲請」；label/text fallback；最後 fallback 取最後一個 radio。
3. **同步更新 hidden `dossier.value`**：點 radio 後立即把 hidden `input[name="dossier"]`.value 設為 radio 的 value，並 dispatch change event。
4. **加 sanity check warning**：執行完若 hidden dossier value 不含「合併」或 getway 不是「線上交付」，push 警告到 selected log。

### Live 驗收（2026-04-29 19:58:30，陳文明 ILD 115.原訴.000036, auto_submit=false）
```
✓ JS 批次選擇: 聲請方式=電子, 聲請範圍=合併聲請本審及本審以外之電子卷證 [name=dossier_radio], 交付方式
result: Ready  (沒送出，也清掉所有測試生成的 pending tokens)
```
表單最終狀態 audit：
- applyway:複製電子卷證 ✓
- dossier hidden:合併聲請本審及本審以外之電子卷證 ✓
- getway:線上交付 ✓
- isobligation:N（非義務辯護）✓

## Bug C：繳費憑證上傳搜到已繳 row

### 表象
陳文明同案有兩列：`聲請本審`（已繳）+ `合併聲請`（待繳）。律師繳完合併聲請的款後，傳繳費截圖給 MAGI，期望上傳到「合併聲請」那列；但 `upload_to_existing_application` 從 row 0 forward search 只比對案號 + 當事人名，第一個 match（可能是已繳的「聲請本審」列）就停下，把繳費憑證傳錯地方。

### 根因
`upload_to_existing_application` 的 `target_row_idx` JS（line ~8523）搜尋條件：
- 當事人名 in rowText
- 案號 pattern in rowText OR data-json.yyidno

未考慮 `paystatus` / `payment` flag 區分「已繳 vs 待繳」。

### 修法
JS 改為兩階段：先收集所有 match candidates 含 `paid` + `hasPending` flags，再依 `file_remark == "繳費憑證"` 挑選：
1. 優先挑 `hasPending=true && !paid` 的 row（待繳費）
2. 其次挑 `!paid` 的 row
3. 全都已繳 → log 警告 + 回 `-1` 拒絕上傳（不亂傳到已繳列）

判斷標準：
- 已繳：rowText 含「已繳/繳費完成/繳訖」 OR `data-json.paystatus==='1'` OR `data-json.payment==='Y'`
- 待繳：rowText 含「待繳費」 OR `data-json.paystatus==='2'`

非繳費憑證上傳（例如委任狀）維持原行為，仍取首個 match。

### 守則
- 同案多 row 是 OLA 常見情況（律師可在同案發多次閱卷聲請）
- **凡上傳到「特定狀態」的 row（例如繳費憑證只上待繳）必須在搜尋階段過濾**，不可只靠案號+人名 match
- `_is_payment_proof_upload` flag 由 `file_remark == "繳費憑證"` 推導；新增其他需依狀態挑 row 的 file_remark 時，仿此模式擴充

## 驗收層級（B、C）
- 測試：`py_compile` 通過
- Live（待觸發）：
    - Bug B：下次有新案件需 cmd_apply 電子閱卷時觀察 dossier 應為「合併聲請本審及本審以外之電子卷證」
    - Bug C：律師上傳陳文明繳費截圖時，應上傳到「合併聲請」row（待繳）而非「聲請本審」row（已繳）。實驗條件：陳文明 OLA portal 同案兩列，paystatus=1 vs 2 互異
