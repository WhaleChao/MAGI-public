# MAGI — 多代理治理基礎設施

[English](README.md)

MAGI v2 是一套部署於本地硬體的 AI 作業平台，專為台灣法律事務所的日常營運設計。全系統在單台 Apple Silicon 節點上運行，結合 Flask 控制平面、60+ 模組化技能、三哲人 ensemble 推理管線、ReAct Agentic 工具呼叫引擎、定時排程、本地 LLM 推理，以及深度法務工作流程自動化——全部整合於一個程式碼庫。

**macOS 原生。** 生產環境在 Apple Silicon 透過 [oMLX](https://github.com/omlx/omlx) 以三模型日夜輪換架構運行。Windows / Linux 透過 Ollama 亦可支援。

> **單機模式（預設）。** 所有生產工作負載皆在 Casper（Mac Mini M4）本地運行。程式碼保留 Melchior / Balthasar 分散推理架構；設定 `MAGI_AVOID_DISTRIBUTED=0` 可啟用多節點推理。

---

## 目錄

- [快速開始](#快速開始)
- [系統架構](#系統架構)
  - [三模型日夜切換](#三模型日夜切換)
  - [三哲人 Ensemble 審查](#三哲人-ensemble-審查)
  - [Agentic 工具呼叫（ReAct）](#agentic-工具呼叫react)
  - [中文 NLP 與知識圖譜](#中文-nlp-與知識圖譜)
- [法務自動化](#法務自動化)
  - [法律扶助基金會（LAF）](#法律扶助基金會laf)
  - [電子閱卷](#電子閱卷)
  - [電子筆錄](#電子筆錄)
- [操作管理 — `magi` CLI](#操作管理--magi-cli)
- [技能目錄](#技能目錄)
- [訊息處理流程](#訊息處理流程)
- [治理與安全](#治理與安全)
- [環境設定](#環境設定)
- [技術堆疊](#技術堆疊)
- [目錄結構](#目錄結構)
- [服務埠口](#服務埠口)
- [測試](#測試)
- [授權](#授權)

---

## 快速開始

### macOS（Apple Silicon）

```bash
# 1. 複製專案
git clone https://github.com/WhaleChao/MAGI.git && cd MAGI

# 2. 建立 Python 環境（需 Python 3.9+）
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
pip install -r requirements-optional.txt  # MarkItDown、Scrapling、RapidOCR 等

# 3. 複製並填寫環境變數
cp .env.example .env   # 填入 token / DB 憑證

# 4. 啟動
launchctl load ~/Library/LaunchAgents/com.magi.daemon.plist
magi status
```

### Linux / Windows（Ollama 後端）

```bash
ollama pull gemma2:9b   # 或任何支援的模型
MAGI_ALLOW_CLOUD_MODELS=1 python daemon.py
```

---

## 系統架構

```
使用者（LINE / Discord / Telegram / Web）
          │
          ▼
 ┌─────────────────────────────────┐
 │   message_pipeline.py           │  意圖分類
 │   20+ 攔截器（法務 / 作業）      │  指令分派
 └────────────┬────────────────────┘
              │  QUERY / CMD
              ▼
 ┌─────────────────────────────────────────────────────┐
 │         ensemble_chat_with_tools()                  │
 │                                                     │
 │  Phase 1 — Casper + ReAct 引擎                      │
 │    ├─ 不需工具  → FINAL（直接回答）                  │
 │    └─ 需要工具  → ACTION → 執行 → OBSERVE           │
 │                    ↺ 最多 5 步                       │
 │                                                     │
 │  Phase 2 — Melchior + Balthasar 並行審查            │
 │    ├─ 兩者同意  → MAGI 共識                         │
 │    └─ 任一否決  → 顯示個別哲人意見                  │
 └─────────────────────────────────────────────────────┘
              │
              ▼
      format_magi_response()
      Iron Dome 輸出守衛
      tw_output_guard（信任標籤洩漏防護）
```

### 三模型日夜切換

MAGI 依時段自動切換模型組合：

| 模式 | Port 8080 | Port 8082 | Port 8083 | 觸發方式 |
|------|-----------|-----------|-----------|----------|
| **日間**（07:00–21:50） | Gemma-4 E4B（Casper） | Phi-4-mini（Melchior） | SmolLM3-3B（Balthasar） | `cron` + daemon 自動啟動 |
| **夜間**（21:50–07:00） | Gemma-4 26B | — | — | `cron` 切換 |

- **日間模式**：三個模型並行，各自注入獨立 SOUL persona（`docs/soul/SOUL_*.md`）。
- **夜間模式**：單一高容量 26B 模型處理批次任務（法扶稽核、PDF 命名、筆錄索引、LoRA 蒸餾訓練）。
- 切換由兩個 cron job 管理（`job_omlx_switch_night` / `job_omlx_switch_day`），`daemon.py` 在日間啟動時自動 kickstart Phi-4 與 SmolLM3。

### 三哲人 Ensemble 審查

每個回應都經過兩階段管線：

**Phase 1 — Casper 生成**
- 以 `ReActEngine.for_omlx()` 搭配最多 8 個工具，執行最多 5 步 ReAct 迴圈。
- 使用 `get_compact_tools(user_query)` — 8 個常駐工具，加上條件開啟的 `remember`（只有使用者明確要求記住時才開放）。

**Phase 2 — Melchior + Balthasar 並行審查**
- Melchior（Phi-4-mini）：邏輯一致性與法律正確性審查。
- Balthasar（SmolLM3-3B）：格式與引用稽核。
- 各自獨立投票 `APPROVE` 或 `VETO`（附一句理由）。

**輸出格式**
- 全數通過 → `「MAGI：...」`（共識標籤）
- 任一否決 → 顯示該哲人姓名與否決理由。
- 使用工具時附上資料來源（例：`（資料來源：web_search、query_cases）`）。

**模型選用政策**
- 排除中國大陸模型（Qwen / DeepSeek / GLM / Yi）——因審查風險。
- 規則式繁簡體偵測器，無需 LLM 推理即可攔截簡體中文輸出。

### Agentic 工具呼叫（ReAct）

`ReActEngine.for_omlx()` 對 E4B 執行同步 ReAct 迴圈：

```
使用者查詢
  → 建立 system prompt（soul + 工具清單 + ReAct 格式）
  → LLM 輪次：THINK → ACTION: <工具> / PARAMS: {...}
  → 本地執行工具
  → 將 OBSERVATION 注入對話
  → 重複直到 FINAL: <答案> 或達到 max_steps（5）
```

**可用工具（精簡集）**

| 工具 | 說明 |
|------|------|
| `search_memory` | FAISS + Graph-RAG 記憶檢索 |
| `web_search` | Scrapling 即時網路搜尋 |
| `query_cases` | 依案號 / 當事人查詢案件資料庫 |
| `get_schedule` | 讀取法庭行事曆 |
| `calculate` | 安全算術計算 |
| `current_time` | 當前日期時間 |
| `summarize` | 長文摘要（含抽取式 fallback） |
| `translate` | 翻譯（Google GTX 快速路徑） |
| `remember` | *（條件開啟）* 寫入長期記憶 |

功能旗標：`MAGI_ENSEMBLE_TOOLS=1`（預設 `0`）。

### 中文 NLP 與知識圖譜

- **PKUSeg** 分詞器搭配法律詞典（`skills/engine/legal_dict.txt`），透過 Python 3.11 sidecar 確保相容性。
- **Graph-RAG**（`skills/engine/knowledge_graph/`）：實體抽取 → 關係建構 → 社群偵測 → 注入 `recall()` 上下文。
- GraphStore 使用 mtime 鍵值快取；短法律查詢走快速路徑，p95 延遲 < 200ms。
- 所有向量 embedding 使用 NLP 正規化輸入；顯示時保留原始文字。

---

## 法務自動化

### 法律扶助基金會（LAF）

自動化法扶案件完整生命週期：

| 階段 | MAGI 執行內容 |
|------|---------------|
| **來信偵測** | Gmail 監控自動偵測法扶通知信 |
| **申辦開案** | 自動填寫開辦表單、上傳委任狀 + 法扶通知書 |
| **待辦掃描** | 掃描 portal 中未簽署的暫存草稿，通知律師 |
| **結案辦理** | 依規則草擬結案申請，附正確備註格式 |
| **批次作業** | 透過自然語言指令執行批次查詢 / 結案 / 稽核 |
| **智慧辨識** | 依狀態優先順序 + 關鍵字過濾，自動消除多案歧義 |

NAS 資料夾結構依案件類型（法扶 / 一般 / 無償 / 指定辯護）分別處理。

### 電子閱卷

兩階段閱卷申請流程（`file-review-orchestrator`）：

1. **填表** — 系統自動填寫電子閱卷申請表並截圖。
2. **確認** — 產生 6 字元 hex 確認碼（30 分鐘 TTL），將截圖傳送律師審閱。
3. **律師核可** — 回覆確認碼；系統重新驗證並送出申請。

安全閘門：確認端點只接受來源為 `user/telegram/discord/line` 的請求（非 CLI 直接呼叫），除非設定 `MAGI_FILE_REVIEW_ALLOW_CONFIRM=1`。

附件掃描有 20 秒預算限制與 600 筆候選上限，防止 NAS I/O 飽和。

### 電子筆錄

- 自動下載並以 MD5 去重（JSON + MariaDB 雙寫）。
- DB fallback：本機 JSON 遺失時，可從 MariaDB 回復去重記憶。
- 整合自我測試、`db_probe` 與登入 smoke 步驟。

---

## 操作管理 — `magi` CLI

```
magi status       # 完整系統健康（服務、oMLX、NAS、DB、殭屍）
magi restart      # 透過 launchctl kickstart 乾淨重啟
magi stop         # 正常關機
magi zombie       # 列出並回收殭屍程序
magi logs         # 追蹤所有日誌
```

NAS 狀態同時檢查 `/Volumes/` 與 `~/.magi_mounts/`（Tailscale fallback 路徑）。

**37 個定時排程任務**（由 `cron_jobs.json` 管理，由 Discord Bot 排程器執行）：

| 類別 | 任務 |
|------|------|
| 法務 | 法扶待辦掃描、法扶夜間稽核、司法院 API 夜拉 + 晨間拉取、閱卷檢查（平日 10:00 / 15:00） |
| 知識庫 | Obsidian 向量入庫、見解同步、Wiki 合成、知識 lint、見解重處理、判決補查 |
| 運維 | 健康報告、夜間 autopilot、最佳化報告、夜間回歸測試、人格清理、debug 截圖清理 |
| NAS / 文件 | PDF 命名（夜間）、週末書籤、筆錄同步、每週法律爬取 |
| 市場 | 市場簡報（平日 08:30）、全球情報監控（每 6 小時）、對沖基金委員會 |
| 基礎設施 | oMLX 日夜切換、OSC 案件索引 / 掃描、Google 日曆同步、external chat 健康檢查 |

---

## 技能目錄

60+ 個技能模組位於 `skills/`，各自有獨立的 `action.py` 入口。

### 法務
| 技能 | 功能 |
|------|------|
| `laf-orchestrator` | 法扶案件完整生命週期自動化 |
| `file-review-orchestrator` | 兩階段電子閱卷申請 |
| `transcript-downloader` | 電子筆錄下載與去重 |
| `statutes-vdb` | 法條向量資料庫 + 條號對應 |
| `judgment-collector` | 司法院判決書爬取 |
| `judicial-web-search` | 司法院即時搜尋（HTTP form + Scrapling） |
| `judicial-flow-search-archive` | 本地判決庫 fallback |
| `contract-review` | AI 合約審閱（搭配 MarkItDown） |
| `trial-prep` | 開庭準備清單 |
| `evidence-admissibility` | 刑事卷證傳聞法則分類 |
| `labor-law-calculator` | 加班費 / 資遣費計算 |
| `laf-refine-case` | 法扶案件資料補強 |
| `laf-withdrawal-report` | 法扶撤回報告自動化 |
| `brief-gen` | AI 書狀草稿生成 |
| `court-hearing-reminder` | 開庭日提醒 |
| `hearing` | 庭期管理 |

### 文件與 PDF
| 技能 | 功能 |
|------|------|
| `pdf-namer` | AI PDF 命名（Vision OCR + 多引擎共識） |
| `pdf-bookmarker` | PDF 目錄與書籤生成 |
| `doc-producer` | 文件產製管線 |
| `docx` | Word 文件創建 / 編輯 |
| `pptx` | PowerPoint 生成 |
| `xlsx` | 試算表處理 |
| `documents` | 統一文件讀取（MarkItDown adapter） |
| `screenshot-sorter-tw` | 截圖分類與歸檔 |

### 情報與研究
| 技能 | 功能 |
|------|------|
| `market-briefing` | 對沖基金委員會：技術 / 基本面 / 情緒分析師 + 風控 / 投資組合經理 |
| `worldmonitor-intel` | 全球新聞與法律情報監控 |
| `autoresearch` | 自主研究管線 |
| `insight-refine` | 見解蒸餾與精煉 |
| `crawler-targets` | 定時爬取目標 |
| `obsidian` | Obsidian 筆記庫同步與向量入庫 |

### 記憶與推理
| 技能 | 功能 |
|------|------|
| `memory` | 長期記憶：FAISS 向量庫 + Graph-RAG |
| `brain_manager` | 跨 session 記憶管理 |
| `reasoning` | 逐步推理鷹架 |
| `bridge` | Ensemble 推理橋接（Casper / Melchior / Balthasar） |
| `casper` | Casper LLM 直接介面 |
| `translator` | 翻譯（Google GTX 快速路徑 + LLM fallback） |

### 運維
| 技能 | 功能 |
|------|------|
| `magi-autopilot` | 夜間批次自動化任務 |
| `magi-doctor` | 系統健康診斷 |
| `magi-self-repair` | 已知故障模式自動修復 |
| `process-hygiene` | 殭屍與過期程序清理 |
| `iron-dome` | 安全規則引擎 |
| `gmail-drafts` | Gmail 草稿管理 |

---

## 訊息處理流程

```
收到訊息
    │
    ├─ 20+ 正則攔截器（法扶 / 閱卷 / 筆錄 / 排庭 / 帳務 …）
    │       ↓ 命中 → 直接走領域處理器（跳過 LLM）
    │
    ├─ 意圖分類器  →  CMD / QUERY / CHAT / SYSTEM
    │
    ├─ CMD / QUERY（MAGI_ENSEMBLE_TOOLS=1）
    │       ↓  ensemble_chat_with_tools()
    │       ↓  Phase 1：ReAct（Casper + 工具，最多 5 步）
    │       ↓  Phase 2：Melchior + Balthasar 並行審查
    │       ↓  format_magi_response()
    │
    ├─ CMD / QUERY（MAGI_ENSEMBLE_TOOLS=0，預設）
    │       ↓  ensemble_chat_verified() — 直接三哲人文字生成
    │
    └─ CHAT  →  grounded_ai.chat_casper()（閒聊快速路徑）
```

**支援頻道**：LINE Messaging API、Discord Bot、Telegram Bot、Web API（`/osc/external/chat`）。

---

## 治理與安全

### Iron Dome
對每次工具呼叫與 shell 指令進行多層安全審查：
- 危險字串模式比對（`rm -rf`、SQL `DROP`、路徑穿越等）。
- 嚴重程度評分：BLOCK / WARN / ALLOW。
- 在每次 ReAct 工具執行前觸發。

### 信任標籤洩漏防護
- 內部上下文標籤（`[已驗證事實]`、`[使用者陳述]` 等）僅供內部推理使用。
- `tw_output_guard.py` 攔截並改寫任何將此類標籤洩漏到外部頻道的回應。
- `grounded_ai.py` 偵測人格幻覺（`身為 CASPER …`）並在輸出前重試。

### 中國模型政策
具有已知內容審查限制的模型（Qwen / DeepSeek / GLM / Yi 系列）依政策排除在外。僅允許無審查限制的開放模型。

### Reaper 安全白名單
`daemon.py` Phase 4 過期程序回收器具有明確安全白名單（`REAPER_SAFE_UTILITIES`），防止 oMLX、magi_menubar、admin_server 與 benchmark 程序被誤殺為「過期 Python 程序」。

---

## 環境設定

主要環境變數（在 `.env` 中設定）：

| 變數 | 預設值 | 用途 |
|------|--------|------|
| `MAGI_ENSEMBLE_TOOLS` | `0` | 啟用 ReAct Agentic 工具呼叫 |
| `MAGI_ALLOW_CLOUD_MODELS` | `0` | 允許 Claude / GPT fallback |
| `MAGI_USE_SCRAPLING` | `0` | 使用 Scrapling 抓網頁（更快，無需瀏覽器） |
| `MAGI_USE_MARKITDOWN` | `0` | 使用 MarkItDown 解析文件 |
| `MAGI_PDF_OCR_CONSENSUS` | `0` | PDF 命名多引擎 OCR 共識 |
| `MAGI_NAS_HOST` | `192.168.1.3` | NAS LAN IP |
| `MAGI_NAS_TAILSCALE_HOST` | `100.111.10.126` | NAS Tailscale IP（自動 fallback） |
| `MAGI_AVOID_DISTRIBUTED` | `1` | 僅單機運行 |
| `MAGI_COMMITTEE_LIGHT_MODEL` | *(E4B)* | 分析師代理模型 |
| `MAGI_COMMITTEE_HEAVY_MODEL` | *(26B)* | 風控 / 投資組合經理模型 |
| `MAGI_FILE_REVIEW_ALLOW_CONFIRM` | `0` | 允許 CLI 觸發閱卷確認 |
| `MAGI_JUDICIAL_VERIFY_SSL` | `0` | 司法院網站 SSL 驗證（TLS 相容模式關閉） |

---

## 技術堆疊

| 層次 | 技術 |
|------|------|
| **執行環境** | Python 3.9+（生產：macOS 3.14），venv |
| **LLM 推理** | [oMLX](https://github.com/omlx/omlx)（MLX / Apple Silicon）· Ollama（Linux/Windows） |
| **模型** | Gemma-4 E4B · Phi-4-mini · SmolLM3-3B · Gemma-4 26B（夜間） |
| **Embedding** | ModernBERT-embed-4bit（port 8081） |
| **向量庫** | FAISS（144K+ 向量，mmap） |
| **資料庫** | MariaDB（本地 + Tailscale 遠端同步） |
| **中文 NLP** | PKUSeg（3.11 sidecar）· Apple NaturalLanguage fallback |
| **知識圖譜** | 自製 Graph-RAG（實體抽取 → 社群偵測） |
| **網路爬取** | Scrapling · requests + BeautifulSoup fallback |
| **文件解析** | MarkItDown · pdftotext · fitz · pdfplumber · Tesseract · macOS Vision |
| **OCR** | macOS Vision · RapidOCR · Tesseract（共識模式） |
| **API 框架** | Flask · Flask-Login · Flask-SocketIO |
| **排程** | `discord_bot.py` 內建 CronScheduler（cron_jobs.json） |
| **訊息頻道** | LINE Messaging API · Discord.py · python-telegram-bot |
| **NAS** | SMB LAN（192.168.1.3）+ Tailscale fallback（100.111.10.126） |
| **日曆** | Google Calendar API（OAuth2，自動 refresh） |
| **安全** | Iron Dome 規則引擎 · tw_output_guard · 信任標籤洩漏偵測器 |
| **測試** | pytest（1,142 個測試） |

---

## 目錄結構

```
MAGI_v2/
├── daemon.py                   # 主程序管理（KeepAlive、reaper、日夜切換）
├── api/
│   ├── server.py               # Flask API（port 5002）
│   ├── tools_api.py            # Tools API（port 5003）
│   ├── discord_bot.py          # Discord Bot + CronScheduler
│   ├── pipelines/              # message_pipeline、command_dispatch、skill_dispatch …
│   ├── domains/                # laf_flow、multimedia_flow、judgment_flow、schedule_flow …
│   ├── blueprints/             # web_runtime、admin_runtime
│   ├── nas_mount_guard.py      # NAS SMB 自動掛載 + Tailscale fallback
│   ├── debug_capture.py        # 統一 debug 截圖 helper
│   └── tw_output_guard.py      # 輸出正規化 + 信任標籤洩漏防護
├── skills/
│   ├── engine/                 # react_engine、tool_registry、chinese_nlp、knowledge_graph …
│   ├── bridge/                 # ensemble_inference、grounded_ai、llm_direct …
│   ├── legal/                  # laf.py、judicial.py（瀏覽器自動化）
│   ├── memory/                 # mem_bridge、vector_pipeline
│   ├── documents/              # file_bridge、multimodal_parser、document_reader
│   ├── research/               # web_research、github_monitor
│   ├── evolution/              # usage_tracker、skill_improver、skill_genesis
│   ├── laf-orchestrator/       # 法扶生命週期技能
│   ├── file-review-orchestrator/
│   ├── transcript-downloader/
│   ├── pdf-namer/
│   ├── market-briefing/        # 對沖基金委員會（agents/、models/、predict/）
│   └── …（共 60+ 技能）
├── docs/
│   └── soul/                   # SOUL_CASPER.md · SOUL_MELCHIOR.md · SOUL_BALTHASAR.md
├── tests/                      # 1,142 個 pytest 測試
├── cron_jobs.json              # 所有排程任務的唯一來源
└── .env                        # 執行環境設定（不提交至版本控制）
```

---

## 服務埠口

| 埠口 | 服務 |
|------|------|
| `5002` | Flask 主伺服器（`/health`、`/chat`、`/skills/…`） |
| `5003` | Tools API（`/summarize`、`/translate`、`/collab/transcribe`、`/osc/external/chat`） |
| `8080` | oMLX 文字 — Gemma-4 E4B（Casper，日間）/ Gemma-4 26B（夜間） |
| `8081` | oMLX Embedding — ModernBERT-embed-4bit |
| `8082` | oMLX 文字 — Phi-4-mini-instruct（Melchior，僅日間） |
| `8083` | oMLX 文字 — SmolLM3-3B（Balthasar，僅日間） |
| `8088` | Website Admin 管理面板 |
| `50052` | gRPC RPC Worker |

---

## 測試

```bash
# 完整測試
./venv/bin/python -m pytest -q          # 1,142 個測試

# 精準測試
pytest tests/test_react_omlx.py         # ReAct + ensemble tools（15 個測試）
pytest tests/test_document_reader.py    # MarkItDown adapter（24 個測試）
pytest tests/test_knowledge_graph.py    # Graph-RAG（5 個測試）
pytest tests/test_judicial_web_search.py

# Live smoke（需要服務運行中）
magi status
curl http://127.0.0.1:5002/health
curl http://127.0.0.1:5003/health
MAGI_USE_SCRAPLING=1 skills/judicial-web-search/action.py --task self_test
skills/laf-orchestrator/action.py --task self_test
skills/file-review-orchestrator/action.py --task self_test
skills/transcript-downloader/action.py --task self_test
```

CI 閘門：`scripts/ci/check_hardcodes.py` — 提交的程式碼中有任何 IP / 憑證即失敗。

---

## 授權

私有 / 專屬。保留所有權利。

原始碼僅供參考與內部使用。未經書面許可，不得重新發布或用於商業用途。
