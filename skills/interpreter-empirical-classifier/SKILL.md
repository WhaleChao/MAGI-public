---
name: interpreter-empirical-classifier
description: 最高法院「通譯」裁判實證研究工具；可依指定關鍵字上網抓取司法院裁判、整理 TXT，並批次分類成可讀表格，保留原文摘錄、通譯爭點類型、法院結果與通譯判讀標記。
author: MAGI
created: 2026-05-16
metadata:
  version: "1.0"
  sage: casper
---

# interpreter-empirical-classifier

用於最高法院裁判中「通譯」關鍵字的實證研究。此 skill 不用 LLM 亂猜，會先依使用者指定的關鍵字到司法院裁判系統搜尋、抓取裁判全文、整理成乾淨 TXT，再以規則與原文摘錄分類；每一列都保留可複核的原文片段。

## 何時使用

- 使用者要求「通譯判決分類」、「判決實證研究分類」、「最高法院通譯表格」。
- 使用者要求「用某個關鍵字抓判決並分類」，例如「最高法院 通譯」、「最高法院 通譯 裁定」。
- 需要區分只是條文帶過，或是通譯品質、未使用通譯、通譯參與程序、外語證據翻譯等實質爭點。
- 需要輸出 CSV / XLSX / Markdown 表格，供人工閱讀與後續研究校正。

## 指令

```bash
python3 action.py --task status
python3 action.py --task self_test
python3 action.py --task 'fetch keyword="最高法院 通譯" max_results=50'
python3 action.py --task 'fetch_and_classify keyword="最高法院 通譯" max_results=50 output_dir=/tmp/interpreter'
python3 action.py --task classify
python3 action.py --task 'classify input_dir=/path/to/TXT output_prefix=/path/to/out'
```

也可傳 JSON：

```bash
python3 action.py --task '{"task":"fetch_and_classify","keyword":"最高法院 通譯","max_results":50}'
python3 action.py --task '{"task":"classify","input_dir":"/path/to/TXT","output_prefix":"/path/to/out"}'
```

## 抓取流程

- `fetch` 只抓取並整理 TXT，會輸出 `fetch_report.json`，保留查詢條件、來源 URL、成功與失敗清單。
- `fetch_and_classify` 會先執行 `fetch`，再對抓到的 TXT 產出 CSV / XLSX / Markdown。
- 關鍵字若包含「最高法院」，系統會自動轉成法院篩選 `最高法院`，並把實際搜尋字串改為其餘關鍵字，例如 `最高法院 通譯` 會搜尋 `通譯` 並限制法院為最高法院。
- 預設不覆蓋已抓過的 TXT；如需重抓可加 `force=true`。

## 輸出欄位

- `最高法院裁判字號`
- `裁判日期`
- `案由`
- `法院判決/裁定結果`
- `主分類`
- `全部分類`
- `通譯角色`
- `通譯爭點處理`
- `通譯判讀標記`
- `分類信心`
- `前審/相關案號`
- `通譯相關原文摘錄`
- `來源檔案`
- `PDF檔案`

## 分類原則

- 刑事訴訟法第 420 條「證言、鑑定或通譯已證明其為虛偽」若只是條文引用，標示 `僅條文引用`，不得當成實質通譯爭點。
- 刑事訴訟法第 403 條「證人、鑑定人、通譯及其他非當事人」若只是抗告權條文引用，標示 `僅條文引用`。
- 只有理由段實際討論通譯虛偽、不實、錯譯、未通譯、通譯選任、公正性、證據能力、偵訊/警詢/審判傳譯等，才標示 `實質通譯爭點`。
- 摘錄欄中的「通譯」會標成 `【通譯】`，方便快速檢視。
