#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""利息及違約金試算 (GDGT12 + GDGT18)

支援：
  - 利息計算：本金 × 年利率 × (天數 / 年天數)
  - 違約金計算：同上公式
  - 跨年度分段計算（閏年 366 天 / 平年 365 天）
  - 按月給付模式

參考：司法院利息試算器 GDGT12、違約金試算器 GDGT18
"""

from __future__ import annotations

import calendar
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from .date_utils import roc_to_date, date_to_roc_display


def calc_interest(
    principal: int | float,
    annual_rate: float,
    start_str: str,
    end_str: str,
    calc_type: str = "利息",
    monthly_payment: int | float = 0,
) -> dict:
    """利息/違約金試算。

    Args:
        principal: 本金（或違約金計算基數）
        annual_rate: 年利率（百分比，例如 5 表示 5%）
        start_str: 起算日（民國年 YYY/MM/DD 或西元 YYYY-MM-DD）
        end_str: 終止日（同上格式）
        calc_type: "利息" 或 "違約金"
        monthly_payment: 若為按月給付模式，每月給付金額（本金逐月遞減）

    Returns:
        dict 包含：
          calc_type       計算類型
          principal       本金
          annual_rate     年利率
          start_date      起算日（西元）
          end_date        終止日（西元）
          total_days      總天數
          total_amount    總金額
          detail_by_year  逐年明細 list
          formula_text    公式說明
    """
    p = Decimal(str(principal))
    rate = Decimal(str(annual_rate)) / Decimal("100")
    monthly_pmt = Decimal(str(monthly_payment))

    if p <= 0:
        raise ValueError("本金必須大於 0")
    if rate <= 0:
        raise ValueError("年利率必須大於 0")

    # 解析日期（支援民國年或西元）
    start = _parse_date(start_str)
    end = _parse_date(end_str)

    if end <= start:
        raise ValueError("終止日必須晚於起算日")

    total_days = (end - start).days

    if monthly_pmt > 0:
        return _calc_monthly_mode(p, rate, start, end, calc_type, monthly_pmt)

    # 跨年度分段計算
    detail: list[dict] = []
    total_amount = Decimal("0")
    cursor = start

    while cursor < end:
        year_end_date = date(cursor.year, 12, 31)
        seg_end = min(year_end_date, end)
        if seg_end <= cursor:
            break

        days_in_seg = (seg_end - cursor).days
        # 若終止日在同年但等於 end，使用 end
        if seg_end == year_end_date and year_end_date < end:
            days_in_seg = (seg_end - cursor).days + 1
            seg_end_display = seg_end
            next_cursor = seg_end + timedelta(days=1)
        else:
            days_in_seg = (seg_end - cursor).days
            seg_end_display = seg_end
            next_cursor = seg_end

        year_days = 366 if calendar.isleap(cursor.year) else 365
        seg_amount = _round2(p * rate * days_in_seg / year_days)

        detail.append({
            "year": cursor.year,
            "roc_year": cursor.year - 1911,
            "start": str(cursor),
            "end": str(seg_end_display),
            "days": days_in_seg,
            "days_in_year": year_days,
            "amount": float(seg_amount),
        })

        total_amount += seg_amount
        cursor = next_cursor

    formula = (
        f"{calc_type}試算\n"
        f"本金：{principal}\n"
        f"年利率：{annual_rate}%\n"
        f"期間：{date_to_roc_display(start)} ~ {date_to_roc_display(end)}\n"
        f"總天數：{total_days} 天\n"
        f"公式：本金 × {annual_rate}% × (天數 ÷ 年天數)\n"
        f"{calc_type}合計：{float(total_amount)}"
    )

    return {
        "calc_type": calc_type,
        "principal": float(p),
        "annual_rate": float(annual_rate),
        "start_date": str(start),
        "end_date": str(end),
        "total_days": total_days,
        "total_amount": float(total_amount),
        "detail_by_year": detail,
        "formula_text": formula,
    }


def _calc_monthly_mode(
    principal: Decimal,
    rate: Decimal,
    start: date,
    end: date,
    calc_type: str,
    monthly_pmt: Decimal,
) -> dict:
    """按月給付模式：每月扣減月付額後計算利息。"""
    detail: list[dict] = []
    total_amount = Decimal("0")
    remaining = principal
    cursor = start
    month_count = 0

    while cursor < end and remaining > 0:
        # 本月結束日
        if cursor.month == 12:
            next_month = date(cursor.year + 1, 1, cursor.day if cursor.day <= 28 else 28)
        else:
            max_day = calendar.monthrange(cursor.year, cursor.month + 1)[1]
            next_month = date(cursor.year, cursor.month + 1, min(cursor.day, max_day))

        seg_end = min(next_month, end)
        days_in_seg = (seg_end - cursor).days
        year_days = 366 if calendar.isleap(cursor.year) else 365

        seg_interest = _round2(remaining * rate * days_in_seg / year_days)
        total_amount += seg_interest

        detail.append({
            "month": month_count + 1,
            "start": str(cursor),
            "end": str(seg_end),
            "days": days_in_seg,
            "remaining_principal": float(remaining),
            "amount": float(seg_interest),
        })

        # 扣減月付額
        remaining -= monthly_pmt
        if remaining < 0:
            remaining = Decimal("0")

        cursor = seg_end
        month_count += 1

    total_days = (end - start).days

    formula = (
        f"{calc_type}試算（按月給付模式）\n"
        f"初始本金：{float(principal)}\n"
        f"年利率：{float(rate * 100)}%\n"
        f"每月給付：{float(monthly_pmt)}\n"
        f"期間：{date_to_roc_display(start)} ~ {date_to_roc_display(end)}\n"
        f"總月數：{month_count}\n"
        f"{calc_type}合計：{float(total_amount)}"
    )

    return {
        "calc_type": calc_type,
        "principal": float(principal),
        "annual_rate": float(rate * 100),
        "start_date": str(start),
        "end_date": str(end),
        "total_days": total_days,
        "total_amount": float(total_amount),
        "monthly_payment": float(monthly_pmt),
        "detail_by_year": detail,
        "formula_text": formula,
    }


def _parse_date(s: str) -> date:
    """解析日期字串，支援民國年與西元年格式。"""
    s = s.strip()
    # 嘗試西元 YYYY-MM-DD
    if len(s) == 10 and s[4] == "-":
        parts = s.split("-")
        return date(int(parts[0]), int(parts[1]), int(parts[2]))
    # 民國年格式
    return roc_to_date(s)


def _round2(d: Decimal) -> Decimal:
    return d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


if __name__ == "__main__":
    print("=== 利息試算範例 ===")
    r1 = calc_interest(
        principal=1000000,
        annual_rate=5,
        start_str="113/06/15",
        end_str="115/03/01",
        calc_type="利息",
    )
    print(r1["formula_text"])
    print()
    for seg in r1["detail_by_year"]:
        print(f"  民國{seg['roc_year']}年: {seg['days']}天, {seg['amount']}")

    print()
    print("=== 違約金試算範例 ===")
    r2 = calc_interest(
        principal=500000,
        annual_rate=10,
        start_str="114/01/01",
        end_str="114/07/01",
        calc_type="違約金",
    )
    print(r2["formula_text"])

    print()
    print("=== 按月給付模式 ===")
    r3 = calc_interest(
        principal=1200000,
        annual_rate=5,
        start_str="114/01/01",
        end_str="114/06/01",
        calc_type="利息",
        monthly_payment=200000,
    )
    print(r3["formula_text"])
