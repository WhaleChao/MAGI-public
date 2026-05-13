---
name: process-hygiene
description: MAGI 程序衛生檢查與清理。偵測殭屍程序、重複 daemon、孤兒子程序、port 佔用，並可自動修復。
created: 2026-03-17
---

# Process Hygiene (程序衛生)

偵測並清理 MAGI 系統中的殭屍程序、重複實例、孤兒程序等問題。

## 功能

| 檢查項 | 說明 |
|--------|------|
| **殭屍程序** | 偵測 defunct 程序，嘗試通知父程序 wait() 回收 |
| **重複 daemon** | 檢查是否有多個 daemon.py 實例 |
| **孤兒子程序** | MAGI 子程序的父程序已不存在 |
| **Port 佔用** | 檢查 MAGI 使用的 port 是否被非 MAGI 程序佔用 |
| **長時間卡死** | 偵測執行超過閾值的 MAGI 子程序 |

## Usage

```bash
# 完整掃描（只報告不修復）
python skills/process-hygiene/action.py --task scan

# 掃描 + 自動修復
python skills/process-hygiene/action.py --task clean

# 只處理殭屍程序
python skills/process-hygiene/action.py --task zombies

# 只處理重複 daemon
python skills/process-hygiene/action.py --task dedup
```

## 觸發詞

- 殭屍程序、zombie、defunct
- 重複程序、duplicate process
- 程序清理、process cleanup
- 程序衛生、process hygiene
