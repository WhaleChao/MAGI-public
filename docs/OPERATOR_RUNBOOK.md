# MAGI Operator Runbook v2

版本：v2.2 | 日期：2026-05-12

---

## 1. 系統概覽

MAGI（Multi-Agent Governance Infrastructure）是一套多代理治理基礎設施，核心由以下元件組成：

- **CASPER** — 主決策與協調節點（Flask 後端，port 5002）
- **Tools API** — 外部工具 HTTP API（Flask，port 5003）
- **Orchestrator** — 自然語言路由與任務編排引擎（委派至 pipelines/ + domains/）
- **Routing Layer** — 統一路由層（api/routing/，含 Registry 系統）
- **Skills** — 70+ 可插拔技能模組
- **Channels** — LINE Bot、Discord Bot、Telegram Bot
- **Status Bar** — macOS 選單列即時監控（gui/magi_menubar.py）
- **CLI** — `magi` 命令列管理工具（scripts/magi_cli.sh）
- **MTP Sidecar** — Gemma 4 E4B assistant / draft model sidecar（FastAPI，port 8090）
- **Public Release Gate** — `scripts/public_release_audit.py` 公開前敏感資訊稽核

---

## 2. 環境需求

| 項目 | 最低需求 |
|------|---------|
| OS | macOS 13+ / Ubuntu 22.04+ |
| Python | 3.12+ |
| Database | MariaDB 10.6+ / MySQL 8.0+ |
| RAM | 8 GB（建議 16 GB） |
| Disk | 30 GB 可用空間建議值；低於 10 GB 視為緊急 |

---

## 3. 首次安裝

### 新手安裝（建議）

```bash
# 1. Clone repo
git clone https://github.com/WhaleChao/MAGI-v2.git && cd MAGI-v2

# 2. 先預演，不改動系統
python3 scripts/install_magi.py --dry-run --check-live

# 3. 正式安裝 core + optional dependencies
python3 scripts/install_magi.py --yes

# 4. 產生第一次使用 checklist 與本機 .env
python3 scripts/first_run_setup.py --write-env
python3 scripts/first_run_setup.py --json

# 5. 編輯 .env 後，偵測本機硬體、Python 套件、MLX/MTP sidecar、模型目錄
python3 scripts/magi_doctor.py
```

`scripts/install_magi.py` 預設採 dry-run，只有傳入 `--yes` 才會建立 `.venv`、安裝 requirements 並執行 doctor。`scripts/first_run_setup.py` 會建立不進 git 的 `.env`、補上本機隨機 secret、列出缺少的必要設定，且不輸出 token 或密碼。`scripts/magi_doctor.py --json` 可輸出機器可讀報告，適合附在 issue 或遠端協助紀錄。

### 維運者手動安裝

```bash
# 1. Clone repo
git clone https://github.com/WhaleChao/MAGI-v2.git && cd MAGI-v2

# 2. Bootstrap（自動建立 venv、安裝依賴、引導設定）
bin/bootstrap

# 3. 編輯 .env（bootstrap 會提示）
#    至少填寫: DB_HOST, DB_USER, DB_PASSWORD, FLASK_SECRET_KEY
#    通道 credentials 依需求填寫

# 4. 初始化資料庫
mysql -u <user> -p <dbname> < setup_magi_brain.sql
mysql -u <user> -p <dbname> < init_auth.sql

# 5. 啟動
bin/start

# 6. 安裝 CLI 工具（建議）
cp scripts/magi_cli.sh /opt/homebrew/bin/magi && chmod +x /opt/homebrew/bin/magi
```

---

## 4. 日常操作

### `magi` CLI（建議方式）

```bash
magi                    # 顯示完整系統狀態（預設）
magi status             # 同上
magi start              # 啟動 daemon + 狀態列
magi stop               # 停止 daemon + 所有服務 + 狀態列
magi restart            # 完整重啟
magi menubar            # 僅重啟 macOS 狀態列
magi zombie             # 偵測並清理殭屍進程
```

### 傳統方式

```bash
# 啟動
bin/start               # 前台啟動
bin/start &             # 背景啟動

# 停止
kill $(cat rpc_server.pid 2>/dev/null)
# 或直接 Ctrl-C（前台模式）
```

### 健康檢查

```bash
magi status                             # 完整系統狀態
curl http://127.0.0.1:5002/health       # Server health
curl http://127.0.0.1:5003/health       # Tools API health
curl http://127.0.0.1:8090/health       # Gemma 4 E4B / MTP sidecar health
python3 scripts/magi_doctor.py          # 新手偵測精靈
python3 scripts/public_release_audit.py # 公開前敏感資訊稽核
python3 skills/ops/system_test.py       # 12 項系統測試
python3 skills/magi-doctor/action.py --task diagnose  # 完整診斷
```

### 對外 / 商用 live gate

```bash
./venv/bin/python scripts/ops/run_test_suite.py --suite ci
./venv/bin/python scripts/ops/run_test_suite.py --suite smoke50
./venv/bin/python scripts/ops/run_test_suite.py --suite production-live --json-out .runtime/production_live_latest.json
./venv/bin/python scripts/ops/run_test_suite.py --suite commercial-release --json-out .runtime/commercial_release_latest.json
./venv/bin/python scripts/public_release_audit.py --strict
```

`smoke50` 是基本冒煙測試；`production-live` 與 `commercial-release` 才是交付他人使用前的 live 門檻。

### 查看 Logs

```bash
tail -f .agent/server.log                      # 主伺服器 log（JSON 格式）
tail -f .agent/channel_delivery_audit.jsonl     # 通道投遞審計
```

---

## 5. LaunchAgent 管理

MAGI 使用 macOS LaunchAgents 管理程序生命週期。所有 plist 位於 `~/Library/LaunchAgents/`。

### 已註冊的 LaunchAgents

| Label | 用途 | 狀態 |
|-------|------|------|
| `com.magi.daemon` | 主程序 daemon（啟動 server、discord、tools_api） | 常駐 |
| `com.magi.menubar` | macOS 選單列健康監控 | 常駐 |
| `com.magi.omlx` | Gemma-4 26B 推理引擎（port 8080） | 常駐 |
| `com.magi.mlx-mtp` | Gemma 4 E4B assistant / MTP sidecar（port 8090） | 常駐 |
| `com.magi.omlx-embed` | ModernBERT embedding（port 8081） | 常駐 |
| `com.magi.db-proxy` | SSH tunnel 至遠端 MariaDB | 常駐 |
| `com.magi.smb-reconnect` | NAS 網路中斷自動重連 | 常駐 |
| `com.magi.nightly-*` | 夜間排程任務（多個） | 按排程 |

### 手動管理

```bash
# 查看已載入的 MAGI agents
launchctl list | grep magi

# 重新載入特定 agent
launchctl bootout gui/$(id -u)/com.magi.menubar
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.magi.menubar.plist

# 強制重啟
launchctl kickstart -k gui/$(id -u)/com.magi.menubar
```

---

## 6. 設定管理

### 環境變數分層

| 分類 | 說明 | 缺少時行為 |
|------|------|-----------|
| Core Required | `DB_HOST`, `DB_USER`, `DB_PASSWORD`, `FLASK_SECRET_KEY` | 阻止啟動 |
| Feature-scoped | `MAGI_LINE_*`, `DISCORD_BOT_TOKEN`, `OPENCLAW_TELEGRAM_BOT_TOKEN` | 僅該通道不可用 |
| Recommended | `MAGI_CLOUDFLARED_PATH`, `MAGI_OMLX_SUMMARY_MODEL` | Warning |

### Registry 系統

v2 將所有硬編碼值外部化為 JSON + Python Registry：

| JSON 設定檔 | Python Registry | 用途 |
|-------------|----------------|------|
| `json/services.json` | `api/routing/service_registry.py` | 服務端點 |
| `json/models.json` | `api/routing/model_registry.py` | 模型別名 |
| `json/nodes.json` | `api/routing/node_registry.py` | 節點 IP |
| `json/datastores.json` | `api/routing/datastore_registry.py` | 資料庫連線 |

覆寫優先級：環境變數 → JSON 設定 → 硬編碼後備

### Feature Enable Flags

```bash
MAGI_ENABLE_LINE=1       # 啟用 LINE Bot
MAGI_ENABLE_DISCORD=0    # 停用 Discord Bot
MAGI_ENABLE_TELEGRAM=0   # 停用 Telegram Bot
MAGI_ENABLE_REMOTE_DB=0  # 停用遠端 DB 同步
MAGI_ENABLE_MTP_DRAFT=1  # 允許 oMLX provider 帶 draft/MTP metadata
```

### Gemma 4 E4B / MTP Sidecar

```bash
# 前台啟動
python3 scripts/serve_mlx_mtp.py --host 127.0.0.1 --port 8090

# launchd 啟動（macOS）
cp config/launchagents/com.magi.mlx-mtp.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.magi.mlx-mtp.plist
launchctl kickstart -k gui/$(id -u)/com.magi.mlx-mtp

# 驗證
curl http://127.0.0.1:8090/health
python3 scripts/live_magi_mtp_eval.py --all-tools
```

Live acceptance 的門檻是：JSON tool route、ReAct 真實工具呼叫、全部工具選擇案例、工具混淆 guard 皆通過，且 hallucination safety 的 unsafe rate 必須在可接受範圍內。

### 安全相關

```bash
MAGI_CORS_ORIGINS=https://your-frontend.com   # CORS 白名單
JUDICIAL_API_ALLOW_INSECURE_SSL=0              # 預設關閉
MAGI_FORCE_HTTPS=1                             # 啟用 Secure cookie
```

---

## 6A. 公開發布檢查

公開前必跑：

```bash
python3 scripts/public_release_audit.py --public-isolation
python3 scripts/first_run_setup.py --public --json
python3 scripts/install_magi.py --dry-run --check-live
git status --short
```

本公開版已移除 git 追蹤中的私有 runtime / operator artifacts：

- `.claude/`
- `.claire/`
- `.runtime/`
- `runtime/supplement_cache/`
- `docs/deploy/`

若 `public_release_audit.py --strict` 回報 error 或 warning，不得 push。公開推送前請使用 `--public-isolation --strict`；公開版隔離會阻擋私有實務見解來源整合、私人信箱與私人 NAS 標記，`.gitignore` 中保留忽略規則不算違規。公開安裝版本若不含私有 DB，可另用 `--skip-db` 做安裝性檢查，但正式環境不得跳過 DB / NAS / channel live gate。

### 6B. Google Calendar / 法扶計數檢查

- 一般 OSC 事件匯入要求標題或描述開頭有 OSC 案件系統編號。
- 法扶活動計數可接受同事手動行程，但必須由 DB 判斷為法扶案件，且內容屬於開庭、會議、律見、閱卷、電話聯繫。
- 同名多案要靠 `laf_case_no`、`application_no`、`case_category=法律扶助案件`、`legal_aid_status`、案由或 OSC 編號消歧。仍無法消歧時跳過，不猜測。
- live 檢查可用 GCal dry-run；不得為了補數字手動把不明事件寫入 `case_todos`。

### 6C. NAS / 磁碟安全

- `/Volumes/lumi`、`/Volumes/homes` 不得預先建立空目錄，避免 macOS 掛成 `lumi-1` / `homes-1`。
- 可重建快取可清；Paperclip / 單機版 JSON、pickle、db、sqlite 狀態檔不可清。
- 退役 Ollama root 可清，但正式模型、訓練成果、司法 raw backlog、DB backup、NAS 資料不可清。

---

## 7. 資料庫操作

### DB 容錯機制

MAGI v2 支援遠端/本地雙活資料庫容錯：

```
遠端 DB (Keeper: MAGI_REMOTE_DB_HOST:3306)
    │
    ├─ 正常：遠端直連
    ├─ 斷線：自動切換至本地 DB
    └─ 恢復：自動 mysqldump 同步回本地
```

檢查容錯狀態：
```bash
magi status                    # 看 Database 段
curl http://127.0.0.1:5002/health  # 看 db_failover 段
```

### 備份

```bash
mysqldump -u <user> -p <dbname> > backup_$(date +%Y%m%d).sql
```

### 還原

```bash
mysql -u <user> -p <dbname> < backup_20260405.sql
```

### Schema Migration

```bash
python migrations/migrate.py status    # 查看版本
python migrations/migrate.py upgrade   # 升級
python migrations/migrate.py rollback  # 回滾上一個
```

---

## 8. 升級流程

```bash
# 1. 備份
mysqldump -u <user> -p <dbname> > pre_upgrade_$(date +%Y%m%d).sql
cp .env .env.bak

# 2. 停止服務
magi stop

# 3. 拉取新版本
git pull origin main

# 4. 更新依賴
venv/bin/pip install -r requirements.txt

# 5. 執行 migration
python migrations/migrate.py upgrade

# 6. 更新 CLI
cp scripts/magi_cli.sh /opt/homebrew/bin/magi

# 7. 重啟
magi start
```

### 回滾

```bash
magi stop
git checkout <previous-tag>
python migrations/migrate.py rollback
mysql -u <user> -p <dbname> < pre_upgrade_20260405.sql
magi start
```

---

## 9. 故障排除

### MAGI 無法啟動

| 症狀 | 可能原因 | 處理 |
|------|---------|------|
| `Missing CORE required environment variables` | .env 缺少核心變數 | 檢查 DB_HOST, DB_USER, DB_PASSWORD, FLASK_SECRET_KEY |
| `ModuleNotFoundError` | 依賴未安裝 | `venv/bin/pip install -r requirements.txt` |
| `Connection refused (DB)` | 資料庫未啟動或連線資訊錯誤 | 檢查 DB 服務狀態與 .env 設定 |
| `Address already in use` | Port 5002/5003 被佔用 | `lsof -i :5002` 找出佔用程序 |
| 網頁 `not_found` | daemon 尚未載入新版 route | 重啟 MAGI daemon 後重試 |
| 結案搬移 HTTP 502 | 搬檔耗時或 NAS 掛載異常 | 查搬移任務狀態、確認 `/Volumes/lumi` 正確掛載 |

### LINE Bot 不回應

1. 檢查 `MAGI_ENABLE_LINE=1` 且 credentials 已設定
2. 檢查 webhook URL 是否正確指向 MAGI
3. 查看 `.agent/server.log` 中的 LINE 相關 error
4. 執行 `magi status` 查看服務狀態

### 推理卡死 / 回應過慢

1. 檢查 oMLX 是否正常: `magi status` 看 oMLX Inference 段
2. 直接測試: `curl http://localhost:8080/v1/models`
3. 檢查模型是否載入: 看 log 中的 inference timeout
4. 重啟 oMLX: `launchctl kickstart -k gui/$(id -u)/com.magi.omlx`

### Gemma 4 MTP benchmark / rollback

目前 `omlx serve --help` 未顯示 draft model 參數；MTP 預設關閉，需先用 benchmark 驗證 runtime 或 sidecar。

```bash
# E4B baseline
python3 scripts/benchmark_gemma4_mtp.py \
  --tasks benchmarks/gemma4_mtp/e4b_tasks.jsonl \
  --model gemma-4-e4b-it-4bit \
  --variant baseline \
  --probe-runtime

# MTP-capable runtime / sidecar 驗證
python3 scripts/benchmark_gemma4_mtp.py \
  --tasks benchmarks/gemma4_mtp/e4b_tasks.jsonl \
  --model gemma-4-e4b-it-4bit \
  --variant mtp \
  --base-url http://127.0.0.1:8090/v1 \
  --draft-model gemma-4-E4B-it-assistant-bf16

# Live acceptance：sidecar health + JSON 工具路由 + ReAct 工具呼叫 + 幻覺安全
python3 scripts/live_magi_mtp_eval.py \
  --base-url http://127.0.0.1:8090/v1 \
  --max-unsafe-rate 0

# 啟動 MLX/VLM MTP sidecar（手動）
python3 scripts/serve_mlx_mtp.py --host 127.0.0.1 --port 8090

# 啟動 MLX/VLM MTP sidecar（launchd）
cp config/launchagents/com.magi.mlx-mtp.plist ~/Library/LaunchAgents/
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.magi.mlx-mtp.plist 2>/dev/null || true
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.magi.mlx-mtp.plist
launchctl kickstart -k gui/$(id -u)/com.magi.mlx-mtp

# 立即 rollback
export MAGI_ENABLE_MTP_DRAFT=0
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.magi.mlx-mtp.plist 2>/dev/null || true
```

### 狀態列不更新

1. `magi menubar` — 重啟狀態列
2. 檢查 log: `tail -f ~/Library/Logs/magi_menubar.log`
3. 手動啟動: `python3 gui/magi_menubar.py`

### 殭屍進程

```bash
magi zombie              # 自動偵測並清理
```

清理流程：
1. 掃描所有 Z 狀態進程
2. 向父進程發送 SIGCHLD
3. 若仍存在，SIGTERM 殺死不響應的父進程
4. 報告最終狀態

---

## 10. 監控要點

| 指標 | 檢查方式 | 告警門檻 |
|------|---------|---------|
| Server 存活 | `magi status` 或 `curl /health` | 連續 3 次失敗 |
| DB 連線 | `magi status` Database 段 | 容錯切換時 |
| NAS 掛載 | `magi status` NAS Mounts 段 | NOT MOUNTED |
| 遠端節點 | `magi status` Remote Nodes 段 | DOWN |
| 磁碟空間 | `df -h` / `disk_low_water_alarm` | < 30 GB 警告；< 10 GB 緊急 |
| Log 大小 | `du -sh .agent/` | > 500 MB |
| 推理延遲 | Log 中的 inference_ms | > 30s 平均 |
| 排程任務 | 狀態列 Cron Jobs 子選單 | 超過 25 小時未執行 |
| 殭屍進程 | `magi zombie` | > 0 |

### macOS 狀態列監控

狀態列（`gui/magi_menubar.py`）每 5 秒自動收集以下資訊：

- 所有核心服務 PID 狀態
- 遠端節點 TCP + HTTP 健康
- 每個排程任務的最後執行時間
- NAS 掛載狀態 + 磁碟用量
- 資料庫容錯詳情
- oMLX 推理引擎狀態

---

## 11. 安全注意事項

- `.env` 不得進入 git（已在 .gitignore）
- CORS 僅允許白名單來源（`MAGI_CORS_ORIGINS`）
- Insecure SSL fallback 預設關閉
- 所有 API 端點需要認證（dashboard session 或 API key）
- Session cookie 啟用 HttpOnly + SameSite=Lax
- 定期輪換 `FLASK_SECRET_KEY` 和 `MAGI_API_KEY`
- Registry JSON 檔案不含密碼（僅端點資訊）

---

## 12. 檔案結構

```
MAGI/
├── api/                     # 核心 API 層
│   ├── server.py            # Flask 入口（802 行）
│   ├── orchestrator.py      # 路由中樞（2,335 行）
│   ├── tools_api.py         # Tools HTTP API
│   ├── discord_bot.py       # Discord 整合 + 排程
│   ├── db_failover.py       # DB 容錯控制器
│   ├── blueprints/          # Flask Blueprint（7 模組）
│   ├── webhooks/            # 頻道 Webhook（2 模組）
│   ├── pipelines/           # 處理管線（8 模組）
│   ├── domains/             # 領域流程（6 模組）
│   ├── routing/             # Registry + 路由（14 模組）
│   ├── permissions/         # RBAC 權限
│   ├── events/              # 事件系統
│   ├── hooks/               # 鉤子匯流排
│   ├── tasks/               # 任務運行時
│   ├── session/             # 會話管理
│   ├── tools/               # 工具登錄
│   └── agents/              # 多代理運行時
├── json/                    # 宣告式設定
│   ├── services.json        # 服務端點
│   ├── models.json          # 模型定義
│   ├── nodes.json           # 節點定義
│   └── datastores.json      # 資料庫連線
├── gui/                     # macOS 狀態列
│   └── magi_menubar.py
├── scripts/                 # 操作腳本
│   ├── magi_cli.sh          # `magi` CLI
│   └── ops/                 # 操作腳本
├── skills/                  # 67+ 可插拔技能
├── providers/               # LLM 供應商抽象
├── migrations/              # DB schema migration
├── tests/                   # 90+ 測試檔
├── templates/               # Web UI 模板
├── static/                  # 靜態資源
├── .env                     # 環境設定（不進 git）
├── .env.example             # 設定範本
└── CONSTITUTION.md          # 治理規則
```
