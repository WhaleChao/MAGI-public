# MAGI Agent Gateway（RC643）

## 目的

RC643 現在的 Tools API 同時承擔歷史相容、技能目錄、執行與外部整合，對 Goose、OpenHands、Codex 或 Cline 來說不夠直覺，也容易把過多能力暴露給通用 Agent。

`/agent/v1` 是一個窄化的、身份綁定的 MCP-facing 介面。它不是新的執行核心，而是把外部 Agent 接到 MAGI 既有的受控自主流程、工具權限、領域 handler 與 receipt。

## 協定邊界

- modern transport 固定使用 MCP `2026-07-28`：每一個 request 都在 `params._meta` 宣告 protocol version 與 client capabilities，以 `server/discover` 取得能力；不建立 protocol session，也不接受 `initialize`。
- r59 時代 client 由明確的 legacy adapter 支援 `2024-11-05`、`2025-03-26`、`2025-06-18` 與 `2025-11-25`；不能把格式錯誤的 modern request 靜默降級成 legacy。
- modern HTTP 必須攜帶一致的 `MCP-Protocol-Version`；`Mcp-Method`、`Mcp-Name` 若存在也必須與 JSON-RPC body 一致。
- request／response 維持 W3C `traceparent`，但預設 trace 不記錄案件內容、工具參數、模型 prompt 或憑證。

## 安全邊界

- 每個 MCP client process 必須以 `X-MAGI-Agent-User-ID` 與 `X-MAGI-Agent-Platform` 綁定一個身份；不能在工具參數中冒用另一個身份。
- 所有 Agent Gateway 請求都需要 `MAGI_EXTERNAL_API_KEY`（或 `OPENCLAW_GATEWAY_TOKEN`）。API key 不放在 URL query string。
- `magi_read` 只接受非寫入意圖；寫入或不可逆意圖會被拒絕，必須改用 `magi_prepare_action`。
- `magi_prepare_action` 只建立 SQLite-backed 計畫，不執行動作；核准碼只在建立計畫的回覆中出現。
- `magi_confirm_plan` 只接受一次性、身份綁定、具期限的核准碼，並把確認命令送回既有 message pipeline。
- Gateway 不提供 raw shell、raw database、任意 skill install、任意 skill run 或 release/cutover 操作。
- `magi_case_status` 強制關閉 runtime mutation flags；`magi_fetch` 沿用 MAGI 的 private/loopback SSRF 防護。

## 暴露工具

唯讀：`magi_health`、`magi_capabilities`、`magi_read`、`magi_case_status`、`magi_search`、`magi_research`、`magi_fetch`、`magi_summarize`、`magi_list_plans`、`magi_get_plan`。

計畫控制：`magi_prepare_action`、`magi_cancel_plan`。

唯一可造成業務副作用的工具：`magi_confirm_plan`；它仍受 MAGI 原有專用流程與人工核准控制，成功回覆也不宣稱業務完成，必須以 receipt 與專用流程結果為準。

## 本機 stdio 設定

stdio bridge 不要求安裝 `mcp` Python 套件：

```sh
export MAGI_AGENT_GATEWAY_URL=http://127.0.0.1:5003
export MAGI_AGENT_GATEWAY_API_KEY='由部署環境注入'
export MAGI_AGENT_USER_ID='local-operator'
export MAGI_AGENT_PLATFORM='GOOSE'
python3 /path/to/MAGI/bin/agent_mcp.py --transport stdio
```

Goose、Cline 等客戶端可使用下列 MCP server 形狀；請將路徑與環境值換成實際部署值：

```json
{
  "mcpServers": {
    "magi": {
      "command": "python3",
      "args": ["/path/to/MAGI/bin/agent_mcp.py", "--transport", "stdio"],
      "env": {
        "MAGI_AGENT_GATEWAY_URL": "http://127.0.0.1:5003",
        "MAGI_AGENT_GATEWAY_API_KEY": "由客戶端 secret store 注入",
        "MAGI_AGENT_USER_ID": "local-operator",
        "MAGI_AGENT_PLATFORM": "GOOSE"
      }
    }
  }
}
```

## Stateless HTTP / 遠端主機

候選 release 預先安裝且鎖定 HTTP runtime 後，可啟動 stateless MCP endpoint：

```sh
magi-agent-mcp --transport streamable-http --host 127.0.0.1 --port 8765
```

此 listener 只能 bind loopback，對外必須經 TLS reverse proxy。遠端模式必須設定 `MAGI_MCP_OAUTH_REQUIRED=1`、resource URL、issuer、audience，以及 release 內固定的 public PEM 或 JWKS snapshot；request path 不會在執行時下載 key。伺服器提供 RFC 9728 protected-resource metadata，scope 分成 `magi:read`、`magi:plan`、`magi:confirm`。API key 仍只供本機 gateway bridge，不能替代遠端 OAuth。

MCP client 端採 `config/mcp/approved_servers.json` default-deny catalog。每個可啟用 server 必須固定絕對 executable、SHA-256、來源 commit、transport 與 arguments；production 禁止 runtime install。空 catalog 是安全預設，不表示有任何第三方 server 已獲核准。

MAGI 的 production release、scheduler、writer ownership 仍由既有 cutover gate 管理；MCP Gateway 不能執行 release/cutover，也不能把 A2A 或 WHALE 聯合運算重新打開。

## 建議驗證順序

1. 以 modern `server/discover` 與 legacy `initialize` 分別驗證雙協定邊界；不得共用 session state。
2. 用 `magi_health`、`magi_capabilities` 確認身份、工具清單與 MAGI readiness。
3. 用 `magi_case_status` 驗證只讀案件快照。
4. 用一個無副作用的 `magi_read` 驗證 HTTP、OAuth scope 與 trace propagation。
5. 以測試資料執行 `magi_prepare_action`，確認 list/get 不會返回核准碼。
6. 只有在人工明確提供一次性核准碼時，才執行 `magi_confirm_plan`；驗證 plan status、receipt 與重播拒絕。
7. sealed candidate 必須附官方 MCP Inspector 的 modern／legacy transport、tool schema、approval、錯誤與 trace 測試證據；單元測試不能替代該項證據。
