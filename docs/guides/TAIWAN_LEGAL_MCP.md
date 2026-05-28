# 台灣法律資料庫與全判決語義檢索對接

MAGI 可選擇性對接 `lawchat-oss/mcp-taiwan-legal-db`，用公開官方來源補強法律資料查詢：

- 司法院裁判書搜尋與全文
- 全國法規資料庫條文
- 憲法法庭與大法官解釋

2026-05-28 起，MAGI 也可對接 `aa0101181514/tw-legal-rag` 使用的 TLR 全判決語義檢索端點，補強「實務見解」與「判決捕捉與分類」：

- 對自然語意、法律議題、關鍵字較友善
- 回傳裁判來源與可核對的 citation bundle
- 不使用 LLM 生成見解，僅做檢索；MAGI 仍須依裁判全文核對後引用
- 查詢送出前會移除明顯電子郵件、身分證字號、電話、OSC 案件編號與長數字

## 安裝

```bash
cd ~/Desktop/MAGI_v2
venv/bin/python scripts/setup_taiwan_legal_mcp.py
```

安裝位置預設為 `.runtime/mcp-taiwan-legal-db`，不會提交到 git。

## 啟用設定

預設只要 `.runtime/mcp-taiwan-legal-db` 存在就會啟用。可用環境變數調整：

```bash
MAGI_TAIWAN_LEGAL_MCP_ENABLE=1
MAGI_TAIWAN_LEGAL_MCP_AUGMENT=1
MAGI_TAIWAN_LEGAL_MCP_MAX_RESULTS=3
MAGI_TAIWAN_LEGAL_MCP_FULLTEXT_LIMIT=1
MAGI_TAIWAN_LEGAL_MCP_ROOT=/absolute/path/to/mcp-taiwan-legal-db
```

TLR 全判決語義檢索預設啟用，可用環境變數調整：

```bash
MAGI_TWLEGALRAG_ENABLE=1
MAGI_TWLEGALRAG_AUGMENT=1
MAGI_TWLEGALRAG_CACHE_HITS=1
MAGI_TWLEGALRAG_MAX_RESULTS=3
MAGI_TWLEGALRAG_FULLTEXT_LIMIT=1
MAGI_TWLEGALRAG_BASE_URL=https://tlr.dr-lawbot.com
```

若服務方未來要求金鑰，可放在 `MAGI_TWLEGALRAG_API_KEY`；MAGI 不會在網頁狀態回傳金鑰內容。

## MAGI 使用方式

使用者問法律資料查詢時，MAGI 會使用 MCP，不需要特別開關或使用固定咒語：

- `實務見解 預售屋遲延交屋`
- `查判決 遲延交屋`
- `查裁判 114年度台上字第3753號`
- `查法條 民法第184條`
- `查釋字 748`

判決與實務見解會保留既有本地見解庫與判決收集流程，並追加 MCP 的司法院公開資料與 TLR 全判決語義檢索；法規與釋憲問題則可直接調用 MCP。查不到時會明確回報查不到，不回到一般聊天猜測。

判決捕捉與分類頁也會顯示「全判決語義預覽」按鈕，方便先確認搜尋式是否能在 TLR 找到相關裁判。預覽結果會保存成 `全判決語義檢索預覽.json`，並納入交付壓縮檔。

## 負載策略：TLR 優先，小量快取

有 TLR 之後，MAGI 不再需要每天對司法院 API 做大規模夜拉。預設 `MAGI_JUDICIAL_API_LOAD_MODE=tlr_smart`：

- TLR 用於「使用者問到時」即時查詢全判決語義結果。
- 命中的 TLR 裁判會小量快取到本地 `court_judgments`，下次相同議題優先走本地。
- 官方司法院 API 保留小量增量：夜間只補近 2 日、上限 300 筆；固定晨間只整理少量抽取摘要，不下載 PDF 附件。
- 一般 `tick`/巡檢預設不再處理司法院 backlog，避免每 2 小時就動用 DB 與磁碟 I/O。
- 既有 2,000 筆級白天批次可由 `scripts/ops/tune_judicial_api_load.py --apply` 停用。
- 需要回到舊行為時可明確設定 `MAGI_JUDICIAL_API_LOAD_MODE=legacy`，但不建議作為日常模式。

調整本機排程：

```bash
cd ~/Desktop/MAGI_v2
venv/bin/python scripts/ops/tune_judicial_api_load.py --apply
```

## 隱私與引用規則

- TLR 是公開遠端服務，請只送「法律關鍵字／法條／案由」，不要送當事人姓名、完整案情或機密文件摘要。
- MAGI 會做基本去識別化，但無法理解所有隱私語境；使用者仍應避免把個案事實寫進搜尋式。
- MAGI 回答法律問題時，TLR 回傳資料只能作為可核對裁判來源；引用前必須核對裁判全文。
- 若生成內容引用了 bundle 以外的裁判字號，MAGI 會標示需人工核對。

## Live 測試

```bash
cd ~/Desktop/MAGI_v2
venv/bin/python scripts/live_test_taiwan_legal_mcp.py
```

測試會驗證：

- `get_interpretation("釋字748")`
- `query_regulation(law_name="民法", article_no="184")`
- `search_judgments("預售屋 遲延交屋")`
- MAGI 的 `實務見解 預售屋遲延交屋` 指令會接上 MCP 補強結果
- MAGI 的 `查法條 民法第184條` 會直接調用 MCP

TLR 全判決語義檢索 live 測試：

```bash
cd ~/Desktop/MAGI_v2
venv/bin/python scripts/live_test_tw_legal_rag.py
```

測試會驗證：

- TLR `/v1/health`
- `通譯 最高法院` 可回傳裁判
- MAGI 實務見解補強管線可合併 TLR 結果
- 判決捕捉與分類頁的 TLR 預覽 helper 可產生結果
- citation bundle 可檢查引用是否在檢索結果內
