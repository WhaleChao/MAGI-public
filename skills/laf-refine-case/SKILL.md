---
name: laf-refine-case
description: 法扶案件資訊（類型/階段）用 CASPER 參考 DB 現有案件做修正；可 headless 呼叫與自測。
author: CASPER
created: 2026-02-13
---

# laf-refine-case

## 指令
1. `help`
1. `self_test`
1. `refine { ... }`

`refine` payload（JSON）：
- `case_info`（必填，dict）
- `existing_cases`（可選，list；每筆含 case_number/case_type/case_stage/case_reason）

