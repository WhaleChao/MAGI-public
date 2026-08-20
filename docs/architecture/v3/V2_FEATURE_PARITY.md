# MAGI V2 → V3 功能相容與驗收矩陣

本文件是 V3 取代 V2 的 release gate，不是產品介紹。來源為程式、設定、實際安裝服務及測試；沒有使用 README 作為功能存在或品質正常的證據。

## 1. 自動覆蓋閘門

基準產生方式：

```bash
python3 scripts/architecture/generate_v2_inventory.py \
  --include-installed-launchagents \
  --output docs/architecture/v3/generated/v2_inventory.json

./venv/bin/python scripts/architecture/capture_v2_runtime_routes.py \
  --output docs/architecture/v3/generated/v2_runtime_routes.json
```

V3 CI 必須另外產生自己的 route、operation、schedule 與 service inventory，並逐項連到本文件的 capability id。以下變更未被映射時禁止 release：

- 新增、刪除或修改 V2 HTTP route／method。
- 新增、刪除或修改 active skill；`.versions` 內 rollback artifact 亦不可遺失。
- 新增、停用或修改 cron、timeout、resource guard、catch-up policy。
- 新增或變更 daemon child、LaunchAgent、port、資料 writer。
- 測試模組增加但沒有分配到 V3 contract 或 legacy parity suite。

### 1.1 2026-07-14 live 基線

| Port | 實際職責 | 驗證結果 |
|---:|---|---|
| 5002 | Main API／OSC／LINE／UI | `/livez`, `/readyz`, `/health`, `/saas-readyz` 為 200 |
| 5003 | Tools API／external gateway | `/livez`, `/health` 為 200；external health 無 key 為 401 |
| 5014 | Paperclip share gateway | listening |
| 50052 | RPC | listening |
| 8080 | 主 oMLX | E4B 已載入；RSS 約 1.75 GiB、Metal-aware footprint 約 5.84 GiB |
| 8081 | embedding | healthy、當時未載入模型 |
| 8082/8083 | Phi-4／Smol reviewer sidecar | HTTP shell healthy、當時未載入模型 |
| 8088 | Website Admin | `/health` 正常 |
| 8090 | MTP draft | 關閉，符合選配設定 |

主服務與 Tools 核心契約組以專案 venv 實跑 `156 passed`；程序 ownership/watchdog 相關組另跑 `108 passed`。系統 Python 因缺少專案依賴會在 collection 失敗，所以 V3 test runner 必須固定 release venv，不可使用 `/usr/bin/python` 或不明確的 PATH。

## 2. 功能矩陣

| Capability id | V2 程式證據 | 必須保留的行為／副作用 | V3 邊界 | Promotion 前驗收 |
|---|---|---|---|---|
| `channels` | `api/server.py`, `api/webhooks/line.py`, `api/discord_bot.py`, `api/webhooks/telegram.py`, message/chat pipelines | LINE callback、Discord、Telegram、附件、回覆分段、權限、通道 routing、待確認流程、錯誤不洩漏內部資訊 | gateway + light channel adapter；重工作業只建立 job | 以錄製 webhook 驗簽；相同輸入的文字、附件、按鈕、channel/topic 與副作用一致；重送不重複發訊息 |
| `osc_case_management` | `api/blueprints/osc_cases.py` 115 routes、`osc_files.py`, `api/osc/*` | 案件 CRUD、搜尋、待辦、checklist、檔案上傳／移動／開啟、草稿、匯入匯出、結案歸檔、權限與 case path mapping | case repository + integration/file worker | V2/V3 對同一匿名化案件做 golden diff；DB rows、todo 狀態、檔案 hash／路徑一致；非法路徑與未授權存取皆拒絕 |
| `osc_accounting_debt_calendar` | `osc_accounting.py`, `osc_debt.py`, `osc_gcal.py`, `api/osc/accounting_*`, calendar agent | 收支／報表、債務補件、試算、Google Calendar 雙向狀態、重複事件治理、token refresh | integration worker；Google action 經 outbox | 金額與日期 golden cases 逐欄相同；timezone 固定 Asia/Taipei；token 過期為 deferred；重試不重複事件或帳目 |
| `laf_legal_aid` | `laf_orchestrator.py`, `laf_flow.py`, `laf-orchestrator/action.py`, `laf-portal-automation/action.py` 及大量 LAF tests | Gmail 分派、案件分類／建立／去重、附件與資料夾、結案、進度／條件／撤回／費用草稿、portal retry；維持「草稿不送出」安全邊界 | browser worker + LAF domain adapter；全域 portal lease=1 | sandbox 帳號跑正常／空結果／登入失敗／captcha／逾時；所有正式動作仍只暫存；重啟能續跑；不得建立重複案件或寄送未核准內容 |
| `court_file_review` | `file-review-orchestrator/action.py`, `file_review_auto_worker.py`, `judicial_automation_v2.py`, portal lock tests | 平日每小時檢查、可下載判斷、smart-skip、下載、歸檔、通知、跨程序鎖、staging cleanup、失敗重試 | browser worker；每輪獨立 browser/process；checkpoint | 真實唯讀 probe + 測試帳號下載；0 新檔也須 success；重疊觸發只能一個 writer；ElementHandle/Page 無累積；worker 退出後 RSS 回基線 |
| `judgments_legal_research` | `judicial-web-search/action.py`, `judicial_api_*`, `judgment_flow.py`, `statutes-vdb/action.py`, `research-brief/action.py` | 裁判書查詢／抓取／快取／backlog、法規向量、引證、研究摘要、來源 provenance、低證據 abstain | integration/research worker；network cache 有 TTL | 固定查詢結果與引用可重現；來源失效要明示；台灣用語／幻覺 guard 通過；API 限流與 backlog 可續跑 |
| `documents_ocr_pdf` | `skills/pdf*`, `skills/engine/ocr/*`, document handlers、docx editor tests | PDF 文字／OCR、命名、書籤、註解、todo、Office 產生與編輯、檔案鎖、品質閘門、fallback | document worker；provider plugin；原檔不可原地破壞 | 掃描／文字／混合／旋轉／大檔 golden corpus；文字、頁數、書籤、檔名、渲染圖 diff；失敗保留原檔；程序退出釋放 framework 記憶體 |
| `audio_transcription_translation` | `multimedia_flow.py`, `balthasar_bridge.py`, `tri_sage_collab.py`, `forensic-transcript-verifier`, audio/transcribe/translate tests | 音檔接收、轉檔、逐字稿、時間戳、segments、語者標記、標點修正、翻譯、摘要、通知；MAGI 本機自主觀看五連格影片畫面、原音／濾波雙路 ASR、兩次獨立發話者與文字複核、人工節文逐字鎖定、同一發話者語意整併及法院 DOCX 產出 | transcription worker；MLX Whisper + Gemma vision；Ownscribe backend 僅離線／隔離評估；法院勘驗由共用 SKILL 與 V3 worker adapter 執行，Codex/OpenAI route fail closed | 10–20 份真實中文法律錄音人工金標；CER/WER、法律詞錯誤、timestamp drift、speaker DER、兩輪視覺一致率、節文 exact match、跨語者零重疊、ASR 零遺漏與 DOCX render 過門檻；現有回傳 shape 不變 |
| `memory_knowledge_search` | `skills/memory/*`, `memory_flow.py`, `knowledge_graph`, transcript indexer、FAISS/vector tests | 三層記憶、來源與信心、禁止把助理幻覺寫成事實、對話歷史、keeper sync、知識圖譜、逐字稿／insight 索引 | memory repository + integration/index worker | 既有查詢 golden set；provenance 完整；重複 ingest 冪等；embedding 失敗不寫零向量；索引 rebuild 不阻塞互動入口 |
| `agents_models_routing` | `orchestrator.py`, routing modules, brain manager, model registry, tri-agent tests | NERV／三哲人協作、意圖／模型 routing、日夜模型切換、circuit breaker、quality verification、embedding | control plane 只做決策；model adapter 呼叫獨立 oMLX | route decision snapshot；模型不存在不得猜測；health 不載入模型；重模型同時數受 lease 限制；日夜切換與 rollback 實測 |
| `skills_lifecycle` | `api/tools_api.py` 67 routes、skill registry/runtime/sandbox tests、`skills/.versions` | skills 列舉、執行、建立／教學／匯入、版本、stable、canary、CI、release、rollback、安全沙箱 | skill registry + worker launcher | 51 active skill 均可 discovery/dispatch；15 version artifact 可 rollback；未知 task 明確 failed；權限、路徑、fork bomb 防護通過 |
| `scheduling_automation` | `cron_jobs.json`, `cron_scheduler.py`, Discord scheduler owner、cron policy tests | 102 定義／92 啟用、cron timezone、timeout、no-catchup、long job、resource guard、owner lock、last run、狀態顯示 | control scheduler + durable ledger；worker 不在 scheduler process 執行 | 模擬時鐘重播一週觸發；每 job 次數相同；deferred/skipped/failed 分開；daemon 重啟不重複；「閱卷完整檢查」等關鍵排程跑真實 smoke |
| `drive_nas_obsidian_sync` | `drive_case_sync.py`, sync workers, `skills/obsidian/action.py`, NAS/Drive tests | 雙向同步、資料夾映射、雜湊去重、large-file deferred、衝突／隔離、Obsidian ingest/repair/vector index | integration worker；每批 checkpoint；單一 case/file lease | sandbox Drive + 測試 NAS corpus；上下載 hash 相同；中斷後續跑；未掛載不誤刪；large file 行為與狀態一致；重跑零重複 |
| `admin_dashboard_menubar` | `dashboard_pages.py` 33 routes、`admin_runtime.py` 29 routes、`web_runtime.py`, `gui/magi_menubar.py` | 登入、dashboard、OSC 頁面、runtime 控制、排程 92/失敗數、agent 黃燈、選單列、mobile entry | gateway web facade + control-plane query model | Playwright 截圖／DOM contract；登入與 CSRF；狀態數字與 ledger 相符；單 job 歷史失敗不把全系統永久顯示黃燈 |
| `operations_health_security` | `daemon.py`, request/auth/csrf guards, memory/input watchdog, health/readiness tests | 啟停／自癒、process ownership、health/readiness、記憶體／Metal、輸入法保護、日誌輪替、備份、PII／權限／稽核 | gateway/control/supervisor + 最小 native watchdog | 斷模型、斷 DB、斷 NAS、卡 browser、壓記憶體、IME 消失 fault injection；互動入口可用；不殺非 MAGI 程序；RTO 與告警分類符合契約 |
| `public_share_external_api` | `tools_api.py`, share gateway/tunnel, RPC LaunchAgent, Tailscale tests | external chat/search/research/fetch/vision、分享檔案、tunnel、RPC、backpressure、安全 allowlist | gateway + external adapters | loopback/public boundary、token、rate limit、proxy timeout、敏感 static 阻擋、tunnel 重連與 backpressure 壓測 |

### 2.1 Compatibility Edge 必須原樣保留的特殊契約

- 既有認證同時存在 session、`X-API-Key`、`X-MAGI-API-KEY`；public health 只能回 minimal payload，不能因 V3 統一認證而破壞現有 caller。
- V2 response boolean 並不一致：有 `ok`、`success` 或只有 `reply`；V3 內部使用 canonical API envelope，但 compatibility edge 必須保留舊 path、method、auth、HTTP status、body、SSE 與 plaintext。
- External chat 支援同步、async、SSE、`@heavy` 與 inflight backpressure；timeout/degraded 目前可能以 HTTP 200 回 `{success:false,degraded:true,...}`，不能擅自改成 5xx。
- Shortcut OCR/PDF/summarize/transcribe 回 `text/plain`，錯誤以 `[error]` 開頭；預設上限分別為 OCR 20 MB、PDF 50 MB、audio 100 MB、text 500 KB，暫存檔必須清除。
- Web chat upload 預設允許 200 MB，文字截斷上限 120k chars；V3 要改用 artifact handle，但 compatibility adapter 仍接受既有 multipart。
- OCR 必須維持 raw/corrected 分離、confidence、writable、critical conflict 與 CAPTCHA 不進法律校正／consensus 的安全紅線。
- PDF action 必須輸出新檔而不覆寫來源，保留 info/extract/rotate/pages/split/merge/watermark/optimize/AES-256 encrypt 與 calendar dry-run。
- 轉錄保留 local-first fallback、language/taigi hint、segments、timestamp、speaker metadata、術語後處理及 transcript quality gate；`/api/transcribe`、`/collab/transcribe`、Shortcut 的錯誤 status/body 不同，需各自做 contract test。
- 台灣用語、gibberish、prompt/ReAct/tool leak、拒答模板、來源詞遺失與 platform length guard 是共同 middleware，不能因 worker 化而漏掉。
- Health/readiness 必須便宜、cache/single-flight、不得載 full FAISS 或模型；liveness 與 readiness 分離，單項業務失敗不等於 control plane 不健康。
- 現有 nested thread pools 可能同時疊加 io/inference/channel/cron、Tools inference、external chat、tool、OCR/PDF page workers。V3 resource governor 要在全系統發 CPU/RAM/Metal/IO token；僅在 Future 上 timeout 不會停止已執行工作，因此重工作業一定要用可終止的 process group 並傳遞 deadline。

## 3. 資料 writer 登記

全域 primary 切換前，下列資源每一刻只能有一個 writer。V3 實作要把實際 table/file 清單擴充成 machine-readable ownership manifest。

| 資源 | 切換前 canonical | 離線／隔離驗證規則 | V3 接管後規則 |
|---|---|---|---|
| OSC／LAF MySQL 資料 | 現有 V2 schema | 複製資料、只讀或 transaction rollback | V3 repository 成唯一 writer；V2 程序已停止 |
| `magi.db`, `law_firm.db` | 現有檔案 | 複本或只讀 | 先 migration + integrity check，再原子換 owner |
| `cron_jobs.json` 與 cron state | V2 scheduler | V3 模擬時鐘，不寫 last_run | cutover 時匯入 cursor，V2 scheduler 停止發 lease |
| `.agent/jobs`, `.agent/mq` | V2 | V3 使用獨立 ledger | 未完成工作轉成 V3 envelope，逐筆對帳 |
| `.runtime` 各 state/lock/report | 各現有 owner | V3 寫 `runtime/MAGI_v3` | 逐檔指定 owner；禁止兩邊共用 lock namespace |
| NAS 案件與閱卷檔案 | V2 | dry-run 或隔離測試根 | V3 staging + hash verify + atomic publish |
| Google Drive／Calendar／Gmail | V2 | mock/sandbox/唯讀 | V3 outbox + idempotency；V2 憑證可讀但不得發 action |
| 法院／LAF portal 草稿 | V2 | 測試帳號或 dry-run | V3 獨占 browser lease；任何提交仍需既有人工確認規則 |
| 訊息通道 outbound | V2 | 隔離驗證不向 production 發送 | V3 outbox 唯一發送；provider message id 去重 |
| oMLX model profile | 現有 launchd/watchdog | V3 只觀察 | V3 model adapter 發切換意圖；單一 model owner 執行 |

## 4. 現有 LaunchAgent 職責映射

本機 26 個已安裝 `com.magi.*.plist` 不能靠「少裝幾個服務」直接消失；每個職責都要被吸收、保留為外部基礎設施，或經驗收後明確退役。

- 由 V3 gateway/control/supervisor 取代：`com.magi.daemon`；切換後不再由一個 daemon 直接養全部子程序。
- 保留為基礎設施並由 model/infrastructure adapter 管理：`db-proxy`, `rpc`, `smb-reconnect`, `omlx`, `omlx-embed`, `omlx-phi4`, `omlx-smol`, `omlx-watchdog`, `omlx-restore`, 選配 `mlx-mtp`。
- 改由 V3 durable scheduler 發 job：`insight-sync`, `laf-nightly-audit`, `nightly-health-report`, `obsidian-ingest`, `pdf-namer-nightly`, `purge-persona-memories`, `reprocess-insights`, `weekend-resummary`, `log-rotate`。
- 保留為最小 OS 級防護但不得載入 MAGI 重框架：`input-method-watchdog`, `memory-watchdog`。
- 保留外部入口職責：`paperclip-share-gateway`, `paperclip-share-tunnel`；V3 gateway 通過驗收後可合併，但 URL/權限不變。
- `menubar` 改讀 V3 query model；顯示目前唯一 active release、維護切換或冷回復狀態，不顯示雙版本 route。
- `worldmonitor` 維持明確選配／停用狀態；停用不是刪除功能。

## 5. 夜間全域切換的 Go/No-Go 清單

只有下列項目全部為 Go 才能在 02:00–04:00 切換：

- Inventory：runtime 342 route（主服務 275 + Tools API 67；靜態快照 335）、51 active skill、15 rollback artifacts、102 cron、26 個現行 LaunchAgent 職責全部映射，無未核准缺口。
- Tests：V2 regression、V3 unit/contract/integration/e2e、黃金資料、fault injection 全數通過，沒有 quarantine P0/P1。
- Verification：七日離線 replay/soak + 至少三次一次性隔離 LIVE 驗證無未解副作用差異；同機 LIVE 驗證先完整停止 V2，結束後先完整停止 V3 才恢復 V2。新發現差異重新計時，驗證程序結束後不得留有 V3 daemon。
- Data：backup restore drill 成功；writer ownership 完整；job/outbox/cron cursor 可對帳。
- Resource：日常與尖峰壓測期間輸入法選字窗正常；無 worker 退出後 RSS/Metal 持續成長；系統 memory pressure 不進 critical。
- Operations：完整停 V2、原子切換 release、啟動 V3，以及完整停 V3 後冷回復 V2，均已在 staging 實際演練且 RTO 在兩分鐘內。
- Single-active：任一時刻 active MAGI release 只能有一個；V2 未完全停止時 V3 production 必須拒絕啟動，V3 未完全停止時 V2 rollback 也必須拒絕啟動。全主機同時只能有一個 scheduler、webhook/Discord consumer、file watcher、portal writer、model switch 與 oMLX watchdog owner。
- Deployment：26 個 live plist 的 rendered manifest/checksum 已封存；source/runtime drift report 無未核准差異。
- Human：切換報告、風險、當班責任人與 rollback 指令已核准。

切換後 V2 立即保持完全停止，只在磁碟保留最後可復原 release、相容狀態快照、DB migration 與檔案 ownership manifest。七日穩定且最終資料對帳為零差異後再封存；需要回復時必須先完整停止 V3，不能雙版並行。
