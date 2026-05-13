---
name: pdf-namer
description: 自動掃描 PDF前5頁 OCR 內容並依照日期、類型改名
---

# PDF Auto-Namer Skill

此技能使用 Python PyMuPDF + RapidOCR 分析 PDF 文件前 5 頁內容，自動識別文件日期、類型與相關人，並將檔案重新命名為標準格式。最新版已支援「批次並行歸檔 + 背景任務」。

## 主要功能

1.  **深度掃描**：讀取前 5 頁內容（含 OCR），確保不錯過位於內頁的關鍵資訊（如判決主文後的附件）。
2.  **智慧命名**：生成 `RRR.MM.DD_姓名_文件類型.pdf` 檔名。
3.  **衝突處理**：若檔名重複，自動加上 `(1)`, `(2)` 後綴。

## System Requirements

- Python 3.8+
- PyMuPDF (`fitz`)
- RapidOCR (`rapidocr_onnxruntime`)

## Usage

```bash
# 1. 處理單一檔案 (直接改名)
python action.py --task rename_file --path "/path/to/case.pdf"

# 2. 預覽模式 (不改名，只輸出建議檔名)
python action.py --task review_name --path "/path/to/case.pdf"

# 3. 批次歸檔（預設背景執行）
python action.py --task file --execute 1 --notify 1

# 4. 查詢背景任務狀態
python action.py --task file_status --job-id latest

# 5. 強制同步執行（除錯/維運）
python action.py --task file_sync --execute 1 --notify 1

# 6. 調整 smart_filer 並行數
python smart_filer.py scan --execute --workers=4
```

## Concurrency Policy

- `smart_filer.py` 會並行處理多份 PDF（預設 3 workers，建議 3-5）。
- OCR / 檔案比對屬 CPU 工作，可多執行緒加速。
- Vision LLM（Ollama / LLaVA / minicpm-v）預設單工保護，避免 VRAM/OOM。

## Environment Flags

- `MAGI_PDF_NAMER_FILE_WORKERS`：批次歸檔 workers（1-5）。
- `MAGI_PDF_NAMER_FILE_BACKGROUND`：`file` 指令是否預設背景（預設 `1`）。
- `MAGI_PDF_NAMER_VISION_MAX_WORKERS`：Vision 併發上限（預設 `1`）。
- `MAGI_PDF_NAMER_FILE_BG_SINGLETON`：背景任務去重（預設 `1`）。

## Naming Logic

- **Date**: 優先使用文件中最早出現的日期，或最符合文件類型的日期（例如判決日期 vs 宣判日期）。
- **Type**: 識別 `起訴書`, `判決`, `筆錄` 等關鍵字。
- **Name**: 提取 `被告 xxx` 或預設案件人名。

## 更名學習器（2026-04-02）
`rename_watcher.py` 常駐監控案件資料夾（排除閱卷/筆錄），偵測使用者手動改名後自動學習。
- 每 5 分鐘掃描一次
- 更名事件寫入 `_corrections.json` 和 `training_data.json`
- 夜間訓練時加權 3 倍學習

## Vision Port 修正（2026-04-02）
- Vision 模型在 port 8082（GLM-OCR-bf16）
- `vision_parser.py` 和 `action.py` 已改為優先讀 `MAGI_OMLX_VISION_URL`

## 呼叫格式
觸發詞：命名、PDF命名、歸檔
參數：path=檔案路徑, action=動作(analyze/rename/batch)

## 呼叫範例
使用者：命名這個 PDF /tmp/doc.pdf
→ PDF命名 action=analyze path=/tmp/doc.pdf

使用者：批次命名資料夾
→ PDF命名 action=batch
