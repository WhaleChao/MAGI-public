# MAGI Google 日曆去重止血與定期巡檢計劃

日期：2026-04-22  
目標執行模型：GPT-5.3  
工作區：`/Users/ai/Desktop/MAGI_v2`

## 0. 執行摘要

本計劃的第一優先目標是「先止住 Google 日曆重複行程繼續增加」，不要一開始就清併 `case_todos`、`calendar_events` 或修改法扶報結統計公式。

目前 MAGI 的日曆混亂已經影響使用體驗與法扶統計可信度，但法扶、閱卷、筆錄等核心流程已屬穩定流程，直接改統計主線風險太高。因此採取低侵入順序：

1. 先讓 `gcal_sync` 變成 idempotent，同一案件、同一時間、同一行程類型只推送一筆 Google Calendar event。
2. 對現有 Google 日曆重複事件先做 dry-run audit 與備份，不預設刪除。
3. 新增每日定期巡檢任務，持續偵測重複、誤匯入與同步異常。
4. Google 日曆穩定後，再另案處理 `case_todos` / `calendar_events` canonical counts 與法扶統一統計公式。

## 1. 嚴格安全邊界

GPT-5.3 執行時必須遵守以下限制：

1. 本階段不得修改法扶 portal 自動填表、送出、附件上傳流程。
2. 本階段不得修改 `casper_ecosystem/law_firm_orchestrators/laf_orchestrator.py` 的 `_gather_case_counts()` 統計公式。
3. 本階段不得直接刪除 DB 內的 `case_todos`、`calendar_events`、`meetings` 歷史資料。
4. Google Calendar 既有重複事件的刪除必須分兩步：
   - 第一步只產生 dry-run 報告與完整 JSON 備份。
   - 第二步需明確指定 `--apply --confidence high --backup-path ...` 才允許刪除。
5. 所有新功能預設 dry-run 或 feature flag 關閉，確認後再啟用。
6. 若遠端 DB 不可達，允許 failover 到本機 DB，但報告必須寫明實際使用的 DB host。
7. 不要使用 destructive git 指令；不要 reset 或 checkout 使用者既有修改。

## 2. 已知問題與根因

### 2.1 `case_todos` 去重條件太窄

檔案：

- `skills/osc-orchestrator/osc_headless/db.py`
- 函式：`insert_case_todos()`

目前重複判斷包含 `source_file` 與 `description`。因此同一庭期如果來自不同 OSC 程式、不同 PDF 檔名、Vision parser、filename parser、manual input，就會被視為不同待辦。

本階段不直接大改 `case_todos` 的資料模型，但 Google Calendar 同步前必須自行判斷同一事件。

### 2.2 `gcal_sync` 直接 insert Google Calendar event

檔案：

- `skills/osc-orchestrator/action.py`
- 函式：`task_gcal_sync()`
- 函式：`_todo_to_gcal_event()`

目前邏輯對每筆 unsynced todo 直接 `service.events().insert()`，只要 DB 有 5 筆重複 todo，Google Calendar 就會出現 5 筆重複 event。

### 2.3 `gcal_import` 只以 Google event id 判斷

檔案：

- `skills/osc-orchestrator/action.py`
- 函式：`task_gcal_import()`

目前只用 `google_calendar_id` 判斷是否已匯入。若使用者手動複製事件、或同一事件存在多個日曆，event id 不同就可能被匯入為多筆 `case_todos`。

### 2.4 多個入口會寫入行程

高風險入口包括：

- `skills/osc-orchestrator/action.py`：PDF / folder scan / todo sync / gcal sync / gcal import
- `api/pipelines/skill_dispatch.py`：口語「排庭」、「排開會」
- `api/blueprints/osc_cases.py`：OSC 後台 todos 與 calendar events API
- `skills/pdf-namer/smart_filer.py`：歸檔後觸發 OSC todo sync 與 gcal sync

本階段只在 Google Calendar 同步層加最後守門，不要求一次統一所有寫入入口。

## 3. 第一階段目標

第一階段完成後，應達到：

1. DB 內即使存在重複 `case_todos`，Google Calendar 也不再新增重複 event。
2. 每個由 MAGI 推送的新 Google event 都帶有可追溯的 `extendedProperties.private.magi_dedup_key`。
3. `gcal_sync` 若發現既有相同事件，應回填該 todo 的 `google_calendar_id`，而非新增 event。
4. 已有 Google Calendar 混亂可被 audit 腳本列出，並區分 high / medium / low confidence。
5. 每日定期巡檢可產生日曆健康報告，並在異常時通知或留下報告檔。

## 4. Feature Flags

新增或使用以下環境變數。預設值必須保守。

```bash
MAGI_GCAL_DEDUP_ENABLED=0
MAGI_GCAL_DEDUP_DRY_RUN=1
MAGI_GCAL_DUP_AUDIT_ENABLED=1
MAGI_GCAL_DUP_AUDIT_APPLY=0
MAGI_GCAL_DUP_AUDIT_LOOKBACK_DAYS=730
MAGI_GCAL_DUP_AUDIT_LOOKAHEAD_DAYS=365
MAGI_GCAL_DUP_AUDIT_MIN_CONFIDENCE=high
MAGI_GCAL_DUP_AUDIT_OUTPUT_DIR=/Users/ai/Desktop/MAGI_v2/reports/gcal_dedup
```

啟用順序：

1. 先跑 `MAGI_GCAL_DEDUP_ENABLED=0`，只新增 audit，不改同步行為。
2. 再跑 `MAGI_GCAL_DEDUP_ENABLED=1` 且 `MAGI_GCAL_DEDUP_DRY_RUN=1`，確認會命中哪些既有 event，但不回填、不新增、不刪除。
3. 驗收通過後設 `MAGI_GCAL_DEDUP_ENABLED=1` 且 `MAGI_GCAL_DEDUP_DRY_RUN=0`，讓 `gcal_sync` 真正避免新增重複 event。

## 5. Dedup Key 設計

第一階段不強制新增 DB 欄位，先用程式即時計算 `magi_dedup_key`。

建議格式：

```text
v1|case:{case_key}|kind:{event_kind}|date:{yyyy-mm-dd}|time:{hh:mm_or_all_day}|subject:{normalized_subject_hash}
```

範例：

```text
v1|case:2025-0081|kind:hearing|date:2026-05-20|time:10:00|subject:7c4fa1c2
```

### 5.1 `case_key`

優先順序：

1. `case_number`，格式如 `2025-0081`
2. 法扶案號或申請案號，若目前 todo row 有可取得欄位
3. 法院案號，例如 `114年度訴字第000083號`
4. 當事人姓名，僅作 medium confidence，不可作 high confidence 自動刪除依據

注意：`case_number` 若只有 `2025`、`2026` 這種年份型字串，視為 invalid case key。

### 5.2 `event_kind`

建議分類：

```text
hearing: 開庭、準備程序、言詞辯論、審理程序、訊問、調解、協商程序
deadline: 補正、繳費、上訴、抗告、再抗告、異議、陳述意見、提出資料
meeting: 開會、會議、律見、接見、視訊會議、法律諮詢
review: 閱卷、影卷、調卷
contact: 電話聯繫、通話、電聯、聯繫
laf_admin: 法扶開辦末日、法扶行政提醒
other: 其他
```

### 5.3 `date` 與 `time`

1. 有 `todo_time` 時使用 `HH:MM`。
2. Google event 若是 timed event，取 `start.dateTime` 的本地時間 `HH:MM`。
3. 全天事件使用 `all_day`。
4. 對庭期類事件，時間相同才視為 high confidence duplicate。
5. 對期限類事件，日期相同即可視為 high confidence duplicate，但必須同案同類。

### 5.4 `normalized_subject`

正規化規則：

1. 移除 emoji 與前綴，例如 `⚖️`、`📝`。
2. 移除來源標記，例如 `[事務所日曆]`、`[Gmail]`、`(視覺辨識)`。
3. 移除常見檔名雜訊，例如 `.pdf`、`_20251014`、`(1)`。
4. 全形轉半形，去除多餘空白。
5. 移除已經在 `case_key` 中出現的案號。
6. 字串過長時先取核心欄位，再做 sha1 前 8 碼。

## 6. 建議新增模組

新增：

```text
skills/osc-orchestrator/osc_headless/gcal_dedup.py
```

需提供函式：

```python
def normalize_case_key(todo_or_event: dict) -> tuple[str, str]:
    """Return (case_key, confidence_source)."""

def classify_event_kind(text: str, todo_type: str = "") -> str:
    """Return hearing/deadline/meeting/review/contact/laf_admin/other."""

def normalize_subject(text: str, *, case_key: str = "") -> str:
    """Return normalized comparable subject."""

def build_dedup_key_from_todo(todo: dict, tz: str = "Asia/Taipei") -> str:
    """Build v1 MAGI dedup key from case_todos row."""

def build_dedup_key_from_gcal_event(event: dict, tz: str = "Asia/Taipei") -> str:
    """Build v1 MAGI dedup key from Google Calendar API event."""

def is_invalid_case_key(case_key: str) -> bool:
    """Reject bare years like 2025/2026 and empty keys."""

def confidence_for_match(a: dict, b: dict) -> str:
    """Return high/medium/low for audit and cleanup decisions."""
```

函式必須有單元測試，測試檔建議：

```text
tests/test_gcal_dedup.py
```

## 7. 修改 `gcal_sync`

目標檔案：

```text
skills/osc-orchestrator/action.py
```

### 7.1 `_todo_to_gcal_event()` 必須加入 metadata

在 `extendedProperties.private` 裡新增：

```json
{
  "magi_case_number": "...",
  "magi_todo_id": "...",
  "magi_dedup_key": "...",
  "magi_source": "osc_gcal_sync",
  "magi_created_by": "MAGI"
}
```

若 feature flag 關閉，也可以安全加入 `magi_dedup_key`，因為這只會增加 metadata，不改變使用者可見內容。

### 7.2 insert 前查重

新增 helper：

```python
def _find_existing_gcal_event(service, calendar_id: str, body: dict, dedup_key: str, tz: str) -> dict | None:
    """
    Return existing Google Calendar event if it matches the same MAGI dedup key
    or same case/date/time/kind high-confidence fallback.
    """
```

查找順序：

1. 用 Google Calendar private extended property 查：

```python
service.events().list(
    calendarId=calendar_id,
    privateExtendedProperty=f"magi_dedup_key={dedup_key}",
    singleEvents=True,
    maxResults=10,
).execute()
```

2. 若查不到，用時間窗查：
   - timed event：start 前後 2 小時
   - all-day / deadline：當日 00:00 到隔日 00:00
3. 對時間窗內 event 逐筆計算 dedup key 或 high-confidence fallback。
4. 找到 high-confidence match 就回傳 existing event。

### 7.3 遇到既有 event 時不要 insert

在 `task_gcal_sync()` 中，原本：

```python
res = service.events().insert(calendarId=calendar_id, body=body).execute()
```

改成：

```python
existing = _find_existing_gcal_event(...)
if existing:
    event_id = existing["id"]
    set_todo_google_calendar_id(...)
    dedup_matched += 1
    continue
else:
    res = service.events().insert(...)
```

回傳 JSON 新增欄位：

```json
{
  "dedup_enabled": true,
  "dedup_dry_run": false,
  "dedup_matched": 0,
  "dedup_would_match": 0,
  "inserted": 0,
  "failed": 0
}
```

### 7.4 dry-run 行為

若：

```bash
MAGI_GCAL_DEDUP_DRY_RUN=1
```

則：

1. 可以查 Google Calendar。
2. 不可以 insert。
3. 不可以 update DB。
4. 回傳 `dedup_would_match`、`would_insert`。

## 8. 修改 `gcal_import`

目標檔案：

```text
skills/osc-orchestrator/action.py
```

函式：

```text
task_gcal_import()
```

最低限度修改：

1. 取得 events 後，先建立 `seen_dedup_keys`。
2. 如果同一輪 import 已看過同 key，跳過並記錄 `dedup_skipped_in_batch`。
3. 查 DB 時不只查 `google_calendar_id`，也查同案同日同時間同類型的既有 `case_todos`。
4. 若 `case_key` 是 bare year，例如 `2025`、`2026`，不要寫入 `case_number`；可放在 description 或 raw note。

此處不要求大規模 refactor，只要避免 Google Calendar 手動複製事件繼續回灌成更多 DB 待辦。

## 9. 新增 Google Calendar 重複盤點腳本

新增：

```text
scripts/audit_gcal_duplicates.py
```

指令介面：

```bash
./venv/bin/python3 scripts/audit_gcal_duplicates.py --dry-run
./venv/bin/python3 scripts/audit_gcal_duplicates.py --lookback-days 730 --lookahead-days 365 --output-dir reports/gcal_dedup
./venv/bin/python3 scripts/audit_gcal_duplicates.py --apply --confidence high --backup-path reports/gcal_dedup/YYYYMMDD_HHMMSS/events_backup.jsonl
```

預設必須 dry-run。

### 9.1 報告內容

輸出：

```text
reports/gcal_dedup/YYYYMMDD_HHMMSS/summary.md
reports/gcal_dedup/YYYYMMDD_HHMMSS/duplicates.json
reports/gcal_dedup/YYYYMMDD_HHMMSS/events_backup.jsonl
```

`summary.md` 至少包含：

1. 掃描時間與時區。
2. 掃描 calendar IDs 與 calendar summaries。
3. raw events 數量。
4. duplicate groups 數量。
5. high / medium / low confidence 數量。
6. 建議刪除候選清單。
7. 不建議自動刪除清單與理由。

`duplicates.json` 每組至少包含：

```json
{
  "dedup_key": "...",
  "confidence": "high",
  "reason": "same_calendar_same_case_kind_date_time",
  "calendar_id": "...",
  "canonical_event_id": "...",
  "duplicate_event_ids": ["..."],
  "events": []
}
```

### 9.2 自動刪除允許條件

只有全部符合才可刪：

1. `--apply` 明確指定。
2. `--confidence high` 明確指定。
3. 同一 calendar 內重複。
4. dedup key 有有效 `case_key`。
5. 同一 event kind。
6. 同一 start date。
7. timed event 必須同一 start time。
8. 備份檔已成功寫入。
9. 不是 recurring master event；若是 recurrence instance，必須確認刪除單一 instance。

以下只列報告，不自動刪：

1. 跨不同 calendar 的重複。
2. 沒有有效案號，只有姓名推測。
3. 同日不同時間。
4. title 類似但 kind 不同。
5. 非 MAGI metadata 且無案號線索。

## 10. 定期任務巡檢

日曆必須加入定期任務排查。本階段建議先加入 nightly audit，不直接刪除。

### 10.1 巡檢頻率

建議每日一次，在既有 MAGI nightly/autopilot 流程中執行。

建議時間：

```text
每日 08:10 Asia/Taipei
```

理由：

1. 目前使用者訊息提到 `gcal_sync` 會在今日 08:00 自動同步。
2. 08:10 巡檢可以檢查剛同步後是否產生重複。
3. 不和夜間法扶、筆錄、PDF 任務搶主要時段。

### 10.2 建議整合點

優先整合到：

```text
skills/magi-autopilot/action.py
```

目前檔案中已有：

- `osc_gcal_sync`
- `osc_gcal_import`

請在 gcal sync/import 之後新增一個 read-only step：

```text
osc_gcal_duplicate_audit
```

該 step 執行：

```bash
./venv/bin/python3 scripts/audit_gcal_duplicates.py \
  --dry-run \
  --lookback-days "${MAGI_GCAL_DUP_AUDIT_LOOKBACK_DAYS:-730}" \
  --lookahead-days "${MAGI_GCAL_DUP_AUDIT_LOOKAHEAD_DAYS:-365}" \
  --output-dir "${MAGI_GCAL_DUP_AUDIT_OUTPUT_DIR:-/Users/ai/Desktop/MAGI_v2/reports/gcal_dedup}"
```

### 10.3 巡檢告警條件

每日報告若符合以下任一條件，應標示異常：

1. high confidence duplicate groups > 0
2. 新增 high confidence duplicates 比昨日增加
3. bare year case number 匯入數 > 0，例如 `case_number = 2025` 或 `2026`
4. `gcal_sync` 本輪 inserted > 0 且 audit 發現同時間重複
5. Google Calendar OAuth 失效
6. 掃描 calendar 數量異常變少，例如只剩 primary

### 10.4 巡檢輸出

至少輸出：

```text
reports/gcal_dedup/latest_summary.md
reports/gcal_dedup/YYYYMMDD_HHMMSS/summary.md
reports/gcal_dedup/YYYYMMDD_HHMMSS/duplicates.json
```

若現有 MAGI 通知機制可用，可發送簡短通知到管理員或 check topic。通知只需包含：

```text
Google 日曆巡檢：
- high duplicates: N
- medium duplicates: N
- invalid case imports: N
- report: reports/gcal_dedup/latest_summary.md
```

不要在通知中貼完整事件描述，避免個資外洩。

## 11. 測試計劃

新增或更新測試：

```text
tests/test_gcal_dedup.py
tests/test_osc_gcal_sync_dedup.py
tests/test_gcal_duplicate_audit.py
```

### 11.1 單元測試案例

必測：

1. 同案號、同類型、同日期、同時間，title 不同但核心相同，dedup key 相同或 high confidence match。
2. 同案號、同日期、不同時間，不應 high confidence match。
3. `2025` / `2026` bare year 不可作有效 case key。
4. `⚖️ 王小明 2025-0081 開庭` 與 `[事務所] 開庭 2025-0081 王小明` 可正規化為相同核心。
5. deadline all-day 同案同日同類型只算 duplicate。
6. 跨 calendar duplicate 只能列報告，不可自動刪。
7. dry-run 不 insert、不 update DB、不 delete event。

### 11.2 Mock Google Calendar API

不要在單元測試打真實 Google API。

請用 fake service object 模擬：

```python
service.events().list(...).execute()
service.events().insert(...).execute()
service.events().delete(...).execute()
```

驗證：

1. 找到 existing event 時不呼叫 insert。
2. 未找到 existing event 時才呼叫 insert。
3. dry-run 時 insert/delete/update 都不呼叫。

### 11.3 Live 驗收

Live 驗收必須分階段：

1. 只跑 audit：

```bash
./venv/bin/python3 scripts/audit_gcal_duplicates.py --dry-run --lookback-days 30 --lookahead-days 180
```

2. gcal sync dry-run：

```bash
MAGI_GCAL_DEDUP_ENABLED=1 MAGI_GCAL_DEDUP_DRY_RUN=1 \
./venv/bin/python3 skills/osc-orchestrator/action.py --task 'gcal_sync {"limit": 20}'
```

3. gcal sync apply：

```bash
MAGI_GCAL_DEDUP_ENABLED=1 MAGI_GCAL_DEDUP_DRY_RUN=0 \
./venv/bin/python3 skills/osc-orchestrator/action.py --task 'gcal_sync {"limit": 20}'
```

4. apply 後再跑 audit，確認 high duplicate 不再因本輪 sync 增加。

## 12. 回滾方案

### 12.1 關閉 dedup sync

立即設定：

```bash
MAGI_GCAL_DEDUP_ENABLED=0
MAGI_GCAL_DEDUP_DRY_RUN=1
```

此時 `gcal_sync` 回到舊行為，或至少不啟用 dedup 阻擋。

### 12.2 停止定期巡檢

設定：

```bash
MAGI_GCAL_DUP_AUDIT_ENABLED=0
```

或移除 autopilot 中的 `osc_gcal_duplicate_audit` step。

### 12.3 還原被刪除的 Google event

若曾使用 `--apply` 刪除 high confidence duplicates，必須使用備份檔：

```text
reports/gcal_dedup/YYYYMMDD_HHMMSS/events_backup.jsonl
```

新增還原腳本可作第二階段工作：

```text
scripts/restore_gcal_events.py
```

本計劃第一輪不要求實作 restore，但 audit apply 前必須確認備份完整。

## 13. 第一階段驗收標準

完成第一階段的最低驗收：

1. `pytest tests/test_gcal_dedup.py tests/test_osc_gcal_sync_dedup.py tests/test_gcal_duplicate_audit.py -q` 通過。
2. `audit_gcal_duplicates.py --dry-run` 可產生報告，不改 Google Calendar。
3. `gcal_sync` 在 mock 測試中遇到既有同 key event 不 insert。
4. `gcal_sync` 回傳包含 `dedup_matched` / `dedup_would_match` / `would_insert` 等欄位。
5. 新增 Google event 具有 `extendedProperties.private.magi_dedup_key`。
6. 定期巡檢 step 已接入 nightly/autopilot，且預設 dry-run。
7. 沒有修改法扶 portal、報結統計公式、閱卷、筆錄核心流程。

## 14. 第二階段預告，不在本輪執行

Google 日曆止血穩定後，再另案處理：

1. `case_todos` canonical map。
2. `calendar_events` canonical map。
3. 法扶統計統一公式。
4. 報結數字與 Google Calendar / DB / folder evidence 三方對照。
5. 歷史 DB 軟合併。
6. Google Calendar 歷史重複事件正式清理。

本輪 GPT-5.3 不應主動進入第二階段，除非使用者另行確認。

## 15. 建議 GPT-5.3 執行順序

請 GPT-5.3 依序執行：

1. 建立新 branch：

```bash
git checkout -b codex/gcal-dedup-stopgap
```

2. 先只讀檢查目前檔案：

```bash
rg -n "task_gcal_sync|task_gcal_import|_todo_to_gcal_event|google_calendar_id" skills/osc-orchestrator/action.py skills/osc-orchestrator/osc_headless/db.py
```

3. 新增 `gcal_dedup.py` 與單元測試。
4. 修改 `_todo_to_gcal_event()`，加入 `magi_dedup_key` metadata。
5. 修改 `task_gcal_sync()`，新增 insert 前查重與 dry-run。
6. 修改 `task_gcal_import()`，避免同輪與明顯既有事件重複匯入。
7. 新增 `scripts/audit_gcal_duplicates.py`。
8. 接入 `skills/magi-autopilot/action.py` nightly read-only audit step。
9. 跑測試。
10. 跑 live dry-run audit。
11. 回報：
    - 改了哪些檔案
    - 測試結果
    - dry-run audit 摘要
    - 是否建議啟用 `MAGI_GCAL_DEDUP_ENABLED=1`

## 16. 禁止事項清單

GPT-5.3 不得做以下事情：

1. 不得為了通過測試而刪除既有測試。
2. 不得直接清空 Google Calendar。
3. 不得直接刪除 DB 重複列。
4. 不得在沒有備份時刪除 Google Calendar event。
5. 不得更改法扶報結 portal submit 行為。
6. 不得把 dedup 規則寫死成單一案件或單一當事人。
7. 不得使用只靠 title 完全相等的去重作為唯一依據。
8. 不得把跨 calendar 的事件預設刪除，因為可能是刻意同步到個人與事務所日曆。

## 17. 成功狀態

本計劃成功時，MAGI 的狀態應為：

1. Google 日曆不再被新的重複待辦污染。
2. 使用者可以每天看到日曆巡檢報告。
3. 舊資料仍完整保留，必要時可回滾。
4. 後續統一法扶統計公式時，有乾淨的 Google Calendar 參照來源。

## 18. 執行狀態（2026-04-22）

以下為本輪已實際完成項目：

- [x] 新增 `skills/osc-orchestrator/osc_headless/gcal_dedup.py`
- [x] 修改 `skills/osc-orchestrator/action.py`：
  - `gcal_sync` 加入 dedup、dry-run、回填既有 event id
  - `_todo_to_gcal_event()` 寫入 `magi_dedup_key` 等 metadata
  - `gcal_import` 加入 in-batch dedup、DB pre-check、invalid case key 保護
- [x] 新增 `scripts/audit_gcal_duplicates.py`（預設 dry-run）
- [x] 修改 `skills/magi-autopilot/action.py`，接入 nightly `osc_gcal_duplicate_audit`
- [x] 新增測試：
  - `tests/test_gcal_dedup.py`
  - `tests/test_osc_gcal_sync_dedup.py`
  - `tests/test_gcal_duplicate_audit.py`

本輪驗證結果：

- `12 passed`：`tests/test_gcal_dedup.py tests/test_osc_gcal_sync_dedup.py tests/test_gcal_duplicate_audit.py`
- `audit_gcal_duplicates.py --dry-run` 成功輸出報告
- `gcal_sync` dedup dry-run 已實跑：
  - `fetched=1`, `inserted=0`, `would_insert=1`, `dedup_would_match=0`
  - 本次 DB 連線為 failover 到本機 `127.0.0.1:3306`（遠端 `100.121.61.74:3306` 不可達）
- 已確認啟動 A 方案（觀測模式）：
  - `MAGI_GCAL_DEDUP_ENABLED=1`
  - `MAGI_GCAL_DEDUP_DRY_RUN=1`
  - `MAGI_GCAL_DUP_AUDIT_ENABLED=1`
  - `MAGI_GCAL_DUP_AUDIT_APPLY=0`
  - `scripts/run_nightly_guardian.sh` 已加入上述預設值（可由外部 env 覆蓋）
  - `daemon.py` 也已加入同組 `setdefault`，供 daemon 啟動子程序時繼承（可由 `.env` 覆蓋）

最新報告路徑：

- `reports/gcal_dedup/20260422_161728/summary.md`
- `reports/gcal_dedup/20260422_161728/duplicates.json`
- `reports/gcal_dedup/20260422_161728/events_backup.jsonl`
- `reports/gcal_dedup/latest_summary.md`

## 19. 上線操作清單（可直接給 GPT-5.3）

### 19.1 第一階段：觀測模式（建議先跑 1-2 天）

```bash
export MAGI_GCAL_DEDUP_ENABLED=1
export MAGI_GCAL_DEDUP_DRY_RUN=1
export MAGI_GCAL_DUP_AUDIT_ENABLED=1
export MAGI_GCAL_DUP_AUDIT_APPLY=0
export MAGI_GCAL_DUP_AUDIT_MIN_CONFIDENCE=high
```

執行：

```bash
./venv/bin/python3 skills/osc-orchestrator/action.py --task 'gcal_sync {"limit": 20}'
./venv/bin/python3 scripts/audit_gcal_duplicates.py --dry-run --lookback-days 30 --lookahead-days 180
```

確認：

1. `gcal_sync` 回傳 `dedup_would_match` 或 `would_insert` 皆合理。
2. `latest_summary.md` 可讀取且無異常增加。
3. 沒有法扶、閱卷、筆錄流程回歸問題。

### 19.2 第二階段：止血生效

```bash
export MAGI_GCAL_DEDUP_ENABLED=1
export MAGI_GCAL_DEDUP_DRY_RUN=0
export MAGI_GCAL_DUP_AUDIT_ENABLED=1
export MAGI_GCAL_DUP_AUDIT_APPLY=0
```

執行：

```bash
./venv/bin/python3 skills/osc-orchestrator/action.py --task 'gcal_sync {"limit": 20}'
./venv/bin/python3 scripts/audit_gcal_duplicates.py --dry-run --lookback-days 30 --lookahead-days 180
```

驗收：

1. 同一批資料下，`inserted` 下降，`dedup_matched` 上升。
2. audit 報告中 high duplicates 不再因新同步而增加。

### 19.3 第三階段（選配）：高信心清理舊重複

只在人工審核報告後執行：

```bash
./venv/bin/python3 scripts/audit_gcal_duplicates.py \
  --apply \
  --confidence high \
  --lookback-days 730 \
  --lookahead-days 365
```

注意：執行前必須確認 `events_backup.jsonl` 已生成且可讀。
