# 2026-05-07 — 消債補件資料改走 OSC 案件補正清單

## 修正內容

- 新增 `POST /api/osc/debt/supplement-checklist`，將消債補件項目同步到 `case_checklists`。
- 產生消債補件書狀（`form_type=supplement`）時，若 payload 有 `items` / `supplement_items` / `pending_items` / `missing_items`，會一併同步案件補正清單。
- 案件工作台的「待補/待辦件數」現在會同時計入 `legal_aid_checklists` 與 `case_checklists`。

## OSC 邏輯

- `case_todos`：有日期、時間、提醒或行程性質的待辦。
- `case_checklists`：案件/當事人待補資料，例如消債補件項目、附件、期間資料。
- 消債當事人的待補資料不再混入行程待辦，以免問行程時顯示補件資料或分類錯亂。

## 驗證

- `tests/test_osc_debt_supplement_checklist.py`
- 驗證補件同步 SQL 寫入 `case_checklists`，且不寫入 `case_todos`。
