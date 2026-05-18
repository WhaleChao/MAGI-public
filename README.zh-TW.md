# MAGI — 多代理治理基礎設施

[English](README.md)

MAGI v2 是一套部署於本地硬體的 AI 作業平台，專為台灣法律事務所的日常業務設計。全系統在單台 Apple Silicon 節點上執行，結合 Flask 控制平面、60+ 模組化技能、三哲人 ensemble 推理流程、ReAct Agentic 工具呼叫引擎、定時排程、本地 LLM 推理，以及深度法務工作流程自動化——全部整合於一個程式碼庫。

**macOS 原生。** 生產環境在 Apple Silicon 透過 [oMLX](https://github.com/omlx/omlx) 以三模型日夜輪換架構執行。Windows / Linux 透過 Ollama 亦可支援。

> **單機模式（預設）。** 所有生產工作負載皆在 Casper（Mac Mini M4）本地執行。程式碼保留 Melchior / Balthasar 分散推理架構；設定 `MAGI_AVOID_DISTRIBUTED=0` 可啟用多節點推理。

---

## 目錄

- [快速開始](#快速開始)
- [目前公開狀態](#目前公開狀態)
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
git clone https://github.com/WhaleChao/MAGI-public.git && cd MAGI-public

# 2. 執行客戶安裝精靈
#    如需先預演，先省略 --yes。
python3 scripts/customer_install_wizard.py --public --yes

# 3. 啟用本機環境
source .venv/bin/activate  # 既有安裝也可能使用 source venv/bin/activate

# 4. 補齊客戶自己的 .env 設定後重新檢查
python3 scripts/first_run_setup.py --public --json
python3 scripts/magi_doctor.py

# 5. 啟動
launchctl load ~/Library/LaunchAgents/com.magi.daemon.plist
magi status
```

既有維運者仍可使用手動流程：

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
pip install -r requirements-optional.txt
```

### Linux / Windows（Ollama 後端）

```bash
ollama pull gemma2:9b   # 或任何支援的模型
MAGI_ALLOW_CLOUD_MODELS=1 python daemon.py
```

---

## 目前公開狀態

此分支已整理為可公開版本：私有 runtime、代理工作記錄、部署手札、OCR 暫存等資料不再納入 git 追蹤，並由 `.gitignore` 保護。`.runtime/`、`.claude/`、`.claire/`、`runtime/supplement_cache/`、`docs/deploy/` 應維持本機私有。

公開前檢查：

```bash
python3 scripts/public_release_audit.py --public-isolation --strict
python3 scripts/customer_install_wizard.py --public --no-live
python3 scripts/first_run_setup.py --public --json
python3 scripts/magi_doctor.py --json
python3 scripts/install_magi.py --dry-run --check-live
```

`customer_install_wizard.py` 是外部客戶的一鍵安裝入口：會建立本機 `.env`、產生本機 secret、在加上 `--yes` 時安裝依賴、建立本機排程設定、執行偵測與商用檢查，並寫出 `.runtime/customer_install_wizard_latest.json`，且不會列印 token 或密碼。`first_run_setup.py` 則保留為較細的 checklist 工具。`public_release_audit.py` 會阻擋高可信度 secret 與被追蹤的私有路徑；公開推送前請加上 `--public-isolation`，一併阻擋私有實務見解來源整合與私人信箱/NAS 標記。正式發布與商用部署請使用 `--strict`；發布分支預期應通過 `0 errors / 0 warnings`。

公開或交付他人使用前，請把以下檢查視為 go/no-go 門檻：

- README、操作手冊、服務條款、隱私權政策、資料保留政策與第三方套件清單均已更新。
- MAGI daemon 可啟動，`/health`、OSC 主要頁籤、訊息頻道、DB、NAS、Google Calendar OAuth 均通過 live 檢查。
- `scripts/public_release_audit.py --strict` 不得有 error 或 warning；若只有公開安裝版本、不含私有 DB，可另外用 `--skip-db` 跑安裝檢查。
- `.env`、OAuth token、DB dump、案件資料、portal 截圖、NAS 路徑與 runtime 報告不得被 git 追蹤。
- 法扶、閱卷、筆錄與日曆同步屬於高風險流程；正式送出、還原 DB、批次搬檔仍需確認碼或人工確認。

外部客戶自行安裝流程：

```bash
git clone https://github.com/WhaleChao/MAGI-public.git
cd MAGI-public
python3 scripts/customer_install_wizard.py --public --yes
python3 scripts/public_release_audit.py --public-isolation --strict
```

正式商用文件：

- [商用上線檢核指南](docs/COMMERCIAL_READINESS.md)
- [一般使用者手冊](docs/USER_GUIDE.md)
- [公開版操作手冊](docs/PUBLIC_OPERATION_MANUAL.md)
- [私有版操作手冊](docs/PRIVATE_OPERATION_MANUAL.md)
- [公開版自行安裝指南](docs/PUBLIC_SELF_INSTALL.md)
- [服務條款範本](docs/TERMS_OF_SERVICE.md)
- [隱私權政策](docs/PRIVACY_POLICY.md)
- [資料保留政策](docs/DATA_RETENTION_POLICY.md)
- [第三方套件 BOM](docs/THIRD_PARTY_BOM.md)
- [安全政策](SECURITY.md)
- [支援政策](SUPPORT.md)

私用正式環境商用檢核：

```bash
./venv/bin/python scripts/ops/commercial_readiness_live.py --strict-public
```

只有公開安裝版本、不含私有 DB 的檢核才使用 `--skip-db`。

Gemma 4 E4B / MTP 已接入 MLX sidecar：

```bash
python3 scripts/serve_mlx_mtp.py --host 127.0.0.1 --port 8090
curl http://127.0.0.1:8090/health
```

`scripts/live_magi_mtp_eval.py` 用於 live acceptance：涵蓋 JSON 工具路由、ReAct 真實工具呼叫、全部 ReAct 工具選擇、工具混淆防護，以及幻覺/不確定時 abstain 檢查。

### 2026-05 穩定化摘要

近期修復已納入公開版文件與 live gate：

- **法扶**：消債應備事項表恢復 OSC 條件邏輯；所得清單依聲請年度自動推算，例如 115 年 5 月後為 113、114 年，隔年自動變成 114、115 年。法扶狀態可在網頁版調整，結案案件可搬到結案區且仍可開資料夾/檔案。
- **法扶結案**：強制執行類案件可用「判決書」資料夾內的執行命令作為結案依據；同名不同程序不會只靠姓名誤判結案。進度逾 18 個月提醒支援 90 天冷卻。
- **活動計數**：開庭、會議、律見、閱卷、電話聯繫會用 OSC、Google Calendar、會議資料、閱卷資料夾交叉統計；閱卷日期排除只有繳費單的資料夾。
- **PDF / OCR**：PDF 命名支援信封頁排除、多引擎 OCR 共識、法律文字修正與訓練資料回饋；法院通知、程序裁定、判決、對方歷次書狀與判決書資料夾均納入抽測。
- **書狀**：OSC 書狀產生已加入 Word/PDF 排版保護與同案由修正學習；使用者改稿後可回饋差異，同案由才會套用經驗。
- **帳務**：Google 試算表匯入可排除非本人標識資料，固定支出與試算表項目會去重，週一/週五排程匯入；薪資等固定支出可在 MAGI 帳務設定修正。
- **實務見解**：台灣法律資料 MCP 可作為法律見解查詢來源；查不到時回報查不到，不以模型補編。
- **所務總覽**：網頁版整合案件、待辦、法扶、書狀索引、對外資料與業務概覽入口，避免重複建立功能。
- **維運**：完整 smoke 已納入商用上線守門、乾淨公開版安裝檢查、公開 secret audit、磁碟低水位告警、快取清理、NAS 掛載守門與通知分流檢查。

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

每個回應都經過兩階段流程：

**Phase 1 — Casper 產生**
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

### 重型雲端後備 — NVIDIA NIM（Plan A，2026-04-19）

當本機 oMLX 失敗或需要 SOTA 推理時，MAGI 可向 NVIDIA NIM 免費雲端推理後備：

- **觸發方式**：使用者訊息加 `@heavy` 或 `@重型` 前綴（opt-in，永不自動）
- **重型主力**：`meta/llama-3.1-405b-instruct`（128K context、多語、無內容審查）
- **快速模型**：`meta/llama-3.3-70b-instruct`（簡單重型任務）
- **硬編封鎖清單**：中國模型（DeepSeek / Qwen / MiniMax / Kimi / GLM / Yi / Baichuan / Moonshot / InternLM / ChatGLM / SenseTime）— 因內容審查不適用於律師業務
- **PII 遮蔽**：可逆遮蔽台灣身分證、法扶案號、法院案號、手機、DB 已知當事人姓名（回覆時還原）
- **安全機制**：Circuit breaker（連 3 次 429 → 60s 冷卻）、每日 500 次上限、同時並行 3 個 request
- **Rate limit**：40 req/min（單一 `nvapi-` key 在所有 NIM 模型間共用）
- **Feature flag**：`NVIDIA_NIM_ENABLE=0`（預設關閉）

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
| **結案辦理** | 依規則草擬結案申請，附正確備註格式；支援 `引用OOO的會議`（繼承他案次數）與 `OOO就是結案檔案`（依關鍵字指定任意檔案為結案依據） |
| **書狀定稿** | OSC 書狀索引可產生蓋章後的正本 / 副本 / 繕本，支援手動點選章位與定稿 PDF 合併 |
| **應備事項清單** | OSC 法扶頁提供法扶應備事項 CRUD；案件卡片仍保留案件補正清單 |
| **CSV 交換** | 案件與當事人可由 Paperclip 匯入 / 匯出 UTF-8 CSV |
| **事務所輸出** | 案件卡片可產生地址標籤 PNG；報價單可匯出 PDF |
| **主題切換** | Paperclip 提供可記憶偏好的淺色 / 深色主題切換 |
| **批次作業** | 透過自然語言指令執行批次查詢 / 結案 / 稽核 |
| **智慧辨識** | 依 DB 案件種類、法扶案號、狀態優先順序與關鍵字過濾，自動消除多案歧義 |
| **法扶活動計數** | 開庭、會議、律見、閱卷、電話聯繫統計會優先使用 OSC/法扶案件資料；同名一般案件不會被混入法扶回報 |

NAS 資料夾結構依案件類型（法扶 / 一般 / 無償 / 指定辯護）分別處理。

### Google Calendar / OSC 行事曆同步規則

MAGI 可以同時讀取多個 Google Calendar，但匯入 OSC 待辦時採白名單規則，避免把同事手動登錄、節日或私人提醒混入案件資料：

- 一般 OSC 事件：標題或描述必須以 OSC 案件系統編號開頭，例如 `[2026-0035] 開庭` 或 `2026-0035：開庭`。
- 法扶活動計數：即使標題不是 OSC 編號開頭，只要事件可由 DB 判斷為法扶案件，且內容屬於開庭、會議、律見、閱卷、電話聯繫，仍可匯入供進度回報統計。
- 同名案件：若同一當事人有一般案件與法扶案件，MAGI 會依 DB 的 `laf_case_no`、`application_no`、`case_category=法律扶助案件`、`legal_aid_status` 與案由判斷法扶案件；只有多件法扶案件仍無法由案由、法扶案號或 OSC 編號分辨時才跳過。
- 已匯入的 Google Calendar event id 會去重，避免同一事件重複建立待辦。

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

**50+ 個定時排程任務**（由 `cron_jobs.json` 管理，由 Discord Bot 排程器執行）：

| 類別 | 任務 |
|------|------|
| 法務 | 法扶待辦掃描、法扶夜間稽核、司法院 API 夜拉 + 晨間拉取、閱卷檢查（平日 10:00 / 15:00） |
| 知識庫 | Obsidian 向量入庫（`--limit 50`，每日 07:10）、案件卡片索引同步、見解同步、知識 lint、見解重處理、判決補查 |
| 維運 | 健康報告（07:30）、夜間 autopilot、最佳化報告、夜間回歸測試、人格清理、debug 截圖清理 |
| NAS / 檔案 | PDF 命名（夜間）、週末書籤、筆錄同步、每週法律爬取 |
| 市場 | 市場簡報（平日 08:30）、全球情報監控（每 6 小時）、對沖基金委員會 |
| 基礎設施 | oMLX 日夜切換、OSC 案件索引 / 掃描、Google 日曆同步、external chat 健康檢查 |
| **磁碟自律（2026-05-12）** | **`disk_low_water_alarm`**（每小時 :05 — High <30 GB / Critical <10 GB → 推 `self_repair`）、**`weekly_cache_cleanup`**（每週日 04:00 — 清退役 Ollama root 與可重建快取；保護 MAGI DB、NAS、正式模型、訓練成果、單機版 JSON/pickle/db 狀態檔與司法院 raw backlog） |

### 自我修復閉環與自主防線（2026-04-21 → 2026-04-25）

- **Phase 1 issue tracker** — 每筆 cron 失敗 / orchestrator 例外 / Tools API errorhandler 自動寫進 `.runtime/issue_agenda.jsonl`（PII 已 scrub、5 分鐘 dedup、5000 筆輪替）。截斷上限：stderr `[:4000]`、error_msg `[:5000]`、context `[:2000]`。需設 `MAGI_ISSUE_TRACKER_ENABLE=1`。
- **Layer 1 — `omlx_heartbeat_reaper.py`** — 以 `--model-dir` 指紋偵測並清除重複 `omlx serve` 進程。預設 `OMLX_HEARTBEAT_KILL_MODE=shadow`。
- **Layer 2 — `memory_watchdog.py`**（LaunchAgent `com.magi.memory-watchdog`）— 連續 90 s 偵測 swap >8 GB 或 free+inactive <2 GB 時殺最高 RSS 可回收 MAGI 子進程。預設 `MAGI_WATCHDOG_KILL_MODE=shadow`；另會回收逾 45 分鐘未關閉的 MAGI-owned Playwright driver/headless browser，避免 portal 自動化 teardown hang 長時間佔用記憶體。決策寫至 `~/.local/share/magi/runtime/metrics/memory_watchdog_decisions.jsonl`。
- **NAS load guard（2026-05-08）** — `com.magi.nas-mountpoints` 只清理未掛載的 `/Volumes/homes`、`/Volumes/lumi` 空/stale 目錄，不預先 `mkdir`，避免 macOS 掛成 `homes-1`/`lumi-1`；daemon 預設不遞迴監看 NAS 案件根目錄，需 `MAGI_ENABLE_NAS_FSWATCHER=1` 才啟用。
- **Portal retry guard（2026-05-08）** — 法扶 Gmail monitor 啟動時不再預設重試 pending portal downloads，避免每次重啟都批次碰 NAS/官網；需 `MAGI_LAF_PORTAL_RETRY_ON_START=1` 才啟用。背景閱卷 email/portal check 預設關閉，需 `MAGI_ENABLE_BACKGROUND_FILE_REVIEW_CHECK=1` 才啟用；`file_review_auto_worker` 也不再預設於啟動瞬間跑第一輪檢查。
- **Cron catch-up guard（2026-05-08）** — 重啟後的 startup catch-up 會跳過 NAS/案件索引/portal 類重型 jobs（如 OSC 掃描、Obsidian ingest、PDF benchmark、法扶夜審），避免 NAS 剛恢復時被補跑工作打滿。
- **Layer 3 — `omlx_switch_gatekeeper.py`** — oMLX 日夜切換前置 RSS 預檢 + TTL pause（上限 24 h），**直接 enforce**。
- **Layer 4 — `disk_cleanup_healthcheck.py`**（cron 03:45）— JSONL 輪替 + LRU cache 清理。預設 `MAGI_DISK_CLEANUP_DRY_RUN=1`。建置輸出若含單機版狀態檔（JSON / pickle / db / sqlite）會先跳過，避免誤刪 Paperclip / MAGI 可攜資料。

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
| `brief-gen` | AI 書狀草稿產生 |
| `court-hearing-reminder` | 開庭日提醒 |
| `hearing` | 庭期管理 |

### 檔案與 PDF
| 技能 | 功能 |
|------|------|
| `pdf-namer` | AI PDF 命名（Vision OCR + 多引擎共識） |
| `pdf-bookmarker` | PDF 目錄與書籤產生 |
| `doc-producer` | 文書產製流程 |
| `docx` | Word 檔案建立 / 編輯 |
| `pptx` | PowerPoint 產生 |
| `xlsx` | 試算表處理 |
| `documents` | 統一檔案讀取（MarkItDown adapter） |
| `screenshot-sorter-tw` | 截圖分類與歸檔 |

### 情報與研究
| 技能 | 功能 |
|------|------|
| `market-briefing` | 對沖基金委員會：技術 / 基本面 / 情緒分析師 + 風控 / 投資組合經理 |
| `worldmonitor-intel` | 全球新聞與法律情報監控 |
| `autoresearch` | 自主研究流程 |
| `insight-refine` | 見解蒸餾與精煉 |
| `crawler-targets` | 定時爬取目標 |
| `obsidian` | Obsidian 筆記庫同步、向量入庫與案件卡片索引（`30_Index/`） |

### 記憶與推理
| 技能 | 功能 |
|------|------|
| `memory` | 長期記憶：FAISS 向量庫 + Graph-RAG |
| `brain_manager` | 跨 session 記憶管理 |
| `reasoning` | 逐步推理鷹架 |
| `bridge` | Ensemble 推理橋接（Casper / Melchior / Balthasar） |
| `casper` | Casper LLM 直接介面 |
| `translator` | 翻譯（Google GTX 快速路徑 + LLM fallback） |

### 維運
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
    │       ↓  ensemble_chat_verified() — 直接三哲人文字產生
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

### SafeProcess — Shell 注入防護（`api/platforms/safe_process.py`）
`MAGI_USE_SAFE_PROCESS=1` 時，所有 cron 指令改走 SafeProcess（保留 `shell=True` 舊路徑供灰度切換）：
- **argv 白名單**：僅允許 `python3`、`launchctl`、`git`、`curl`、`mount_smbfs`、`osascript` 及 MAGI venv interpreter 作為 `argv[0]`。
- **Shell 禁字清單**：`;`、`|`、`&`、`` ` ``、`$`、`<`、`>`、換行符 — 即使在非 shell 模式也拒絕。
- **環境變數白名單**：僅允許 `MAGI_`、`JUDICIAL_`、`PATH`、`HOME`、`USER`、`PYTHONPATH`、`LANG`、`LC_*`、`TZ` 傳入子程序。
- **Timeout 流程**：SIGTERM → 3 秒寬限 → SIGKILL；stdout 上限 1 MB；`BoundedSemaphore(8)` 並發控制。
- **CI 閘門**：`scripts/ci/check_shell_true.py` 阻止新增 `shell=True` / `os.system(f"…")` / `os.popen()`，4 個 legacy 站點列為 grandfather 待 Phase 3 灰度完成後清除。

### RemoteHealthGate — 統一推理 Circuit Breaker（`api/platforms/remote_health_gate.py`）
為所有遠端推理 peer（Balthasar / Melchior / NIM）提供統一 circuit breaker，取代先前散落各模組的 ad-hoc try/except：
- 每個 peer 獨立 `PeerState`，使用 `threading.Lock`（禁止裸 acquire/release）。
- 漸進式冷卻：30 秒 → 5 分鐘 → 30 分鐘 → 2 小時。
- Probe 結果快取（`probe_cache_ttl_sec`），避免重複 HTTP 健康檢查。
- `get_gate()` 模組級 singleton，由 `_SINGLETON_LOCK` 保護。
- Feature flag：`MAGI_USE_REMOTE_HEALTH_GATE=1`。

### RuntimeDir — 集中式 `.runtime/` 路徑管理（`api/platforms/runtime_dir.py`）
確保所有暫態狀態統一落在 `.runtime/`，不再散落至 repo 根目錄或混入 `cron_jobs.json`：
- `atomic_write_json()` — 透過 `.tmp` + `os.replace()` 寫入（crash 不留半成品）。
- `atomic_append_jsonl()` — 執行緒安全 append + 自動輪替。
- `legacy_fallback()` — 雙讀：先試新路徑，失敗退回舊路徑，零停機遷移。
- `cron_state()` — 將 job 執行時間戳（`last_run`、`last_run_minute`）從 `cron_jobs.json` 分離，保持定義檔 commit 穩定。
- Feature flag：`MAGI_USE_RUNTIME_DIR=1`。

---

## 環境設定

主要環境變數（在 `.env` 中設定）：

| 變數 | 預設值 | 用途 |
|------|--------|------|
| `MAGI_ENSEMBLE_TOOLS` | `0` | 啟用 ReAct Agentic 工具呼叫 |
| `MAGI_ALLOW_CLOUD_MODELS` | `0` | 允許 Claude / GPT fallback |
| `MAGI_USE_SCRAPLING` | `0` | 使用 Scrapling 抓網頁（更快，無需瀏覽器） |
| `MAGI_USE_MARKITDOWN` | `0` | 使用 MarkItDown 解析檔案 |
| `MAGI_PDF_OCR_CONSENSUS` | `1` | PDF 命名多引擎 OCR 共識（僅 pdf-namer） |
| `MAGI_OCR_CACHE_ENABLE` | `1` | 新統一 OCR runtime 的 SHA-256 image-hash LRU 快取 |
| `MAGI_VISION_OCR_CONSENSUS_ENABLE` | `1` | `/vision` API opt-in consensus（僅 `task_type=ocr/text/scan`；captcha bypass） |
| `MAGI_SHORTCUT_OCR_CONSENSUS_ENABLE` | `1` | `/shortcut/ocr` consensus（回傳 mimetype 仍為 `text/plain`） |
| `MAGI_PDF_OCR_CONSENSUS_SHADOW` | `1` | pdf_bridge shadow 模式 — 跑新 consensus 記 metrics，仍回舊路文字 |
| `MAGI_PDF_OCR_CONSENSUS_ENABLE` | `0` | pdf_bridge 完全切換（shadow 週期觀察後再切 `1`） |
| `MAGI_LAF_OCR_CONSENSUS_SHADOW` | `0` | LAFVision shadow（僅觀察差異、不改決策；正式環境預設關閉） |
| `MAGI_LAF_OCR_CONSENSUS_ENABLE` | `1` | LAFVision guarded-write OCR 共識（高信心才自動採用；衝突/低信心不寫入） |
| `MAGI_OBSIDIAN_OCR_CONSENSUS_ENABLE` | `0` | Obsidian PDF OCR fallback consensus |
| `MAGI_NAS_HOST` | `MAGI_NAS_HOST` | NAS LAN IP |
| `MAGI_NAS_TAILSCALE_HOST` | `MAGI_NAS_TAILSCALE_HOST` | NAS Tailscale IP（自動 fallback） |
| `MAGI_AVOID_DISTRIBUTED` | `1` | 僅單機執行 |
| `MAGI_COMMITTEE_LIGHT_MODEL` | *(E4B)* | 分析師代理模型 |
| `MAGI_COMMITTEE_HEAVY_MODEL` | *(26B)* | 風控 / 投資組合經理模型 |
| `MAGI_FILE_REVIEW_ALLOW_CONFIRM` | `0` | 允許 CLI 觸發閱卷確認 |
| `MAGI_JUDICIAL_VERIFY_SSL` | `0` | 司法院網站 SSL 驗證（TLS 相容模式關閉） |
| `NVIDIA_NIM_ENABLE` | `0` | 啟用 NVIDIA NIM 雲端重型後備（Plan A） |
| `NVIDIA_NIM_API_KEY` | — | `nvapi-…` key（build.nvidia.com 免費層，40 req/min） |
| `NVIDIA_NIM_MODEL` | `meta/llama-3.1-405b-instruct` | 重型主力（128K context、多語、無審查） |
| `NVIDIA_NIM_MODEL_FAST` | `meta/llama-3.3-70b-instruct` | 一般 @heavy 請求的快速模型 |
| `NVIDIA_NIM_REQUIRE_OPTIN` | `1` | 強制使用者主動 `@heavy` / `@重型` 觸發 |
| `NVIDIA_NIM_REQUIRE_PII_SCRUB` | `1` | 送雲端前強制 PII 遮蔽（永不關閉） |
| `NVIDIA_NIM_DAILY_BUDGET` | `500` | 每日 NIM 請求上限 |
| `MAGI_USE_REMOTE_HEALTH_GATE` | `0` | 啟用統一 Circuit Breaker（Balthasar / Melchior / NIM，R1） |
| `MAGI_USE_SAFE_PROCESS` | `0` | Cron 指令改走 argv 白名單守門，取代 `shell=True`（R2） |
| `MAGI_USE_RUNTIME_DIR` | `0` | Cron 狀態與暫態 JSON 改寫至 `.runtime/`（R3） |

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
| **檔案解析** | MarkItDown · pdftotext · fitz · pdfplumber · Tesseract · macOS Vision |
| **OCR** | macOS Vision · RapidOCR · Tesseract · 統一 runtime `skills/engine/ocr/`（Vision + Tesseract 共識、SHA-256 image cache、法律文字修正、feature flag） |
| **API 框架** | Flask · Flask-Login · Flask-SocketIO |
| **排程** | `discord_bot.py` 內建 CronScheduler（cron_jobs.json） |
| **訊息頻道** | LINE Messaging API · Discord.py · python-telegram-bot |
| **NAS** | SMB LAN（MAGI_NAS_HOST）+ Tailscale fallback（MAGI_NAS_TAILSCALE_HOST） |
| **日曆** | Google Calendar API（OAuth2，自動 refresh） |
| **安全** | Iron Dome 規則引擎 · SafeProcess argv 白名單 · RemoteHealthGate CB · tw_output_guard · 信任標籤洩漏偵測器 |
| **測試** | pytest（~1,575 個測試） |

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
│   ├── platforms/              # RemoteHealthGate（CB）、SafeProcess（argv 守門）、RuntimeDir（路徑管理）
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
├── tests/                      # ~1,575 個 pytest 測試
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
| `8088` | Website Admin 管理頁 |
| `50052` | gRPC RPC Worker |

---

## 測試

```bash
# 完整測試（~140 個檔案・~1,575 個測試・約 12 分鐘）
./venv/bin/python -m pytest -q

# 依模組執行
pytest tests/test_routing_unified.py            # 統一路由（38 個測試）
pytest tests/test_tools_api_async_jobs.py       # 非同步任務佇列 API（18 個測試）
pytest tests/test_react_omlx.py                 # ReAct + ensemble tools
pytest tests/test_document_reader.py            # MarkItDown adapter（24 個測試）
pytest tests/test_translator_legal_termbase.py  # 三層法學術語庫（22 個測試）
pytest tests/test_translator_post_edit.py       # APE 後編輯流程（22 個測試）
pytest tests/test_hallucination_regression.py   # 幻覺防護（22 個測試）
pytest tests/test_laf_progress_helper.py        # 法扶進度回報（16 個測試）
pytest tests/test_memory_policy.py              # 記憶寫入政策（20 個測試）

# Live smoke（需要服務執行中）
magi status
curl http://127.0.0.1:5002/health
curl http://127.0.0.1:5003/health
MAGI_USE_SCRAPLING=1 skills/judicial-web-search/action.py --task self_test
skills/laf-orchestrator/action.py --task self_test
skills/file-review-orchestrator/action.py --task self_test
skills/transcript-downloader/action.py --task self_test
```

### 測試套件分類（~140 個檔案・~1,575 個測試）

| 類別 | 檔案數 | 測試數 | 主要覆蓋範圍 |
|------|--------|--------|------------|
| **路由與指令分派** | 13 | 190 | 統一路由、技能合約（市場簡報 / 庭審準備 / 合約審查）、指令分派、技能煙霧測試 |
| **Apple 平台整合** | 10 | 173 | Spotlight、Keychain、EventKit（行事曆）、CoreML 分類器、NaturalLanguage NLP、聯絡人、檔案監控 |
| **基礎設施** | 33 | 218 | 健康探針、會話/context 管理、音訊處理流程、文字處理、日誌、packaging、entrypoint、安全基線（CORS / 標頭 / Cookie） |
| **平台層（R1–R3）** | 7 | 72 | RemoteHealthGate circuit breaker（16）、Balthasar/Melchior/NIM opt-in（15）、SafeProcess argv/env/timeout（19）、RuntimeDir atomic I/O（14）、cron 狀態遷移（8） |
| **檔案與 PDF** | 7 | 86 | MarkItDown adapter、PDF bridge（OCR + timeout 恢復）、pdf-namer（命名驗證、動態信心度）、pdf-bookmarker（OLA 自適應閾值、Vision fallback） |
| **法扶（LAF）** | 11 | 81 | 進度回報 helper、submit-pending token 生命週期、結案 E2E mock、郵件分類、案件類別解析、重複去重 |
| **設定與 Runtime** | 21 | 80 | Runtime 路徑解析、模組化設定、模型設定、授權閘門、provider adapter、任務排程 |
| **工具 API** | 8 | 76 | 工具優先流程、非同步任務佇列（202/poll 模式）、推理閘道路由、Shortcuts 端點 |
| **翻譯** | 5 | 65 | 三層法學術語庫（MOJ SQLite / JSON / prompt）、Apple Translation + APE 後編輯驗證器、流程韌性、統一 API |
| **記憶系統** | 8 | 58 | 記憶寫入政策、接地驗證與 query 增強、Graph-RAG recall、假記憶回歸測試、助理發言升級保護、溯源追蹤 |
| **驗證與安全** | 6 | 49 | 幻覺回歸（22 情境）、答案驗證器、授權閘門、輸出守衛（trust-badge 洩漏）、安全基線 |
| **資料與持久化** | 6 | 45 | 任務佇列（SQLite）、embedding 路由器、遷移框架、DB helper、向量處理流程 NLP |
| **CI / 發布封裝** | 2 | 29 | Hardcode 檢查器、console-script 目標驗證 |

CI 閘門：
- `scripts/ci/check_hardcodes.py` — 提交的程式碼中有任何 IP / 憑證即失敗。
- `scripts/ci/check_shell_true.py` — 阻止新增 `shell=True` / `os.system(f"…")`（4 個已核准 legacy 站點列為 grandfather）。

---

## 授權

私有 / 專屬。保留所有權利。

原始碼僅供參考與內部使用。未經書面許可，不得重新發布或用於商業用途。
