# `/shortcut/*` Tools API 參考 (2026-04-17)

> **狀態**：端點存在但**預設不使用**。
> **前提**：使用者決定不再經由 Apple Shortcuts 觸發這 4 個功能，改由 MAGI 內部流程自動觸發。OCR / PDF 讀取 / 摘要 / 逐字稿已在閱卷、筆錄、法扶等自動化鏈路內呼叫 internal function，不需額外 shortcut。
> **保留原因**：4 個薄封裝端點已通過測試與 live 驗收（commit `9c30eb3`），留著作為未來 remote trigger / iPad / 外部整合的備用入口，不額外維護文件引導一般使用者操作。

---

## 端點速查

| 端點 | 方法 | 輸入 | 輸出 | 內部呼叫 |
|------|------|------|------|---------|
| `/shortcut/ocr` | POST | octet-stream（JPEG/PNG/HEIC） | `text/plain` | `_INFERENCE_GATEWAY.vision(task_type="ocr")` → fallback `analyze_image` |
| `/shortcut/pdf_text` | POST | octet-stream（`%PDF` 開頭） | `text/plain` | `document_reader.read_document(mode="auto", ocr_fallback=True)` |
| `/shortcut/summarize` | POST | `text/plain; charset=utf-8` | `text/plain` | `summarize_text` via `_run_with_timeout(pool=_INFERENCE_EXECUTOR)` |
| `/shortcut/transcribe` | POST | octet-stream（任意音檔） | `text/plain` | `tri_sage_collab.transcribe_audio` |

共通規格：
- Auth：`X-API-Key`（`MAGI_API_KEY` 或 `MAGI_EXTERNAL_API_KEY`）。
- CSRF：`/shortcut/` 前綴已加入 `CSRF_EXEMPT_API_PATTERNS`。
- 失敗：4xx/5xx + 純文字錯誤（不回 JSON）。
- Body size cap（env 可調）：
  - `MAGI_SHORTCUT_OCR_MAX_BYTES`（預設 10 MB）
  - `MAGI_SHORTCUT_PDF_MAX_BYTES`（預設 50 MB）
  - `MAGI_SHORTCUT_TEXT_MAX_BYTES`（預設 1 MB）
  - `MAGI_SHORTCUT_AUDIO_MAX_BYTES`（預設 100 MB）

---

## 冒煙測試

```bash
KEY=$(grep '^MAGI_API_KEY=' /Users/ai/Desktop/MAGI_v2/.env | cut -d= -f2)

# OCR
curl -s -X POST http://127.0.0.1:5003/shortcut/ocr \
  -H "X-API-Key: $KEY" -H "Content-Type: application/octet-stream" \
  --data-binary @test.png

# PDF
curl -s -X POST http://127.0.0.1:5003/shortcut/pdf_text \
  -H "X-API-Key: $KEY" -H "Content-Type: application/octet-stream" \
  --data-binary @test.pdf

# 摘要
curl -s -X POST http://127.0.0.1:5003/shortcut/summarize \
  -H "X-API-Key: $KEY" -H "Content-Type: text/plain; charset=utf-8" \
  --data-binary @long_text.txt

# 逐字稿
curl -s -X POST http://127.0.0.1:5003/shortcut/transcribe \
  -H "X-API-Key: $KEY" -H "Content-Type: application/octet-stream" \
  --data-binary @test.aiff
```

---

## 舊壞掉的 Apple Shortcuts

`MAGI OCR 掃描 / MAGI 讀取 PDF / MAGI 摘要 / MAGI 音檔轉文字`（含 `_signed` 版本）已因 MAGI Bridge app 淘汰而無法執行。**不再重建**；可忽略或隨手從 Shortcuts.app 刪除，不影響 MAGI。

保留可用：`MAGI GoodNotes 建立資料夾`、`MAGI 螢幕掃描`、`MAGI 語音辨識`。

---

（完）
