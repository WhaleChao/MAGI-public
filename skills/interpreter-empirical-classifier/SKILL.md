---
name: interpreter-empirical-classifier
description: 最高法院「通譯」裁判實證研究分類工具；將 TXT 裁判批次分類成可讀表格，保留原文摘錄、通譯爭點類型、法院結果與通譯判讀標記。
author: MAGI
created: 2026-05-16
metadata:
  version: "1.0"
  sage: casper
---

# interpreter-empirical-classifier

用於最高法院裁判中「通譯」關鍵字的實證研究分類。此 skill 不用 LLM 亂猜，會以規則與原文摘錄為主，每一列都保留可複核的原文片段。

## 何時使用

- 使用者要求「通譯判決分類」、「判決實證研究分類」、「最高法院通譯表格」。
- 需要區分只是條文帶過，或是通譯品質、未使用通譯、通譯參與程序、外語證據翻譯等實質爭點。
- 需要輸出 CSV / XLSX / Markdown 表格，供人工閱讀與後續研究校正。

## 指令

```bash
python3 action.py --task status
python3 action.py --task self_test
python3 action.py --task classify
python3 action.py --task 'classify input_dir=/path/to/TXT output_prefix=/path/to/out'
```

也可傳 JSON：

```bash
python3 action.py --task '{"task":"classify","input_dir":"/path/to/TXT","output_prefix":"/path/to/out"}'
```

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

