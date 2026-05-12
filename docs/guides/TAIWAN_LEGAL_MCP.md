# 台灣法律資料庫 MCP 對接

MAGI 可選擇性對接 `lawchat-oss/mcp-taiwan-legal-db`，用公開官方來源補強法律資料查詢：

- 司法院裁判書搜尋與全文
- 全國法規資料庫條文
- 憲法法庭與大法官解釋

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

## MAGI 使用方式

使用者問法律資料查詢時，MAGI 會使用 MCP，不需要特別開關或使用固定咒語：

- `實務見解 預售屋遲延交屋`
- `查判決 遲延交屋`
- `查裁判 114年度台上字第3753號`
- `查法條 民法第184條`
- `查釋字 748`

判決與實務見解會保留既有本地見解庫與判決收集流程，並追加 MCP 的司法院公開資料來源；法規與釋憲問題則可直接調用 MCP。查不到時會明確回報查不到，不回到一般聊天猜測。

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
