---
name: laf-withdrawal-report
description: 法扶「受扶助人撤回」回報技能（正式站、只暫存不送出）。可用自然語句或 JSON 指定姓名/案號/原因。
author: CASPER
created: 2026-02-22
---

# laf-withdrawal-report

## 功能
1. 走正式站撤回流程（`portal-draft + action=withdrawal`）。
2. 只做填寫與暫存，不做送出。
3. 自動回傳預覽快照路徑（HTML/PNG）供人工確認。

## 指令
1. `help`
2. `self_test`
3. `run {"client_name":"[當事人F]","reason":"申請人撤回"}`
4. `run 幫我做[當事人F]受扶助人撤回 原因 申請人撤回`

## 安全原則
1. 不允許送出按鈕。
2. 預設 `MAGI_NO_DELETE=1`。
3. 若缺少目標或原因，直接回錯誤，不猜測送出。
