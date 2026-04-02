---
name: pdf-annotator
description: "[DEPRECATED] 已被 pdf-bookmarker v2.0 取代。視覺模型方式太慢且不穩定。"
license: MIT
compatibility: Python 3.10+
metadata:
  author: MAGI
  version: "1.0"
  sage: keeper
  updated: "2026-03-09"
  deprecated: true
---

# pdf-annotator (DEPRECATED)

> **已被 `pdf-bookmarker` v2.0 取代。** 新版使用文字辨識而非視覺模型，速度更快、覆蓋率更高。

案件卷宗 PDF 書籤自動標註工具。可從既有書籤學習命名慣例，再批次對未標註的 PDF 自動建立導覽書籤。

## 指令

```bash
# 從現有書籤學習命名慣例
python action.py --task learn --learn_files 5

# 對指定案件的卷宗加書籤
python action.py --task annotate --case "[當事人E]"

# 強制重新處理已標籤檔案
python action.py --task annotate --case "[當事人E]" --force 1

# 測試模式（預覽不寫入）
python action.py --task test --sample 5

# 查看狀態
python action.py --task status
```

## 參數

| 參數 | 說明 | 預設值 |
|------|------|--------|
| `--task` | learn / annotate / test / status | annotate |
| `--case` | 案件名稱（模糊匹配資料夾） | — |
| `--force` | 1=強制重新處理已標籤檔案 | 0 |
| `--sample` | test 模式樣本頁數 | 5 |
| `--learn_files` | learn 模式樣本檔數 | 5 |

## 流程

1. **learn**: 掃描已有書籤的 PDF，學習書籤命名慣例
2. **annotate**: 以視覺模型辨識頁面內容，依學習結果或推斷建立書籤
3. **test**: 預覽模式，不實際寫入 PDF
4. **status**: 顯示目前學習狀態與已處理檔案統計
