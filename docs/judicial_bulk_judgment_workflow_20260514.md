# 大量判決抓取與通譯分類流程

更新日：2026-05-14

## 核心原則

- 只保留一份 canonical 工作版資料；舊檔移出主資料夾封存，不混在工作資料中。
- 大量裁判抓取必須可斷點續抓，依清單序號逐筆檢查 txt/pdf 是否存在且有效。
- FJUD 網頁端出現 `Connection reset by peer` 時，不連續硬打；改用小批次、低頻重試。
- 官方 `data.judicial.gov.tw` API 只能在 00:00-06:00 服務時段使用，夜間排程負責補完。
- PDF 優先使用司法院官方匯出 PDF；臨時生成 PDF 必須留下 marker，日後由官方 PDF 覆蓋。

## 通譯分類規則

- 刑事訴訟法第 420 條「證言、鑑定或通譯已證明其為虛偽」如果只是條文引用，不得標成高信心通譯案件。
- 刑事訴訟法第 403 條「證人、鑑定人、通譯及其他非當事人」如果只是抗告權條文引用，也不得標成實質通譯爭點。
- 純條文引用標示為：
  - `primary_category`: `法條或程序清單引用`
  - `issue_role`: `非通譯爭點`
  - `issue_result`: `非通譯爭點`
  - `confidence`: `中`
  - `interpreter_marker`: `僅條文引用`
- 只有判決理由實際討論通譯虛偽、不實、錯譯、未通譯、通譯選任、公正性、證據能力、偵訊/警詢/審判傳譯等，才標為 `實質通譯爭點`。
- 實質通譯爭點必須在 snippets 欄引出該段原文，且把「通譯」標成 `【通譯】`。

## 目前專案路徑

- 專案：`/Users/ai/Desktop/最高法院_通譯_TXT`
- canonical 資料：`/Users/ai/Desktop/最高法院_通譯_TXT/完整812`
- 腳本：`/Users/ai/Desktop/最高法院_通譯_TXT/scripts/complete_interpreter_dataset.py`
- 舊檔封存：`/Users/ai/Desktop/最高法院_通譯_TXT_舊檔封存_20260514`

## 操作指令

```bash
/Users/ai/Desktop/MAGI_v2/venv/bin/python3 \
  /Users/ai/Desktop/最高法院_通譯_TXT/scripts/complete_interpreter_dataset.py \
  --mode status

/Users/ai/Desktop/MAGI_v2/venv/bin/python3 \
  /Users/ai/Desktop/最高法院_通譯_TXT/scripts/complete_interpreter_dataset.py \
  --mode nightly --max-api 50

/Users/ai/Desktop/MAGI_v2/venv/bin/python3 \
  /Users/ai/Desktop/最高法院_通譯_TXT/scripts/complete_interpreter_dataset.py \
  --mode table
```
