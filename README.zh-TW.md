# MAGI v2 — 多代理治理基礎設施

[English](README.md)

MAGI v2 是一套部署於本地硬體的 AI 作業平台，專為台灣法律事務所的日常營運設計。v2 在 v1 的基礎上進行了全面重構，新增企業級基礎設施（權限、事件、鉤子、任務、會話、工具登錄、多代理運行時、供應商抽象），並完成低幻覺架構改造。全系統含 67+ 模組化技能、247+ 測試、模組化路由架構。

**跨平台支援**：支援 **macOS**（Apple Silicon，透過 oMLX）及 **Windows**（NVIDIA/CPU，透過 Ollama）。內建設定精靈自動偵測硬體、推薦模型、產生組態。

> **單機模式。** 所有生產工作負載皆在 Casper 本地運行。程式碼保留分散推理架構（Melchior、Balthasar），設定 `MAGI_AVOID_DISTRIBUTED=0` 可啟用多節點推理。

---

## 目錄

- [與 MAGI v1 的差異](#與-magi-v1-的差異)
- [快速開始](#快速開始)
- [其他電腦安裝方式](#其他電腦安裝方式)
- [設定精靈](#設定精靈)
- [平台支援](#平台支援)
- [系統架構](#系統架構)
- [操作管理 (`magi` CLI)](#操作管理-magi-cli)
- [v2 基礎設施模組](#v2-基礎設施模組)
- [Registry 系統](#registry-系統)
- [低幻覺架構](#低幻覺架構)
- [全部技能 (67+)](#全部技能-67)
- [訊息處理流程](#訊息處理流程)
- [治理與安全](#治理與安全)
- [OpenClaw 整合](#openclaw-整合)
- [組態設定](#組態設定)
- [技術棧](#技術棧)
- [目錄結構](#目錄結構)
- [連接埠](#連接埠)
- [測試](#測試)
- [授權](#授權)

---

## 與 MAGI v1 的差異

MAGI v2 是完全重寫的企業級平台，v1 僅為早期概念驗證。

| 面向 | v1 | v2 |
|------|----|----|
| **定位** | 概念驗證骨架 | 完整企業級平台 |
| **技能數** | 1（pdf-namer） | 67+ 核心技能 |
| **測試** | 0 個測試 | 90+ 測試檔（247+ tests） |
| **架構** | 單一巨型檔案 | 模組化拆分（blueprints + pipelines + domains + routing） |
| **基礎模組** | 無 | 10+ 子系統（權限、事件、鉤子、任務、會話、工具、多代理、協調器、供應商） |
| **路由系統** | 無 | 三層路由（短語 → 語義 → LLM 降級）+ Registry 統一路由 |
| **組態系統** | 硬編碼 | JSON 設定 + Registry 模組 + 環境變數覆寫 |
| **記憶系統** | 無 | ModernBERT 向量 RAG + 信任分層 |
| **防幻覺** | 無 | 快取版本化、記憶降權、非權威標記、timeout 安全回退 |
| **LLM 供應商** | 無 | oMLX / Anthropic / OpenAI / Ollama 統一抽象 |
| **安全** | 無 | RBAC 權限、CSRF、事件稽核、會話硬化 |
| **系統管理** | 無 | `magi` CLI + macOS 狀態列 |

### v2 主要新增能力

1. **模組化架構** — server.py（9463→802行）、orchestrator.py（10269→2335行）拆分為 blueprints/webhooks/pipelines/domains
2. **Registry 系統** — 所有硬編碼值（IP、端口、模型名）外部化為 JSON 設定 + Python 註冊模組
3. **統一路由層** — `api/routing/` 下 14 個模組，提供服務/模型/節點/資料庫的統一查詢
4. **企業級基礎設施** — 權限系統、事件匯流排、鉤子生命週期、任務運行時、會話管理
5. **操作管理工具** — `magi` CLI（status/start/stop/restart/menubar/zombie）
6. **macOS 狀態列** — 即時顯示所有服務、遠端節點、排程任務、NAS、資料庫容錯狀態
7. **低幻覺架構** — 路由收緊、記憶信任分層、摘要非權威標記、timeout 不自由回答
8. **DB 容錯** — 遠端/本地雙活同步，自動切換 + mysqldump 同步

---

## 快速開始

### macOS（Apple Silicon — 建議）

```bash
# 1. 下載
git clone https://github.com/WhaleChao/MAGI.git && cd MAGI

# 2. Python 環境（需 Python 3.12+）
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
pip install -r requirements-optional.txt   # 完整技能支援

# 3. 安裝 oMLX — 本地 Apple Silicon MLX 推理引擎
brew install omlx

# 4. 資料庫
brew install mariadb && brew services start mariadb
mysql -u root < init_auth.sql
mysql -u root < setup_magi_brain.sql

# 5. 設定精靈（自動偵測硬體、推薦模型、產生 .env）
python3 setup_wizard.py

# 6. 啟動
./start_magi.sh

# 7. 安裝 CLI 工具（選配）
cp scripts/magi_cli.sh /opt/homebrew/bin/magi && chmod +x /opt/homebrew/bin/magi
```

### Windows

```powershell
# 1. 下載
git clone https://github.com/WhaleChao/MAGI.git && cd MAGI

# 2. Python 環境
python -m venv venv && venv\Scripts\activate
pip install -r requirements.txt
pip install -r requirements-optional.txt
pip install -r requirements-windows.txt   # Windows 專用（pywin32, llama-cpp-python）

# 3. 安裝 Ollama — 跨平台推理引擎
# 從 https://ollama.com/download/windows 下載

# 4. 資料庫
# 從 https://mariadb.org/download/ 安裝 MariaDB

# 5. 設定精靈
python setup_wizard.py

# 6. 啟動
start_magi.bat
```

### 驗證

```bash
curl http://localhost:5002/health        # 完整健康（FAISS、磁碟、運行時間）
curl http://localhost:5003/sages         # Tools API 健康
magi status                              # 完整系統儀表板
python3 skills/ops/system_test.py        # 系統測試
```

---

## 其他電腦安裝方式

MAGI v2 支援在多台電腦上部署，可依角色分為主節點（Casper）和輔助節點（Melchior / Balthasar）。

### 主節點（Casper）— 完整功能

即上方「快速開始」的安裝步驟。主節點運行所有核心服務。

### 輔助節點（Melchior — Embedding 服務）

Melchior 負責 Embedding 推理，減輕主節點負擔：

```bash
# 1. 下載
git clone https://github.com/WhaleChao/MAGI.git && cd MAGI

# 2. Python 環境
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# 3. 安裝推理引擎（依平台選擇）
# macOS: brew install omlx
# Windows/Linux: 安裝 Ollama

# 4. 設定 .env（僅需 Embedding 相關）
cat > .env << 'EOF'
MAGI_OMLX_ENABLED=1
MAGI_OMLX_EMBED_PORT=8081
MEM_EMBED_MODEL=ModernBERT-embed-base-4bit
EOF

# 5. 啟動 Embedding 服務
./start_melchior.sh
```

### 輔助節點（Balthasar — 摘要/翻譯分流）

```bash
# 1. 下載同上
# 2. Python 環境同上
# 3. 推理引擎同上

# 4. 設定 .env（僅需推理相關）
cat > .env << 'EOF'
MAGI_OMLX_ENABLED=1
MAGI_MAIN_MODEL=TAIDE-12b-Chat-mlx-4bit
EOF

# 5. 啟動
./start_balthasar.sh
```

### 多節點組網

節點間通訊透過 Tailscale VPN：

```bash
# 每台機器安裝 Tailscale
# macOS: brew install tailscale
# Windows: https://tailscale.com/download
# Linux: curl -fsSL https://tailscale.com/install.sh | sh

# 登入同一個 Tailnet
tailscale up

# 主節點 .env 設定輔助節點地址
MAGI_MELCHIOR_HOST=100.x.x.x    # Melchior 的 Tailscale IP
MAGI_BALTHASAR_HOST=100.x.x.x   # Balthasar 的 Tailscale IP
```

### 最低硬體需求

| 角色 | RAM | 儲存 | GPU |
|------|-----|------|-----|
| Casper（主節點） | 16 GB+ | 50 GB+ | Apple Silicon 或 NVIDIA 8GB+ |
| Melchior（Embedding） | 8 GB+ | 10 GB+ | Apple Silicon 或 CPU |
| Balthasar（推理分流） | 16 GB+ | 30 GB+ | Apple Silicon 或 NVIDIA 8GB+ |

---

## 設定精靈

首次使用者會透過網頁介面的設定精靈引導完成設定：

1. **硬體偵測** — 自動偵測 CPU、GPU（Metal/CUDA）、RAM、磁碟空間
2. **引擎檢查** — 確認 oMLX（macOS）或 Ollama（Windows/Linux）已安裝
3. **模型推薦** — 依硬體推薦最佳模型：
   - Apple Silicon（>=16 GB）：TAIDE-12b（文字+視覺）+ Coder-14B + ModernBERT + GLM-OCR
   - NVIDIA GPU（>=8 GB）：TAIDE-8b GGUF + Qwen2.5-7b + Nomic-embed
   - 純 CPU（>=8 GB）：輕量 GGUF 模型
4. **組態收集** — 收集 LINE API 憑證、資料庫帳密、管理員身分
5. **連線測試** — 驗證 LINE API 及資料庫連線
6. **產生 `.env`** — 產生完整環境組態檔

隨時手動執行：`python3 setup_wizard.py`

---

## 平台支援

| 功能 | macOS (Apple Silicon) | Windows (NVIDIA/CPU) | Linux |
|------|----------------------|---------------------|-------|
| 推理引擎 | oMLX (MLX) | Ollama (GGUF) | Ollama (GGUF) |
| 檔案鎖定 | fcntl | msvcrt | fcntl |
| 服務管理 | LaunchAgent | 工作排程器 | systemd |
| 行事曆整合 | Apple Calendar (osascript) | Outlook (COM) | — |
| 瀏覽器自動化 | Playwright / Selenium | Playwright / Selenium | Playwright / Selenium |
| 啟動腳本 | `start_magi.sh` | `start_magi.bat` | `start_magi.sh` |
| 狀態列 | `gui/magi_menubar.py` (rumps) | — | — |

---

## 系統架構

### 模組化架構（v2）

v2 將原本的巨型檔案拆分為專責模組：

```
┌──────────────────────────────────────────────────────────────┐
│                          頻道層                                │
│   LINE Webhook      │  Discord Bot  │  Telegram Bot           │
│  (webhooks/line.py) │(discord_bot.py)│(webhooks/telegram.py)  │
└─────────┬───────────┴──────┬────────┴──────────┬─────────────┘
          │                  │                    │
┌─────────▼──────────────────▼────────────────────▼─────────────┐
│              Flask 應用 (api/server.py — 802 行)               │
│  Blueprints: admin_runtime │ dashboard │ osc_cases │ web      │
│  Webhooks:   line.py       │ telegram.py                       │
│  啟動流程:   thread pools, security headers, CSRF              │
└─────────────────────────────┬─────────────────────────────────┘
                              │
┌─────────────────────────────▼─────────────────────────────────┐
│            協調器 (api/orchestrator.py — 2,335 行)              │
│  委派至:                                                        │
│  ├─ pipelines/message_pipeline.py    (訊息接收與消毒)           │
│  ├─ pipelines/command_pipeline.py    (指令解析)                 │
│  ├─ pipelines/chat_pipeline.py       (對話式 AI)                │
│  ├─ pipelines/command_dispatch.py    (技能派遣)                 │
│  ├─ pipelines/skill_dispatch.py      (技能解析)                 │
│  ├─ pipelines/message_router.py      (意圖路由)                 │
│  ├─ pipelines/attachment_pipeline.py (附件處理)                 │
│  └─ domains/{codex,judgment,laf,market,memory}_flow.py         │
└─────────────────────────────┬─────────────────────────────────┘
                              │
┌─────────────────────────────▼─────────────────────────────────┐
│                    路由層 (api/routing/)                         │
│  Registry:  service_registry │ model_registry │ node_registry  │
│  Policy:    policy_engine    │ route_policy   │ route_decision │
│  路由器:    request_router   │ inference_router                │
│  設定檔:    json/services.json │ models.json │ nodes.json      │
└─────────────────────────────┬─────────────────────────────────┘
                              │
┌─────────────────────────────▼─────────────────────────────────┐
│                         執行層                                  │
│  oMLX / Ollama (本地 LLM)   │  67+ 技能  │  MCP Server        │
│  Embedding Router (ModernBERT)│ Playwright │  FAISS            │
└─────────────────────────────┬─────────────────────────────────┘
                              │
┌─────────────────────────────▼─────────────────────────────────┐
│                         資料層                                  │
│  magi_brain (本地 MariaDB)   │  law_firm_data (遠端)           │
│  FAISS 向量索引              │  NAS 案件資料夾                  │
│  DB 容錯 (自動切換 遠端 ↔ 本地 + mysqldump 同步)                │
└───────────────────────────────────────────────────────────────┘
```

### 核心拆分成果

| 原始檔案 | 原始行數 | 拆分後行數 | 策略 |
|---------|---------|-----------|------|
| `api/server.py` | 9,463 | 802 | 拆分為 `blueprints/`（7 模組）+ `webhooks/`（2 模組） |
| `api/orchestrator.py` | 10,269 | 2,335 | 拆分為 `pipelines/`（8 模組）+ `domains/`（6 模組） |
| `templates/osc.html` | 7,558 | 2,543 | 拆分為 `osc/` 模板片段 |
| 硬編碼值 | 散落各處 | 0 | 外部化為 `json/` 設定 + `api/routing/` Registry |

### 推理模型

#### macOS（Apple Silicon + oMLX）

| 模型 | 用途 | 量化 |
|------|------|------|
| **Gemma-4-26B** | 文字生成、法律推理、視覺、OCR、程式碼 — 全部角色 | MLX 4-bit |
| **ModernBERT-embed** | Embedding 路由、語意搜尋 | MLX 4-bit |

所有文字角色（primary、review、summary、code、vision、OCR）皆對應同一個 **Gemma-4 26B** 模型。可透過環境變數覆寫個別角色 — 詳見 `json/models.json`。

#### Windows / Linux（Ollama + GGUF）

| 模型 | 用途 | 量化 |
|------|------|------|
| **Gemma-4**（或相容模型） | 文字生成、法律推理 | GGUF Q4 |
| **Nomic-embed-text** | Embedding 路由、語意搜尋 | GGUF |

---

## 操作管理 (`magi` CLI)

`magi` 命令管理完整 MAGI 生命週期，包括 daemon、所有服務及 macOS 狀態列。

### 安裝

```bash
cp scripts/magi_cli.sh /opt/homebrew/bin/magi && chmod +x /opt/homebrew/bin/magi
```

### 指令

```bash
magi                 # 顯示完整系統狀態（預設）
magi status          # 同上 — 服務、節點、NAS、DB、殭屍、記憶體
magi start           # 透過 launchctl 啟動 daemon + 狀態列
magi stop            # 停止 daemon + 所有服務 + 狀態列
magi restart         # 完整停止 → 啟動循環
magi menubar         # 僅重啟 macOS 狀態列
magi zombie          # 偵測並清理殭屍進程
```

### 狀態儀表板

`magi status` 顯示完整即時概覽：

```
═══ MAGI System Status ═══

Core Services:
  ● Daemon             PID 4272
  ● Server             PID 4358
  ● Discord Bot        PID 4359
  ● Tools API          PID 4361

UI:
  ● Status Bar         PID 4275

oMLX Inference:
  ● Text (Gemma-4)     port 8080  PID 1234
  ● Embed (BERT)       port 8081  PID 1235

Remote Nodes:
  ● Melchior           100.116.54.16:8080
  ○ Balthasar          100.118.235.126:5002  DOWN
  ● Keeper             100.121.61.74:3306

NAS Mounts:
  ● homes              1.2T/3.6T (34%)
  ● lumi               800G/1.8T (45%)

Database:
  ● 雙活同步 (remote+local)

Zombies: 0
Memory:  ~2.3GB (MAGI + oMLX)
```

### macOS 狀態列

狀態列（`gui/magi_menubar.py`）以 macOS menu bar 應用程式即時顯示系統健康：

- **服務狀態**：Daemon、Server、Discord Bot、Tools API — 彩色指示燈
- **遠端節點**：Melchior、Balthasar、Keeper — TCP + HTTP 健康檢查
- **排程任務**：每個排程任務的最後執行時間 + 過期偵測（31 個排程任務）
- **NAS 掛載**：每個分享的掛載狀態 + 磁碟用量（`os.statvfs()`）
- **資料庫**：容錯詳情（遠端+本地雙活、容錯狀態、同步狀態）
- **oMLX 推理**：文字及 Embedding 模型狀態 + 端口檢查

### LaunchAgent 管理

MAGI 使用 macOS LaunchAgents 管理程序生命週期：

| 代理 | Label | 用途 |
|------|-------|------|
| Daemon | `com.magi.daemon` | 主程序（啟動 server、discord、tools_api） |
| 狀態列 | `com.magi.menubar` | macOS menu bar 健康監控 |
| oMLX Text | `com.magi.omlx` | Gemma-4 26B 推理（port 8080） |
| oMLX Embed | `com.magi.omlx-embed` | ModernBERT embedding（port 8081） |
| DB Proxy | `com.magi.db-proxy` | SSH tunnel 至遠端 MariaDB |
| SMB 重連 | `com.magi.smb-reconnect` | NAS 網路中斷自動重連 |
| Caddy | `com.magi.caddy-openclaw` | 反向代理（OpenClaw） |

---

## v2 基礎設施模組

以下為 v2 新增的企業級基礎設施，v1 完全沒有這些子系統：

### 權限系統（`api/permissions/`）

RBAC 權限引擎，控制技能執行、工具存取、API 端點的存取權限。

### 事件系統（`api/events/`）

全局事件發射器，所有操作產生可追蹤的事件流。

### 鉤子匯流排（`api/hooks/`）

實時事件訂閱/發佈機制，支援 JSONL 持久化。

### 任務運行時（`api/tasks/`）

異步任務排隊與執行、狀態管理（PENDING → RUNNING → COMPLETE/FAILED）、超時控制。

### 會話管理（`api/session/`）

多輪對話上下文保存、歷史管理、摘要生成、待處理狀態追蹤。

### 工具登錄（`api/tools/`）

動態工具發現、執行合約驗證、HTTP/Callable 雙模式執行器。

### 多代理運行時（`api/agents/`）

代理協調器、團隊輪轉分派、代理間消息路由。

### 供應商抽象（`providers/`）

統一 LLM 供應商介面，支援動態註冊與健康檢查。

| 供應商 | 檔案 | 說明 |
|--------|------|------|
| oMLX | `omlx.py` | 本地 Apple Silicon 推理 |
| Anthropic | `anthropic.py` | Claude API |
| OpenAI | `openai.py` | OpenAI / 相容 API |
| Ollama | `ollama.py` | 本地 GGUF 推理 |

---

## Registry 系統

MAGI v2 將所有硬編碼值（IP、端口、模型名、連線字串）外部化為宣告式 JSON + Python Registry 系統。每個 Registry 遵循相同模式：**JSON 設定 → Python 單例模組 → 環境變數覆寫 → 硬編碼後備**。

### JSON 設定檔（`json/`）

| 檔案 | 用途 | 範例 |
|------|------|------|
| `services.json` | 服務端點（host、port、path） | `{"casper": {"host": "127.0.0.1", "port": 5002}}` |
| `models.json` | 模型別名、供應商、參數 | `{"taide-12b": {"provider": "omlx", "ctx": 4096}}` |
| `nodes.json` | 執行節點（IP、角色、健康 URL） | `{"melchior": {"ip": "100.116.54.16", "role": "vision"}}` |
| `datastores.json` | 資料庫及儲存連線 | `{"magi_brain": {"host": "127.0.0.1", "port": 3306}}` |

### Python Registry 模組（`api/routing/`）

| 模組 | 函式 | 讀取自 |
|------|------|--------|
| `service_registry.py` | `get_service()`, `get_service_url()`, `get_service_host_port()` | `json/services.json` |
| `model_registry.py` | `get_role_model()`, `resolve_model()`, `is_alias()` | `json/models.json` |
| `node_registry.py` | `get_node()`, `get_node_ip()`, `get_node_url()` | `json/nodes.json` |
| `datastore_registry.py` | `get_datastore()`, `get_connection_params()` | `json/datastores.json` |

### 覆寫鏈

```
環境變數 (MAGI_CASPER_PORT=5002)
    → JSON 設定 (json/services.json)
        → 硬編碼後備 (在 Registry 模組中)
```

### 統一路由（Phase 4）

| 模組 | 角色 |
|------|------|
| `context.py` | `RoutingContext` — 每請求狀態 |
| `models.py` | `RoutingDecision`, `FallbackPlan`, `ServiceTarget` |
| `policy_engine.py` | `PolicyEngine` — 套用路由規則 |
| `request_router.py` | `RequestRouter` — HTTP 請求路由至服務 |
| `inference_router.py` | `InferenceRouter` — LLM 呼叫路由至供應商 |
| `telemetry.py` | `RoutingTelemetry` — 可觀察性與指標 |

---

## 低幻覺架構

v2 的核心改進之一是從架構層面切斷「模型講錯 → 寫回記憶 → 再引用自己」的幻覺循環。

### 已實作的防護

| 防護措施 | 說明 |
|---------|------|
| **意圖快取版本化** | 快取綁定 schema/policy 版本，只持久化低風險意圖（CHAT），CMD/QUERY 不緩存 |
| **語義路由收緊** | 廣義詞（摘要、翻譯、記得）改為軟提示，不再直接硬派遣 |
| **自動記憶寫入停用** | `_auto_remember()` 預設關閉，assistant 回覆不自動進入長期記憶 |
| **記憶信任分層** | `assistant_generated` 權重 0.18、`chatlog` 權重 0.28、高信任來源 1.00 |
| **摘要非權威標記** | 歷史壓縮摘要標記為「非原文、僅供延續上下文」，不作為事實依據 |
| **timeout 安全回退** | 查詢逾時不再自由回答，改為 evidence-only 回覆 |
| **skill interview 收斂** | admin gap 只在明確創建請求時觸發，不再對普通問題誤判 |

### 幻覺鏈條防護測試

```bash
python -m pytest tests/test_routing.py tests/test_memory_grounding.py \
  tests/test_orchestrator_low_hallucination.py tests/test_tw_output_guard_metadata.py -q
```

---

## 全部技能 (67+)

每個技能遵循標準結構：

```
skills/{skill-name}/
├── SKILL.md       # 元資料、能力說明、用法
├── action.py      # CLI 入口（--task / --text）
└── *.py           # 支援模組
```

### 法務自動化 (14 個技能)

| 技能 | 說明 |
|------|------|
| **`file-review-orchestrator`** | 端到端閱卷自動化：申請、驗證碼破解、文件下載、繳費追蹤、歸檔 |
| **`laf-orchestrator`** | 法律扶助基金會報結與結案 |
| **`laf-portal-automation`** | 法扶入口網站表單自動化，6 種工作流程，人機協作 |
| **`judicial-web-search`** | 司法院裁判書查詢爬蟲（Playwright） |
| **`judicial-flow-search-archive`** | 自然語言 → 布林查詢，裁判書歸檔 |
| **`judgment-collector`** | 裁判自動收集 + 結構化 LLM 摘要 |
| **`transcript-downloader`** | 電子筆錄自動下載、重命名、歸檔 |
| **`transcript-indexer`** | 筆錄向量化索引（FAISS 語意搜尋） |
| **`trial-prep`** | 開庭準備：庭期查詢、資料夾掃描、法條比對 |
| **`brief-gen`** | 書狀輔助產生（7 種範本） |
| **`legal_attest`** | 存證信函產生器 |
| **`statutes-vdb`** | 法規條文向量資料庫 |
| **`labor-law-calculator`** | 勞基法計算器（加班費、特休、資遣費） |
| **`law_review`** | 法律用語審核 |

### 文件處理 (7 個技能)

| 技能 | 說明 |
|------|------|
| **`pdf`** | PDF 合併、分割、擷取、OCR、加密 |
| **`pdf-namer`** | 智慧 PDF 重命名（OCR → 視覺模型） |
| **`pdf-annotator`** | 自動產生 PDF 書籤及目錄 |
| **`pdf-bookmarker`** | PDF 書籤管理 |
| **`translator`** | GTX+TAIDE post-edit 全文翻譯 |
| **`docx`** / **`pptx`** / **`xlsx`** | Office 文件處理 |

### 金融分析

| 子指令 | 說明 |
|--------|------|
| `market-briefing --task briefing` | 每日股價預測（台美股），自調整模型 |
| `--task comps` | 同業比較分析 |
| `--task sector` | 產業分析（38 個 TWSE 分類） |
| `--task export` | 匯出追蹤清單 |
| `--task performance` | 模型績效指標 |

### 系統智能 (7 個技能)

memory、obsidian、brain_manager、evolution、magi-doctor、magi-autopilot、iron-dome

### 通訊與工具 (7 個技能)

browser、apple、translator、research、gmail-drafts、worldmonitor-intel、crawler-targets

---

## 訊息處理流程

```
收到訊息（LINE / Discord / Telegram）
    │
    ▼
頻道處理器 (webhooks/line.py, webhooks/telegram.py, discord_bot.py)
    │  ─ 簽章驗證、角色檢查、快速路徑
    │
    ▼
背景執行器（非同步 — LINE webhook 須於 3 秒內回應）
    │
    ▼
協調器 (api/orchestrator.py) → 委派至 pipelines/
    │
    ├─ message_pipeline.py     ─ 輸入消毒、上下文載入
    ├─ command_pipeline.py     ─ 指令前綴偵測與解析
    ├─ message_router.py       ─ Embedding Router (ModernBERT)
    └─ command_dispatch.py     ─ 技能解析與派遣
        │
        ▼
    意圖分類器（regex → heuristic → LLM）
        ├─ DANGER → 阻擋 + red_phone 警報
        ├─ CMD    → skill_dispatch.py → action.py
        ├─ QUERY  → chat_pipeline.py（記憶檢索 + 網路研究）
        └─ CHAT   → chat_pipeline.py（對話模式）
        │
        ▼
    特定領域流程 (domains/)：
        ├─ judgment_flow.py   ─ 司法裁判查詢
        ├─ laf_flow.py        ─ 法扶作業
        ├─ market_flow.py     ─ 股市分析
        ├─ memory_flow.py     ─ RAG 記憶操作
        └─ codex_flow.py      ─ 程式碼分析
        │
        ▼
    回應透過頻道 API 推送
```

---

## 治理與安全

```
使用者（Admin Token）        ← 最高權限
  └── 憲法                   ← 覆蓋所有 AI 邏輯
      └── Casper（總督）     ← AI 權限
          └── Permission Enforcer ← RBAC 權限
              └── Iron Dome       ← 硬性保護層
```

- **SQL 防護**：硬性阻擋 `DELETE` / `DROP` / `TRUNCATE`
- **Shell 防護**：阻擋 `rm -rf`、`mkfs`、`dd` 等
- **提示注入**：模式匹配 + 幻覺關鍵字偵測
- **訪客隔離**：非管理員為唯讀
- **CSRF**：令牌驗證
- **安全標頭**：CSP、HSTS、X-Content-Type-Options

---

## OpenClaw 整合

OpenClaw 是 MAGI 的分散推論橋接層，**v2 中預設停用**。

### 狀態

| 項目 | 說明 |
|------|------|
| 橋接模組 | `skills/bridge/openclaw_codex_bridge.py` |
| 支援功能 | OCR、翻譯、視覺、意圖識別、轉錄 |
| 預設狀態 | **停用**（`MAGI_AVOID_DISTRIBUTED=1`） |
| 啟用方式 | 設定 `MAGI_AVOID_DISTRIBUTED=0` 及 `OPENCLAW_TELEGRAM_BOT_TOKEN` |

---

## 組態設定

### 引導式設定（建議）

```bash
python3 setup_wizard.py
```

### 手動設定

複製 `.env.example` 為 `.env`：

| 類別 | 變數 | 說明 |
|------|------|------|
| **Flask** | `FLASK_SECRET_KEY`, `MAGI_API_KEY` | 產生：`python3 -c "import secrets; print(secrets.token_hex(32))"` |
| **資料庫** | `DB_HOST`, `DB_USER`, `DB_PASSWORD` | `magi_brain` — 向量記憶 |
| **遠端資料庫** | `OSC_DB_HOST`, `MAGI_REMOTE_DB_HOST` | `law_firm_data`（OSC） |
| **LINE** | `MAGI_LINE_CHANNEL_ACCESS_TOKEN`, `MAGI_LINE_CHANNEL_SECRET` | LINE Messaging API |
| **Discord** | `DISCORD_BOT_TOKEN`, `DISCORD_NOTIFY_CHANNEL_ID` | Discord Bot |
| **Telegram** | `OPENCLAW_TELEGRAM_BOT_TOKEN`, `MAGI_TG_ADMIN_CHAT_ID` | Telegram Bot |
| **管理員** | `MAGI_ADMIN_DISPLAY_NAME`, `MAGI_ADMIN_LINE_IDS` | LINE user ID |
| **模型** | `MAGI_MAIN_MODEL` | `gemma-4-26b-a4b-it-4bit`（macOS） |
| **推理** | `MAGI_OMLX_ENABLED` | `1` 用 oMLX（macOS），`0` 用 Ollama |

完整設定見 [.env.example](.env.example)。

---

## 技術棧

| 層次 | 技術 |
|------|------|
| 語言 | Python 3.12+（核心）、Node.js v22（OpenClaw 前端） |
| 網頁框架 | Flask + Jinja2 + Blueprint |
| 資料庫 | MariaDB 10.11+（雙活容錯：遠端 + 本地） |
| 推理 | oMLX（macOS）/ Ollama（Windows/Linux） |
| 供應商層 | `providers/` — oMLX / Anthropic / OpenAI / Ollama |
| 嵌入 | ModernBERT（oMLX）/ Nomic-embed（Ollama） |
| 通訊 | LINE Bot SDK、Discord.py、python-telegram-bot |
| 網路 | Tailscale VPN、Cloudflare Tunnel（自動管理） |
| 反向代理 | Caddy（選配，用於對外服務） |
| 瀏覽器 | Playwright、Selenium |
| PDF/OCR | PyMuPDF、RapidOCR、pdfplumber、ReportLab |
| 向量資料庫 | FAISS（本地） |
| 排程 | LaunchAgent（macOS）/ 工作排程器（Windows）/ systemd（Linux） |
| 狀態列 | rumps + PyObjC（macOS menu bar） |

---

## 目錄結構

```
MAGI/
├── api/                              # 核心 API 層
│   ├── server.py                     # Flask 入口（802 行 — 委派至模組）
│   ├── orchestrator.py               # 意圖路由中樞（2,335 行 — 委派至 pipelines）
│   ├── tools_api.py                  # RESTful 工具 API（port 5003）
│   ├── discord_bot.py                # Discord 整合 + 排程
│   ├── db_failover.py                # DB 容錯控制器（遠端 ↔ 本地自動切換）
│   ├── blueprints/                   # Flask Blueprint 模組（拆自 server.py）
│   │   ├── admin_runtime.py          # 管理員儀表板路由
│   │   ├── dashboard_pages.py        # 儀表板頁面路由
│   │   ├── osc_cases.py              # 案件管理系統路由
│   │   ├── osc_accounting.py         # 會計系統路由
│   │   ├── osc_debt.py               # 債務案件路由
│   │   ├── osc_settings.py           # 系統設定路由
│   │   └── web_runtime.py            # Web 應用路由
│   ├── webhooks/                     # 頻道 Webhook 處理器（拆自 server.py）
│   │   ├── line.py                   # LINE 訊息 webhook
│   │   └── telegram.py               # Telegram bot webhook
│   ├── pipelines/                    # 處理管線（拆自 orchestrator.py）
│   │   ├── message_pipeline.py       # 訊息接收與消毒
│   │   ├── command_pipeline.py       # 指令解析與驗證
│   │   ├── chat_pipeline.py          # 對話式 AI 管線
│   │   ├── command_dispatch.py       # 技能派遣調度
│   │   ├── skill_dispatch.py         # 技能解析邏輯
│   │   ├── message_router.py         # 意圖路由
│   │   ├── attachment_pipeline.py    # 附件處理
│   │   └── specialized_commands.py   # 特定領域指令
│   ├── domains/                      # 特定領域流程（拆自 orchestrator.py）
│   │   ├── judgment_flow.py          # 司法裁判查詢
│   │   ├── laf_flow.py              # 法扶作業
│   │   ├── market_flow.py           # 股市分析
│   │   ├── memory_flow.py           # RAG 記憶操作
│   │   ├── codex_flow.py            # 程式碼分析
│   │   └── skill_interview_flow.py  # 技能查詢
│   ├── routing/                      # 統一路由 & Registry 系統
│   │   ├── service_registry.py      # 服務端點 Registry
│   │   ├── model_registry.py        # 模型 Registry
│   │   ├── node_registry.py         # 節點 Registry
│   │   ├── datastore_registry.py    # 資料庫 Registry
│   │   ├── policy_engine.py         # 路由策略引擎
│   │   ├── request_router.py        # HTTP 請求路由器
│   │   ├── inference_router.py      # LLM 推理路由器
│   │   └── telemetry.py             # 路由遙測
│   ├── handlers/                     # 請求處理器
│   ├── agents/                       # 多代理運行時
│   ├── coordinator/                  # 任務協調
│   ├── events/                       # 事件系統
│   ├── hooks/                        # 鉤子系統
│   ├── osc/                          # 線上服務中心整合
│   ├── permissions/                  # 授權與權限
│   ├── session/                      # 會話管理
│   ├── tasks/                        # 任務佇列與執行
│   ├── tools/                        # 工具定義與登錄
│   └── verification/                 # 回應驗證
├── json/                             # 宣告式設定（Registry 系統）
│   ├── services.json                 # 服務端點
│   ├── models.json                   # 模型定義與別名
│   ├── nodes.json                    # 執行節點定義
│   ├── datastores.json               # 資料庫連線設定
│   └── holidays_config.json          # 假日行事曆
├── skills/                           # 67+ 模組化技能
│   ├── bridge/                       # 推理閘道、路由、安全（14 模組）
│   ├── ops/                          # 營運 + 平台抽象（19 模組）
│   ├── magi/                         # 自治（3 模組）
│   ├── memory/                       # FAISS 向量記憶 + RAG
│   └── {skill-name}/                 # 個別技能
├── gui/                              # GUI 元件
│   └── magi_menubar.py               # macOS 狀態列（rumps + PyObjC）
├── scripts/                          # 操作腳本（60+）
│   ├── magi_cli.sh                   # `magi` CLI 工具
│   ├── nightly_council.py            # 每日知識整合
│   └── ops/                          # 操作腳本（smoke tests、DB 同步等）
├── providers/                        # AI 供應商整合
├── mcp/                              # MCP 伺服器
├── casper_ecosystem/                 # 法務自動化引擎
├── tests/                            # 90+ 測試檔
├── docs/                             # 文件
│   ├── ARCHITECTURE.md               # 系統架構
│   ├── OPERATOR_RUNBOOK.md           # 操作手冊
│   └── API_CONTRACT.md               # API 規格
├── migrations/                       # 資料庫遷移
├── templates/                        # Flask/Jinja2 模板
├── static/                           # 靜態資源
├── daemon.py                         # 程序守護 daemon
├── setup_wizard.py                   # 首次設定精靈
├── requirements.txt                  # 核心依賴
├── requirements-optional.txt         # 選配依賴
├── requirements-windows.txt          # Windows 專用依賴
├── .env.example                      # 環境變數範本
└── CONSTITUTION.md                   # 治理規則
```

---

## 連接埠

| 埠號 | 服務 | 說明 |
|------|------|------|
| 5002 | MAGI Server | LINE Webhook + 儀表板（主要端口） |
| 5003 | Tools API | RESTful 工具 API |
| 8080 | oMLX / Ollama | 本地推理引擎 |
| 8081 | Embedding 服務 | ModernBERT 嵌入 |
| 8199 | 設定精靈 | 首次設定（暫時） |
| 18789 | OpenClaw Gateway | 僅 loopback |

---

## 測試

```bash
# 全部測試（247+ tests）
python -m pytest tests/ -v

# Registry & 路由測試
python -m pytest tests/test_registry*.py tests/test_routing*.py -v

# Blueprint 測試
python -m pytest tests/test_*_blueprint.py -v

# Pipeline 測試
python -m pytest tests/test_*_pipeline.py tests/test_command_dispatch.py -v

# 技能合約測試
python -m pytest tests/test_skill_contract_*.py -v

# 低幻覺專項測試
python -m pytest tests/test_routing.py tests/test_memory_grounding.py \
  tests/test_orchestrator_low_hallucination.py tests/test_tw_output_guard_metadata.py -q

# 系統自我測試
python3 skills/ops/system_test.py

# 系統診斷
python3 skills/magi-doctor/action.py --task diagnose
```

---

## 授權

No open-source license. All rights reserved until a LICENSE file is published.
