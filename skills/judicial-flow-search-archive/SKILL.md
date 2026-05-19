---
name: judicial-flow-search-archive
description: 流程型技能：把使用者的自然語句轉成司法院裁判書「全文內容」可用的布林查詢（AND/OR/NOT），上司法院抓判決清單與全文，並將結果歸檔（manifest+txt），回傳歸檔路徑與摘要。支援口語指令「判決搜尋 …」。
author: CASPER
created: 2026-02-14
---

# judicial-flow-search-archive

## 指令
1. `help`
1. `self_test`
1. `boolify { ... }`（也支援 `布林化 { ... }`）
1. `search_archive { ... }`（也支援 `判決搜尋 { ... }`）

也可以直接用口語（不用 JSON）：
- `判決搜尋 詐欺 並且 洗錢 排除 未遂`

也支援「Google 搜尋風格」：
- `判決搜尋 詐欺 洗錢 -未遂`
- `判決搜尋 詐欺+洗錢-未遂`
- `判決搜尋 詐欺 OR 洗錢`
- `判決搜尋 "損害 賠償" 臺灣高等法院`（引號片語內的空白會自動轉成 `+`）

## 法院偏好（預設行為）
- 預設會 **以「最高法院」為主**：只收錄標題開頭是 `最高法院` 的裁判。
- 只有當查詢語句明顯是 **行政案件**（包含 `行政`/`高等行政法院`/`最高行政法院`）時，才會改成只收錄行政法院（例如 `最高行政法院`、`臺北高等行政法院` 等）。
- 若過濾後為 0 筆，會自動 fallback 回「未過濾」清單（並在 `report.txt` 標註）。

## boolify payload（JSON）
- `query`（必填）: 你的自然語句/關鍵詞
- `timeout_sec`（可選，預設 60）

### boolify 輸出補充
- `boolean_query`：實際要貼到司法院「全文內容」欄位的查詢字串（偏向 `+` / `-` / `OR`）
- `boolean_query_raw`：中介格式（AND/OR/NOT），方便你檢查

## search_archive payload（JSON）
- `query`（必填）: 你的自然語句/關鍵詞
- `max_results`（可選，預設 10）
- `max_chars`（可選，預設 40000）：每筆抓回全文上限
- `headless`（可選，預設 true）
- `timeout_sec`（可選，預設 120）

## 輸出（重點）
為避免輸出被截斷，完整內容會落檔：
- `archive_dir`：本次歸檔資料夾
- `manifest_path`：歸檔索引（JSON）
- `report_path`：給使用者看的歸檔摘要（TXT）
只會在 stdout 回傳短摘要與路徑。
