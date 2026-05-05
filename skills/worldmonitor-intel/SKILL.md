---
name: worldmonitor-intel
description: 全球情報監控 — 收集、分析國際情勢並產出報告。
---

# worldmonitor-intel — 全球情報監控

## 基本資訊
- **技能名稱**: worldmonitor-intel
- **功能**: 收集全球情報（新聞、地緣政治、市場動態），分析後產出摘要報告
- **觸發方式**: `cron_jobs.json` 的 `job_worldmonitor_intel`（每日）+ 手動指令
- **依賴**: MAGI mem_bridge (記憶), oMLX/TAIDE (推理)

## 使用方式

### 自動排程
每日由 cron scheduler 觸發 `@MAGI 執行全球情報收集`，對應 `cron_jobs.json` 的 `job_worldmonitor_intel`。

### 手動
在 TG/DC 輸入：`執行全球情報收集` 或 `全球情報`

## 技術架構
```
cron_jobs.json (每日)
    ↓ Discord cron scheduler
orchestrator → embedding router → worldmonitor-intel/action.py
    ↓ 網路抓取 + 推理分析
oMLX / TAIDE-12b
    ↓ 報告輸出
static/worldmonitor_reports/intel_YYYYMMDD_HHMMSS.md
    ↓ Web 存取
/intel (dashboard 內嵌面板)
```

## 輸出
- 報告存放：`static/worldmonitor_reports/`
- Web 面板：`/intel`（需登入）
- 純本地執行，不依賴外部雲端服務或 edge functions
- 若記憶層不可用或不相容，會安全降級寫入本機 `static/worldmonitor_reports/`
- 若部分新聞源失敗，報告會保留來源健康狀態與降級說明，不會直接中斷整份輸出
