# MAGI 修正紀錄 2026-04-02

## 法扶報結流程修正

### Parser 修正（`api/handlers/laf_handler.py`）
- 「準備」「案件」等非人名詞彙不再被誤判為 client_name
- 含法扶案號格式（XXXXXXX-X-XXX）的訊息自動識別為法扶指令
- 「結案」加入 looks_like_laf 觸發詞

### Identity Lookup 修正（`laf_orchestrator.py`）
- 法扶案號或案件編號完全匹配時，client_name 不符不再阻擋
- 錯誤訊息改為引導式（告訴使用者缺什麼、怎麼補）

### 報結表單修正（`laf_automation_v2.py` + `laf_orchestrator.py`）
- 案號欄位 relcode/relno 改為無條件覆蓋（不再跳過已有值的欄位）
- 案號 regex 支援 `年` 不帶「度」（如憲法法庭 115年審裁字第578號）
- 法院選單：憲法法庭走「其他」+ 手動填入
- 結案類型：加入「法院裁定 → 憲法訴訟程序」路徑
- fields override：disc_times/document_count 可覆蓋自動統計值
- .odt 檔案自動轉 PDF 上傳

### 通知頻道修正
- 報結暫存成功/失敗通知加上 `topic_key="laf_closing"`
- 不再 fallback 到一般頻道

### 管理員限制移除（`api/orchestrator.py`）
- 以下業務指令開放一般使用者：
  - laf_go_live, laf_fee, laf_inquiry, laf_withdrawal
  - judgment_search, judgment_collect, judgment_daily_crawl
  - db_backup, calendar_sync, autopilot_tick/nightly/self_test

## 實務見解改善（`skills/judgment-collector/action.py`）
- 移除 prompt 範例字號（防止 LLM 幻覺）
- sanitizer 加入 CoT 推理洩漏清理
- 低價值判決過濾：支付命令、本票裁定、司執、補費、附民、續收、司催、司消債核
- 最高法院/高等法院全部保留，不過濾
- `ingest_raw_judgments.py`：快速入庫腳本，跳過低價值判決
- DB 從 1,610 筆增長到 15,534 筆有價值判決

## PDF 命名精準度改善
- `vision_parser.py`：Vision port 改為 8082（oMLX vision）
- `action.py`：Vision API 端點修正
- `rename_watcher.py`：新增 — 監控案件資料夾更名事件，自動學習命名規則
- `nightly_train.py`：PYTHONPATH 修正

## 夜間守護機制（`scripts/run_nightly_guardian.sh`）
- 取代 macOS cron（cron 因 TCC 權限停止工作）
- 統一管理所有夜間排程任務：
  - 22:00 autopilot nightly
  - 23:00 PDF namer 訓練
  - 00:30 司法院 API 夜拉 + 入庫
  - 02:30 法扶夜間巡檢
  - 03:00 夜間巡邏
  - 03:30 三哲人議會
  - 06:30 晨報入庫 + 健康報告
  - 每小時 DB sync
  - 週末見解庫回填
  - 常駐 PDF 更名學習器

## DB 同步機制（`scripts/db_sync_to_remote.py`）
- 遠端 → 本機同步（每 10 分鐘）
- 同步前自動備份（保留 72 小時）
- bash 守護自動重啟
