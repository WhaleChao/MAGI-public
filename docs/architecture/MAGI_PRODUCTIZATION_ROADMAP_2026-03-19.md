# MAGI 商品化 Roadmap

日期：2026-03-19
狀態：Delivery Draft v2
適用對象：Founder / Engineering / Ops / Product

## 1. 目的

本文件將目前對 MAGI 的商品化審查結果，轉成可執行的 30 天 / 60 天 / 90 天 roadmap。

目標不是只讓 MAGI「在作者的機器上能跑」，而是讓它具備以下能力：

- 可在非作者機器重現部署
- 可分環境管理與升級
- 可被團隊接手維運
- 可向付費客戶交付並維持基本 SLA
- 可通過最基本的資安、授權、稽核與資料邊界要求

## 2. 目前判斷

目前 MAGI 的狀態較接近：

- 高能力的 founder-operated internal system
- 可做技術展示與受控 pilot
- 尚未達到可直接對外銷售的商品級

目前商品化成熟度評估：約 4/10。

## 3. 已確認仍待修的缺失

以下缺失是本次重新盤點後，確認仍需修補的項目。

### A. 封裝與發版缺口

- root `pyproject.toml` 仍缺少完整 `build-system`、封裝後端、明確依賴聲明與 entry points
- root repo 仍缺少正式 CI workflow，尚未形成標準化 release gate
- 目前沒有單一、可重現的產品安裝與升級入口
- root 尚無明確 `LICENSE` 或商業授權主文件

### B. 資料邊界與 repo hygiene 缺口

- repo 仍混入 runtime 輸出、debug 產物、健康檢查結果與自動化報表
- repo 內仍可見瀏覽器 profile 狀態，例如 `.laf_chrome_profile`
- repo 內仍存在可能含真實案件或操作痕跡的檔案，例如 `apply_form_*.png`、`apply_form_inside_*.html`
- repo 內仍存在帶有實務資料風險的訓練或案例檔，例如 `skills/pdf-namer/training_data.json`
- 目前缺少正式的 redaction、fixture、retention 與 data classification 政策

### C. 安全與合規缺口

- `api/tools_api.py` 目前使用 `CORS(app)`，尚未見明確 origin allowlist
- `api/server.py` 的 rate limit 仍是單機記憶體內實作，不適用多實例或反向代理拓樸
- `skills/judgment-collector/action.py` 預設允許 `JUDICIAL_API_ALLOW_INSECURE_SSL=1` fallback，商品版不應預設開啟
- 權限控制仍大量依賴散落的 `role != "admin"` / `admin_only` 判斷，尚未形成統一 authz policy
- session cookie、安全標頭、CSRF、API auth contract 仍需要明確產品級基線

### D. 架構與可維護性缺口

- `api/server.py`、`api/orchestrator.py` 仍是超大型單體檔案，維護與回歸風險高
- `api/blueprints` 已開始拆分，但目前仍屬「逐步遷移中」，尚未真正完成模組化
- API surface 已很大，但尚無正式 OpenAPI / external contract / compatibility policy
- schema 初始化與演進仍分散在各模組中，ownership 與升級責任不夠集中

### E. 產品治理缺口

- 尚未完全定義正式 SKU、支援邊界、升級政策與 deprecation policy
- 尚未建立 customer-safe release tree 的驗收規則
- 尚未建立資料法遵、第三方授權、模型授權的完整盤點流程

## 4. 商品化定義

本 roadmap 以「可商品化」作為以下門檻：

- 新環境可依文件完成安裝與啟動
- 核心功能不依賴單一使用者家目錄或單一桌機設定
- secrets、資料、logs、原始碼、客戶檔案分離
- 有正式 deploy、upgrade、rollback 與 backup/restore 流程
- 有最小可接受的 CI、測試、監控、告警
- 有清楚的授權、隱私、支援與版本政策

## 5. 先做的 P0 實作項目

以下項目是「不做就不建議開始正式商品銷售」的 P0：

- [ ] `P0` 移除所有 `/Users/ai/Desktop/MAGI`、`~/Desktop/MAGI` 等硬編碼路徑，改為統一 runtime path resolver
- [ ] `P0` 將 source code、runtime logs、報表、備份、瀏覽器 profile、客戶輸出檔徹底分離
- [ ] `P0` 建立單一可重現安裝流程：`bootstrap`、`start`、`test`、`upgrade`
- [ ] `P0` 重構 config：核心功能與 LINE/Discord/DB/LAF 等整合改為可選模組，不得在未啟用時阻塞整體啟動
- [ ] `P0` 建立 secrets 注入策略，不再依賴手工維護的 live `.env`
- [ ] `P0` 補齊正式資料庫 migration 機制與 schema version
- [ ] `P0` 建立 root CI pipeline，至少包含 lint、unit/integration smoke、packaging check
- [ ] `P0` 移除 repo 中瀏覽器 profile、真實或半真實案件輸出、敏感訓練資料，建立 redaction / fixture policy
- [ ] `P0` 關閉 insecure SSL fallback 預設值，改為明確 opt-in 並留下審計痕跡
- [ ] `P0` 明確定義部署模式：先決定是 self-hosted、managed service，還是 SaaS
- [ ] `P0` 補齊 operator runbook 與安裝文件，讓非作者可接手

建議優先順序：

1. 路徑與 runtime/source 分離
2. config/secrets 模組化
3. deploy/bootstrap 標準化
4. migration 與 backup/restore
5. CI 與最小監控

## 6. 30 天 Roadmap

### 30 天目標

把 MAGI 從「只能在作者機器上運作」拉到「可在受控新環境部署並由團隊操作」。

### 30 天主要交付物

#### A. 部署與路徑去綁定

- 將所有硬編碼路徑集中改為 `api/runtime_paths.py` 或等效模組統一管理
- 建立 `MAGI_ROOT`、`MAGI_DATA_DIR`、`MAGI_LOG_DIR`、`MAGI_CONFIG_DIR`、`MAGI_SECRETS_DIR`
- 補一個標準啟動入口，例如 `bin/bootstrap`、`bin/start`、`bin/check`
- 統一 launchd/system service 所需的 env 與工作目錄載入方式

30 天驗收標準：

- 在同一台機器的另一個路徑可啟動
- 在另一台乾淨測試機可依文件完成安裝
- 啟動腳本中不再出現使用者 home path 假設

#### B. Repo 與 runtime 邊界整理

- 將 `_autopilot_runs`、`_db_backups`、`_logs`、debug screenshots、health reports 移出 source tree
- 清理不應出現在 release artifact 的營運資料
- 清除 `.laf_chrome_profile`、`apply_form_*`、`apply_form_inside_*`、敏感 training data 等不應進商品版的檔案
- 建立 `.gitignore`、runtime output 目錄政策與 retention 規則
- 定義哪些資料進 repo、哪些只能存在部署環境
- 建立測試資料去識別化與 fixture 產生規則

30 天驗收標準：

- repo 可產出乾淨 source bundle
- release tree 不包含客戶資料、報表、備份與執行殘留
- release tree 不包含瀏覽器 profile、操作快照或真實案件樣本
- 新生成的 logs/reports 預設寫到 data/log 目錄而非 repo 根目錄

#### C. Config 與 secrets 模組化

- 將 `REQUIRED_VARS` 改為 core required 與 feature-scoped required
- 各通道與各 skill 採 lazy validation，不啟用就不阻塞
- 補 `config.example`、`env.example`、config schema 說明
- 釐清本機開發、受控測試、正式環境三種 secrets 注入方式

30 天驗收標準：

- 關閉 LINE/Discord/LAF 時，核心系統仍可啟動
- 可用單一文件說明每個 env var 的用途、是否必填、適用模組
- 正式環境不需靠人工修改 `.env` 才能上線

#### D. 安全與隱私基線

- 將 `JUDICIAL_API_ALLOW_INSECURE_SSL` 預設值改為關閉，僅允許明確 opt-in
- 對 `CORS(app)` 改成 allowlist 配置，不允許無限制跨域
- 補 session/cookie/security headers 最小基線
- 建立 release 前敏感資料掃描與 secret scan

30 天驗收標準：

- 商品版預設不會繞過 SSL 驗證
- tools/api 只接受明確允許的前端來源
- release checklist 含 secret scan 與敏感資料掃描

#### E. 最小可重現測試與 CI

- 補 root `.github/workflows` 或等效 CI
- 定義最小驗證組合：config、inference routing、product runtime、health smoke
- 將測試文件改為 repo-relative，不得使用 `/Users/ai/Desktop/MAGI`
- 加入 packaging 與 import smoke test
- 補 `pyproject.toml` 的 build metadata 與 packaging check

30 天驗收標準：

- PR 或 release branch 有自動測試
- 新環境 clone 後可依文件跑最小 smoke test
- 測試不依賴作者個人路徑

#### F. 商品策略先決策

- 決定 Phase 1 交付型態：`single-tenant self-hosted` 或 `managed deployment`
- 暫不建議直接跳多租戶 SaaS
- 決定第一個 commercial SKU 的範圍：例如「對外版 MAGI 基礎協作 + 指定通道 + 指定技能包」

30 天驗收標準：

- 有一頁商業交付定義
- 研發不再同時追三種完全不同架構

### 30 天建議 P0 清單

- [ ] `P0` 路徑去硬編碼
- [ ] `P0` runtime/source 分離
- [ ] `P0` config 模組化
- [ ] `P0` secrets 注入方案
- [ ] `P0` repo 敏感資料/瀏覽器 profile 清理
- [ ] `P0` insecure SSL fallback 關閉 + CORS allowlist
- [ ] `P0` root CI 與最小 smoke test
- [ ] `P0` 安裝文件 v1

## 7. 60 天 Roadmap

### 60 天目標

把 MAGI 從「可部署」拉到「可維運、可升級、可做付費 pilot」。

### 60 天主要交付物

#### A. 正式部署包與升級流程

- 定義版本化 release artifact
- 補 `upgrade`、`rollback`、`preflight`、`post-deploy checks`
- 統一服務管理方式，避免混用手工 shell、launchd、零散背景程序
- 對外版與內部版拆出不同 profile

60 天驗收標準：

- 可完成一次升版演練與一次回滾演練
- 升級流程有文件、腳本與檢查點
- 操作員可依 runbook 執行，不需作者在場

#### B. 資料庫與狀態管理產品化

- 導入正式 migration framework
- 定義 schema version、init、upgrade、rollback
- 為核心資料表補 migration test
- 建立備份、還原與資料保存政策

60 天驗收標準：

- 新環境可自動初始化 schema
- 升版不再依靠散落的 `CREATE TABLE IF NOT EXISTS`
- 至少完成一次 backup/restore drill

#### C. 可靠性與觀測性

- 區分 `liveness`、`readiness`、`dependency health`
- 將 watchdog、channel health、inference health 納入統一監控事件
- 集中 logs，改為結構化 logging
- 設定最小 alerting：服務掛掉、推理卡死、queue 累積、DB 連線失敗、通道驗證失敗

60 天驗收標準：

- 可在 5 分鐘內判斷故障位置
- 重要服務異常能主動告警
- 可以回看前一天的核心事件與錯誤趨勢

#### D. 安全與權限基線

- 明確定義 admin/operator/end-user 權限
- 補 API key / bearer policy、session policy、操作審計
- 將目前散落的 `role != "admin"` 判斷整理成統一 authz policy
- 補 cookie hardening、security headers、CSRF policy
- 將單機記憶體 rate limiter 升級為反向代理或共享 backend 可用方案
- 為外部 endpoint 建立 API contract / OpenAPI baseline
- 加入 dependency scan、secret scan、基本輸入驗證與 rate limit
- 盤點高風險整合：LAF、Gmail、LINE、Discord、browser automation

60 天驗收標準：

- 高權限操作可追溯到執行者
- release 流程中有基本安全掃描
- 文件中明確標示敏感能力與權限要求
- 對外 API 有最小契約與認證說明

#### E. 架構拆分與維護性

- 將 `api/server.py`、`api/orchestrator.py` 的高變動區塊逐步拆入 blueprint / service modules
- 建立 route ownership、module ownership 與回歸測試責任
- 將「逐步遷移中」的模組化工作轉成正式拆分計畫

60 天驗收標準：

- 至少完成一輪高風險模組拆分
- 新功能不再優先疊加進超大型單體檔
- 團隊可明確知道每個模組的 owner

#### F. 文件與 Pilot Enablement

- 補 operator guide、troubleshooting、support matrix、FAQ
- 補 deployment architecture diagram
- 補「客戶導入流程」與「故障回報流程」
- 為第一個 pilot 客戶整理 onboarding package

60 天驗收標準：

- 非作者工程師可獨立完成部署與基本故障排除
- 客戶上線前 checklist 完整
- 支援流程與責任分界清楚

### 60 天建議 P0/P1 清單

- [ ] `P0` 版本化 release + rollback
- [ ] `P0` migration framework + schema version
- [ ] `P1` structured logging + alerting
- [ ] `P1` 權限模型與 audit log
- [ ] `P1` API contract / OpenAPI baseline
- [ ] `P1` monolith 拆分第一階段
- [ ] `P1` operator runbook v2

## 8. 90 天 Roadmap

### 90 天目標

把 MAGI 從「可付費 pilot」拉到「可稱商品級、可穩定續約與擴大交付」。

### 90 天主要交付物

#### A. 正式產品邊界

- 確立第一版商品 SKU、功能邊界、加購項與不支援範圍
- 將實驗性 skill、內部工具、臨時腳本與正式產品功能分層
- 建立產品 profile，例如 `core`、`legal`、`ops`、`enterprise add-ons`

90 天驗收標準：

- 可以明確回答客戶「你賣的是哪一版 MAGI」
- 實驗功能不會混入正式交付版本

#### B. SLA / SLO / 支援體系

- 定義 uptime、response time、incident severity、support hours
- 定義 on-call、交接、事故復盤、RCA 模板
- 為高風險依賴建立降級策略

90 天驗收標準：

- 有書面的 SLA/SLO 草案
- 發生事故時可以照流程處理
- 支援與研發責任邊界清楚

#### C. 法務與合規

- 補 root license 或商業授權方案
- 盤點第三方授權、模型授權、通道使用條款
- 建立 privacy policy、data retention policy、customer data handling policy
- 建立第三方 BOM 與發版授權盤點流程
- 定義審計資料保存期與刪除政策

90 天驗收標準：

- 可提供客戶基本法務文件包
- 可回答資料保存、刪除、備份與第三方依賴問題

#### D. 客戶化與商業交付能力

- 建立客戶環境模板
- 建立報價與交付估工框架
- 建立版本命名、release notes、deprecation policy
- 決定是否進一步投資多租戶架構

90 天驗收標準：

- 新客戶導入不再從零手作
- 每次版本更新有 release notes 與升級說明
- 客戶成功與技術交付可重複執行

#### E. 長穩測試與商品級驗證

- 補 24h/72h soak test
- 補通道故障、模型卡死、DB 斷線、磁碟滿載、憑證失效等演練
- 補安裝測試、升級測試、回滾測試、災難復原測試

90 天驗收標準：

- 有一組正式的 go-live checklist
- 可以完成 staging -> production 的完整演練
- 商品版具備可驗證的穩定性數據

### 90 天建議 P1/P2 清單

- [ ] `P1` SKU/profile 分層
- [ ] `P1` SLA/SLO 與 incident process
- [ ] `P1` 法務與隱私文件
- [ ] `P1` root license / 第三方 BOM / 模型授權盤點
- [ ] `P2` 客戶導入模板
- [ ] `P2` 72h soak + disaster drills

## 9. 建議的執行順序

### 波段 1：先把「不能賣」的問題清掉

- 硬編碼路徑
- runtime/source 混放
- repo 內存在敏感資料、瀏覽器 profile、案件輸出或訓練樣本
- config/secrets 單體化
- insecure SSL fallback 預設開啟
- 無標準部署
- 無 migration

### 波段 2：建立「可以交付」的能力

- CI
- upgrade/rollback
- monitoring/alerting
- API contract / authz / rate limit / security baseline
- backup/restore
- operator 文件

### 波段 3：建立「可以續約與擴張」的能力

- SKU
- SLA/SLO
- 法務與隱私
- root license / 第三方 BOM / release governance
- 客戶 onboarding
- 長穩與 DR 演練

## 10. 建議的團隊分工

### Engineering

- runtime path、config、migration、CI、測試、release engineering

### Ops / SRE

- deployment、secrets、監控、告警、backup/restore、runbook

### Product / Founder

- SKU、交付型態、支援邊界、版本政策、pilot 客戶範圍

### Legal / Business

- license、第三方授權盤點、隱私政策、商業條款

## 11. Go / No-Go 建議

### 現階段結論

目前不建議直接以「標準商品」對外販售。

### 可接受的短期路徑

- 可做 founder-led pilot
- 可做受控客製部署
- 可先賣服務型專案，不要先賣成品型 SaaS 承諾

### 正式 Go-Live 最低門檻

- P0 全部完成
- 至少一輪新環境部署成功
- 至少一輪升級/回滾演練成功
- 至少一輪 backup/restore 演練成功
- 有最小 operator runbook、安裝手冊、release checklist

## 12. 建議的第一個里程碑

建議以「30 天內做出可在第二台機器部署的單租戶對外版 MAGI」為第一里程碑。

這個里程碑若完成，MAGI 就會從「只能由作者維持的系統」進化成「可以被團隊交付的產品雛形」。

## 13. 預設商品化假設

為了讓 roadmap 可以直接執行，本文件先採以下預設。若未來商業策略改變，再以版本化方式更新本文件。

- Phase 1 交付模式預設為 `single-tenant managed deployment`
- Phase 1 不預設直接做多租戶 SaaS
- Phase 1 可接受的部署宿主，以現況相容性優先，預設為 MAGI 團隊可控的專屬主機
- Phase 1 商品邊界預設為「MAGI Core + 指定通道 + 指定技能包」，不是整個 repo 內所有實驗能力
- 法律、自動登入、瀏覽器自動化、外部政府系統整合等高風險能力，預設歸類為受控功能，不納入第一版通用商品承諾
- 商品化第一階段目標客群預設為 1 到 3 個受控 pilot 客戶，而非大規模開放註冊

## 14. 成功指標與 Release Gates

### Gate A：30 天完成後的基礎可交付門檻

- 第二台機器可依文件完成安裝
- 啟動不依賴作者個人家目錄
- release tree 已排除敏感資料與 runtime 殘留
- 核心功能在未啟用 LINE/Discord/LAF 時仍可啟動
- CI 能自動跑最小 smoke test
- operator runbook v1 完成

### Gate B：60 天完成後的付費 Pilot 門檻

- 可完成一次升級與回滾演練
- 可完成一次 backup 與 restore 演練
- 有統一 authz policy 與最小 API contract
- 有結構化 logging、告警與故障定位路徑
- 有 migration framework 與 schema version
- 非作者工程師可獨立完成部署與基本排障

### Gate C：90 天完成後的商品級 Release Candidate 門檻

- SKU、支援邊界、版本政策已明確
- 有 SLA/SLO 草案與事故流程
- 有 root license、第三方 BOM 與模型授權盤點
- 有 go-live checklist、release checklist、customer onboarding pack
- 通過 24h/72h soak 與關鍵災難演練
- 可對外提供基本法務與隱私文件包

### 建議追蹤 KPI

- 新環境安裝成功率
- P0 backlog 完成率
- 升級成功率與平均回滾時間
- 重大故障偵測時間與修復時間
- smoke / integration test pass rate
- release tree 敏感資料掃描為 0 的次數

## 15. 12 週執行節奏

### Week 1

- 凍結商品化 Phase 1 範圍
- 建立 P0 issue board
- 完成路徑、敏感資料、部署模式盤點
- 定義 release tree 應包含與不得包含的內容

### Week 2

- 開始 runtime path abstraction
- 建立 data/log/config/secrets 目錄策略
- 草擬 bootstrap / start / check 腳本
- 開始清理 repo 內敏感資料與 profile

### Week 3

- 重構 config required/optional 分層
- 補 root CI skeleton
- 關閉 insecure SSL fallback 預設值
- 對 CORS 與 security headers 打第一版基線

### Week 4

- 完成第二台機器安裝演練
- 完成 release tree 檢查
- 交付 operator runbook v1
- 完成 30 天 Gate A 驗收

### Week 5

- 導入 migration framework
- 定義 schema version 與 migration ownership
- 補 preflight / post-deploy checks
- 開始 release artifact 標準化

### Week 6

- 建立 backup / restore 流程
- 將單機 rate limit 改為可擴充方案
- 開始 API auth contract 與 OpenAPI baseline
- 開始結構化 logging

### Week 7

- 盤整 authz policy
- 將高風險 endpoint 與高權限操作納入 audit
- 補 alert 規則
- 進行第一次升級/回滾 rehearsal

### Week 8

- 完成第一輪 monolith 拆分
- 補文件、support matrix、故障排除指南
- 完成 backup/restore drill
- 完成 60 天 Gate B 驗收

### Week 9

- 凍結第一版 SKU 與 profile
- 建立 customer onboarding package
- 建立版本命名、release notes 模板與 deprecation policy

### Week 10

- 補 SLA/SLO 草案與 incident/RCA 模板
- 建立 root license / 商業授權方案草稿
- 開始第三方 BOM 與模型授權盤點

### Week 11

- 執行 24h/72h soak test
- 執行故障、憑證、磁碟、DB 中斷等情境演練
- 修正 release governance 缺口

### Week 12

- 完成 go-live checklist
- 完成 staging -> production 演練
- 完成法務與隱私文件包
- 完成 90 天 Gate C 驗收

## 16. 可執行 Backlog

### 16.1 P0 Backlog

| ID | 項目 | 建議 Owner | 估計 | 依賴 | 完成定義 |
| --- | --- | --- | --- | --- | --- |
| P0-01 | Runtime path abstraction | Platform Eng | M | 無 | 所有核心服務不再引用 `/Users/ai/Desktop/MAGI` 或 `~/Desktop/MAGI` |
| P0-02 | Runtime/source/data 目錄分離 | Platform Eng | M | P0-01 | logs、reports、backups、exports、profiles 全部移出 source tree |
| P0-03 | 敏感資料與 profile 清理 | Platform Eng + Ops | M | P0-02 | repo 中無 `.laf_chrome_profile`、`apply_form_*`、`apply_form_inside_*`、敏感 training data |
| P0-04 | Redaction 與 fixture policy | Backend Eng | S | P0-03 | 測試資料去識別化規範與 fixture 產生流程已文件化 |
| P0-05 | Config 模組化 | Backend Eng | M | P0-01 | core 啟動不依賴未啟用功能 secrets |
| P0-06 | Secrets 注入方案 | Ops | M | P0-05 | dev/stage/prod secrets 流程定義完成，正式環境不靠手改 `.env` |
| P0-07 | Packaging metadata 補齊 | Platform Eng | S | 無 | `pyproject.toml` 具備 build metadata、entry point、packaging check |
| P0-08 | Root CI pipeline | Platform Eng | M | P0-07 | PR 可自動跑 lint、unit、smoke、packaging check |
| P0-09 | Bootstrap / start / check 腳本 | Platform Eng | M | P0-01 | 新環境可依文件一鍵 bootstrap 並完成 health check |
| P0-10 | Deployment profile 決策 | Founder/Product | S | 無 | Phase 1 部署模式、支援範圍、非目標範圍定案 |
| P0-11 | Migration framework 導入 | Backend Eng | M | P0-05 | schema version、init、upgrade、rollback 可執行 |
| P0-12 | Insecure SSL 預設關閉 | Backend Eng | S | 無 | 商品版預設 verify on，僅明確 opt-in 才允許 fallback |
| P0-13 | CORS allowlist + 安全標頭 | Backend Eng | S | 無 | tools/api 只允許白名單來源，補最小 security headers |
| P0-14 | Operator runbook v1 | Ops | S | P0-08, P0-09 | 非作者可依 runbook 完成安裝、啟動、基本排障 |

### 16.2 P1 Backlog

| ID | 項目 | 建議 Owner | 估計 | 依賴 | 完成定義 |
| --- | --- | --- | --- | --- | --- |
| P1-01 | 版本化 release artifact | Platform Eng | M | P0-08, P0-09 | 產出可版本化交付包與 checksum |
| P1-02 | Upgrade / rollback 流程 | Platform Eng + Ops | M | P1-01, P0-11 | 完成一輪升級與回滾 rehearsal |
| P1-03 | Backup / restore 流程 | Ops | M | P0-11 | 完成一輪 restore drill 並有記錄 |
| P1-04 | Structured logging | Backend Eng | M | 無 | 核心服務輸出結構化 log，便於集中檢索 |
| P1-05 | Alerting 與 health model | Ops | M | P1-04 | readiness / liveness / dependency health 與警報規則完成 |
| P1-06 | 統一 authz policy | Backend Eng | M | P0-05 | 高權限判斷不再散落於 `role != "admin"` |
| P1-07 | API contract / OpenAPI baseline | Backend Eng | M | P1-06 | 對外 endpoint 具備最小契約與認證說明 |
| P1-08 | Distributed rate limit | Backend Eng + Ops | M | P1-05 | rate limit 可於多實例或反向代理架構下正確生效 |
| P1-09 | Monolith 拆分第一階段 | Backend Eng | L | P1-06 | `api/server.py`、`api/orchestrator.py` 的高風險區塊完成首輪拆分 |
| P1-10 | Operator runbook v2 + support matrix | Ops | S | P1-02, P1-03 | 完整導入、排障、升級、回滾文件齊備 |

### 16.3 P2 Backlog

| ID | 項目 | 建議 Owner | 估計 | 依賴 | 完成定義 |
| --- | --- | --- | --- | --- | --- |
| P2-01 | SKU / profile 分層 | Founder/Product | M | P1-01 | `core/legal/ops` 等 profile 與支援邊界明確 |
| P2-02 | SLA/SLO 與 incident process | Founder/Product + Ops | M | P1-05 | 有書面 SLA/SLO 與 incident 等級與流程 |
| P2-03 | Root license / 商業授權方案 | Legal/Business | M | 無 | root license 或商業授權條款可交付 |
| P2-04 | 第三方 BOM 與模型授權盤點 | Legal/Business + Eng | M | P1-01 | 可回答第三方與模型使用條款問題 |
| P2-05 | Customer onboarding pack | Product + Ops | S | P1-10, P2-01 | 新客戶導入包、檢查表、責任邊界文件完成 |
| P2-06 | Release governance | Platform Eng + Product | S | P1-01 | release notes、versioning、deprecation policy 完成 |
| P2-07 | 24h/72h soak 與 DR drill | Ops + Backend Eng | M | P1-05 | 完成長穩測試與災難演練並有報告 |
| P2-08 | Go-live checklist | Ops + Product | S | P2-05, P2-07 | 商品版正式上線檢查表可直接使用 |

## 17. 各工作流的完成定義

### 封裝/安裝完成定義

- 乾淨環境可依文件完成 bootstrap
- 主服務能成功啟動並通過最小 health/smoke
- 不需要手動修改個人目錄路徑

### 安全基線完成定義

- CORS 有 allowlist
- 預設不允許 insecure SSL fallback
- secret scan 與敏感資料掃描為 0
- 高風險 endpoint 有一致的 auth/authz 規則

### 資料治理完成定義

- source tree 不含 profile、exports、客戶資料、debug 殘留
- fixture 與真實資料分離
- release tree 經過資料掃描與人工抽查

### 運維完成定義

- 有 runbook、alert、升級回滾、備份還原文件
- 非作者工程師可完成演練
- 故障能在既定時間內定位

## 18. Release Checklist

- [ ] release tree 已通過敏感資料掃描
- [ ] root CI 全綠
- [ ] packaging check 通過
- [ ] migration upgrade / rollback 測試通過
- [ ] backup / restore drill 最近一次成功
- [ ] operator runbook 與 release notes 已更新
- [ ] root license / 授權文件已附帶
- [ ] 對外 API 與環境設定說明已同步

## 19. 風險登錄表

| 風險 | 影響 | 早期訊號 | 緩解方式 | 建議 Owner |
| --- | --- | --- | --- | --- |
| 硬編碼路徑殘留 | 新環境無法部署 | 第二台機器 bootstrap 失敗 | 路徑掃描 + runtime path abstraction | Platform Eng |
| repo 仍夾帶敏感資料 | 法遵與交付風險 | release tree 掃描出 profile / exports | 建立 redaction / fixture / release gate | Platform Eng |
| config 仍單體化 | 無法做 SKU 與 stage/prod 分離 | 關閉通道後核心無法啟動 | required/optional 模組化 | Backend Eng |
| migration 不完整 | 升級失敗或資料不一致 | 新環境初始化不穩、手動建表 | 導入 migration framework | Backend Eng |
| authz 散落 | 高權限行為失控 | 新功能各自補 `role != "admin"` | 統一 policy + audit | Backend Eng |
| insecure SSL fallback 被遺留 | 安全與合規風險 | verify error 時默默降級 | 改為預設關閉 + opt-in + audit | Backend Eng |
| monolith 持續膨脹 | 回歸風險、開發速度下降 | 新功能持續加進超大檔案 | 模組拆分與 owner 制 | Backend Eng |
| 無 root license/BOM | 商務與法務阻塞 | 客戶索取授權資料時無法交付 | 建立 license/BOM 流程 | Legal/Business |
| 監控不足 | 問題發現過晚 | 客戶先發現服務異常 | 結構化 logging + alerting | Ops |

## 20. 建議立即建立的 Issue 清單

以下 issue 建議在本週內直接建立，不需再等待下一輪規劃：

- `P0-01` Runtime path abstraction
- `P0-02` Runtime/source/data 分離
- `P0-03` 清除 repo 中 profile、exports、敏感訓練資料
- `P0-05` Config required/optional 模組化
- `P0-06` Secrets 注入方案
- `P0-07` 補 root packaging metadata
- `P0-08` 建 root CI workflow
- `P0-09` Bootstrap / start / check 腳本
- `P0-11` Migration framework 導入
- `P0-12` 關閉 insecure SSL fallback 預設值
- `P0-13` CORS allowlist + 安全標頭
- `P0-14` Operator runbook v1
