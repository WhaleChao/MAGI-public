---
name: judgment-collector
description: 司法見解收集器 — 根據案由自動收集判決、生成結構化見解摘要、存入資料庫。透過司法院裁判書系統搜尋全文。
author: CASPER
created: 2026-02-16
metadata:
  version: "3.0"
  sage: casper
  updated: "2026-07-31"
---

# judgment-collector

根據案件案由，自動收集最高法院判決（行政案件放寬到高等行政法院），生成結構化司法見解摘要後存入資料庫。

## 指令一覽

| 指令 | 說明 |
|------|------|
| `help` | 顯示可用指令清單 |
| `self_test` | 自我測試 |
| `extract_practice_summary {payload}` | 從單一裁判全文擷取可入庫實務見解（不寫 DB） |
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

## 實務見解擷取規格

每筆裁判先由來源綁定的擷取器判定法律爭點，再產生下列結構：

```
## 法律爭點
（從裁判標題與開頭判定；「一般」不是有效爭點）

## 實務見解
（逐字擷取法律原則，不改寫）

## 法院涵攝
（逐字擷取法院如何把原則適用到本案；有內容才列）

## 裁判結果
（只取主文中的結果）

## 適用法條
（只列擷取段落實際出現的法條）

## 摘要方式
原文擷取；未以模型改寫（僅正規化空白）
```

### 品質控制

- **NVIDIA 只負責選段**：NVIDIA 120B 只能回傳候選段落編號，不得撰寫、改寫或補充引文；MAGI 依編號回取裁判原文後才組版。
- **不得退回小模型充數**：NVIDIA、JSON 格式、候選編號或品質驗證失敗時不寫入摘要，也不得改由本機小模型生成看似流暢的替代內容。
- **來源逐字支援**：每一段實務見解都必須能在裁判全文核對；只支援部分段落亦拒絕。
- **爭點對齊**：舊資料若只標「一般」，從裁判開頭辨識再審、羈押、定應執行刑、傷害、詐欺等實際爭點。
- **拒絕純法條抄錄**：只有法條文字、沒有涵攝、法理概念或權威裁判訊號者不入庫。
- **誠實空結果**：找不到可用實務見解時回傳空結果，不以案件事實、主文或預覽片段充數。
- **批次共用同一閘門**：即時搜尋、七萬筆回填、實務見解庫與書狀引用均使用相同品質判定。
- **去重**：以 URL 為 key，同一判決不重複存入
- **幻覺偵測**：案由不符、提示詞殘留、推理軌跡、樣板文字或無來源支持均拒絕
- **降級重試**：降級摘要自動排入重試佇列，分 fast / standard / deep 三級
- **自我修復**：每次存入時自動清除殘留的降級/垃圾條目

### 單筆品質驗證

```bash
python action.py --task 'extract_practice_summary {"text_path":"/path/to/judgment.txt","case_reason":"一般","case_number":"114年度聲再字第21號"}'
```

只有 `success=true` 且 `quality.ok=true` 的摘要才能寫入實務見解庫。

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

- `api/domains/judgment_nvidia_summary.py`（NVIDIA 120B 候選編號選擇與 fail-closed 寫回）
- `api/domains/judgment_summary_quality.py`（原文候選、來源支持與實務價值閘門）
- `skills/bridge/nim_heavy.py`（NVIDIA NIM API、PII 清理、額度與斷路器）
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

## 大量判決專案流程（2026-05-14）

大量抓取裁判（例如最高法院「通譯」812 筆）時，MAGI 必須採下列流程：

1. **單一 canonical 資料夾**：只保留一份工作版資料，不讓根目錄 txt、PDF、舊分類表與新資料混放。專案內以 `完整812/TXT`、`完整812/PDF`、`完整812/最高法院_通譯_分類表.*` 作為唯一工作版；舊檔移到專案外封存資料夾，不直接刪除。
2. **斷點續抓**：抓取腳本必須以清單序號檢查 txt/pdf 是否已存在且有效，已完成者跳過，缺漏者補抓。所有補抓只寫入 canonical 資料夾。
3. **節流策略**：FJUD 網頁端若出現 `Connection reset by peer`，視為站方節流或 WAF 冷卻，不連續硬打；改成小批次、低頻重試。官方 `data.judicial.gov.tw` API 受 00:00-06:00 服務時段限制，夜間再補完。
4. **PDF 來源優先序**：優先使用司法院 `轉存PDF` 或 API `JFULLPDF` 官方 PDF；只有官方 PDF 暫時不可取時，才可臨時由 HTML 文字生成 PDF，並以標記檔區分，夜間 API 成功後應覆蓋成官方 PDF。
5. **分類表視覺規則**：分類表必須整列依主分類上色，並保留醒目的 `interpreter_marker` 欄，標示 `實質通譯爭點`、`僅條文引用`、`含通譯文字`、`缺全文` 等狀態。摘錄欄中的「通譯」必須標為 `【通譯】`，避免埋在長文字中。
6. **再審/抗告條文排除規則**：刑事訴訟法第 420 條「證言、鑑定或通譯已證明其為虛偽」以及第 403 條「證人、鑑定人、通譯及其他非當事人」只是條文清單時，不得標成高信心通譯爭點；應標 `法條或程序清單引用`、`僅條文引用`，信心不得為高。只有理由段實際討論通譯虛偽、不實、錯譯、未通譯、通譯選任、公正性、證據能力、偵訊/警詢/審判傳譯等，才標 `實質通譯爭點`，且必須引出該段原文。
7. **完成報告**：每輪必須輸出 expected、txt_count、pdf_count、missing preview、補抓來源與失敗原因，方便隔日續跑。

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
