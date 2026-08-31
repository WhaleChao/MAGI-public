# MAGI V3 — 本機優先的 AI 作業平台

MAGI V3 不是「聊天伺服器加上一堆腳本」，而是一套單一有效版本、本機優先、
可驗證且可回滾的作業平台。它統合互動式網頁、排程、隔離工作程序、律師業務、
文件與影音處理、儲存同步、本機模型、通知及自動復原。

本公開倉庫是經過隱私過濾的 MAGI V3 工程快照。已退役的 V2 歷史只在
私有工程倉庫保留供稽核與追溯，不是現行 runtime、相容目標或升級驗證矩陣。
V3 採「不可變 release＋三個正式角色＋明確 owner＋有界 worker＋持久化狀態＋
雜湊綁定升級」。

> Git 倉庫是工程來源，不是 LIVE runtime。密碼、Cookie、token、案件資料、
> 資料庫、瀏覽器 profile、mutable queue 與正式收據都不得提交。

English: [README.md](README.md)

## 現行驗證版本

- **公開產品版號：** MAGI RC643／R75（2026-08-31 驗證）。
- **維修文件版次：** RC643／R75；本分支提供 HTML、PDF、Markdown 與
  machine-readable 原始碼索引。
- **線上維修百科：** 登入後開啟 `/manual`；內容由不可變 active release 提供，
  回應固定為 `private, no-store`。
- **回滾底線：** 不可變 r59；後續 R75 熱修 package 不改變公開產品版號。
- **有界 LIVE 觀察：** 業務模組、商用就緒、MAGI Doctor 與 active
  operational failure 聚合均通過；公開倉庫不包含正式收據或主機路徑。

部署主機的 active marker 與 installed manifest 才是現行精確 package 的唯一權威。
歷史 RC627–RC641 檔案僅是封存證據，線上維修路由不會選用它們。

## 目錄

- [先理解四層邊界](#先理解四層邊界)
- [現行執行架構](#現行執行架構)
- [原始碼目錄](#原始碼目錄)
- [資料狀態與安全邊界](#資料狀態與安全邊界)
- [排程重試與復原](#排程重試與復原)
- [業務與 AI 功能](#業務與-ai-功能)
- [健康燈號如何判定](#健康燈號如何判定)
- [開發與測試](#開發與測試)
- [發布切換與回滾](#發布切換與回滾)
- [故障排查順序](#故障排查順序)
- [文件入口](#文件入口)
- [公私版邊界](#公私版邊界)

## 先理解四層邊界

V3 把 V2 容易混在一起的事物拆成四層：

1. **Source**：Git 內經審查的原始碼與測試。
2. **Release**：完整、不可變、逐檔雜湊的可執行程式包。
3. **Deployment**：精確綁定某個 release 的 launchd、環境、owner 與外部輸入。
4. **Runtime state**：跨版本保留的 ledger、checkpoint、queue、receipt、cache、lock、log。

判定「現在跑哪一版」的權威是 active marker 與 installed manifest，不是 Git branch、
Desktop 工作樹、舊的 command SHA、PID 檔名或歷史測試報告。

完整發布鏈如下：

```text
來源 commit
  → 聚焦測試與隱私稽核
  → sealed release manifest
  → exact/full 正式驗證（同一組 immutable inputs 僅一次）
  → 備份與兩種還原演練
  → 安裝但不啟用
  → render deployment 與 ownership
  → prepare transaction
  → single-active 切換
  → 同步核心 Web／security 收據
  → 獨立模組與背景健康收據
```

任一階段失敗就停止。失敗候選版的證據不能拿給下一版沿用。

## 現行執行架構

```text
瀏覽器／選單列／LINE／Discord／Telegram
                     │
                     ▼
       ┌──────────────────────────┐
       │ Gateway（互動優先）      │
       │ 5002 OSC／主網頁         │
       │ 5003 Tools／API          │
       └──────────────────────────┘
                     │
                     ▼
       ┌──────────────────────────┐
       │ Control（背景）          │
       │ 8088 health／admin       │
       │ release identity／ledger │
       └──────────────────────────┘
                     │
                     ▼
       ┌──────────────────────────┐
       │ Supervisor（背景）       │
       │ scheduler／worker／retry │
       │ resource lease／recovery │
       └──────────────────────────┘
              │          │
              ▼          ▼
       Browser／Document／Integration／Model workers
```

| 角色 | 主要責任 | launchd 類型 |
|---|---|---|
| `gateway` | 使用者 HTTP、登入、相容路由、工具 API | Interactive |
| `control` | 健康、管理、版本身分、輕量控制平面 | Background |
| `supervisor` | 排程與非 HTTP child 的生命週期、重啟與資源管理 | Background |

服務 manifest 精確定義角色、埠、factory、child entrypoint、deployment mode 與禁止
再啟動的 legacy 程序。launchd 同時綁定 manifest 路徑及 SHA-256；檔案被替換、路徑
越界或 mode 不符，entrypoint 會拒絕啟動。

OCR、PDF、Playwright、逐字稿、Drive/NAS、模型與維護工作不得塞進 Gateway。
它們在 supervisor 管理的隔離 child 中執行，受 timeout、資源上限、取消、process
group 回收及 durable completion receipt 約束。

## 原始碼目錄

| 路徑 | 責任 |
|---|---|
| `magi_v3/` | Gateway、Control、Supervisor、ledger、排程、health、owner、recovery |
| `api/` | OSC/web 組合、領域 adapter、相容 endpoint、認證與 API |
| `api/blueprints/` | Flask 頁面與 API 邊界；含需登入的維修百科路由 |
| `api/osc/` | 案件、行事曆、Drive、檔案及 OSC 業務流程 |
| `skills/` | 明確工具入口與各領域工作流；重依賴不進 control plane |
| `scripts/v3_validation/` | 正式認證、route replay、fault/performance 與 evidence schema |
| `scripts/v3_cutover/` | single-active preflight、activation、rollback、owner/process probe |
| `scripts/v3_release_bundle.py` | 從 clean source 建立 allowlist、逐檔雜湊的封存 release |
| `scripts/v3_deploy_prepare.py` | 離線 render deployment，不會啟動服務 |
| `scripts/v3_backup_prepare.py` | 產生切換前備份 |
| `scripts/v3_backup_verify.py` | 驗證備份可實際還原，而不只檢查檔案存在 |
| `scripts/ops/` | 健康快照、稽核、清理與有界修復入口 |
| `config/` | 版本化 policy、service、cron、validation、feature 定義 |
| `templates/`、`static/` | MAGI 網頁 UI 與本機靜態資產 |
| `gui/` | macOS 選單列狀態 |
| `mobile_app/` | 行動入口與相容頁面 |
| `tests/` | unit、contract、security、replay、release、cutover、regression |
| `docs/` | 架構、技術手冊與維修百科全書 |
| `magi_v3/manual_assets/` | 隨 release 封裝的 authenticated／no-store 維修手冊 |

機器可讀原始碼索引提供文件條目到檔案、行號、symbol、SHA-256 的對照。若文字敘述
與程式看起來不一致，以 active release 的 manifest、實際檔案與測試為準。

## 資料狀態與安全邊界

### 不可變 release

installed release 只能包含 manifest 宣告的 regular files。每檔都有 path、size、mode、
SHA-256；symlink、special file、未知新增、封存後異動或 source drift 都會被拒絕。
release 目錄不可拿來寫 queue、cache、log、credential 或業務資料。

主機層 singleton 服務的已安裝啟動設定不得綁死 `releases/v3-*` 版本路徑。
穩定啟動器會讀取 active marker，先核對 release manifest 與指定腳本 SHA-256，
再啟動 memory watchdog、選配 MTP 或 Paperclip 服務；因此退役舊 release 時不會
留下只有重開機或服務重啟後才爆發的隱藏依賴。

### 可變 runtime

runtime 位於 release 之外，並按 owner 拆分：

- control ledger 與 release transaction；
- immutable cron definition 與 mutable occurrence/retry 結果；
- Drive checkpoint、cursor 與 content-hash cache；
- browser/domain lock 與 owner metadata；
- notification outbox 與 business receipt；
- log、有界 cache 與 export。

lock 與 owner metadata 是證據，不是垃圾。不能因紅燈就直接刪除；必須先核對 PID、
process group、executable、argv、release root 與 schema，證明 owner 不存在後，才可由
官方 reconciliation/cleanup 流程處理。

### 外部副作用與個資

NAS、Drive、法扶／法院、Calendar、Email 與訊息平台都在本機 transaction 之外。
因此寫入須採 prepare／commit／read-back、idempotency 或 durable outbox。只收到要求、
只排入背景、只得到 HTTP 200，都不能直接算業務完成。

密鑰由 Keychain 或 deployment-bound env 注入。真實案號、當事人、路徑、token、
Cookie、原始信件、runtime DB 與 browser profile 不得進 Git 或公開 evidence。
公開報告只留安全計數、固定 reason code、不可逆 digest 及 artifact hash。

## 排程重試與復原

排程器把 immutable job definition 與 mutable state 分開。每個 occurrence 都記住建立時
的 command-definition hash；重啟時會 reconcile 未完成工作、supersede 舊 command，
再以目前 sealed definition 重建，不會執行佇列裡殘留的舊版程式內容。

結果語意不可混用：

- `succeeded`：業務副作用與必要 receipt 已完成；
- `deferred`：外部資源忙碌／不可用，安全等待重試；
- `waiting_children`：child 工作尚未全部回收；
- `skipped`：證據已驗證，確實不必做；
- `needs_confirmation`／`awaiting_input`：需要人工證據；
- `failed`／`timed_out`／`cancelled`：帶明確 cleanup 的終局。

重試有上限且 reason-coded。timeout、storage unavailable、portal busy、identity guard、
process interrupted 與 human required 不可互相替代。健康頁只看已 reconcile 的目前結果，
不應因很久以前的一次 failure 永久紅燈。

Drive all-files 採單案／小批次 checkpoint。每輪保存 cursor、progress time、hash cache、
匿名 staging bytes 與風險計數。語意上同名的路徑，必須靠來源 identity 或有界雜湊
驗證；無法證明時維持 deferred，不能猜測後覆蓋或刪檔。

## 業務與 AI 功能

MAGI 把使用者功能拆成可各自健康、等待、降級或需處理的領域：

- OSC 案件、檔案、待辦、行事曆、帳務與案件生命週期；
- 法扶信件、案件建立、入口補抓、附件、進度草稿與結案；
- 閱卷偵測、下載、簽章對帳、歸檔與重試；
- 錄音／影片下載、逐字稿、語者、索引、摘要與翻譯；
- DOCX／PDF／OCR、命名、書籤、註解、文件產製與審閱；
- Drive／NAS 雙向同步、staging、hash 驗證與 storage recovery；
- 裁判書、法規、司法搜尋、證據分析與庭前準備；
- 本機模型 routing、NERV／agent 推理、embedding、記憶與知識庫；
- notification outbox 與多通道訊息；
- Cookie Cutter 圖片轉可列印 mesh，含資源限制、不持久化及不外送驗證；
- 依「創作與製造／學習與活動／法律實務／系統維護」分類的公開工具首頁；
- 由固定版本 `video-autopilot-kit` 程式化路徑衍生的本機影片工作室：輸出
  9:16 H.264／AAC 樣片，不需要 CapCut，不抓遠端素材、不自動發布，也不保存輸入。

`/tools`、`/video-studio`、`/cookie-cutter`、`/lottery` 與 `/exam-tutor`
均免 MAGI 帳號。免登入不代表無限制：寫入端點仍有 CSRF、精確輸入 schema、
持久化節流、單一並行、逾時、程序回收、輸出驗證與 no-store。案件、法律業務、
健康及維護功能仍維持登入保護。

某個 portal 暫時 deferred 不代表 Gateway 死亡；某個歷史工作失敗也不代表整個業務
模組故障。Menu Bar 與 business snapshot 只把 allowlist reason code 轉成安全文案。

## 健康燈號如何判定

MAGI 不用「程序還活著」冒充全系統正常，而是分層：

1. **Liveness**：輕量程序與 event loop 可回應。
2. **Readiness**：release identity、依賴、role owner、child 正確。
3. **Business health**：各領域有新鮮、已對帳的業務 receipt。
4. **Function／Doctor／guardian／Funnel**：功能契約、守護、路由、使用者流程全通。
5. **Post-cutover evidence**：該 release 與 transaction 的 LIVE 驗收已完成。

需登入的維修百科位於 `/manual`；PDF、Markdown 與 machine-readable source index
均有固定 allowlist route。它們從 active immutable release 提供、禁止 symlink/path
traversal，並使用 `Cache-Control: no-store`。

## 開發與測試

依 repository pin 的 runtime/dependency 執行。不要從任意 source worktree 直接啟動
production entrypoint；V3 會拒絕 source/runtime/manifest 混用。

常用的窄測試與稽核：

```bash
python3 -m pytest -q tests/test_dashboard_pages_blueprint.py
python3 -m pytest -q tests/test_web_information_architecture.py
python3 -m py_compile api/blueprints/dashboard_pages.py
python3 scripts/public_release_audit.py --public-isolation --strict
git diff --check
```

正式 promotion 還要驗 exact test selection、source hash、route replay、fault/recovery、
privacy isolation、clean worktree 與 host-outer certification。Codex sandbox 內無法再建立
第二層 macOS Seatbelt 時，應在外層執行 hash-bound runner；不能為求通過就關掉正式
inner Seatbelt。

驗證分成三個不得混用的執行層：

1. **同步 promotion／cutover**：驗 immutable identity、完整 formal suite、
   backup/restore、ownership、rollback、核心本機 route 與 security。同一 commit、
   manifest、suite manifest、runtime 與 test-source hashes 的 formal full 只跑一次。
2. **變更模組驗收**：只驗本次受影響的領域，例如 Cookie Cutter 幾何、
   判決來源、閱卷對帳、手冊排版或 Drive checkpoint；不得再跑同一 sealed
   receipt 已覆蓋的 formal nodes。
3. **獨立背景健康**：business、function、Doctor、guardian、Funnel、Drive、
   portal/MCP 與 benchmark 各自產生 receipt。可恢復的 `waiting` / `deferred` 要在該模組
   繼續顯示，但不得回頭把其他已正常模組或整版判失敗。

只有上述 immutable bindings 每一項都 byte-identical 時才可沿用 receipt。外部網路、
lock 時點、每日 sample 與 cron occurrence 屬於可變 operational evidence，不是 source
certification cache key。詳見
[驗證関門政策](docs/architecture/v3/VALIDATION_GATE_POLICY.md)。

測試不得讀寫 canonical runtime。HOME、runtime、agent、browser、queue、upload、cache
都必須隔離；network 與 launchctl 只允許經審核、限定版本的 LIVE wrapper 操作。

## 發布切換與回滾

V3 採 single-active 冷切換：

1. 驗證 clean sealed release；
2. 建 fresh backup 並做兩種獨立 restore drill；
3. 安裝候選版但不啟用；
4. render launchd 並把每項外部輸入綁定 path＋hash；
5. prepare transaction 與 rollback artifacts；
6. 在 rollback envelope 內 quiesce 舊 supervisor；
7. 驗證 old owner 為零且 durable handoff 沒遺失；
8. 原子切換並啟動 Gateway／Control／Supervisor；
9. 執行同步核心 Web／security gate 與變更模組驗收；
10. 分別發布 business、function、Doctor、guardian、Funnel、Drive、portal/MCP
    與品質 benchmark 的背景健康 receipt；
11. 只有 transaction commit 前的同步 hard gate 失敗才自動停止新版並還原舊版。

禁止同時跑兩個完整 production release；禁止改 installed release 內單檔；禁止沿用其他
transaction 的 receipt；禁止手改 cron state 或刪 checkpoint 來製造綠燈。

## 故障排查順序

1. 先讀 active marker 與 transaction。
2. 驗 installed manifest 與 deployment ownership manifest。
3. 查 role/worker owner metadata，核對 PID、PGID、executable、argv、release root。
4. 讀最新 terminal/business receipt 與安全 checkpoint 計數。
5. 分清楚是本輪 failure、舊 failure、deferred 或等待外部資源。
6. 用最窄的 source test 或 read-only probe 重現。
7. 修 source 並加入會在舊程式失敗的 adversarial regression。
8. 建新 immutable release；不要 hot-patch LIVE。

常見錯誤作法：

- 未證明 owner 消失就刪 lock；
- 把 exit code 0 當成業務完成；
- 用 path、檔名或舊 command SHA 猜 active release；
- 精確 integer counter 卻接受 `bool` 或字串 coercion；
- broad exception 吞掉 deadline；
- child 還在跑就把 parent 排程寫成 success；
- 把 mutable runtime、cache、證據垃圾打包進 release／Git；
- 需要 byte-exact input 時拿視覺相似、重新編碼的檔案替代。

程序健康的唯一正式定義位於 `magi_v3/process_monitor.py`。Golem 與 macOS
MENUBAR 共用同一份核心／worker／孤兒／殭屍／重複摘要；shell `-c` 啟動器不算
worker，孤兒必須沿真 worker ancestry 找 canonical MAGI owner，殭屍則需持續五秒。

完整 symptom→source→test→repair 對照請看維修百科。

## 文件入口

- [MAGI V3 維修百科全書 HTML](docs/MAGI_V3_維修百科全書_rc643.html)
- [MAGI V3 維修百科全書 PDF](docs/MAGI_V3_維修百科全書_rc643.pdf)
- [可維護 Markdown 原稿](docs/MAGI_V3_維修百科全書_rc643.md)
- [逐檔／行號／symbol／SHA 原始碼索引](docs/MAGI_V3_原始碼索引_rc643.json)
- [自動產生的實作狀態](docs/architecture/v3/V3_IMPLEMENTATION_STATUS.md)
- [V3 架構參考](docs/architecture/v3/MAGI_V3_ARCHITECTURE.md)
- [V3 Agent Gateway（核准 client 的 MCP 介面）](docs/architecture/v3/MAGI_AGENT_GATEWAY.md)
- [通用自架部署](docs/SELFHOST_DEPLOYMENT.md)

HTML 百科也封裝在 `magi_v3/manual_assets/`，會在 MAGI 導覽列另開分頁。它具有目錄、
全文搜尋、原始碼連結與日／夜切換，並沿用 MAGI 的本機主題偏好。

RC627–RC641 手冊已移至 `docs/archive/legacy-releases/`，僅保留為移轉歷史；
它們不是 release asset，線上 `/manual` 永遠不會選用。

## 公私版邊界

- **`WhaleChao/MAGI-v3`**：原私有 V2 倉庫原地更名，保留完整 V2/V3 工程歷史。
  私有不代表可以提交 secrets 或案件內容。
- **`WhaleChao/MAGI-public`**：經 public isolation audit 的架構、來源、測試、範例與文件。

canonical sealed payload 與 production receipts 保留在本機 release evidence chain。
GitHub 刻意排除案件／runtime-only 資料；任何 repository checkout 都不能單憑自身宣稱
是目前 LIVE 部署。

安全問題請勿在公開 issue 貼入真實憑證或案件資料；請依 [SECURITY.md](SECURITY.md)
與 [SUPPORT.md](SUPPORT.md) 處理（若該 snapshot 有收錄）。
