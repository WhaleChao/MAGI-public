---
name: worldmonitor-intel
description: 全球情報監控 — 透過 worldmonitor 儀表板收集、分析、記憶國際情勢。
---

# worldmonitor-intel — 全球情報監控 MAGI 技能

## 基本資訊
- **技能名稱**: worldmonitor-intel
- **功能**: 透過 worldmonitor 全球情報儀表板收集、分析、記憶國際情勢
- **觸發方式**: 排程 (cron) + 手動指令
- **依賴**: worldmonitor API, MAGI mem_bridge (記憶), melchior_client (推理)

## 使用方式

### 自動排程
每 6 小時自動抓取一次全球情報摘要，儲存到 MAGI 記憶系統。

### 手動指令
```
/worldmonitor           # 取得最新全球情報摘要
/worldmonitor <region>  # 取得特定區域情報
/worldmonitor threats   # 取得威脅警報
```

## 技術架構
```
worldmonitor (vercel dev :3000)
    ↓ API calls
worldmonitor-intel skill (action.py)
    ↓ 推理分析
Melchior (Ollama/MoE)
    ↓ 記憶儲存
MAGI mem_bridge → LanceDB Pro
```
