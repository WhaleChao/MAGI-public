---
name: judicial-web-search
description: 司法院裁判書網頁爬蟲（用 Playwright 控制本機 Chrome）：可用關鍵字查詢判決清單、抓取單筆全文純文字；提供自測。
author: CASPER
created: 2026-02-14
---

# judicial-web-search

這個技能用「瀏覽器」方式操作司法院裁判書查詢頁（避免被 WAF 擋、也避開某些 Python SSL 驗證問題）。

## 指令
1. `help`
1. `self_test`
1. `search { ... }`（也支援 `搜尋判決 { ... }`）
1. `fetch_text { ... }`（也支援 `抓全文 { ... }`）

也可以用更口語的用法（不用 JSON）：
- `搜尋判決 詐欺`
- `抓全文 https://judgment.judicial.gov.tw/FJUD/data.aspx?id=...`

`search` payload（JSON）：
- `keywords`（通常必填）: 例如 `詐欺`、`侵權&臺灣高等法院`（會直接填入「全文內容」）
- `max_results`（可選，預設 10）
- `headless`（可選，預設 true）
- `timeout_sec`（可選，預設 60）
- `courts`（可選）: 法院篩選（多選）。例如 `["最高法院"]`、`["最高行政法院(含改制前行政法院)", "臺北高等行政法院"]`
- `case_year` / `case_word` / `case_no`（可選）: 以案號欄位輔助查詢（例如 `76` / `台上` / `192`）

補充：
- 若有提供 `case_year` / `case_word` / `case_no` 這組結構化案號欄位，`keywords` 可以留空，直接走精準案號查詢。

輸出補充：
- `results` 只會回傳前 3 筆預覽（避免輸出被截斷）
- `results_path` 會存完整結果 JSON

`fetch_text` payload（JSON）：
- `url`（必填）: `https://judgment.judicial.gov.tw/FJUD/data.aspx?id=...`
- `headless`（可選，預設 true）
- `timeout_sec`（可選，預設 45）
- `max_chars`（可選，預設 40000）

輸出補充：
- `text_path`：全文會寫到本機快取檔（避免技能輸出被截斷導致 JSON 壞掉）
- `text_preview`：短預覽（固定很短，避免 stdout 被截斷）
