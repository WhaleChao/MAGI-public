# MAGI

[English](README.md)

[![MAGI CI](https://github.com/WhaleChao/MAGI-public/actions/workflows/ci.yml/badge.svg?branch=codex%2Ffactory-release-20260712-public)](https://github.com/WhaleChao/MAGI-public/actions/workflows/ci.yml)

MAGI 是以臺灣法律工作為核心的本機優先 AI 作業平台。它把案件、檔案、行事曆、待辦、法扶、閱卷、筆錄、法律研究、書狀、帳務、通知與系統健康整合在同一套流程中，可從網頁、LINE、Discord、Telegram 與管理員工具使用。

正式環境預設部署在單一 Apple Silicon 主機。本機推論由 oMLX 提供日間／夜間模型自動切換；使用 `@heavy`、`@HEAVY` 或 `@重型` 時，會在完成個資遮蔽與資源檢查後走受控的高品質模型路徑。Windows 與 Linux 可使用支援的 Ollama 路徑。

## 目前版本狀態

2026-07-12 完成的出廠版已於 2026-07-13 再次確認：

- Agent 能力清冊共 37 項，涵蓋 28 個業務與維運領域。
- 意圖、實體、限制條件、缺少欄位、信心分數、確認、驗證、重試與回復都有明確資料合約。
- 行事曆支援自然語言查詢、預覽、建立、修改、取消、全天、單次、跨日與週／月重複事件。
- NERV 與 MAGI 選單列可顯示經過濾的 Agent 狀態，不洩漏提示詞、案件內容、路徑或模型推理。
- 長時間排程可依業務完成證據自行校正，服務重啟不再把已完成工作誤判成失敗。
- 完整測試基準為 `4392 passed`。
- `commercial-release` 為 `12/12 passed`，公開版隔離稽核為 `0 errors / 0 warnings`。

GitHub 會永久保留舊的失敗 workflow。修正後，舊紅燈不會被改成綠燈；請以最新提交或目前 PR 的 checks 為準。現在公庫與私庫的最新發佈檢查皆已通過。

## MAGI 能做什麼

| 領域 | 目前可用功能 |
|---|---|
| 案件與客戶 | 新增、搜尋、更新、結案、身分消歧、開啟受管案件資料夾 |
| 行事曆與待辦 | 白話查詢／新增／修改／取消、全天與重複事件、衝突檢查、案件待辦 |
| 檔案與文件 | 上傳、預覽、OCR、命名、索引、定稿、NAS 與雲端硬碟受控同步 |
| 法扶 | 活動統計、草稿預填、確認後送出、附件重試、結案文件檢查 |
| 閱卷 | 可下載檢查、聲請準備、確認後送出、下載與檔案核對 |
| 筆錄 | 單案下載、批次同步、去重、更名、索引與人工處理佇列 |
| 錄音與翻譯 | 本機錄音轉文字、文件翻譯、多引擎 OCR 共識、高品質模型路由 |
| 法律研究與書狀 | 法條查詢、裁判蒐集、研究資料匯入、附來源的法律文件草擬 |
| 所務管理 | 帳務、報價單、記憶規則、Obsidian 寫回、通知、備份與受控還原 |
| 系統維運 | 模型設定檔、出廠驗證、程序衛生、排程健康、自我修復證據 |

功能權威清冊是 [`config/agent_capabilities.json`](config/agent_capabilities.json)。每個功能必須同時列出工具、可能造成的變更、完成驗證方式及需要人工處理的情況，才能通過 Agent readiness gate。

## 自然語言 Agent

MAGI 不是只靠固定關鍵字。新的 Agent 層會在既有穩定路由與工具外，依序處理：

1. 判斷使用者意圖、對象、日期、限制條件與信心分數。
2. 找出缺少的必要資訊；不確定時先問，不直接猜。
3. 建立有前後依賴關係的工作計畫。
4. 依工具副作用等級決定能否直接執行。
5. 外部送出、破壞性操作與受保護網站動作必須明確確認。
6. 執行後回查資料庫、檔案或外部結果，確認真的完成。
7. 失敗時保留重試、降級、回復與人工接手狀態，不把失敗說成完成。

可以直接這樣說：

```text
下週有哪些行程？
下週五新增一個全天行程，名稱是遞狀期限。
把明天下午的當事人會議改到三點半。
每月第一個星期一早上九點安排案件檢討。
檢查這件是否有新的閱卷資料。
@重型 把這段錄音轉成逐字稿並翻譯，法律術語保留原文。
```

行事曆寫入會先顯示預覽並要求確認。同一對話可保守引用最近提到的案件、人物、附件、行程、草稿與計畫；若候選不只一個，MAGI 會要求指定，不會自行選一件寫入。

## MAGI 選單列與 NERV

MAGI 選單列是日常查看全系統狀態的入口，應能一眼看到：

- 核心服務與 MAGI 三模組是否正常。
- 日間／夜間模型、重型模型路徑與模型端點。
- 記憶體、資料庫、NAS／儲存容量與程序狀態。
- 法扶附件、閱卷、筆錄、案件回報、監控信箱及網頁模組。
- 啟用排程數、執行中工作、失敗、過期及需要人工確認的項目。
- 最近一次 Agent 意圖、計畫步驟、工具類別、確認狀態、重試次數與路由信心。

點選異常項目會開啟可複製的白話說明與案件／工作細項，不只顯示數字或原始 JSON。「最後活動」代表最近一次有可信執行證據的時間，不代表該功能現在仍在執行。

Agent 公開狀態只允許顯示預先核准欄位。提示詞、使用者訊息、案件資料、當事人、token、本機路徑與模型內部推理不得出現在公開狀態檔。

## 安全設計

MAGI 將工具變更分為唯讀、本機草稿、可回復寫入、外部正式動作及破壞性操作。高風險功能會使用權限檢查、確認碼、防重複鍵、執行後驗證，以及回復或人工接手機制。

以下確認不可取消：

- 法扶、閱卷或其他網站的正式送出。
- 資料庫還原與 migration rollback。
- 批次刪除、案件資料夾搬移或對外發布。
- 第一次外部結果不明時的重複送出。

AI 產出是輔助工作成果。法律引用、姓名、案號、法院、期限、金額、翻譯、逐字稿與書狀在對外使用前，仍須由專業人員確認。

## 公開版安裝

```bash
git clone https://github.com/WhaleChao/MAGI-public.git
cd MAGI-public
python3 scripts/customer_install_wizard.py --public --yes
python3 scripts/public_release_audit.py --public-isolation --strict
magi status
```

公庫不得包含正式資料庫、真實案件、帳號、憑證、portal 截圖、runtime 狀態或私有部署路徑。私有整合在安裝者提供設定與權限前保持停用。

## 管理員常用指令

```bash
magi status
magi start
magi stop
magi restart
magi menubar
magi zombie
```

出廠驗證請使用固定入口：

```bash
./venv/bin/python scripts/ops/run_test_suite.py --suite ci
./venv/bin/python scripts/ops/run_test_suite.py --suite smoke62
./venv/bin/python scripts/ops/run_test_suite.py --suite production-live --json-out .runtime/production_live_latest.json
./venv/bin/python scripts/ops/run_test_suite.py --suite commercial-release --json-out .runtime/commercial_release_latest.json
./venv/bin/python scripts/ops/agent_readiness_gate.py --strict
```

| Gate | 用途 |
|---|---|
| `ci` | GitHub 可執行的公開安全、語法、靜態防護與快速測試 |
| `smoke62` | 正式主機的本機完整冒煙測試 |
| `production-live` | 正式環境依賴與業務模組的非破壞 LIVE 檢查 |
| `commercial-release` | 公開隔離、乾淨安裝、模型、通道、重型路由、技能與健康出廠門檻 |

權威矩陣是 [`config/test_matrix.json`](config/test_matrix.json)，詳細說明見 [`docs/TESTING_SYSTEM.md`](docs/TESTING_SYSTEM.md)。請勿只挑一個 pytest 結果就宣稱完成出廠驗證。

## 系統架構

```text
Web / LINE / Discord / Telegram
               |
          訊息處理管線
               |
        意圖封裝與安全計畫
               |
      既有確定性路由 / ReAct 工具
               |
     工具副作用與確認／權限合約
               |
      驗證、重試、回復、人工接手
               |
 OSC / 檔案 / portal / 行事曆 / 模型
```

| 路徑 | 責任 |
|---|---|
| `api/agentic/` | 意圖、計畫、確認、副作用、工作階段、公開狀態與影子 Agent |
| `api/domains/calendar_agent*.py` | 自然語言行事曆解析與受控執行 |
| `api/pipelines/message_pipeline.py` | 跨通道訊息路由與確定性攔截器 |
| `api/orchestrator.py` | 既有協調流程與工具整合 |
| `api/blueprints/` | 網頁與 OSC API |
| `skills/` | 各領域工具與業務流程 |
| `gui/magi_menubar.py` | 選單列狀態與可複製診斷內容 |
| `scripts/ops/` | 健康、出廠、模型、備份、發布與自我修復 |

## 文件

- [一般使用者手冊](docs/USER_GUIDE.md)
- [目前操作手冊](docs/guides/MAGI_操作手冊.md)
- [商用上線檢核](docs/COMMERCIAL_READINESS.md)
- [公開版自行安裝](docs/PUBLIC_SELF_INSTALL.md)
- [公開版操作手冊](docs/PUBLIC_OPERATION_MANUAL.md)
- [私有版操作手冊](docs/PRIVATE_OPERATION_MANUAL.md)
- [管理員維運手冊](docs/OPERATOR_RUNBOOK.md)
- [測試與出廠制度](docs/TESTING_SYSTEM.md)
- [安全政策](SECURITY.md)
- [支援政策](SUPPORT.md)
- [服務條款](docs/TERMS_OF_SERVICE.md)
- [隱私權政策](docs/PRIVACY_POLICY.md)
- [資料保留政策](docs/DATA_RETENTION_POLICY.md)
- [第三方套件清單](docs/THIRD_PARTY_BOM.md)

## 授權

請見 [`LICENSE`](LICENSE)。第三方元件仍依各自授權條款使用。
