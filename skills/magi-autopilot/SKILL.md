---
name: magi-autopilot
description: 三哲人自動巡檢與全流程自動化（法扶/信件/閱卷/筆錄/PDF 歸檔/待辦/爬蟲）。只有卡住或需要人工時才發 LINE。
author: CASPER
created: 2026-02-16
triggers:
  - "自動巡檢"
  - "自動處理"
  - "自動跑流程"
  - "全自動"
  - "卡住再 LINE"
  - "排程巡檢"
  - "夜間任務"
  - "每日巡檢"
  - "法扶自動化"
  - "閱卷自動化"
  - "筆錄同步"
  - "待辦同步"
---

# magi-autopilot

把 MAGI/CASPER 的多個技能串成「流程型自動巡檢」，預設靜默執行；只有遇到需要人工或不確定（例如 OAuth `invalid_grant`、驗證碼辨識失敗、登入失敗、案件歸檔歧義）才用 LINE 通知管理者。

## 指令

1. `help`
2. `self_test`
3. `tick`：輕量巡檢（適合每 2 小時）
4. `nightly`：夜間重任務（適合每日凌晨）

## 行為概要

- `tick`
  - 法扶 Gmail 一次掃描（法扶信件 + 一般專員來信規則）
  - 閱卷 Gmail 通知「預覽」（不下載、不送件）
  - PDF 掃描命名歸檔（`pdf-namer --task file --execute 1 --notify 0`）
  - 待辦同步（`osc-orchestrator --task scan_cases` + `queue_flush`）

- `nightly`
  - 包含 `tick` 全部步驟
  - 閱卷下載（`file-review-orchestrator --task download` + `check_emails`）
  - 筆錄同步（`transcript-downloader --task sync`）
  - 判決爬取（`judgment-collector --task daily_crawl`）

## 產出

每次執行都會落地報告到：
`/Users/ai/Desktop/code/_autopilot_runs/<timestamp>_<task>/report.json` 與 `report.txt`

## 安全策略

- 預設 `MAGI_NO_DELETE=1`，並透過 `/Users/ai/Desktop/code/safe_fs.py` 保護 Synology Drive：永不刪除，只會隔離重複檔。
- 任何對外系統的「送出/回報」動作不會在 `tick/nightly` 裡執行（只做掃描/下載/歸檔/入庫/預覽）。

