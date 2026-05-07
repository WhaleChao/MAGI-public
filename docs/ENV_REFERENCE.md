# MAGI Environment Variables Reference

版本：v1.0 | 日期：2026-03-19

---

## Quick Reference

| Required? | Variable | Default | Description |
|-----------|----------|---------|-------------|
| **CORE** | `DB_HOST` | — | 資料庫主機 |
| **CORE** | `DB_USER` | — | 資料庫使用者 |
| **CORE** | `DB_PASSWORD` | — | 資料庫密碼 |
| **CORE** | `FLASK_SECRET_KEY` | — | Flask session 加密金鑰 |
| Feature | `MAGI_LINE_CHANNEL_ACCESS_TOKEN` | — | LINE Bot token（需 MAGI_ENABLE_LINE=1） |
| Feature | `MAGI_LINE_CHANNEL_SECRET` | — | LINE webhook secret（需 MAGI_ENABLE_LINE=1） |
| Feature | `DISCORD_BOT_TOKEN` | — | Discord Bot token（需 MAGI_ENABLE_DISCORD=1） |
| Feature | `OPENCLAW_TELEGRAM_BOT_TOKEN` | — | Telegram Bot token（需 MAGI_ENABLE_TELEGRAM=1） |
| Optional | `MAGI_CORS_ORIGINS` | localhost | Tools API CORS 白名單 |
| Optional | `MAGI_API_KEY` | — | API 認證金鑰 |

---

## Classification

### Tier 1: Core Required
缺少任一項會阻止 MAGI 啟動（拋出 RuntimeError）。

| Variable | Type | Example | Description |
|----------|------|---------|-------------|
| `DB_HOST` | string | `127.0.0.1` | MariaDB/MySQL 主機位址 |
| `DB_USER` | string | `magi` | 資料庫使用者名稱 |
| `DB_PASSWORD` | string | — | 資料庫密碼 |
| `DB_PORT` | int | `3306` | 資料庫連接埠（預設 3306） |
| `DB_NAME` | string | `magi_brain` | 資料庫名稱（預設 magi_brain） |
| `FLASK_SECRET_KEY` | string | — | Flask session 加密金鑰。產生方式：`python3 -c "import secrets; print(secrets.token_hex(32))"` |

### Tier 2: Feature Enable Flags
控制各通道與功能是否啟用。設為 `0` 時對應的 credentials 不需要填。

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `MAGI_ENABLE_LINE` | bool | `1` | 啟用 LINE Bot 通道 |
| `MAGI_ENABLE_DISCORD` | bool | `0` | 啟用 Discord Bot 通道 |
| `MAGI_ENABLE_TELEGRAM` | bool | `0` | 啟用 Telegram Bot 通道 |
| `MAGI_ENABLE_REMOTE_DB` | bool | `0` | 啟用遠端 DB 同步 |

### Tier 3: Feature-Scoped Credentials
僅在對應 feature flag 啟用時才需要。

| Variable | Required when | Description |
|----------|--------------|-------------|
| `MAGI_LINE_CHANNEL_ACCESS_TOKEN` | `MAGI_ENABLE_LINE=1` | LINE Messaging API token |
| `MAGI_LINE_CHANNEL_SECRET` | `MAGI_ENABLE_LINE=1` | LINE Webhook validation secret |
| `DISCORD_BOT_TOKEN` | `MAGI_ENABLE_DISCORD=1` | Discord Bot token |
| `OPENCLAW_TELEGRAM_BOT_TOKEN` | `MAGI_ENABLE_TELEGRAM=1` | Telegram Bot token |
| `MAGI_REMOTE_DB_HOST` | `MAGI_ENABLE_REMOTE_DB=1` | 遠端 DB 主機 |
| `MAGI_REMOTE_DB_USER` | `MAGI_ENABLE_REMOTE_DB=1` | 遠端 DB 使用者 |
| `MAGI_REMOTE_DB_PASSWORD` | `MAGI_ENABLE_REMOTE_DB=1` | 遠端 DB 密碼 |

### Tier 4: Security & Policy

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `MAGI_API_KEY` | string | — | API 認證金鑰（保護 /api/* 端點） |
| `MAGI_CORS_ORIGINS` | csv | `http://localhost:3000,...` | Tools API CORS 白名單（逗號分隔） |
| `MAGI_FORCE_HTTPS` | bool | `0` | 啟用 Secure session cookie |
| `JUDICIAL_API_ALLOW_INSECURE_SSL` | bool | `0` | 允許 SSL 驗證失敗時 fallback（會留下 audit log） |
| `MAGI_NO_DELETE` | bool | `1` | 禁止自動刪除操作 |
| `MAGI_DB_NO_DELETE` | bool | `1` | 禁止自動刪除 DB 資料 |
| `MAGI_LAF_DRAFT_ONLY` | bool | `1` | LAF 僅限 draft 模式 |

### Tier 5: Node Identity & Federation

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `MAGI_ROLE` | string | `CASPER` | 節點角色：CASPER / BALTHASAR / MELCHIOR |
| `BALTHASAR_HOST` | string | — | Balthasar 節點 IP |
| `BALTHASAR_PORT` | int | `5002` | Balthasar 連接埠 |
| `MELCHIOR_HOST` | string | — | Melchior 節點 IP |
| `MELCHIOR_PORT` | int | `5002` | Melchior 連接埠 |
| `WATCHER_HOST` | string | — | Watcher 節點 IP |
| `WATCHER_PORT` | int | `5010` | Watcher 連接埠 |
| `MAGI_AVOID_DISTRIBUTED` | bool | `1` | 避免分散式推理 |

### Tier 6: LLM Configuration

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `MAGI_MAIN_MODEL` | string | `llama3.1:8b` | 主要推理模型 |
| `CASPER_LOCAL_MODEL` | string | `llama3.1:8b` | 本地推理模型 |
| `CASPER_CLASSIFIER_MODEL` | string | `gemma-4-e4b-it-4bit` | 意圖分類模型 |
| `MAGI_ENABLE_MTP_DRAFT` | bool | `0` | 啟用 Gemma 4 MTP / speculative decoding draft 欄位（需 runtime 支援） |
| `MAGI_E4B_DRAFT_MODEL` | string | `gemma-4-E4B-it-assistant-bf16` | E4B target 對應 assistant / draft model |
| `MAGI_26B_DRAFT_MODEL` | string | `gemma-4-26B-A4B-it-assistant-bf16` | 26B A4B target 對應 assistant / draft model |
| `MAGI_MTP_DRAFT_KIND` | string | `mtp` | draft decoding 類型 |
| `MAGI_MTP_BLOCK_SIZE` | int | `4` | MTP draft block size（需 benchmark 後調整） |
| `MAGI_HEAVY_AUTO_UPGRADE` | bool | `0` | 允許長文 / 低信心任務自動升級 26B（預設關閉） |
| `MAGI_HEAVY_MIN_CHARS` | int | `6000` | 自動升級 26B 的文字長度門檻 |
| `MAGI_MLX_MTP_HOST` | string | `127.0.0.1` | MLX/VLM MTP sidecar host |
| `MAGI_MLX_MTP_PORT` | int | `8090` | MLX/VLM MTP sidecar port |
| `MLX_MTP_BASE_URL` | string | `http://127.0.0.1:8090/v1` | `mlx_mtp` provider OpenAI-compatible base URL |
<!-- MAGI_OPENCLAW_PRIMARY_MODEL / MAGI_OPENCLAW_FALLBACK_MODEL rows
     removed 2026-04-20 (cleanup plan Phase 5): OpenClaw Gateway chain
     has been deleted. Text-model routing is now handled by
     MAGI_OMLX_* / MAGI_MAIN_MODEL / CASPER_LOCAL_MODEL. -->

### Tier 7: Runtime Paths (Override)

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `MAGI_ROOT_DIR` | path | 自動推算 | MAGI 根目錄 |
| `MAGI_DATA_DIR` | path | `{root}/data` | 資料目錄 |
| `MAGI_LOG_DIR` | path | `{root}/.agent` | Log 目錄 |
| `MAGI_CONFIG_DIR` | path | `{root}` | 設定目錄 |
| `MAGI_EXPORTS_DIR` | path | `{root}/static/exports` | 匯出目錄 |
