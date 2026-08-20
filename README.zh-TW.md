# MAGI V3 — 本機優先的 AI 作業平台（公版）

MAGI V3 統合互動網頁、持久化排程、隔離 worker、律師業務、文件與影音處理、
儲存同步、本機模型、健康證據與可回滾發布。它不是單一聊天程序，也不會把
「程序仍在」當成「業務已完成」。

本倉庫是隱私過濾後的公版。正式密碼、Cookie、token、案件資料、資料庫、瀏覽器
profile、mutable queue 與 LIVE 收據均刻意排除。

English: [README.md](README.md)

## 目錄

- [架構](#架構)
- [原始碼地圖](#原始碼地圖)
- [資料與信任邊界](#資料與信任邊界)
- [排程與復原](#排程與復原)
- [功能領域](#功能領域)
- [健康判定](#健康判定)
- [開發與正式驗證](#開發與正式驗證)
- [發布與回滾](#發布與回滾)
- [排查原則](#排查原則)
- [文件與倉庫邊界](#文件與倉庫邊界)

## 架構

MAGI 把舊架構容易混在一起的內容拆為四層：

1. **Source**：Git 內經審查的程式與測試。
2. **Release**：不可變、逐檔 manifest 雜湊的可執行包。
3. **Deployment**：精確綁定一個 release 的 launchd、環境、owner 與外部輸入。
4. **Runtime state**：跨版本保存的 ledger、checkpoint、queue、receipt、cache、lock、log。

```text
瀏覽器／選單列／訊息平台
          │
          ▼
Gateway（Interactive；5002／5003）
          │
          ▼
Control（Background；8088）
          │
          ▼
Supervisor（Background；scheduler／workers）
       │        │        │
    browser  document  integration／model
```

服務 manifest 是角色、埠、factory、child entrypoint、deployment mode 與禁用 legacy
程序的權威。正式 launchd 綁定 manifest 路徑與 SHA-256；檔案被移動或竄改就拒絕。

Playwright、OCR/PDF、逐字稿、Drive/NAS、模型及維護工作都在 supervisor 管理的
child 中執行，不塞進 Gateway；每個 child 有 timeout、資源、取消、process group
清理與完成證據。

## 原始碼地圖

| 路徑 | 責任 |
|---|---|
| `magi_v3/` | Gateway、Control、Supervisor、ledger、排程、owner、health、recovery |
| `api/` | OSC/web、認證、相容 route 與領域 adapter |
| `api/osc/` | 案件、檔案、Calendar、Drive 與 OSC 工作流 |
| `api/blueprints/` | 頁面／API 邊界與 authenticated manual routes |
| `skills/` | 明確工具與業務工作流入口 |
| `scripts/v3_validation/` | exact/full 認證、replay、fault、evidence |
| `scripts/v3_cutover/` | single-active preflight、activation、rollback、owner probe |
| `scripts/v3_release_bundle.py` | clean allowlist release 與 manifest |
| `scripts/v3_deploy_prepare.py` | 離線 render deployment，不啟動服務 |
| `scripts/v3_backup_prepare.py` / `v3_backup_verify.py` | 備份與實際 restore gate |
| `scripts/ops/` | 健康、稽核、清理與有界修復 |
| `config/` | service、cron、feature、validation policy |
| `templates/`、`static/`、`gui/`、`mobile_app/` | Web、桌面、行動 UI |
| `tests/` | unit、contract、security、replay、release、cutover、regression |
| `docs/` | 架構與維修文件 |
| `magi_v3/manual_assets/` | 隨 release 封裝的 authenticated／no-store 手冊 |

## 資料與信任邊界

installed release 只接受 manifest 宣告的 regular file，逐檔驗 path、size、mode、SHA-256；
symlink、special file、未知新增、source drift 與封存後異動都拒絕。runtime state 不得
寫進 release。

mutable state 按 owner 拆成 control ledger、cron occurrence/retry、Drive checkpoint/hash
cache、domain lock、owner metadata、notification outbox、business receipt、log、cache、
export。lock 是證據，不是垃圾；清理前必須證明 PID／PGID／executable／argv／release
root 與 schema 不再代表有效 owner。

NAS、Drive、portal、Calendar、Email、訊息平台都在本機 transaction 之外，外部副作用
須有 idempotency、prepare／commit／read-back 或 durable outbox。HTTP 200 或「已排入
佇列」不等於業務完成。

真實案號、當事人、路徑、憑證、原始信件與 runtime DB 不得進公版；evidence 只留
安全計數、固定 reason code、不可逆 digest 與 artifact hash。

## 排程與復原

immutable job definition 與 mutable state 完全分離。occurrence 帶建立時的 command-
definition hash；重啟後會 reconcile 中斷工作、supersede 舊 command，再以目前 sealed
definition 重建，不會執行佇列殘留的舊版程式。

`succeeded`、`deferred`、`waiting_children`、`skipped`、`needs_confirmation`、
`awaiting_input`、`failed`、`timed_out`、`cancelled` 的語意互不混用。timeout、storage
outage、portal busy、identity guard、process interrupted 與 human required 都有獨立且
有界的 retry contract。

Drive all-files 以 checkpoint 小批次前進；terminal evidence 要有 cursor、fresh progress、
hash cache、匿名 staging bytes 與嚴格 zero-risk counters。檔名語意碰撞只有在來源 identity
或有界 content hash 證實後才可解開，不能猜測覆蓋。

## 功能領域

- OSC 案件、檔案、待辦、Calendar、帳務與生命週期；
- 法扶／法院信件、portal、附件與進度流程；
- 閱卷偵測、下載、signature reconciliation、歸檔與重試；
- 逐字稿、語者、索引、翻譯、摘要；
- DOCX／PDF／OCR、命名、書籤、註解與文件審閱；
- Drive／NAS 雙向同步與 storage recovery；
- 裁判書、法規、研究、證據與庭前準備；
- 本機模型、agent reasoning、embedding、記憶與知識；
- outbox-backed 通知；
- 圖片轉可列印 mesh 的有界、不持久化驗證。

各領域可獨立 healthy、waiting、degraded 或 action-required。某 portal deferred 不代表
Gateway 死亡；一筆歷史 failure 也不能覆蓋已 reconcile 的成功結果。

## 健康判定

健康分為五層：

1. 輕量 liveness；
2. dependency、identity、role owner、child readiness；
3. 新鮮且已對帳的 business receipt；
4. Function／Doctor／guardian／Funnel；
5. 綁定 release 與 transaction 的 post-cutover evidence。

登入後可從 `/manual` 開啟維修百科。HTML、PDF、Markdown 與 source index 都是固定
allowlist route，從 active immutable release 以 `Cache-Control: no-store` 提供。

## 開發與正式驗證

測試的 HOME、runtime、agent、browser、queue、upload、cache 必須與 canonical runtime
隔離。常用窄測試：

```bash
python3 -m pytest -q tests/test_dashboard_pages_blueprint.py
python3 -m pytest -q tests/test_web_information_architecture.py
python3 -m py_compile api/blueprints/dashboard_pages.py
python3 scripts/privacy_audit.py --strict
python3 scripts/public_release_audit.py --public-isolation --strict
git diff --check
```

正式 promotion 另綁 exact test selection、source hash、route replay、fault/recovery、privacy
isolation 與 clean worktree。macOS 的 inner Seatbelt 由 hash-bound host-outer runner 建立；
不能因 sandbox 無法巢狀就弱化正式安全 gate。

## 發布與回滾

V3 採 single-active 冷切換：

1. certify clean sealed release；
2. fresh backup 與兩種 restore drill；
3. 候選版先安裝但不啟用；
4. render 並 hash-bind deployment inputs；
5. prepare transaction 與 rollback artifacts；
6. 在 rollback envelope 內 quiesce 舊 supervisor；
7. 證明 old owner 為零且 durable handoff 完整；
8. 原子啟用三角色；
9. 跑 Web／Funnel／STL／business／health／Drive LIVE gates；
10. hard gate 失敗時自動還原上一版。

禁止兩版同時 production、禁止 hot-patch installed release、禁止沿用別的 transaction
receipt，也禁止手改 cron/checkpoint 來製造綠燈。

## 排查原則

依序讀 active marker／transaction、installed manifest、deployment ownership、owner PID／
PGID／executable／argv、最新 terminal/business receipt 與 checkpoint。先分辨本輪 failure、
舊 failure、deferred、waiting，再用最窄 read-only probe 重現；修 source、補 adversarial
regression、建立新 immutable release，不直接修 LIVE 單檔。

常見錯誤包括：刪 lock 假綠、exit 0 當完成、用 path 猜 identity、integer 接受 bool、
broad exception 吞 deadline、child 未完成就標 parent success，以及把 mutable runtime 打包。

## 文件與倉庫邊界

- [維修百科 HTML](docs/MAGI_V3_維修百科全書_rc627.html)
- [維修百科 PDF](docs/MAGI_V3_維修百科全書_rc627.pdf)
- [可維護 Markdown](docs/MAGI_V3_維修百科全書_rc627.md)
- [機器可讀原始碼索引](docs/MAGI_V3_原始碼索引_rc627.json)
- [公版技術手冊](docs/MAGI_V3_技術手冊_rc627_公版.md)
- [V3 架構](docs/architecture/v3/MAGI_V3_ARCHITECTURE.md)
- [自架部署](docs/SELFHOST_DEPLOYMENT.md)

HTML 百科有目錄、全文搜尋、source link 與沿用 MAGI 偏好的日／夜切換。

`WhaleChao/MAGI-public` 是隱私過濾公版；原私有 V2 倉庫已原地更名為
`WhaleChao/MAGI-v3` 並保留完整歷史。兩個 Git checkout 都不是 LIVE runtime；canonical
sealed payload 與 production receipt 保留在本機 release evidence chain。

請勿在公開 issue 貼真實憑證或案件資料。另見 [SECURITY.md](SECURITY.md)、
[SUPPORT.md](SUPPORT.md) 與 [LICENSE](LICENSE)。
