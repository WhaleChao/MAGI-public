---
name: magi-doctor
description: MAGI 全系統自我排查與檢修技能。掃描所有 Skill 可載入性、依賴套件完整性、基礎設施健康，並自動修復已知問題。
created: 2026-03-02
---

# MAGI Doctor (三哲人自我檢修)

**一站式**系統排查 Skill — 讓 MAGI 有能力自我診斷與修復。

## 功能

| 檢查層 | 說明 |
|--------|------|
| **Skill Import** | 掃描所有 `skills/*/action.py` 是否可 compile |
| **依賴套件** | 檢查 core dependencies 是否已安裝 |
| **基礎設施** | Ollama / Melchior / Balthasar / Keeper DB / 網路 |
| **排程** | 夜間排程 plist 是否存在且已載入 |
| **自動修復** | 整合 magi-self-repair 策略自動修復已知問題 |

## Usage

```bash
# 完整排查（不修復）
python skills/magi-doctor/action.py --task diagnose

# 排查 + 自動修復
python skills/magi-doctor/action.py --task heal

# 只產出報告
python skills/magi-doctor/action.py --task report
```

## 輸出

- JSON 報告存於 `static/doctor_report.json`
- 終端機人類可讀格式 (emoji)
