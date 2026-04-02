---
name: trial-prep
description: 開庭準備自動化 — 依開庭日期自動彙整案件資料、產生開庭備忘
author: CASPER
created: 2026-03-16
---

# trial-prep

開庭準備自動化技能，根據開庭日期自動收集案件相關資料，產生開庭備忘清單。

## 指令

| 指令 | 說明 |
|------|------|
| `--task upcoming [--days 7]` | 列出未來 N 天的開庭排程 |
| `--task prepare --text "案號"` | 針對指定案號產生開庭準備備忘 |
| `--task checklist --text "案號"` | 產生開庭前確認清單（文件、證據、證人） |
| `--task timeline --text "案號"` | 產生案件時間軸摘要 |

## 流程

### upcoming — 排程查詢
1. 查詢 Apple Calendar「開庭」相關事件（未來 N 天）
2. 解析案號、法院、庭別
3. 依日期排序輸出

### prepare — 開庭準備
1. 從案號查詢案件資料夾（NAS 路徑）
2. 掃描資料夾內的書狀、筆錄、證據
3. 查詢相關法條（statutes-vdb）
4. 查詢相關判決見解（judgment-collector）
5. 產生開庭備忘：
   - 案件基本資訊（當事人、案由、承審法官）
   - 本次庭期重點（歷次筆錄摘要、待釐清爭點）
   - 需準備文件清單
   - 相關法條提示
   - 相關判決見解

### checklist — 確認清單
1. 掃描案件資料夾
2. 比對書狀 vs 法院收文確認
3. 列出已備妥 / 待補文件
4. 證人出庭確認提醒

## 依賴
- `apple` skill（行事曆查詢）
- `statutes-vdb` skill（法條查詢）
- `judgment-collector` skill（見解查詢）
- `transcript-indexer` skill（筆錄搜尋）
- NAS 案件資料夾

## LINE/DC 指令格式
- 「開庭準備」「下週開庭」→ upcoming
- 「準備 112年度勞訴字第XXX號」→ prepare
- 「開庭清單 案號」→ checklist
