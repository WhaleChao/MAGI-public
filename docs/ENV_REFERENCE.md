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
| Feature | `MAGI_TELEGRAM_BOT_TOKEN` | — | Telegram Bot token（需 MAGI_ENABLE_TELEGRAM=1） |
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
| `MAGI_TELEGRAM_BOT_TOKEN` | `MAGI_ENABLE_TELEGRAM=1` | Telegram Bot token |
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
| `MAGI_MAIN_MODEL` | string | `gemma-4-e4b-it-4bit` | 主要推理模型；若填入中國模型家族會自動退回安全預設 |
| `CASPER_LOCAL_MODEL` | string | `gemma-4-e4b-it-4bit` | 本地推理模型 |
| `MAGI_TEXT_PRIMARY_MODEL` | string | `gemma-4-e4b-it-4bit` | 白天穩定主模型 |
| `MAGI_TEXT_HEAVY_MODEL` | string | `gemma-4-26b-a4b-it-4bit` | 高品質本地候選；必須通過智慧路由資源閘門才會使用 |
| `CASPER_CLASSIFIER_MODEL` | string | `gemma-4-e4b-it-4bit` | 意圖分類模型 |
| `MAGI_SMART_MODEL_ROUTER` | bool | `1` | 啟用智慧模型路由：依任務、目前上線模型與資源狀態選 E4B / 26B / @heavy |
| `MAGI_ROUTER_26B_MIN_DISK_GB` | int | `70` | 26B-A4B 最低可用磁碟；低於門檻即退回 E4B |
| `MAGI_ROUTER_26B_MIN_FREE_GB` | int | `8` | 26B-A4B 最低 free + inactive memory |
| `MAGI_ROUTER_26B_MAX_SWAP_GB` | int | `20` | 26B-A4B 最高 swap 使用量 |
| `MAGI_ROUTER_QUALITY_PROMPT_CHARS` | int | `6000` | 超過此長度的摘要 / 翻譯 / 法律分析視為高品質任務 |
| `MAGI_ROUTER_26B_MAX_PROMPT_CHARS` | int | `60000` | 超過此長度不啟用 26B，避免 KV cache 造成 OOM |
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
<!-- Deprecated model-routing rows were removed 2026-04-20. Text-model routing
     is now handled by MAGI_OMLX_* / MAGI_MAIN_MODEL / CASPER_LOCAL_MODEL.
     Legacy Telegram deployments may still expose
     OPENCLAW_TELEGRAM_BOT_TOKEN, but new installs should use
     MAGI_TELEGRAM_BOT_TOKEN. -->

### Tier 7: Runtime Paths (Override)

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `MAGI_ROOT_DIR` | path | 自動推算 | MAGI 根目錄 |
| `MAGI_DATA_DIR` | path | `{root}/data` | 資料目錄 |
| `MAGI_LOG_DIR` | path | `{root}/.agent` | Log 目錄 |
| `MAGI_CONFIG_DIR` | path | `{root}` | 設定目錄 |
| `MAGI_EXPORTS_DIR` | path | `{root}/static/exports` | 匯出目錄 |

### Tier 8: PDF / OCR Extraction

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `MAGI_OPENDATALOADER_PDF_ENABLE` | bool/auto | `auto` | 啟用 OpenDataLoader PDF 作為版面感知文字/OCR 供應器；缺套件或 Java 時自動退回原流程 |
| `MAGI_OPENDATALOADER_PDF_HYBRID` | string | — | OpenDataLoader hybrid OCR 模式；留空時只用預設轉換 |
| `MAGI_OPENDATALOADER_PDF_MAX_CHARS` | int | `24000` | 每份 PDF 從 OpenDataLoader 讀入 MAGI 的最大字元數 |
| `MAGI_PDF_NAMER_OPENDATALOADER_MIN_SCORE` | float | `0.55` | PDF 命名採用 OpenDataLoader 結果的最低品質分數 |
| `MAGI_PDF_NAMER_OPENDATALOADER_MIN_GAIN` | float | `0.08` | PDF 命名改用 OpenDataLoader 結果所需的最低品質增益 |
| `MAGI_CHANDRA_OCR_ENABLE` | bool | `0` | 私用版專用 Chandra OCR fallback；僅在既有 OCR 低品質時嘗試 |
| `MAGI_CHANDRA_PRIVATE_DEPLOYMENT` | bool | `0` | Chandra 私用版確認；未設定時即使 enable 也不會推論 |
| `MAGI_CHANDRA_ACCEPT_MODEL_LICENSE` | bool | `0` | Chandra model license 確認；未設定時不會推論 |
| `MAGI_CHANDRA_ACCEPT_QWEN_BACKEND` | bool | `0` | Chandra OCR 2 upstream 標示 `qwen3_5`/Qwen 3.5，私用版啟用前必須明確確認 |
| `MAGI_CHANDRA_CLI` | path | auto | Chandra CLI 路徑；建議使用隔離 venv，不污染 MAGI 主環境 |
| `MAGI_CHANDRA_OCR_METHOD` | `vllm`/`hf` | `vllm` | Chandra 後端；HF 另需 `MAGI_CHANDRA_ALLOW_HF=1`，避免誤下載大型模型 |
| `MAGI_CHANDRA_VLLM_API_BASE` | URL | `http://127.0.0.1:8000/v1` | Chandra vLLM OpenAI-compatible endpoint |
| `MAGI_CHANDRA_OCR_MIN_SCORE` | float | `0.45` | pdf-namer 既有 OCR 分數低於此值才呼叫 Chandra |
