# 2026-04-30 — LAF placeholder 案件自動修正（reconcile_placeholder_cases）

## 觸發案例
2026-0041 / 1150429-T-052 民事案件，派案 email 解析錯誤：
- DB `client_name = ")--案情文件"`（垃圾，從 email 附件名稱解析）
- DB `case_reason = "待確認"`（placeholder）
- 資料夾名 `2026-0041-)--案情文件-一審-待確認`

## 設計原則（依使用者指示）
1. 派案 email 不完整時 → **仍建立資料夾與 DB 記錄**（user 想看到案件存在）
2. **GO LIVE 不啟動**（條件還沒齊，啟動會送錯資料）
3. 接案清冊 Excel 為權威資料源 → 自動修正 DB（client_name / case_reason / case_stage）
4. 安全 rename 資料夾：lsof 偵測有人開啟 → DC 通知，但 DB 仍更新
5. 客戶名允許族名 `UTAK KUAD`（半形空白 + `-`），拒絕特殊字元
6. 觸發節流：1 小時最多一次（避免 portal Excel 匯出過頻）

## 變更檔案

### A. `casper_ecosystem/law_firm_orchestrators/laf_nightly_audit.py`
新增 helper 與主函數：
- `_is_placeholder_client_name(name)` — 規則：含 `)(<>[]{}!@#$%^&*+=|\;:"'?/`~`、`--`、含「案情/文件/卷宗/附件/信件/資料夾」、長度 > 30 → placeholder
- `_is_placeholder_case_reason(reason)` — 空 / `待確認` / `未確認` → placeholder
- `_is_folder_open_by_other(path)` — `lsof +D` 偵測，排除自身 process（python3 / lsof）
- `_safe_rename_case_folder(old, new)` — 4 道閘門：old 存在、new 不存在、folder 沒被開、`os.rename()` OK
- `_replace_folder_basename_canonical(path, new_basename)` — Z:/Y: canonical 路徑改最後一段
- `_check_reconcile_throttle()` / `_write_reconcile_state()` — 1 小時節流
- **`reconcile_placeholder_cases(db, force=False, only_laf_no="", notifier=None)`** — 主函數

整合到 nightly audit `run_audit()` Step 2c（緊跟在 backfill_from_case_list 之後）。

CLI 新增 `--mode reconcile_placeholder --laf-no <案號>` + `--force`。

### B. `casper_ecosystem/law_firm_orchestrators/laf_orchestrator.py::handle_go_live`
偵測 placeholder 案件 → 跳過 `execute_portal_go_live_draft`：
- `_is_placeholder_case` 標誌（規則 inline mirror nightly_audit 的 helper，避免 import 整 module 觸發 NAS 掛載副作用）
- 通知文案改寫：「⚠️ 派案 email 資料不完整（當事人/案由），已建立臨時資料夾。系統會每小時自動從接案清冊修正 DB 與資料夾名稱後再啟動開辦。」

### C. `skills/laf-orchestrator/action.py`
新增 `--task reconcile_placeholder` 入口，呼叫 laf_nightly_audit 函數。

## reconcile 流程
1. 找 placeholder 案件（`legal_aid_number != ''` 且 `client_name` 或 `case_reason` placeholder）
2. 登入 LAF portal → 匯出接案清冊 Excel
3. 用 `legal_aid_number` 比對 portal entry（不靠 client_name，因為它是垃圾）
4. UPDATE DB: `client_name`, `case_reason`, `case_stage`（後者只有原值是空/待確認/未確認時才覆蓋）
5. 找實體舊路徑（`local_synology_path_candidates` NAS/SynologyDrive 雙 fallback）
6. lsof 偵測 → 安全 rename
7. 若 rename 成功：UPDATE DB folder_path 為新 canonical 路徑
8. 若 folder 被開：DC 通知律師「請關閉 Finder 後重跑」（DB 仍已更新）
9. DC 通知律師每筆修正詳情

## 觸發點
1. **每晚 02:30 nightly audit** — `run_audit()` 自動執行（`force=True` 跳過節流）
2. **手動 CLI**（單筆，跳過節流）：
   ```bash
   python3 casper_ecosystem/law_firm_orchestrators/laf_nightly_audit.py \
     --mode reconcile_placeholder --laf-no 1150429-T-052
   ```
3. **批次 CLI**（多筆，受 1 小時節流；`--force` 跳過）：
   ```bash
   python3 .../laf_nightly_audit.py --mode reconcile_placeholder --force
   ```

## 驗收層級

### ✅ 測試（已通過）
- `py_compile` 全綠
- Placeholder 偵測：對 2026-0041 真實 row（`client_name=")--案情文件"`, `case_reason="待確認"`）回傳 `placeholder_count=1`
- DB 連線（osc.py force-reload 修補生效）
- portal login 流程進入到 OCR 驗證碼階段

### ⏸️ Live 驗收（暫卡 LAF portal captcha OCR）
2 次連續嘗試（15:36 + 15:47）都因 LAF portal captcha 識別連 8 次 < 4 字元而失敗（`OCR 未取得四碼`）。這是 LAF portal 端的暫時性問題（同日 02:30 nightly audit 跑 portal 是成功的）。

下次驗收時機：
- **A. 自然觸發**：今晚 02:30 nightly audit 跑 `reconcile_placeholder_cases` 自動修正 2026-0041
- **B. 手動重試**：captcha 順時手動跑 CLI 命令（不同小時 OCR 機率不同）

預期效果（OCR 順時）：
- DB `client_name` 從 `)--案情文件` → 真實名（從接案清冊 Excel）
- DB `case_reason` 從 `待確認` → 真實案由
- DB `case_stage` 從 `''` → 真實程序（一審/二審/...）
- 資料夾從 `2026-0041-)--案情文件-一審-待確認` → `2026-0041-{真實名}-{程序}-{案由}`
- DC 通知律師修正詳情

## 守則更新
- LAF placeholder 偵測規則（`_is_placeholder_client_name` / `_is_placeholder_case_reason`）一旦改動，**laf_orchestrator.py** 的 inline mirror 也要同步改
- 客戶名允許範圍：CJK + 半形空白 + 單個 `-`（族名如 `UTAK KUAD`、英文名如 `Mary-Ann`）；長度上限 30
- 接案清冊 Excel 是**唯一權威資料源** — 不靠 client_name 比對（垃圾）只靠 `applyno`
