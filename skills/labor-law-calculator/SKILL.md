---
name: labor-law-calculator
description: 勞基法計算器 — 純 Python 計算加班費、特休假、資遣費，支援自然語言輸入、Excel/PDF 出勤紀錄解析與 Google Sheets 代算。
license: MIT
compatibility: Python 3.10+ (no LLM required)
metadata:
  author: MAGI
  version: "1.0"
  sage: keeper
---

# labor-law-calculator

台灣勞動基準法純 Python 計算技能，無需 LLM。涵蓋加班費（平日/休息日/例假日/國定假日，含一例一休前後各修法版本）、特休假天數、資遣費（舊制/新制/混合制）三大計算。每項計算均內建自我驗算機制。支援自然語言輸入、Excel/PDF 出勤明細紀錄批次計算、Google Sheets URL 自動代算。

## Capabilities

- **加班費計算** — 依月薪與假別（平日/休息日/例假日/國定假日/停班停課）計算加班加給，自動適用 2016/2018 修法版本
- **特休假計算** — 依到職日計算年度特休天數，涵蓋 6 個月至 25 年以上各級距
- **資遣費計算** — 支援舊制（2005/7/1 前）、新制、混合制，自動依年資切割計算
- **經常性薪資拆分** — 支援本薪 + 伙食津貼 + 全勤獎金 + 交通津貼 + 職務加給等各項經常性給與
- **歷年基本工資** — 內建 2015-2024 歷年法定基本月薪/時薪對照
- **Excel/PDF 出勤紀錄** — 解析打卡系統匯出的出席明細紀錄表 (xlsx) 或加班單 (PDF)，依日別自動分類加總
- **Google Sheets** — 支援公開分享試算表 URL，自動擷取 CSV 代算
- **自然語言解析** — 支援中文自然語言輸入（如「月薪50000，休息日加班3小時」）
- **自我驗算** — 每項計算結果均以獨立邏輯複算驗證一致性

## Usage

```bash
# 加班費
python action.py --task "月薪50000，休息日加班3小時"

# 特休假
python action.py --task "到職日2020-03-01，計算特休"

# 資遣費
python action.py --task "月薪45000，資遣費，到職2018-01-01，離職2026-03-07"

# Excel 出勤紀錄批次計算
python action.py --task "月薪42000 /path/to/attendance.xlsx"

# Google Sheets
python action.py --task "https://docs.google.com/spreadsheets/d/..."
```

## Commands

1. 加班費 — 提供月薪（或經常性薪資各項）+ 假別 + 時數
2. 特休假 — 提供到職日（自動以今日為計算基準）
3. 資遣費 — 提供月薪 + 到職日 + 離職日
4. Excel/PDF 批次計算 — 提供月薪 + 檔案路徑（支援多檔）
5. Google Sheets 代算 — 提供試算表 URL

## Dependencies

- 標準庫（無外部套件需求）
- `openpyxl`（僅 Excel 解析時需要）
- `PyMuPDF`（僅 PDF 加班單解析時需要）

## 呼叫格式
觸發詞：加班費、特休、資遣費、勞基法計算
參數：type=計算類型(overtime/leave/severance), salary=月薪, hours=加班時數(選填), years=年資(選填)

## 呼叫範例
使用者：月薪 35000 加班 3 小時多少錢
→ 勞基法 type=overtime salary=35000 hours=3

使用者：年資 5 年的特休有幾天
→ 勞基法 type=leave years=5

使用者：月薪 42000 年資 3 年的資遣費
→ 勞基法 type=severance salary=42000 years=3
