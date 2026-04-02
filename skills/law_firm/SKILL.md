---
name: law_firm
description: Legal domain skills (Crawler, Analysis).
metadata:
  iron_dome: true
  dependencies: [mysql-connector-python]
---

# Law Firm Skills

## 1. Legal Crawler Wrapper (`legal_crawler_wrapper.py`)
- **Capabilities**: Run the OpenClaw legal crawler.
- **Safety**: Wraps existing trusted script.
- **Commands**:
  - `執行爬蟲`: 預設背景啟動，立即回覆 job id（避免阻塞主流程）。
  - `python legal_crawler_wrapper.py --task status --job-id latest`: 查詢背景狀態。
  - `python legal_crawler_wrapper.py --task run_sync`: 同步執行（給 cron/維運）。

## 2. Cortex Sync (`skills/memory/cortex_sync.py`)
- **Capabilities**: Sync crawler data to Vector DB.
- **Safety**: Read-only from source, local write to vector DB.
- **Commands**:
  - `執行同步`: Sync new data and embed.

## 3. Crawler Architect (`skills/law_firm/crawler_architect.py`)
- **Capabilities**: Self-modifying crawler generation using LLM.
- **Safety**: ⚠️ High Risk. Includes auto-backup and rollback. Writes only to `legal_crawler.py`.
- **Commands**:
  - `修改爬蟲 [需求]`: Generate and inject new crawler code.

## 背景任務說明（2026-03）

- 預設 `run_crawler()` 走背景模式，避免 LINE/API 長任務 timeout。
- 同步排程請改用 `--task run_sync`（例如 nightly cron）。
- 背景任務狀態檔位於：`skills/law_firm/_bg_jobs/`
