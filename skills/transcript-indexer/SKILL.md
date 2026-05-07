---
name: transcript-indexer
description: 筆錄向量化索引器 — 掃描案件資料夾中的筆錄 PDF，依發言人分段後寫入 KEEPER 向量記憶庫，支援自然語句查詢並附出處。
license: MIT
compatibility: Python 3.10+, PyMuPDF, mem_bridge
metadata:
  author: MAGI
  version: "1.0"
  sage: keeper
---

# transcript-indexer

掃描 Synology Drive 案件資料夾下的筆錄 PDF（訊問筆錄、審判筆錄、準備程序筆錄等），使用 PyMuPDF 擷取文字後依發言人（法官、被告、證人、檢察官等）自動分段，再透過 `mem_bridge` 批次寫入 KEEPER 向量資料庫。支援增量索引（依 mtime 跳過未變動檔案）與自然語句查詢。

## Capabilities

- 自動遍歷多個案件根目錄（SynologyDrive、lumi 結案卷等），辨識筆錄子目錄 (05_筆錄 ~ 08_筆錄)
- PDF 文字擷取與 OCR 行號雜訊清除
- 依發言人（法官/被告/證人/辯護人/檢察官等）切換分段，每段 200-400 字
- 增量索引：依檔案 mtime 自動跳過已索引檔案，支援 `--force` 全量重建
- 向量查詢：回傳案件名稱、筆錄類型、日期、頁次、發言人等出處資訊
- 索引統計：各案件筆錄數量與總段落數

## Usage

```bash
python action.py --task index                        # 增量索引所有筆錄
python action.py --task index --force 1              # 強制全量重新索引
python action.py --task query --query "被告承認收到款項"  # 自然語句查詢
python action.py --task status                       # 顯示索引統計
```

## Commands

1. `index` — 掃描所有案件筆錄 PDF，分段向量化存入 KEEPER（排程用）
2. `query` — 自然語句查詢筆錄內容，回傳 JSON 含出處（案件、日期、頁次、發言人）
3. `query_docx` — 同 `query`，但額外輸出 DOCX 表格（發言人｜時間｜內容），檔案存入 `/static/exports`
4. `status` — 顯示已索引筆錄統計（檔案數、段落數、各案件分布）

## Environment Variables

| 變數 | 說明 | 預設值 |
|---|---|---|
| `SYNOLOGY_CASE_ROOT` | 案件根目錄 | `~/Library/CloudStorage/SynologyDrive-homes/01_案件` |
| `TRANSCRIPT_DIRS` | 筆錄子目錄名（逗號分隔） | `05_筆錄,06_筆錄,07_筆錄,08_筆錄` |
| `TRANSCRIPT_INDEX_DB` | 索引狀態紀錄路徑 | `MAGI_ROOT/.agent/transcript_index.json` |
| `TRANSCRIPT_BATCH` | 每批向量化筆數 | `20` |

## Dependencies

- `PyMuPDF (fitz)` — PDF 文字擷取
- `skills/memory/mem_bridge` — KEEPER 向量記憶庫讀寫
