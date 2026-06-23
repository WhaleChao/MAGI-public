# MAGI Operator Release Runbook

版本：2026-06-23 post-release operator edition
適用對象：MAGI 發布、部署、維運、交接人員
canonical CLI：`magi`

本 runbook 是 2026-06-23 後的操作員發布手冊。它區分三種環境：public install、private production、developer checkout。正式操作以 `magi` 為 canonical CLI；腳本路徑只在安裝、測試、發版與故障排除時使用。

本文件不得記錄或複製真實案件、當事人、帳號、token、OAuth cookie、DB dump、NAS 私有路徑、portal 截圖或任何個資。需要示例時一律使用泛用佔位字。

---

## 1. 環境分類

| 類型 | 用途 | 可含資料 | 主要守門 |
|---|---|---|---|
| public install | 對外公開或客戶自行安裝 | 無私有 runtime、無案件資料、無私有整合標記 | `commercial-release` + public isolation strict |
| private production | 私有正式作業主機 | 可連正式 DB、NAS、OAuth、通道，但不得進 git | `production-live` + NERV + confirmation gates |
| developer checkout | 工程開發與測試 | fixture / 假資料；不得混入 production secrets | `ci` + `smoke62`，發版前 cleanroom |

環境紅線：

- public install 不得依賴私有信箱、私有 NAS、私有法律資料來源、私有 runtime 目錄。
- private production 可處理真實資料，但所有正式送出、批次搬移、DB restore、刪除與 portal 提交都必須有人確認。
- developer checkout 可以有本機 `.env`，但 `.env`、token、runtime output、DB dump、個資與 portal 截圖不得被追蹤。

---

## 2. Canonical CLI

正式操作用 `magi`：

```bash
magi status
magi start
magi stop
magi restart
magi menubar
magi zombie
```

安裝 CLI：

```bash
cp scripts/magi_cli.sh /opt/homebrew/bin/magi
chmod +x /opt/homebrew/bin/magi
magi status
```

若文件或舊腳本仍出現 `./scripts/magi_cli.sh`，視為相容入口；新 runbook、交接與發版紀錄一律寫 `magi`。

---

## 3. Public Install

Public install 是乾淨、可交付的公開安裝路徑。它不能要求使用者擁有私有事務所資料，也不能把私有整合帶進 release bundle。

建議流程：

```bash
python3 scripts/customer_install_wizard.py --public
python3 scripts/customer_install_wizard.py --public --yes
source .venv/bin/activate
python3 scripts/magi_doctor.py --json
magi status
```

若交付 DMG / EXE：

```bash
python3 scripts/packaging/build_installers.py --force
```

Public install 的 `.env` 僅由使用者本機填入。缺少正式 DB 或通道時，可以做安裝性 dry-run，但不得宣稱 production ready。

Public release 必跑：

```bash
./venv/bin/python scripts/ops/run_test_suite.py --suite ci
./venv/bin/python scripts/ops/run_test_suite.py --suite commercial-release --json-out .runtime/commercial_release_latest.json
```

Public isolation strict 的要求：

- `scripts/public_release_audit.py --public-isolation --strict` 必須通過。
- `.gitignore` 中保留忽略規則不算違規；被 git 追蹤的私有 runtime 或 private marker 才是阻斷項。
- 公版不得包含 private legal source marker、private mailbox marker、private NAS marker、私有帳號提示、真實姓名、電話、token、DB dump、portal 截圖。
- 公版不得啟用多租戶、公開上傳入口、電子簽章或任何未通過 commercial-release 的外部入口。

---

## 4. Private Production

Private production 是正式工作主機。部署前確認：

```bash
magi status
curl http://127.0.0.1:5002/health
python3 scripts/magi_doctor.py --json
./venv/bin/python scripts/ops/run_test_suite.py --suite production-live --json-out .runtime/production_live_latest.json
```

上線前人工檢查：

- NERV：`http://127.0.0.1:5002/dashboard/nerv` 或 `/nerv`。
- DB：連線、migration 狀態、最近備份、restore drill 紀錄。
- NAS / storage：掛載名稱正確，沒有掛到 `*-1` 類型的錯位目錄。
- 模型：day/night model live gate 通過，resource governor 非 critical。
- 通道：LINE / Telegram / Discord 或客戶啟用的通知通道 live smoke 通過。
- 業務模組：LAF、file review、transcript 使用 `business_module_live_check.py` 的非破壞性 live check。

Production release 必跑：

```bash
./venv/bin/python scripts/ops/run_test_suite.py --suite ci
./venv/bin/python scripts/ops/run_test_suite.py --suite smoke62
./venv/bin/python scripts/ops/run_test_suite.py --suite production-live --json-out .runtime/production_live_latest.json
```

若 private production 也要產生可交付 build，再加跑：

```bash
./venv/bin/python scripts/ops/run_test_suite.py --suite commercial-release --json-out .runtime/commercial_release_latest.json
```

---

## 5. Developer Checkout

Developer checkout 用於修 code、跑測試、建立 release candidate。不要把 production runtime 混進開發樹。

基本流程：

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
python3 scripts/first_run_setup.py --json
./venv/bin/python scripts/ops/run_test_suite.py --suite ci
./venv/bin/python scripts/ops/run_test_suite.py --suite smoke62
```

Developer checkout 規則：

- 使用 fixture、假資料或本機臨時 DB；不得把正式資料拿來寫測試輸出。
- 任何 destructive script 預設先 dry-run。
- 改資料庫 schema 前先備份，migration 要有 rollback 說明。
- 發版前必做 cleanroom current-worktree 檢查。

Cleanroom current-worktree：

```bash
git status --short
./venv/bin/python scripts/ops/run_test_suite.py --suite ci
./venv/bin/python scripts/ops/run_test_suite.py --suite commercial-release --json-out .runtime/commercial_release_cleanroom_latest.json
```

cleanroom 的意思不是丟掉目前 worktree，而是用目前要發布的工作樹做一次乾淨檢查：確認沒有私有資料、未追蹤 runtime、臨時報告、DB dump 或 portal 截圖被納入發布範圍。不得用 `git reset --hard` 來「清乾淨」別人的工作。

---

## 6. Release SOP

正式 release SOP 以 `scripts/ops/run_test_suite.py` 為主，不再以散落的單一 smoke script 作為交付依據。

| Suite | 何時跑 | 目的 |
|---|---|---|
| `ci` | 每次 PR、修核心程式、發版前第一步 | 公開安全、快速語法與單元測試 |
| `smoke62` | 私版日常、開發完成、production-live 前 | 本機完整冒煙，不含公版/商用 strict guard |
| `production-live` | private production 上線、重大更新、交接 | 真實本機服務、NERV 關聯健康、業務模組 live |
| `commercial-release` | 對外分享、銷售、打包、客戶安裝前 | public isolation strict、商用 readiness、通道與核心 route |

標準順序：

```bash
./venv/bin/python scripts/ops/run_test_suite.py --suite ci
./venv/bin/python scripts/ops/run_test_suite.py --suite smoke62
./venv/bin/python scripts/ops/run_test_suite.py --suite production-live --json-out .runtime/production_live_latest.json
./venv/bin/python scripts/ops/run_test_suite.py --suite commercial-release --json-out .runtime/commercial_release_latest.json
```

判定：

- 任一 required check fail，不得 release。
- skipped 只可接受於該 suite 明確標示的缺環境情境；production handoff 不可用 skipped 取代 live 驗證。
- JSON report 要保留在 `.runtime/`，但 `.runtime/` 不得進 git。
- 文件、交接與 release note 使用 `smoke62` 作為本機完整冒煙測試名稱。

---

## 7. NERV 與監控

NERV 是 private production 的上線狀態頁：

```text
http://127.0.0.1:5002/dashboard/nerv
http://127.0.0.1:5002/nerv
```

交付前 NERV 至少要看：

- 主 daemon、Tools API、排程與 queue。
- DB 連線與 failover / backup 狀態。
- NAS / storage 掛載與可用空間。
- oMLX / MLX / model sidecar health。
- OCR、向量庫、背景 job、issue agenda。
- resource governor 是否為 normal 或可接受 degraded。

NERV 紅燈、resource governor `critical`、DB restore 未確認、NAS 掛載錯位時，停止 release 或批次操作。

---

## 8. Business Module Live Check

業務三模組 live check 使用非破壞性入口：

```bash
python3 scripts/ops/business_module_live_check.py
python3 scripts/ops/business_module_live_check.py --skip-laf-live
python3 scripts/ops/business_module_live_check.py --notify
```

它會檢查：

- LAF self-test 與 portal live scan，不送出表單。
- file review self-test 與 downloadable probe。
- transcript self-test 與 DB probe。

`production-live` 已包含此檢查。若單獨執行失敗，不得以單一模組人工操作成功取代；必須修正後重跑，或在交接紀錄寫明阻斷理由與暫緩範圍。

---

## 9. Confirmation Gates

以下流程必須保留人工確認或確認碼，不得為了方便改成直接送出：

- LAF 開辦、進度、結案、條件或補件回報。
- file review 閱卷聲請、下載、繳費憑證上傳。
- transcript 需要登入外部 portal 的批次同步。
- 大量 NAS 搬移、結案搬移、資料夾清理。
- DB restore、migration rollback、資料修復與批次刪除。
- 對外訊息、帳務輸出或正式文件送出。

DB restore confirmation gate：

1. 先產生 restore plan，列出目標 DB、來源備份、時間、影響範圍與預計停機。
2. 確認目前 production 已備份，且備份檔可讀。
3. 停止 MAGI daemon 與相關排程。
4. 由操作員輸入明確確認字串；不得用預設 yes 或自動 yes。
5. 執行 restore。
6. 跑 migration status、magi doctor、NERV、production-live。
7. 由操作員確認資料筆數、最近案件索引、行事曆、NAS 路徑與通道狀態合理。

範例命令只示意，不含真實 DB 名稱：

```bash
magi stop
mysqldump -u <user> -p <database> > pre_restore_<yyyymmdd_hhmm>.sql
mysql -u <user> -p <database> < <approved_backup>.sql
python migrations/migrate.py status
magi start
./venv/bin/python scripts/ops/run_test_suite.py --suite production-live --json-out .runtime/production_live_after_restore.json
```

---

## 10. DB、Backup、Restore

私有正式環境以本機 MariaDB / MySQL 為主要服務資料庫。備援建議採「本機主 DB -> 遠端備份庫 replica」，避免雙活互相覆寫。

檢查：

```bash
magi status
curl http://127.0.0.1:5002/health
python3 scripts/ops/configure_mariadb_master_backup.py --check-only
```

備份：

```bash
mysqldump -u <user> -p <database> > backup_<yyyymmdd_hhmm>.sql
```

Migration：

```bash
python migrations/migrate.py status
python migrations/migrate.py upgrade
python migrations/migrate.py rollback
```

Restore 永遠走 confirmation gate。不得在 daemon、cron、portal automation 正在運作時直接灌回。

---

## 11. NAS、Storage、Disk

NAS / storage 規則：

- 不預先建立應由 SMB 掛載的空目錄，避免系統掛到錯位名稱。
- 正式案件資料、DB 正文、模型本體、訓練成果與不可重建狀態檔不得被快取清理刪除。
- 可重建 cache、log、暫存 export 可依保留期清理。
- 大量搬檔先 dry-run，再確認，再執行。

常用檢查：

```bash
magi status
df -h
python3 scripts/ops/disk_low_water_alarm.py
python3 scripts/ops/disk_cleanup_healthcheck.py --dry-run
```

低水位、掛載錯位或 resource governor critical 時，停止 OCR batch、判決收集、長文件翻譯、批次搬檔與 release。

---

## 12. LaunchAgent 與服務

macOS private production 通常使用 LaunchAgents 管理服務。實際 label 以主機設定為準，常見服務包含：

- MAGI daemon。
- menubar / status monitor。
- model runtime / sidecar。
- embedding runtime。
- DB proxy 或 backup helper。
- NAS reconnect。
- nightly jobs。

查看：

```bash
launchctl list | grep magi
magi status
```

重啟優先使用 `magi restart`。只有在單一 LaunchAgent 卡住時才手動 `launchctl kickstart`。

---

## 13. Upgrade / Rollback

Upgrade：

```bash
./venv/bin/python scripts/ops/run_test_suite.py --suite ci
mysqldump -u <user> -p <database> > pre_upgrade_<yyyymmdd_hhmm>.sql
cp .env .env.bak
magi stop
git pull --ff-only
venv/bin/pip install -r requirements.txt
python migrations/migrate.py upgrade
cp scripts/magi_cli.sh /opt/homebrew/bin/magi
magi start
./venv/bin/python scripts/ops/run_test_suite.py --suite production-live --json-out .runtime/production_live_after_upgrade.json
```

Rollback：

- 先判斷是 code rollback、migration rollback、DB restore，或三者都需要。
- DB restore 必須走 confirmation gate。
- rollback 後必跑 NERV、magi doctor、`production-live`。

---

## 14. Troubleshooting

| 症狀 | 優先處理 |
|---|---|
| `magi status` 不正常 | 看 NERV、`.agent/server.log`、LaunchAgent 狀態 |
| `/health` 失敗 | 確認 daemon、DB、port、env |
| DB connection refused | 確認 DB 服務、帳密、port、migration |
| NAS not mounted | 停止批次任務，重新掛載到 canonical path |
| model timeout | 看 model live gate、sidecar health、resource governor |
| 通道不回 | 先看 webhook / token / channel smoke，不重送正式表單 |
| business module live fail | 單跑 `business_module_live_check.py`，修正後重跑 `production-live` |
| public audit fail | 移除 tracked private data 或 marker，不得 bypass |

---

## 15. Release Sign-off

發版或交接紀錄至少包含：

- 環境類型：public install / private production / developer checkout。
- commit 或 build identifier。
- `ci`、`smoke62`、`production-live`、`commercial-release` 的結果與 JSON report 路徑。
- NERV 截止時間與總結，不附含個資截圖。
- DB backup 與 restore drill 狀態。
- public isolation strict 結果。
- 已知 skipped 項目、原因與風險接受人。
- 是否保留所有 confirmation gates。

一行判定：

```text
Release is allowed only when the required suite for the target environment passes, NERV is acceptable, public isolation is strict-clean when applicable, and destructive/portal/DB actions remain confirmation-gated.
```
