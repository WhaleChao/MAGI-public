#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""相當租金不當得利之試算 (GDGT16)

依土地法第 97 條、第 105 條及民法第 179 條，計算土地被無權占用時，
所有權人得請求相當於租金之不當得利。消滅時效 5 年（民法§126）。
"""

from __future__ import annotations

import calendar
from datetime import date
from typing import Union

from .date_utils import roc_to_date, date_to_roc_display


def _parse_date(d: Union[str, date]) -> date:
    """接受 date 物件或民國年字串"""
    if isinstance(d, date):
        return d
    return roc_to_date(d)


def _days_in_year(year: int) -> int:
    return 366 if calendar.isleap(year) else 365


def _get_land_value(land_values: Union[float, list[dict]], year: int) -> float:
    """取得該年度的申報地價。

    land_values 可以是：
    - float：固定地價
    - list[dict]：[{"year": 2020, "value": 10000}, ...]
      取該年或之前最近的地價
    """
    if isinstance(land_values, (int, float)):
        return float(land_values)

    # 按年排序，取 <= year 中最大的
    sorted_vals = sorted(land_values, key=lambda x: x["year"])
    result = sorted_vals[0]["value"]  # 預設用最早的
    for lv in sorted_vals:
        if lv["year"] <= year:
            result = lv["value"]
        else:
            break
    return result


def calc_unjust_enrichment(
    land_values: Union[float, list[dict]],
    area: float,
    share_n: int,
    share_d: int,
    rate: float,
    start_str: Union[str, date],
    end_str: Union[str, date],
) -> dict:
    """相當租金不當得利試算。

    Parameters
    ----------
    land_values : float 或 list[dict]
        申報地價。若為 list，每項 {"year": int, "value": float}。
    area : float
        占用面積（平方公尺）
    share_n, share_d : int
        請求人之應有部分（分子/分母）
    rate : float
        年租金比率（例如 0.05 代表 5%，土地法上限為 10%）
    start_str : str 或 date
        起算日（民國年字串或 date 物件）
    end_str : str 或 date
        終算日

    Returns
    -------
    dict
        yearly_breakdown: 逐年明細
        total: 總金額
        monthly_equivalent: 月付金額（以最後一年計算）
        explanation: 公式說明
    """
    start = _parse_date(start_str)
    end = _parse_date(end_str)

    if end <= start:
        return {"error": "終算日須晚於起算日"}

    # 5 年時效檢查
    from datetime import timedelta
    five_years_ago = end - timedelta(days=5 * 365)
    statute_warning = None
    if start < five_years_ago:
        statute_warning = (
            f"注意：自終算日 {date_to_roc_display(end)} 起算回溯 5 年為 "
            f"{date_to_roc_display(five_years_ago)}，"
            f"起算日 {date_to_roc_display(start)} 部分可能已罹於時效。"
        )

    yearly: list[dict] = []
    total = 0.0
    current = start

    while current < end:
        year = current.year
        year_end = date(year, 12, 31)
        period_end = min(year_end, end)

        # 該期間天數
        days = (period_end - current).days
        if period_end == end:
            days = (end - current).days
        if days <= 0:
            current = date(year + 1, 1, 1)
            continue

        total_days = _days_in_year(year)
        land_val = _get_land_value(land_values, year)

        # 年租金 = 申報地價 × 面積 × 持分 × 利率
        annual_rent = land_val * area * (share_n / share_d) * rate
        # 按日比例
        amount = annual_rent * days / total_days

        roc_year = year - 1911
        yearly.append({
            "roc_year": roc_year,
            "western_year": year,
            "land_value": land_val,
            "start": date_to_roc_display(current),
            "end": date_to_roc_display(period_end),
            "days": days,
            "total_days_in_year": total_days,
            "annual_rent": round(annual_rent, 2),
            "amount": round(amount, 2),
        })
        total += amount

        current = date(year + 1, 1, 1)

    # 月付金額（以最後一年為基準）
    last_land_val = _get_land_value(land_values, end.year)
    monthly = last_land_val * area * (share_n / share_d) * rate / 12

    explanation_lines = [
        "計算公式：",
        "  年租金 = 申報地價 × 占用面積 × 應有部分 × 年利率",
        "  各年度金額 = 年租金 × (該年度天數 / 全年天數)",
        f"  面積：{area} m²，持分：{share_n}/{share_d}，利率：{rate * 100}%",
        f"  期間：{date_to_roc_display(start)} 至 {date_to_roc_display(end)}",
    ]

    result = {
        "yearly_breakdown": yearly,
        "total": round(total, 2),
        "monthly_equivalent": round(monthly, 2),
        "explanation": "\n".join(explanation_lines),
    }
    if statute_warning:
        result["statute_warning"] = statute_warning

    return result


# ─── CLI 測試 ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    result = calc_unjust_enrichment(
        land_values=[
            {"year": 2022, "value": 15000},
            {"year": 2024, "value": 16000},
        ],
        area=100.0,
        share_n=1,
        share_d=2,
        rate=0.05,
        start_str="111/06/01",
        end_str="115/03/01",
    )

    print("=== 相當租金不當得利試算 ===")
    print(result["explanation"])
    if "statute_warning" in result:
        print(f"\n{result['statute_warning']}")
    print(f"\n逐年明細：")
    for y in result["yearly_breakdown"]:
        print(f"  民國 {y['roc_year']} 年（{y['start']}～{y['end']}，"
              f"{y['days']} 日）：地價 {y['land_value']:,.0f}，"
              f"金額 {y['amount']:,.0f} 元")
    print(f"\n總金額：{result['total']:,.0f} 元")
    print(f"月付金額（最近年度）：{result['monthly_equivalent']:,.0f} 元")
