---
name: contract-review
description: 合約審閱工具 — 審查任意合約文件並找出不利條款與風險點。使用時機：當使用者說「幫我看這份合約」「審閱 NDA」「合約有沒有問題」「這份保密協議可以簽嗎」「幫我查這個廠商合約」「法律文件摘要」等相關指令時，務必使用本技能。
metadata:
  version: "1.0"
  created: 2026-03-15
---

# 合約審閱工具 (Contract Review)

## Purpose

以本地 TAIDE 模型審閱合約文件，輸出結構化風險分析。四個子任務涵蓋臺灣法律實務中最常見的合約審閱需求，不依賴外部 API。

## Trigger When

- 使用者上傳或貼上合約、NDA、保密協議、採購合約、勞務合約等文件
- 使用者問「這份合約有沒有問題？」
- 使用者問「這個 NDA 可以簽嗎？」
- 使用者說「幫我做法律文件摘要」
- 使用者問「廠商合約有沒有缺漏？」

## Tasks

| Task | 說明 | 輸出重點 |
|------|------|---------|
| `review` | 合約審閱 | 不利條款、缺漏條款、風險等級、修改建議 |
| `nda` | NDA 分流 | 可簽/需修改/拒絕、保密範圍、期限、違約罰則評估 |
| `summarize` | 法律文件摘要 | 當事人、主要義務、付款條件、爭議解決、風險點 |
| `vendor_check` | 供應商查核 | 與標準模板比對落差、付款天數、終止通知期、智財歸屬 |

## Inputs

- 合約檔案（.txt / .pdf / .docx）或直接貼入文字
- [vendor_check] 可選：自訂標準合約範本路徑

## Outputs

- 結構化 JSON（風險等級、條款清單、建議）
- `ok: true/false`
- 適合直接呈現給使用者或存檔備查

## Runtime Contract

```bash
# 合約審閱
python3 action.py --task review --file 合約.pdf

# NDA 分流
python3 action.py --task nda --file NDA.docx

# 法律文件摘要
python3 action.py --task summarize --text "合約全文..."

# 供應商查核（可附自訂標準範本）
python3 action.py --task vendor_check --file 採購合約.pdf --template references/vendor_standard.txt

# 輸出至檔案
python3 action.py --task review --file 合約.pdf --output result.json
```

## Guardrails

- 所有分析在本地執行（TAIDE），不傳送至外部服務
- 輸出為建議參考，最終法律判斷仍需律師確認
- 文件超過 12,000 字元時自動截取前後段（可調 `CONTRACT_REVIEW_MAX_CHARS` 環境變數）
- 不執行任何簽署、送出或系統寫入操作

## References

- `references/vendor_standard.txt` — 臺灣供應商合約標準條款清單（vendor_check 預設範本）
- `evals/evals.json` — 測試案例

---
> If adding more contract types, put template lists in `references/` and keep action.py under 400 lines.

## 呼叫格式
觸發詞：審閱契約、合約審查
參數：path=檔案路徑

## 呼叫範例
使用者：審閱這份合約 /tmp/contract.pdf
→ 審閱契約 path=/tmp/contract.pdf
