---
name: pdf-bookmarker
description: 自動掃描法院卷宗 PDF 並建立書籤目錄 (v2.0)
---

# PDF Bookmarker v2.0

自動偵測法院卷宗 PDF 中的文件邊界，建立書籤（目錄）讓律師快速翻閱。

## 特點

- **放寬觸發**：有文件類型就標，不要求日期+類型同時存在
- **60+ 種文件類型**：筆錄、判決、裁定、起訴書、函文、鑑定、證據等
- **前案紀錄表整段合併**：不再每頁建一個書籤
- **連續同類合併**：28 份送達證書 → 「送達證書（共 28 份）」
- **直接寫入原檔**：不產出 `_bookmarked.pdf` 分身
- **OCR 支援**：掃描頁自動 RapidOCR fallback
- **OLA 浮水印過濾**：跳過司法院閱卷系統空白頁

## 使用

```bash
# 單檔處理（直接覆寫原檔）
python action.py --task scan_file --path "/path/to/volume.pdf"

# 單檔另存
python action.py --task scan_file --path "/path/to/volume.pdf" --output "/path/to/out.pdf"

# Dry run（只顯示不寫入）
python action.py --task test --path "/path/to/volume.pdf"

# 顯示現有書籤
python action.py --task show --path "/path/to/volume.pdf"

# 整個資料夾批次處理
python action.py --task batch --path "/path/to/06_閱卷資料/"
```

## 書籤格式

```
日期 文件類型 [當事人姓名]
```

範例：
- `113.04.15 準備程序筆錄`
- `113.05.17 鑑定報告`
- `113.08.01 前案紀錄表`（130 頁整段一個書籤）
- `送達證書（共 28 份）`

## 層級

- Level 1（主要）：筆錄、判決、裁定、起訴書、鑑定報告等
- Level 2（次要）：送達證書、傳票、報到單、戶籍謄本、照片等

## 依賴

- PyMuPDF (`fitz`)
- RapidOCR (`rapidocr_onnxruntime`) — 選用
