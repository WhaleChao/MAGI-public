---
name: judicial-tools
description: 司法院辦案小工具集 — 純 Python 實作 17 項司法計算工具，包含規費試算、上訴期間、折舊、霍夫曼、利息違約金、刑度加重減輕、土地分割、繼承系統表等
author: MAGI
created: 2026-03-12
updated: 2026-03-12
metadata:
  version: "1.0"
  sage: keeper
  source: https://gdgt.judicial.gov.tw/judtool/MAINPAGE.htm
---

# judicial-tools

司法院辦案小工具的純 Python 離線實作。原始工具位於 `gdgt.judicial.gov.tw/judtool/`，本 skill 將全部 17 項計算功能離線化，無需連網即可使用。

## 工具清單

### 通用
| 代碼 | 指令 | 說明 |
|------|------|------|
| GDGT01 | `appeal_period` | 上訴抗告再審期間試算 |
| GDGT09 | `elapsed_time` | 經過時間試算（天/月/年） |
| GDGT08 | `judicial_fee` | 司法規費試算（114年新法） |
| GDGT24 | `judicial_fee_old` | 司法規費試算（113年底前舊法） |

### 民事
| 代碼 | 指令 | 說明 |
|------|------|------|
| GDGT02 | `depreciation` | 折舊自動試算（平均法/定率遞減法） |
| GDGT03 | `hoffman` | 霍夫曼一次給付試算 |
| GDGT04 | `severance` | 資遣費試算（→ 委派 labor-law-calculator） |
| GDGT07 | `annual_leave` | 特休日數試算（→ 委派 labor-law-calculator） |
| GDGT12 | `interest` | 利息及違約金試算 |
| GDGT20 | `co_owner_share` | 共有人應有部分比例 |

### 刑事
| 代碼 | 指令 | 說明 |
|------|------|------|
| GDGT22 | `sentence` | 法定刑度加重減輕試算 |

### 其他
| 代碼 | 指令 | 說明 |
|------|------|------|
| GDGT13 | `land_division` | 土地分割共有物面積與地價之試算 |
| GDGT14 | `land_partial` | 土地單筆部分維持共用/應有部分之試算 |
| GDGT15 | `land_merge` | 土地數筆合併後應有部分之試算 |
| GDGT16 | `unjust_enrichment` | 相當租金不當得利之試算 |
| GDGT18 | `penalty_interest` | 違約金與利息之試算 |
| GDGT19 | `inheritance` | 繼承系統表 |

## Usage

```bash
# 列出所有工具
python action.py --task "help"

# 司法規費試算
python action.py --task 'judicial_fee {"category":"民事","procedure":"訴訟事件","amount":1000000}'

# 上訴期間試算
python action.py --task 'appeal_period {"case_type":"民事","court":"TPD","appeal_type":"上訴","serve_date":"1150312","serve_method":"一般","location":"臺北市"}'

# 經過時間
python action.py --task 'elapsed_time {"start":"1140101","end":"1150312"}'

# 折舊試算
python action.py --task 'depreciation {"method":"平均法","cost":500000,"useful_years":5,"used_years":3,"used_months":6}'

# 霍夫曼一次給付
python action.py --task 'hoffman {"monthly_amount":30000,"rate":5,"years":10,"months":0}'

# 利息試算
python action.py --task 'interest {"principal":1000000,"rate":5,"start":"1140101","end":"1150312"}'

# 刑度加重減輕
python action.py --task 'sentence {"type":"有期徒刑","min_years":1,"max_years":7,"aggravations":[{"rule":"加重1/2","count":1}],"mitigations":[{"rule":"減輕1/2","count":1}]}'

# 土地分割
python action.py --task 'land_division {"parcels":[{"id":"A","area":1000,"price":50000,"owners":[{"name":"甲","numerator":1,"denominator":3},{"name":"乙","numerator":2,"denominator":3}]}]}'

# 繼承系統表
python action.py --task 'inheritance {"decedent":{"name":"王大明","birth":"0500101","death":"1140601"},"heirs":[{"name":"王小明","relation":"長子","birth":"0750301"},{"name":"李美麗","relation":"配偶","birth":"0520601"}]}'

# 不當得利
python action.py --task 'unjust_enrichment {"land_value":5000,"area":100,"share_n":1,"share_d":3,"rate":5,"start":"1090312","end":"1150312"}'
```

## Dependencies

- 標準庫（無外部套件）
- 資遣費/特休委派至 labor-law-calculator skill
