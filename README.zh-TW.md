# MAGI v2 — 多代理治理基礎設施

[English](README.md)

MAGI v2 是一套部署於本地硬體的 AI 作業平台，專為台灣法律事務所的日常營運設計。v2 在 v1 的基礎上進行了全面重構，新增企業級基礎設施（權限、事件、鉤子、任務、會話、工具登錄、多代理運行時、供應商抽象），並完成低幻覺架構改造。

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
- [v2 基礎設施模組](#v2-基礎設施模組)
- [低幻覺架構](#低幻覺架構)
- [全部技能 (57+)](#全部技能-57)
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
| **技能數** | 1（pdf-namer） | 57+ 核心技能、283 SKILL.md |
| **測試** | 0 個測試 | 57 個測試檔（378+ tests） |
| **基礎模組** | 無 | 10+ 子系統（權限、事件、鉤子、任務、會話、工具、多代理、協調器、供應商） |
| **路由系統** | 無 | 三層路由（短語 → 語義 → LLM 降級） |
| **記憶系統** | 無 | ModernBERT 向量 RAG + 信任分層 |
| **防幻覺** | 無 | 快取版本化、記憶降權、非權威標記、timeout 安全回退 |
| **LLM 供應商** | 無 | oMLX / Anthropic / OpenAI / Ollama 統一抽象 |
| **安全** | 無 | RBAC 權限、CSRF、事件稽核、會話硬化 |
| **Lawsnote** | 無 | 完整整合（商業授權，不含於公開版） |
| **OpenClaw** | 無 | 分散推論橋接（預設停用，可選啟用） |
| **頁面路由** | 無 | Blueprint 模組化（dashboard、intel、openclaw） |
| **Git 倉庫** | 無版控 | `https://github.com/WhaleChao/MAGI-v2.git` |

### v2 主要新增能力

1. **企業級基礎設施** — 權限系統、事件匯流排、鉤子生命週期、任務運行時、會話管理
2. **低幻覺架構** — 路由收緊、記憶信任分層、摘要非權威標記、timeout 不自由回答
3. **統一工具登錄** — 動態工具發現、執行合約、全局登錄
4. **多代理運行時** — 代理協調器、團隊輪轉分派、代理間通信
5. **供應商抽象** — oMLX / Anthropic / OpenAI / Ollama 統一介面
6. **模組化頁面路由** — Flask Blueprint 拆分，`server.py` 不再包含所有路由

---

## 快速開始

### macOS（Apple Silicon — 建議）

```bash
# 1. 下載
git clone https://github.com/WhaleChao/MAGI-v2.git && cd MAGI-v2

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
```

### Windows

```powershell
# 1. 下載
git clone https://github.com/WhaleChao/MAGI-v2.git && cd MAGI-v2

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
python3 skills/ops/system_test.py        # 12 項系統測試
python3 skills/magi-doctor/action.py --task diagnose  # 技能完整性診斷
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
git clone https://github.com/WhaleChao/MAGI-v2.git && cd MAGI-v2

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
   - Apple Silicon（≥16 GB）：TAIDE-12b（文字+視覺）+ Coder-14B + ModernBERT + GLM-OCR
   - NVIDIA GPU（≥8 GB）：TAIDE-8b GGUF + Qwen2.5-7b + Nomic-embed
   - 純 CPU（≥8 GB）：輕量 GGUF 模型
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

---

## 系統架構

```
┌──────────────────────────────────────────────────────────────┐
│                         頻道層                                 │
│        LINE Webhook  │  Discord Bot  │  Telegram Bot          │
└───────────────────────┬──────────────────────────────────────┘
                        │
┌───────────────────────▼──────────────────────────────────────┐
│                  Casper 協調器                                  │
│  輸入消毒 → Iron Dome → 意圖分類 → Embedding 路由 → 技能派遣    │
│                                                                │
│  ┌─ Permission Enforcer ─┐  ┌─ Hook Bus ─┐  ┌─ Event Sink ─┐ │
│  │  權限檢查              │  │  前後鉤子   │  │  JSONL 稽核  │ │
│  └────────────────────────┘  └─────────────┘  └──────────────┘ │
└───────────────────────┬──────────────────────────────────────┘
                        │
┌───────────────────────▼──────────────────────────────────────┐
│                       執行層                                    │
│  ┌─ Provider Registry ────────────────────────────────────┐   │
│  │  oMLX │ Anthropic │ OpenAI │ Ollama                    │   │
│  └────────────────────────────────────────────────────────┘   │
│  ┌─ Tool Registry ─┐  ┌─ Task Runtime ─┐  ┌─ Session ─┐     │
│  │  動態工具登錄    │  │  異步任務隊列  │  │  對話歷史  │     │
│  └──────────────────┘  └────────────────┘  └───────────┘     │
│  57+ 技能  │  Playwright  │  FAISS  │  MCP                    │
└───────────────────────┬──────────────────────────────────────┘
                        │
┌───────────────────────▼──────────────────────────────────────┐
│                       資料層                                    │
│  magi_brain (本地 MariaDB)  │  law_firm_data (遠端)            │
│  FAISS 向量索引 (426K+ 向量) │  NAS 案件資料夾                  │
└──────────────────────────────────────────────────────────────┘
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

## v2 基礎設施模組

以下為 v2 新增的企業級基礎設施，v1 完全沒有這些子系統：

### 權限系統（`api/permissions/`）

RBAC 權限引擎，控制技能執行、工具存取、API 端點的存取權限。

| 模組 | 功能 |
|------|------|
| `models.py` | PermissionMode、PermissionRule、PermissionDecision |
| `policy.py` | 權限策略定義 |
| `rules.py` | 允許/拒絕規則構築 |
| `enforcer.py` | 權限檢查引擎 |

### 事件系統（`api/events/`）

全局事件發射器，所有操作產生可追蹤的事件流。

| 事件類型 | 說明 |
|---------|------|
| `RouteDecisionEvent` | 路由決策追蹤 |
| `MemoryWriteEvent` | 記憶寫入稽核 |
| `PreToolHookEvent` / `PostToolHookEvent` | 工具執行前後 |
| `TaskLifecycleEvent` | 任務狀態變化 |
| `FallbackEvent` | 降級事件 |

### 鉤子匯流排（`api/hooks/`）

實時事件訂閱/發佈機制，支援 JSONL 持久化。

### 任務運行時（`api/tasks/`）

異步任務排隊與執行、狀態管理（PENDING → RUNNING → COMPLETE/FAILED）、超時控制。

### 會話管理（`api/session/`）

多輪對話上下文保存、歷史管理、摘要生成、待處理狀態追蹤。支援權威/非權威內容分離。

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
# 預期：63 passed
```

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
Webhook 處理器 → 簽章驗證、角色檢查
    │
    ▼
Permission Enforcer → 權限檢查
    │
    ▼
協調器（api/orchestrator.py）
    │  ─ 輸入消毒 → Iron Dome → Embedding Router
    │
    ▼
意圖分類器（regex → heuristic → LLM）
    ├─ DANGER → 阻擋 + red_phone 警報
    ├─ CMD    → Tool Registry → 技能執行 → Hook Bus（前後鉤子）
    ├─ QUERY  → ask_casper()（記憶檢索 + 信任分層 + 網路研究）
    └─ CHAT   → chat_casper()（不自動寫回記憶）
    │
    ▼
Event Sink → JSONL 稽核日誌
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
| 橋接模組 | `skills/bridge/openclaw_codex_bridge.py` — 完整的分散式推論管理 |
| 支援功能 | OCR、翻譯、視覺、意圖識別、轉錄 |
| 預設狀態 | **停用**（`MAGI_AVOID_DISTRIBUTED=1`） |
| 啟用方式 | 設定 `MAGI_AVOID_DISTRIBUTED=0` 及 `OPENCLAW_TELEGRAM_BOT_TOKEN` |

### 為什麼預設停用

v2 的本地推理（oMLX + TAIDE-12b）已足夠應付所有核心任務。OpenClaw 的分散推論主要用於：
- 需要大量並行推論的場景
- 本地 GPU 記憶體不足時的降級方案
- 多節點協作的特殊工作流程

### Lawsnote 整合

Lawsnote 判決資料庫爬蟲為**商業授權功能**，不包含於公開版倉庫中。相關檔案已在 `.gitignore` 中排除。替代方案：使用 `judgment-collector` 技能透過司法院公開 API 收集裁判書。

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
| **模型** | `MAGI_MAIN_MODEL` | `TAIDE-12b-Chat-mlx-4bit`（macOS） |
| **推理** | `MAGI_OMLX_ENABLED` | `1` 用 oMLX（macOS），`0` 用 Ollama |

完整設定見 [.env.example](.env.example)。

---

## 技術棧

| 層次 | 技術 |
|------|------|
| 語言 | Python 3.12+（核心） |
| 網頁框架 | Flask + Jinja2 + Blueprint |
| 資料庫 | MariaDB 10.11+ |
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

---

## 目錄結構

```
MAGI-v2/
├── api/                                # Flask 伺服器、協調器、基礎設施
│   ├── server.py                       # 主入口 — webhook、儀表板（port 5002）
│   ├── orchestrator.py                 # 意圖路由與技能派遣
│   ├── orchestrator_core.py            # RuntimeFoundations 聚合容器
│   ├── tools_api.py                    # RESTful 工具 API（port 5003）
│   ├── discord_bot.py                  # Discord 整合
│   ├── tw_output_guard.py              # 輸出品質防護 + 非權威標記
│   ├── permissions/                    # [v2] RBAC 權限系統
│   ├── events/                         # [v2] 事件發射器 + JSONL Sink
│   ├── hooks/                          # [v2] 鉤子匯流排
│   ├── tasks/                          # [v2] 任務運行時
│   ├── session/                        # [v2] 會話管理
│   ├── tools/                          # [v2] 工具登錄
│   ├── agents/                         # [v2] 多代理運行時
│   ├── coordinator/                    # [v2] 代理協調器
│   ├── blueprints/                     # [v2] 模組化頁面路由
│   └── handlers/                       # 模組化請求處理器
├── providers/                          # [v2] LLM 供應商抽象層
│   ├── omlx.py / anthropic.py / openai.py / ollama.py
│   └── base.py
├── skills/                             # 57+ 模組化技能
│   ├── bridge/                         # 推理閘道、路由、安全
│   ├── ops/                            # 營運 + 平台抽象
│   ├── magi/                           # 自治（夜議、共識、核准）
│   ├── memory/                         # FAISS 向量記憶 + RAG
│   └── {skill-name}/                   # 個別技能模組
├── casper_ecosystem/                   # 法務自動化引擎
├── tests/                              # 57 個測試檔（378+ tests）
├── scripts/                            # 排程任務
├── migrations/                         # 資料庫遷移
├── templates/                          # Jinja2 模板
├── static/                             # 靜態資源
├── daemon.py                           # 程序守護 daemon
├── setup_wizard.py                     # 首次設定精靈
├── pyproject.toml                      # Python 套件設定
├── requirements.txt                    # 核心依賴
├── requirements-optional.txt           # 選配依賴
├── requirements-windows.txt            # Windows 專用依賴
├── .env.example                        # 環境變數範本
└── CONSTITUTION.md                     # 治理規則
```

---

## 連接埠

| 埠號 | 服務 | 說明 |
|------|------|------|
| 5002 | MAGI Server | LINE Webhook + 儀表板（主要端口） |
| 5003 | Tools API | RESTful 工具 API（5002 會自動 fallback proxy） |
| 8080 | oMLX / Ollama | 本地推理引擎 |
| 8081 | Embedding 服務 | ModernBERT 嵌入 |
| 8199 | 設定精靈 | 首次設定（暫時） |
| 18790 | Caddy | 反向代理（選配，對外服務用） |

---

## 測試

```bash
# 全部測試（378+ tests）
python -m pytest tests/ -q

# 低幻覺專項測試（63 tests）
python -m pytest tests/test_routing.py tests/test_memory_grounding.py \
  tests/test_orchestrator_low_hallucination.py tests/test_tw_output_guard_metadata.py -q

# 基礎設施測試
python -m pytest tests/test_permissions_runtime.py tests/test_hooks_runtime.py \
  tests/test_event_stream_runtime.py tests/test_task_runtime_foundation.py -q

# 系統自我測試
python3 skills/ops/system_test.py

# 系統診斷（266 項）
python3 skills/magi-doctor/action.py --task diagnose

# Smoke 整合測試
python -m pytest tests/smoke_synology.py -q
```

---

## 授權

MIT License — 詳見 [LICENSE](LICENSE) 檔案。
