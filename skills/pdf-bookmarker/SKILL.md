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

---

## 開發進度（2026-04-18）

### 已完成

| Round | 內容 | 狀態 |
|-------|------|------|
| Round 3-A | `skills/engine/doc_type_detector.py` — 28+ regex patterns，`DocTypeResult` class，Vision fallback hook | ✅ 已完成，19 tests |
| Round 3-B | `action.py` OLA 自適應閾值（`_compute_ola_threshold()`，P10 分佈），Vision fallback（`MAGI_BOOKMARKER_VISION_FALLBACK=1`），`bookmark_validator.py` 非阻斷守門 | ✅ 已完成，9 tests |
| Round 3-C | `benchmark_pdf_bookmarker.py`（掃最多 20 個 NAS PDF，bookmark_recall ≥ 0.80），`cron_jobs.json` 加 `job_benchmark_pdf_bookmarker`（每日 14:40；避開夜間重任務並維持健康頁 48h freshness） | ✅ 已完成 |

### 待完成

| Round | 內容 | 備註 |
|-------|------|------|
| Round 3-D | Live benchmark 驗收：在真實 NAS PDF 執行 `benchmark_pdf_bookmarker.py`，確認 recall ≥ 0.80 | 需要 NAS 連線 + 15-20 分鐘 |
| Round 3-E | `bookmark_validator` 接入主流程（`_build_bookmark_label()` 後）+ live 單檔 E2E 驗收 | 目前 validator 已存在但未接入 action.py |
| Round 4 | `merge_consecutive` 邏輯升級：同一類型連續頁合併時，日期範圍顯示（如「送達證書 2025.01-03（共28份）」） | 增強可讀性 |
| Round 5 | 整合 `doc_type_detector` 與現有 `DOC_PATTERNS`：讓兩套規則共用 confidence 評分，淘汰硬碼重複 pattern | 較大重構，需保留現有 fallback |

### Feature Flags

| Flag | 預設 | 說明 |
|------|------|------|
| `MAGI_BOOKMARKER_VISION_FALLBACK` | `1`（已開啟） | DOC_PATTERNS 全未命中時呼叫 `doc_type_detector`，信心 ≥ 0.60 採用 |

### 相關檔案

- `skills/pdf-bookmarker/action.py` — 主邏輯
- `skills/pdf-bookmarker/bookmark_validator.py` — 格式守門（非阻斷）
- `skills/engine/doc_type_detector.py` — 共用文件類型偵測器
- `scripts/ops/benchmark_pdf_bookmarker.py` — NAS 批次 benchmark
- `tests/test_bookmarker_ola.py` — OLA 閾值測試
- `tests/test_doc_type_detector.py` — 偵測器測試
