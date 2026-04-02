# MAGI — 多代理治理基礎設施

[English](README.md)

MAGI 是一套部署於本地硬體的 AI 作業平台，專為台灣法律事務所的日常營運設計。系統運行在單一節點上，整合 Flask 控制層、57+ 模組化技能、排程工作、本地 LLM 推理，以及深度法務流程自動化。

**跨平台支援**：支援 **macOS**（Apple Silicon，透過 oMLX）及 **Windows**（NVIDIA/CPU，透過 Ollama）。內建設定精靈自動偵測硬體、推薦模型、產生組態。

> **預設單機模式。** 程式碼保留分散推理架構（Melchior、Balthasar），但所有生產工作負載皆在 Casper 本地運行。設定 `MAGI_AVOID_DISTRIBUTED=0` 可重新啟用多節點推理。

---

## 目錄

- [快速開始](#快速開始)
- [設定精靈](#設定精靈)
- [平台支援](#平台支援)
- [系統架構](#系統架構)
- [全部技能 (57+)](#全部技能-57)
  - [法務自動化 (14 個技能)](#法務自動化-14-個技能)
  - [文件處理 (7 個技能)](#文件處理-7-個技能)
  - [金融分析 (1 個技能，7 個子指令)](#金融分析-1-個技能7-個子指令)
  - [系統智能 (7 個技能)](#系統智能-7-個技能)
  - [通訊與工具 (7 個技能)](#通訊與工具-7-個技能)
  - [基礎設施 — Bridge 模組 (14 個)](#基礎設施--bridge-模組-14-個)
  - [基礎設施 — Ops 模組 (19 個)](#基礎設施--ops-模組-19-個)
  - [自治機制 (3 個模組)](#自治機制-3-個模組)
- [訊息處理流程](#訊息處理流程)
- [治理與安全](#治理與安全)
- [組態設定](#組態設定)
- [技術棧](#技術棧)
- [目錄結構](#目錄結構)
- [連接埠](#連接埠)
- [測試](#測試)
- [授權](#授權)

---

## 快速開始

### macOS（Apple Silicon — 建議）

```bash
# 1. 下載
git clone https://github.com/WhaleChao/MAGI.git && cd MAGI

# 2. Python 環境
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
pip install -r requirements-optional.txt   # 完整技能支援

# 3. 安裝 oMLX — 本地 Apple Silicon MLX 推理引擎
brew install omlx

# 4. 資料庫
brew install mariadb && brew services start mariadb

# 5. 設定精靈（自動偵測硬體、推薦模型、產生 .env）
python3 setup_wizard.py

# 6. 啟動
./start_magi.sh
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
curl http://localhost:5003/sages        # Tools API 健康檢查
curl http://localhost:5002/api/status    # 伺服器狀態
curl http://localhost:5002/health        # 完整健康（FAISS、磁碟、運行時間）
```

---

## 設定精靈

首次使用者會透過網頁介面的設定精靈引導完成設定：

1. **硬體偵測** — 自動偵測 CPU、GPU（Metal/CUDA）、RAM、磁碟空間
2. **引擎檢查** — 確認 oMLX（macOS）或 Ollama（Windows/Linux）已安裝
3. **模型推薦** — 依硬體推薦最佳模型：
   - Apple Silicon（≥16 GB）：TAIDE-12b（文字+視覺）+ Coder-14B + ModernBERT + GLM-OCR
   - NVIDIA GPU（≥8 GB）：TAIDE-8b GGUF + Qwen2.5-7b + Nomic-embed
   - 純 CPU（≥8 GB）：輕量 GGUF 模型
4. **組態收集** — 收集 LINE API 憑證、資料庫帳密、管理員身分
5. **連線測試** — 驗證 LINE API 及資料庫連線
6. **產生 `.env`** — 產生完整環境組態檔

隨時手動執行：`python3 setup_wizard.py`

首次啟動 `daemon.py` 時，若 `.env` 遺失或不完整，精靈會自動啟動。

---

## 平台支援

| 功能 | macOS (Apple Silicon) | Windows (NVIDIA/CPU) | Linux |
|------|----------------------|---------------------|-------|
| 推理引擎 | oMLX (MLX) | Ollama (GGUF) | Ollama (GGUF) |
| 檔案鎖定 | fcntl | msvcrt | fcntl |
| 服務管理 | LaunchAgent | 工作排程器 | systemd |
| 行事曆整合 | Apple Calendar (osascript) | Outlook (COM) | — |
| 瀏覽器自動化 | Playwright / Selenium | Playwright / Selenium | Playwright / Selenium |
| 工具搜尋 | Homebrew 路徑 | Program Files 路徑 | 標準路徑 |
| 啟動腳本 | `start_magi.sh` | `start_magi.bat` | `start_magi.sh` |

### 平台抽象層

所有平台特定程式碼集中於 `skills/ops/platform_utils.py`：

```python
from skills.ops.platform_utils import (
    IS_MACOS, IS_WINDOWS, IS_LINUX,
    file_lock, file_unlock, locked_file,
    get_venv_python, find_executable,
    get_service_manager, query_calendar_events,
)
```

核心抽象：
- **`file_lock` / `file_unlock`** — fcntl（Unix）/ msvcrt（Windows）
- **`get_service_manager()`** — 回傳 LaunchAgent / 工作排程器 / systemd 管理器
- **`find_executable(name)`** — 搜尋 PATH + 平台特定目錄
- **`query_calendar_events()`** — Apple Calendar 或 Outlook COM
- **`get_venv_python()`** — 解析 `venv/bin/python3` 或 `venv\Scripts\python.exe`

---

## 系統架構

```
┌──────────────────────────────────────────────────────────┐
│                       頻道層                               │
│        LINE Webhook  │  Discord Bot  │  Telegram Bot      │
└───────────────────────┬──────────────────────────────────┘
                        │
┌───────────────────────▼──────────────────────────────────┐
│          Casper 協調器 (api/orchestrator.py)               │
│  輸入消毒 → Iron Dome → 意圖分類 → Embedding 路由 → 技能派遣│
└───────────────────────┬──────────────────────────────────┘
                        │
┌───────────────────────▼──────────────────────────────────┐
│                     執行層                                 │
│  oMLX / Ollama (本地 LLM)      │  57+ 技能  │  MCP      │
│  Embedding Router (ModernBERT)  │  Playwright │  FAISS   │
└───────────────────────┬──────────────────────────────────┘
                        │
┌───────────────────────▼──────────────────────────────────┐
│                     資料層                                 │
│  magi_brain (本地 MariaDB)  │  law_firm_data (遠端)       │
│  FAISS 向量索引              │  NAS 案件資料夾              │
└──────────────────────────────────────────────────────────┘
```

### 推理模型

#### macOS（Apple Silicon + oMLX）

| 模型 | 用途 | 量化 |
|------|------|------|
| **TAIDE-12b-Chat** | 中文法律推理、翻譯、視覺辨識、一般對話 | MLX 4-bit |
| **Qwen2.5-Coder-14B** | 程式碼產生、技能演化 | MLX 4-bit |
| **ModernBERT-embed** | Embedding 路由、語意搜尋 | MLX 4-bit |
| **GLM-OCR** | 文件 OCR（PDF、圖片） | MLX bf16 |

#### Windows / Linux（Ollama + GGUF）

| 模型 | 用途 | 量化 |
|------|------|------|
| **TAIDE-8b-Chat** | 中文法律推理 | GGUF Q4 |
| **Qwen2.5-7b** | 一般對話、分類 | GGUF Q4 |
| **Nomic-embed-text** | Embedding 路由、語意搜尋 | GGUF |

---

## 全部技能 (57+)

每個技能遵循標準結構：

```
skills/{skill-name}/
├── SKILL.md       # 元資料、能力說明、用法
├── action.py      # CLI 入口（--task / --text）
└── *.py           # 支援模組
```

### 法務自動化 (14 個技能)

| 技能 | 說明 | 主要指令 |
|------|------|---------|
| **`file-review-orchestrator`** | 端到端閱卷自動化：申請送件、驗證碼破解（ddddocr+RapidOCR 雙引擎）、文件下載、繳費追蹤、案件資料夾歸檔 | `apply`, `download`, `payment`, `archive`, `probe` |
| **`laf-orchestrator`** | 法律扶助基金會報結與結案：活動次數統計、費用請領表單自動填寫、文件生成 | `close`, `prepare`, `status` |
| **`laf-portal-automation`** | 法扶入口網站表單自動化，支援 6 種工作流程。人機協作含視覺驗證 | `run_workflow`, `capture` |
| **`judicial-web-search`** | 台灣司法院裁判書查詢系統爬蟲（Playwright），支援全文搜尋及布林查詢 | `search`, `download` |
| **`judicial-flow-search-archive`** | 自然語言 → 布林查詢轉換；裁判書全文下載及歸檔 | `search`, `archive` |
| **`judgment-collector`** | 最高法院裁判自動收集，含結構化 LLM 摘要。URL 去重、幻覺偵測、快取清理 | `collect`, `search`, `summary` |
| **`transcript-downloader`** | 法院電子筆錄自動下載、重新命名、歸檔至 NAS | `download`, `rename`, `archive` |
| **`transcript-indexer`** | 筆錄向量化索引（FAISS）— 語意搜尋 | `index`, `search` |
| **`trial-prep`** | **開庭準備自動化**：查詢行事曆庭期、掃描案件資料夾、交叉比對法條及判決、產生備忘及確認清單 | `upcoming`, `prepare`, `checklist`, `timeline` |
| **`brief-gen`** | **書狀輔助產生**：7 種範本。自動偵測書狀類型、查詢法條及判決、匯出 Word | `draft`, `template`, `enrich`, `export` |
| **`legal_attest`** | 存證信函產生器 — 互動式問答、台灣郵局 PDF 格式 | `generate`, `preview` |
| **`statutes-vdb`** | 法規條文向量資料庫 — 依案件類型推斷相關法條，FAISS 語意搜尋 | `search`, `index`, `info` |
| **`labor-law-calculator`** | 台灣勞基法計算器：加班費、特休假、資遣費。純法定計算 | `overtime`, `leave`, `severance`, `verify` |
| **`law_review`** | 法律用語審核 — 使用 TAIDE 檢查法律慣用語及正式文體 | `review` |

### 文件處理 (7 個技能)

| 技能 | 說明 | 主要指令 |
|------|------|---------|
| **`pdf`** | PDF 瑞士刀：合併、分割、擷取、OCR、加密、解密、表單填寫 | `merge`, `split`, `extract`, `ocr`, `encrypt` |
| **`pdf-namer`** | 智慧 PDF 重新命名：OCR → 視覺模型 → 自動命名 | `rename`, `batch`, `learn` |
| **`pdf-annotator`** | 視覺模型自動產生 PDF 書籤及目錄 | `annotate`, `toc` |
| **`pdf-bookmarker`** | PDF 書籤管理 | `add`, `list`, `remove` |
| **`docx`** | Word 文件處理，支援台灣法律文書格式 | `create`, `edit`, `template` |
| **`pptx`** | PowerPoint 簡報 | `create`, `edit` |
| **`xlsx`** | Excel 建立、編輯、公式驗證 | `create`, `edit`, `validate` |

### 金融分析 (1 個技能，7 個子指令)

| 子指令 | 說明 |
|--------|------|
| **`market-briefing --task briefing`** | 每日股價預測，含自調整模型。三種模式：quick、technical、deep |
| **`--task comps --text "台積電"`** | 同業比較分析：P/E、EPS、營收 YoY%、動量 |
| **`--task sector --text "半導體"`** | 產業分析：38 個 TWSE 分類、技術面共識、量能趨勢 |
| **`--task export`** | 匯出追蹤清單至 Excel/CSV |
| **`--task performance`** | 模型績效指標 |
| **`--task backtest`** | 交叉驗證回測 |
| **`--task set/add/remove`** | 管理追蹤清單 |

**資料來源**：Yahoo Finance v8 chart API、TWSE OpenAPI、SEC EDGAR。

### 系統智能 (7 個技能)

| 技能 | 說明 | 主要指令 |
|------|------|---------|
| **`memory`** | 長期向量記憶 + RAG 語意搜尋。FAISS 索引、MD5 去重、雙向同步 | `store`, `search`, `consolidate` |
| **`obsidian`** | Obsidian 知識庫整合 | `extract`, `sync`, `search` |
| **`brain_manager`** | 推理模式切換 | `status`, `switch` |
| **`evolution`** | 自我演化引擎 — 從自然語言生成新技能 | `create`, `list`, `review` |
| **`magi-doctor`** | 系統自我診斷及自動修復 | `diagnose`, `repair`, `report` |
| **`magi-autopilot`** | 夜間自動維護 | `run`, `status` |
| **`iron-dome`** | 安全核心：規則掃描、注入過濾、指令阻擋 | `scan`, `update`, `status` |

### 通訊與工具 (7 個技能)

| 技能 | 說明 | 主要指令 |
|------|------|---------|
| **`browser`** | 瀏覽器自動化 | `navigate`, `screenshot`, `fill` |
| **`apple`** | Apple 生態系整合（僅 macOS） | `calendar_upcoming`, `reminder`, `ocr` |
| **`translator`** | 本地 LLM 全文翻譯 | `translate` |
| **`research`** | 多來源研究 | `rss`, `github`, `web` |
| **`gmail-drafts`** | Gmail 草稿（**絕不自動發送**） | `create_draft`, `list` |
| **`worldmonitor-intel`** | 全球事件監控 | `monitor`, `report` |
| **`crawler-targets`** | 排程爬取目標管理 | `add`, `list`, `remove` |

### 基礎設施 — Bridge 模組 (14 個)

位於 `skills/bridge/`：

| 模組 | 說明 |
|------|------|
| **`inference_gateway.py`** | 統一 LLM 路由 — 本地優先、fallback 遠端、再 fallback 雲端 |
| **`embedding_router.py`** | ModernBERT 餘弦相似度路由。61 技能，100% 準確率 |
| **`intention_classifier.py`** | 三階段分類：正則 → 啟發式 → LLM |
| **`semantic_router.py`** | 舊版意圖路由（embedding_router 前身） |
| **`melchior_client.py`** | Melchior 遠端推理閘道 |
| **`iron_dome.py`** | 安全過濾器 — 阻擋危險 SQL、Shell、注入 |
| **`grounded_ai.py`** | 接地回應生成 |
| **`code_analysis.py`** | 程式碼分析橋接 |
| **`legal_bridge.py`** | 法律領域路由 |
| **`casper_bridge.py`** | Casper 主協調橋接 |
| **`melchior_bridge.py`** | Melchior 節點橋接（待命） |
| **`balthasar_bridge.py`** | Balthasar 節點橋接（待命） |
| **`watcher_bridge.py`** | Watcher 節點橋接（待命） |
| **`tri_sage_collab.py`** | 三哲人協作推理 |

### 基礎設施 — Ops 模組 (19 個)

位於 `skills/ops/`：

| 模組 | 說明 |
|------|------|
| **`platform_utils.py`** | **跨平台抽象層** — 檔案鎖定、服務管理、硬體偵測、工具搜尋、行事曆整合 |
| **`red_phone.py`** | 多頻道警報系統 |
| **`heartbeat.py`** | 節點健康監控 |
| **`process_guardian.py`** | 程序生命週期管理 |
| **`db_sync.py`** | 雙向資料庫同步 |
| **`cron_scheduler.py`** | 排程任務管理 |
| **`openclaw_cron_runner.py`** | OpenClaw 排程 |
| **`openclaw_updater.py`** | OpenClaw 自動更新 |
| **`file_review_auto_worker.py`** | 閱卷背景工作者 |
| **`system_test.py`** | 端到端系統測試 |
| **`system_monitor.py`** | 資源監控 |
| **`circuit_breaker.py`** | 斷路器模式 |
| **`structured_log.py`** | JSON 結構化日誌 |
| **`iron_dome_sync.py`** | Iron Dome 規則同步 |
| **`daily_reflection.py`** | 每日 AI 自我反思 |
| **`smart_summary.py`** | 智慧摘要管線 |
| **`safe_state.py`** | 安全狀態管理 |
| **`export_text.py`** | 文字匯出工具 |
| **`task_tracker.py`** | 背景任務追蹤 |

### 自治機制 (3 個模組)

位於 `skills/magi/`：

| 模組 | 說明 |
|------|------|
| **`night_talk.py`** | 夜間討論 — 三哲人審查系統健康 |
| **`local_council.py`** | 本地共識引擎 |
| **`council_approval.py`** | 提案核准工作流程 |

### 法務後端引擎

位於 `casper_ecosystem/law_firm_orchestrators/`：

| 引擎 | 說明 |
|------|------|
| **`file_review_automation.py`** | 核心閱卷引擎：SSO、驗證碼、申請、繳費、歸檔 |
| **`judicial_automation_v2.py`** | 司法入口：筆錄下載、PDF 擷取、案件對應 |
| **`laf_automation_v2.py`** | 完整法扶自動化 |
| **`laf_orchestrator.py`** | 法扶工作流程協調 |
| **`legalbridge_core.py`** | 核心法務橋接 |
| **`osc/database.py`** | OSC 資料庫介面 |

---

## 訊息處理流程

```
收到訊息（LINE / Discord / Telegram）
    │
    ▼
Webhook 處理器（api/server.py）
    │  ─ 簽章驗證、角色檢查、探測快速路徑
    │
    ▼
背景執行器（非同步 — LINE webhook 必須 < 3 秒回應）
    │
    ▼
協調器（api/orchestrator.py）
    │  ─ 輸入消毒
    │  ─ Iron Dome 安全檢查
    │  ─ Embedding Router（ModernBERT 餘弦相似度）
    │
    ▼
意圖分類器（正則 → 啟發式 → 可選 LLM）
    ├─ DANGER → 阻擋 + red_phone 警報
    ├─ CMD    → 透過 action.py 執行技能
    ├─ QUERY  → ask_casper() 含記憶檢索 + 網路研究
    └─ CHAT   → chat_casper() 對話模式
    │
    ▼
回應透過頻道 API 推送
```

---

## 治理與安全

MAGI 決策遵循 `CONSTITUTION.md` 定義的嚴格層級：

```
使用者（Admin Token）        ← 最高權限
  └── 憲法                   ← 覆蓋所有 AI 邏輯
      └── Casper（總督）     ← AI 權限
          └── Iron Dome      ← 硬性保護層
```

### Iron Dome

- **SQL 防護**：硬性阻擋 `DELETE` / `DROP` / `TRUNCATE`
- **Shell 防護**：阻擋 `rm -rf`、`mkfs`、`dd` 等破壞性指令
- **提示注入**：模式匹配過濾
- **訪客隔離**：非管理員為唯讀

### 夜議

每日凌晨 03:00：Synology 同步 → 夜談 → 共識投票 → 報告。

---

## 組態設定

### 引導式設定（建議）

```bash
python3 setup_wizard.py
```

精靈會依硬體自動產生 `.env`。

### 手動設定

複製 `.env.example` 為 `.env`：

| 類別 | 變數 | 說明 |
|------|------|------|
| **Flask** | `FLASK_SECRET_KEY`, `MAGI_API_KEY` | 產生：`python3 -c "import secrets; print(secrets.token_hex(32))"` |
| **資料庫** | `DB_HOST`, `DB_USER`, `DB_PASSWORD` | `magi_brain` — 向量記憶 |
| **LINE** | `MAGI_LINE_CHANNEL_ACCESS_TOKEN`, `MAGI_LINE_CHANNEL_SECRET` | LINE Messaging API |
| **管理員** | `MAGI_ADMIN_DISPLAY_NAME`, `MAGI_ADMIN_LINE_IDS` | LINE user ID |
| **模型** | `MAGI_MAIN_MODEL` | `taide-12b`（macOS）/ `taide-lx-7b-chat`（Windows） |
| **推理** | `MAGI_OMLX_ENABLED` | `1` 用 oMLX（macOS），`0` 用 Ollama |

完整設定見 [.env.example](.env.example)。

---

## 技術棧

| 層次 | 技術 |
|------|------|
| 語言 | Python 3.12+（核心）、Node.js v22（OpenClaw 前端） |
| 網頁框架 | Flask + Jinja2 |
| 資料庫 | MariaDB 10.11+ |
| 推理 | oMLX（macOS）/ Ollama（Windows/Linux）— 相容 Ollama API |
| 嵌入 | ModernBERT（oMLX）/ Nomic-embed（Ollama） |
| 通訊 | LINE Bot SDK、Discord.py、python-telegram-bot |
| 網路 | Tailscale VPN、Cloudflare Tunnel（自動管理） |
| 瀏覽器 | Playwright、Selenium |
| PDF/OCR | PyMuPDF、RapidOCR、pdfplumber、ReportLab |
| 向量資料庫 | FAISS（本地） |
| 排程 | LaunchAgent（macOS）/ 工作排程器（Windows）/ systemd（Linux） |
| 平台層 | `skills/ops/platform_utils.py` — 跨平台抽象 |

---

## 目錄結構

```
MAGI/
├── api/                              # Flask 伺服器、協調器、Tools API
│   ├── server.py                     # 主入口 — LINE webhook、儀表板（port 5002）
│   ├── orchestrator.py               # 意圖路由與技能派遣
│   ├── tools_api.py                  # RESTful 工具 API（port 5003）
│   ├── discord_bot.py                # Discord 整合
│   ├── runtime_paths.py              # 跨平台路徑解析
│   └── handlers/                     # 模組化請求處理器
├── skills/                           # 57+ 模組化技能
│   ├── bridge/                       # 推理閘道、路由、安全（14 個模組）
│   ├── ops/                          # 營運 + 平台抽象（19 個模組）
│   │   └── platform_utils.py         # 跨平台抽象層
│   ├── magi/                         # 自治（3 個模組）
│   ├── memory/                       # FAISS 向量記憶 + RAG
│   ├── definitions.json              # 中央技能註冊表
│   └── {skill-name}/                 # 個別技能模組
├── casper_ecosystem/                  # 法務自動化引擎
│   └── law_firm_orchestrators/
├── scripts/                           # 排程任務與自動化
│   └── install_service.py             # 跨平台服務安裝器
├── templates/
│   └── wizard/                        # 設定精靈 HTML 模板
├── setup_wizard.py                    # 首次設定 GUI（硬體偵測 + .env 產生）
├── daemon.py                          # 程序守護 daemon（跨平台）
├── start_magi.sh                      # macOS / Linux 啟動腳本
├── start_magi.bat                     # Windows 啟動腳本
├── requirements.txt                   # 核心 Python 依賴
├── requirements-optional.txt          # 選配技能依賴
├── requirements-windows.txt           # Windows 專用依賴
├── .env.example                       # 環境變數範本
└── CONSTITUTION.md                    # 治理規則
```

---

## 連接埠

| 埠號 | 服務 | 存取 |
|------|------|------|
| 5002 | LINE Webhook + 儀表板 | localhost（via Caddy） |
| 5003 | Tools API | localhost |
| 8080 | oMLX / Ollama 推理 | localhost |
| 8081 | 嵌入服務 | localhost |
| 8199 | 設定精靈（暫時） | localhost |
| 18789 | OpenClaw Gateway | 僅 loopback |

---

## 測試

```bash
# 全部測試
python -m pytest tests/ -v

# 煙霧測試
python -m pytest tests/smoke_*.py -v

# 系統自我測試
python3 skills/ops/system_test.py

# 市場模組
python3 skills/market-briefing/action.py --task briefing --force 1 --mode quick

# 法務技能
python3 skills/trial-prep/action.py --task upcoming --days 7
python3 skills/brief-gen/action.py --task template
python3 skills/market-briefing/action.py --task comps --text "台積電"
```

---

## 授權

尚未發布開源授權。保留所有權利，直至發布 LICENSE 檔案。
