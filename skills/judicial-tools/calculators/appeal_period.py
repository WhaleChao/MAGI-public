#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""上訴抗告再審期間試算

依據民事訴訟法、刑事訴訟法、行政訴訟法，
計算上訴、抗告、再審之法定期間及截止日期。

考量因素：
- 案件類型（民事/刑事/行政）對應不同法定期間
- 送達方式：寄存送達 +10天、公示送達 +20天（國內）/+60天（國外）
- 在途期間：依法院所在地加計
- 期間末日如遇假日順延至次一工作日
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Optional

from .date_utils import (
    roc_to_date,
    date_to_roc,
    date_to_roc_display,
    is_holiday,
    next_business_day,
)


# ─── 法定期間（天） ──────────────────────────────────────────────────────────────

# (案件類型, 上訴類型) → 基本天數
_BASE_PERIODS: dict[tuple[str, str], int] = {
    ("民事", "上訴"): 20,
    ("民事", "抗告"): 10,
    ("民事", "再審"): 30,
    ("刑事", "上訴"): 20,
    ("刑事", "抗告"): 10,
    ("刑事", "再審"): 30,
    ("行政", "上訴"): 20,
    ("行政", "抗告"): 10,
    ("行政", "再審"): 30,
}

# 刑事案件高等法院上訴期間為 10 天
_CRIMINAL_HIGH_COURT_APPEAL_DAYS = 10


# ─── 在途期間（簡化表） ─────────────────────────────────────────────────────────

# 法院代碼 → 在途期間天數
# 主要法院的在途期間（依司法院公告）
_TRANSIT_DAYS: dict[str, int] = {
    # 最高法院 / 最高行政法院（位於臺北）
    "TPS": 0, "TPA": 0,
    # 臺灣高等法院
    "TPH": 0,
    # 高等法院分院
    "TCH": 1,   # 臺中分院
    "TNH": 2,   # 臺南分院
    "KSH": 2,   # 高雄分院
    "HLH": 4,   # 花蓮分院
    # 地方法院 — 北部
    "TPD": 0,   # 臺北地院
    "SLD": 0,   # 士林地院
    "PCD": 0,   # 新北地院
    "ILD": 2,   # 宜蘭地院
    "KLD": 1,   # 基隆地院
    "TYD": 1,   # 桃園地院
    "SCD": 1,   # 新竹地院
    # 地方法院 — 中部
    "MLD": 2,   # 苗栗地院
    "TCD": 1,   # 臺中地院
    "CHD": 2,   # 彰化地院
    "NTD": 2,   # 南投地院
    "ULD": 2,   # 雲林地院
    # 地方法院 — 南部
    "CYD": 2,   # 嘉義地院
    "TND": 2,   # 臺南地院
    "KSD": 2,   # 高雄地院
    "CTD": 3,   # 橋頭地院
    "PTD": 3,   # 屏東地院
    # 地方法院 — 東部與離島
    "HLD": 4,   # 花蓮地院
    "TTD": 4,   # 臺東地院
    "PHD": 4,   # 澎湖地院
    "KMD": 4,   # 金門地院
    "LCD": 4,   # 連江地院（馬祖）
}


# ─── 送達方式加計天數 ─────────────────────────────────────────────────────────

def _serve_addition(serve_method: str, location_type: str) -> int:
    """依送達方式計算加計天數

    Args:
        serve_method: 一般 / 寄存送達 / 公示送達
        location_type: 臺灣地區 / 國外

    Returns:
        加計天數
    """
    if serve_method == "寄存送達":
        return 10
    elif serve_method == "公示送達":
        if location_type == "國外":
            return 60
        return 20
    return 0


# ─── 主計算函式 ───────────────────────────────────────────────────────────────

def calc_appeal_period(
    case_type: str,
    court: str,
    appeal_type: str,
    serve_date: str,
    serve_method: str = "一般",
    location_type: str = "臺灣地區",
    extra_transit_days: int = 0,
) -> dict:
    """上訴/抗告/再審期間試算

    Args:
        case_type: 案件類型 — 民事 / 刑事 / 行政
        court: 法院代碼（如 TPD, KSD）或法院名稱
        appeal_type: 上訴 / 抗告 / 再審
        serve_date: 送達日期（民國年 YYYMMDD 或 YYY/MM/DD）
        serve_method: 送達方式 — 一般 / 寄存送達 / 公示送達
        location_type: 地區 — 臺灣地區 / 國外
        extra_transit_days: 額外在途期間天數（手動加計）

    Returns:
        dict 包含 base_days, transit_days, serve_addition,
             total_days, serve_date, raw_deadline, final_deadline,
             holiday_extended, breakdown
    """
    # 解析送達日期
    serve_dt = roc_to_date(serve_date)

    # 查詢基本期間
    key = (case_type, appeal_type)
    base_days = _BASE_PERIODS.get(key)
    if base_days is None:
        raise ValueError(f"不支援的案件類型/上訴類型組合: {case_type}/{appeal_type}")

    # 刑事高院上訴特例
    court_upper = court.upper().strip()
    if case_type == "刑事" and appeal_type == "上訴" and court_upper in ("TPH", "TCH", "TNH", "KSH", "HLH"):
        base_days = _CRIMINAL_HIGH_COURT_APPEAL_DAYS

    # 在途期間
    transit_days = _TRANSIT_DAYS.get(court_upper, 0) + extra_transit_days

    # 送達方式加計
    serve_add = _serve_addition(serve_method, location_type)

    # 總期間天數
    total_days = base_days + transit_days + serve_add

    # 計算截止日
    # 期間從送達翌日起算（民訴法第161條）
    start_dt = serve_dt + timedelta(days=1)
    raw_deadline = start_dt + timedelta(days=total_days - 1)  # 起算日算第1天

    # 如遇假日順延
    final_deadline = next_business_day(raw_deadline)
    holiday_extended = final_deadline != raw_deadline

    # 建立說明
    parts = [
        f"案件類型: {case_type}{appeal_type}",
        f"法院: {court_upper}",
        f"送達日: {date_to_roc_display(serve_dt)}（{serve_dt.strftime('%A')}）",
        f"起算日: {date_to_roc_display(start_dt)}（送達翌日）",
        f"法定期間: {base_days} 天",
    ]
    if transit_days > 0:
        parts.append(f"在途期間: +{transit_days} 天")
    if serve_add > 0:
        parts.append(f"送達方式加計（{serve_method}）: +{serve_add} 天")
    parts.append(f"合計期間: {total_days} 天")
    parts.append(f"届滿日: {date_to_roc_display(raw_deadline)}")
    if holiday_extended:
        parts.append(f"遇假日順延至: {date_to_roc_display(final_deadline)}（{final_deadline.strftime('%A')}）")

    return {
        "case_type": case_type,
        "appeal_type": appeal_type,
        "court": court_upper,
        "base_days": base_days,
        "transit_days": transit_days,
        "serve_addition": serve_add,
        "serve_method": serve_method,
        "total_days": total_days,
        "serve_date": date_to_roc_display(serve_dt),
        "serve_date_ad": serve_dt.isoformat(),
        "start_date": date_to_roc_display(start_dt),
        "raw_deadline": date_to_roc_display(raw_deadline),
        "raw_deadline_ad": raw_deadline.isoformat(),
        "final_deadline": date_to_roc_display(final_deadline),
        "final_deadline_ad": final_deadline.isoformat(),
        "holiday_extended": holiday_extended,
        "breakdown": "；".join(parts),
    }


# ─── 測試 ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 60)
    print("上訴抗告再審期間試算測試")
    print("=" * 60)

    # 民事上訴 — 臺北地院，一般送達
    r = calc_appeal_period("民事", "TPD", "上訴", "115/03/10")
    print(f"\n【民事上訴 — 臺北地院】")
    print(f"  送達日: {r['serve_date']}")
    print(f"  基本期間: {r['base_days']} 天")
    print(f"  在途期間: {r['transit_days']} 天")
    print(f"  截止日: {r['final_deadline']}")
    print(f"  假日順延: {'是' if r['holiday_extended'] else '否'}")

    # 民事上訴 — 花蓮地院，寄存送達
    r = calc_appeal_period("民事", "HLD", "上訴", "115/03/10", "寄存送達")
    print(f"\n【民事上訴 — 花蓮地院，寄存送達】")
    print(f"  送達日: {r['serve_date']}")
    print(f"  基本期間: {r['base_days']} 天，在途: +{r['transit_days']}，寄存: +{r['serve_addition']}")
    print(f"  合計: {r['total_days']} 天")
    print(f"  截止日: {r['final_deadline']}")

    # 刑事上訴 — 高等法院（10天）
    r = calc_appeal_period("刑事", "TPH", "上訴", "115/03/05")
    print(f"\n【刑事上訴 — 臺灣高等法院（10天）】")
    print(f"  基本期間: {r['base_days']} 天")
    print(f"  截止日: {r['final_deadline']}")

    # 民事抗告 — 高雄地院
    r = calc_appeal_period("民事", "KSD", "抗告", "115/03/01")
    print(f"\n【民事抗告 — 高雄地院】")
    print(f"  基本期間: {r['base_days']} 天，在途: +{r['transit_days']}")
    print(f"  截止日: {r['final_deadline']}")

    # 公示送達 — 國外
    r = calc_appeal_period("民事", "TPD", "上訴", "115/03/01", "公示送達", "國外")
    print(f"\n【民事上訴 — 公示送達（國外）】")
    print(f"  送達加計: +{r['serve_addition']} 天")
    print(f"  合計: {r['total_days']} 天")
    print(f"  截止日: {r['final_deadline']}")
