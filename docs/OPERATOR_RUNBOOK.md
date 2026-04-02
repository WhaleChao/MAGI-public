# MAGI Operator Runbook v1

版本：v1.0 | 日期：2026-03-19

---

## 1. 系統概覽

MAGI（Multi-Agent Governance Infrastructure）是一套多代理治理基礎設施，核心由以下元件組成：

- **CASPER** — 主決策與協調節點（Flask 後端，port 5002）
- **Tools API** — 外部工具 HTTP API（Flask，port 5003）
- **Orchestrator** — 自然語言路由與任務編排引擎
- **Skills** — 可插拔技能模組（pdf-namer、judgment-collector、magi-doctor 等）
- **Channels** — LINE Bot、Discord Bot、Telegram Bot（可選）

---

## 2. 環境需求

| 項目 | 最低需求 |
|------|---------|
| OS | macOS 13+ / Ubuntu 22.04+ |
| Python | 3.12+ |
| Database | MariaDB 10.6+ / MySQL 8.0+ |
| RAM | 8 GB（建議 16 GB） |
| Disk | 10 GB（不含模型檔案） |

---

## 3. 首次安裝

```bash
# 1. Clone repo
git clone <repo-url> MAGI && cd MAGI

# 2. Bootstrap（自動建立 venv、安裝依賴、引導設定）
bin/bootstrap

# 3. 編輯 .env（bootstrap 會提示）
#    至少填寫: DB_HOST, DB_USER, DB_PASSWORD, FLASK_SECRET_KEY
#    通道 credentials 依需求填寫

# 4. 初始化資料庫
#    方式一：手動匯入
mysql -u <user> -p <dbname> < setup_magi_brain.sql
mysql -u <user> -p <dbname> < init_auth.sql
#    方式二：使用 migration
python migrations/migrate.py upgrade

# 5. 啟動
bin/start
```

---

## 4. 日常操作

### 啟動
```bash
bin/start           # 前台啟動
bin/start &         # 背景啟動
```

### 停止
```bash
# 如果用 daemon.py 啟動：
kill $(cat rpc_server.pid 2>/dev/null)
# 或直接 Ctrl-C（前台模式）
```

### 健康檢查
```bash
bin/check           # 完整環境診斷
curl http://127.0.0.1:5002/health   # Server health
curl http://127.0.0.1:5003/health   # Tools API health
```

### 查看 Logs
```bash
tail -f .agent/server.log           # 主伺服器 log（JSON 格式）
tail -f .agent/channel_delivery_audit.jsonl  # 通道投遞審計
```

---

## 5. 設定管理

### 環境變數分層

| 分類 | 說明 | 缺少時行為 |
|------|------|-----------|
| Core Required | `DB_HOST`, `DB_USER`, `DB_PASSWORD`, `FLASK_SECRET_KEY` | 阻止啟動 |
| Feature-scoped | `MAGI_LINE_*`, `DISCORD_BOT_TOKEN`, `OPENCLAW_TELEGRAM_BOT_TOKEN` | 僅該通道不可用 |
| Recommended | `MAGI_CLOUDFLARED_PATH`, `MAGI_OMLX_SUMMARY_MODEL` | Warning |

### Feature Enable Flags
```bash
MAGI_ENABLE_LINE=1       # 啟用 LINE Bot
MAGI_ENABLE_DISCORD=0    # 停用 Discord Bot
MAGI_ENABLE_TELEGRAM=0   # 停用 Telegram Bot
MAGI_ENABLE_REMOTE_DB=0  # 停用遠端 DB 同步
```

### 安全相關
```bash
MAGI_CORS_ORIGINS=https://your-frontend.com   # CORS 白名單
JUDICIAL_API_ALLOW_INSECURE_SSL=0              # 預設關閉
MAGI_FORCE_HTTPS=1                             # 啟用 Secure cookie
```

---

## 6. 資料庫操作

### 備份
```bash
mysqldump -u <user> -p <dbname> > backup_$(date +%Y%m%d).sql
```

### 還原
```bash
mysql -u <user> -p <dbname> < backup_20260319.sql
```

### Schema Migration
```bash
python migrations/migrate.py status    # 查看版本
python migrations/migrate.py upgrade   # 升級
python migrations/migrate.py rollback  # 回滾上一個
```

---

## 7. 升級流程

```bash
# 1. 備份
mysqldump -u <user> -p <dbname> > pre_upgrade_$(date +%Y%m%d).sql
cp .env .env.bak

# 2. 拉取新版本
git pull origin main

# 3. 更新依賴
venv/bin/pip install -r requirements.txt

# 4. 執行 migration
python migrations/migrate.py upgrade

# 5. 重啟
bin/start
```

### 回滾
```bash
git checkout <previous-tag>
python migrations/migrate.py rollback
mysql -u <user> -p <dbname> < pre_upgrade_20260319.sql
bin/start
```

---

## 8. 故障排除

### MAGI 無法啟動

| 症狀 | 可能原因 | 處理 |
|------|---------|------|
| `Missing CORE required environment variables` | .env 缺少核心變數 | 檢查 DB_HOST, DB_USER, DB_PASSWORD, FLASK_SECRET_KEY |
| `ModuleNotFoundError` | 依賴未安裝 | `venv/bin/pip install -r requirements.txt` |
| `Connection refused (DB)` | 資料庫未啟動或連線資訊錯誤 | 檢查 DB 服務狀態與 .env 設定 |
| `Address already in use` | Port 5002/5003 被佔用 | `lsof -i :5002` 找出佔用程序 |

### LINE Bot 不回應

1. 檢查 `MAGI_ENABLE_LINE=1` 且 credentials 已設定
2. 檢查 webhook URL 是否正確指向 MAGI
3. 查看 `.agent/server.log` 中的 LINE 相關 error
4. 執行 `bin/check` 查看通道狀態

### 推理卡死 / 回應過慢

1. 檢查 Ollama 是否正常: `curl http://localhost:11434/api/tags`
2. 檢查模型是否載入: 看 log 中的 inference timeout
3. 嘗試重啟 Ollama: `systemctl restart ollama`
4. 降級模型: 修改 `MAGI_MAIN_MODEL` 為較小模型

---

## 9. 監控要點

| 指標 | 檢查方式 | 告警門檻 |
|------|---------|---------|
| Server 存活 | `curl /health` | 連續 3 次失敗 |
| DB 連線 | `bin/check` DB 段 | 連線失敗 |
| 磁碟空間 | `df -h` | < 10% 可用 |
| Log 大小 | `du -sh .agent/` | > 500 MB |
| 推理延遲 | Log 中的 inference_ms | > 30s 平均 |

---

## 10. 安全注意事項

- `.env` 不得進入 git（已在 .gitignore）
- CORS 僅允許白名單來源（`MAGI_CORS_ORIGINS`）
- Insecure SSL fallback 預設關閉
- 所有 API 端點需要認證（dashboard session 或 API key）
- Session cookie 啟用 HttpOnly + SameSite=Lax
- 定期輪換 `FLASK_SECRET_KEY` 和 `MAGI_API_KEY`

---

## 11. 檔案結構

```
MAGI/
├── api/                 # 核心 API 模組
│   ├── server.py        # 主 Flask 應用
│   ├── tools_api.py     # Tools HTTP API
│   ├── orchestrator.py  # NL 路由引擎
│   └── runtime_paths.py # 路徑抽象層
├── bin/                 # 標準化入口
│   ├── bootstrap        # 首次安裝
│   ├── start            # 啟動服務
│   └── check            # 健康檢查
├── skills/              # 可插拔技能
├── migrations/          # DB schema migration
├── templates/           # Web UI 模板
├── static/              # 靜態資源
├── .env                 # 環境設定（不進 git）
├── .env.example         # 設定範本
├── pyproject.toml       # 專案元資料
└── start_magi.sh        # 傳統啟動腳本
```
