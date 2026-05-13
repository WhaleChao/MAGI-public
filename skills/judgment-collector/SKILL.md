---
name: judgment-collector
description: 司法見解收集器 — 根據案由自動收集判決、生成結構化見解摘要、存入資料庫。透過司法院裁判書系統搜尋全文。
author: CASPER
created: 2026-02-16
metadata:
  version: "2.0"
  sage: casper
  updated: "2026-03-12"
---

# judgment-collector

根據案件案由，自動收集最高法院判決（行政案件放寬到高等行政法院），生成結構化司法見解摘要後存入資料庫。

## 指令一覽

| 指令 | 說明 |
|------|------|
| `help` | 顯示可用指令清單 |
| `self_test` | 自我測試 |
| `collect {payload}` | 收集判決 + 生成見解摘要 |
| `daily_crawl` | 每日自動爬取（掃描進行中案件） |
| `official_api_night_pull` | 司法院 API 夜間批量拉取裁判書 |
| `official_api_day_process` | 日間處理已拉取的裁判書（摘要+入庫） |
| `official_api_auto` | 自動判斷時段執行 pull 或 process |
| `backfill_archive_summaries` | 掃描 archive 全文批次生成見解摘要（回填見解庫） |
| `retry_summary_queue` | 手動重試降級摘要佇列 |
| `retry_summary_queue_auto` | 自動分級重試降級摘要 |
| `scan_active_cases` | 掃描進行中案件清單 |
| `scan_active_reasons` | 掃描進行中案件的案由 |
| `backfill_court_judgments` | 回填 court_judgments 表 |

## collect 參數（JSON）

| 參數 | 必填 | 預設 | 說明 |
|------|------|------|------|
| `case_reason` | 二擇一 | — | 案由（如 `詐欺`、`過失傷害`、`撤銷行政處分`） |
| `case_number` | 二擇一 | — | 案件編號，自動查 DB 取得 case_reason |
| `case_type` | 否 | 自動判斷 | 案件類型：`刑事`、`民事`、`行政` |
| `max_results` | 否 | 5 | 最多收集幾筆判決 |
| `max_chars` | 否 | 12000 | 全文最大字元數 |
| `headless` | 否 | true | 無頭模式 |
| `timeout_sec` | 否 | 300 | 逾時秒數 |
| `save_to_db` | 否 | true | 是否存入 judgment_archive 表 |
| `notify` | 否 | true | 完成後 LINE/DC 通知 |

`case_reason` 必須是合法案由（≥2 字，不接受對話片段如「查一下」）。

## backfill_archive_summaries 參數（JSON）

| 參數 | 預設 | 說明 |
|------|------|------|
| `max_items` | 50 | 本次最多處理幾筆 |
| `min_text_bytes` | 2000 | 檔案最小位元組數（排除空檔） |
| `timeout_per_item` | 300 | 每筆摘要逾時秒數 |
| `year_min` | 0 | 最小年度（民國年） |
| `year_max` | 9999 | 最大年度 |
| `notify` | false | 完成後通知 |

用法範例：
```bash
# 回填近兩年、最多 30 筆
python action.py --task 'backfill_archive_summaries {"max_items":30,"year_min":113}'

# 全量回填（離峰執行）
python action.py --task 'backfill_archive_summaries {"max_items":200}'
```

## 摘要規格

每筆判決的 LLM 摘要必須包含以下結構：

```
## 裁判要旨
（一句話概括本判決的核心法律見解）

## 事實摘要
（案件事實經過，100字以內）

## 爭點
（本案的法律爭點，條列式）

## 法院見解
（法院對各爭點的論述與結論 — 最重要的部分）

## 適用法條
（列出本判決適用的法條）
```

### 品質控制

- **僅儲存真正 LLM 摘要**：搜尋預覽片段不存入 judgments.json
- **去重**：以 URL 為 key，同一判決不重複存入
- **幻覺偵測**：若 LLM 摘要的裁判案由與預期不符，自動標記為降級
- **降級重試**：降級摘要自動排入重試佇列，分 fast / standard / deep 三級
- **自我修復**：每次存入時自動清除殘留的降級/垃圾條目

### judgments.json 欄位

| 欄位 | 說明 |
|------|------|
| `title` | 判決標題（如「最高法院 112,台上,1234 詐欺」） |
| `url` | 判決來源連結 |
| `summary` | 完整結構化摘要 |
| `summary_type` | `llm`（LLM 生成）— 預覽片段不存入 |
| `case_reason` | 案由 |
| `timestamp` | 收集時間 |
| `source` | 來源（Judicial Yuan） |

## 法院自動判斷

| case_type 或 case_reason 含有 | 搜尋法院 |
|------|------|
| `行政` / `訴願` / `行政訴訟` / `稅捐` | 最高行政法院 + 高等行政法院 |
| 其他 | 最高法院 |

## 資料來源

1. **司法院裁判書 Archive** → 搜尋全文
2. **司法院 API** → 案號精準查詢、全文取得

## daily_crawl 行為

1. 掃描 Synology Drive `01_案件/` 下進行中案件
2. 取出各案件的 `case_reason`（去重）
3. 對每個 case_reason 呼叫 `collect`
4. 結果存 DB，LINE 通知摘要

## LINE/DC 指令格式

律師可透過 LINE 或 Discord 對 CASPER 說：
- `判決搜集 詐欺`
- `收集判決 撤銷行政處分`
- `搜尋最高法院判決 過失致死`
- `查判決 傷害`

## 輸出

| 欄位 | 說明 |
|------|------|
| `archive_dir` | 本次收集的歸檔資料夾 |
| `summary_path` | 合併摘要報告（Markdown） |
| `db_ids` | 存入 judgment_archive 表的 ID 列表 |
| `items` | 各筆判決的標題、URL、摘要預覽 |

## 資料庫

- **judgment_archive**: 主要判決存檔表（含 `is_degraded` 品質標記）
- **court_judgments**: 法院判決完整表（含全文）

## Cache 管理

- 每次 `collect` 自動清理 >14 天的 run 目錄
- 可透過 `JUDGMENT_CACHE_RETENTION_DAYS` 環境變數調整保留天數

## 依賴

- `skills/bridge/inference_gateway.py`（LLM 推理閘道）
- `skills/insight-refine/action.py`（摘要精煉）
- MariaDB（`law_firm_data` 資料庫）

## 低價值判決過濾（2026-04-02）
以下類型判決不入庫、不摘要（最高/高等法院除外）：
- 支付命令（司促/促字）
- 本票裁定（司票/票字）
- 強制執行（司執字）
- 補費裁定（補字）
- 附帶民事（附民字）
- 續收、催告、消債核

## 快速入庫（2026-04-02）
`scripts/ingest_raw_judgments.py` 可將 judicial_api/raw/ 的 JSON 直接寫入 court_judgments，不需 LLM 摘要。
夜間守護自動在 00:30 拉取 + 06:30 入庫。

## 呼叫格式
觸發詞：查判決、找判決、搜尋判決、實務見解
參數：keyword=關鍵字, court=法院(選填), year=年度(選填)

## 呼叫範例
使用者：查關於詐欺的最高法院判決
→ 查判決 keyword=詐欺 court=最高法院

使用者：112年度的侵權行為判決
→ 查判決 keyword=侵權行為 year=112

使用者：找監護權改定的實務見解
→ 查判決 keyword=監護權改定
