#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""折舊自動試算 (GDGT02)

支援兩種折舊方法：
  - 平均法（直線法）：每年折舊額固定
  - 定率遞減法：每年按帳面價值乘以固定折舊率

參考：司法院折舊試算器 GDGT02
"""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import Optional


def calc_depreciation(
    method: str,
    cost: int | float,
    useful_years: int,
    used_years: int,
    used_months: int = 0,
    residual: Optional[int | float] = None,
) -> dict:
    """折舊試算主函式。

    Args:
        method: "平均法" 或 "定率遞減法"
        cost: 取得成本
        useful_years: 耐用年數
        used_years: 已使用整年數
        used_months: 已使用不足一年之月數 (0-11)
        residual: 殘價（若不指定，平均法依法定公式計算，定率遞減法為成本 10%）

    Returns:
        dict 包含：
          residual_value        殘價
          annual_depreciation   每年折舊額（平均法適用）
          depreciation_rate     折舊率（定率遞減法適用）
          accumulated_depreciation  累積折舊
          current_value         目前帳面價值
          depreciation_schedule 逐年折舊明細 list
          formula_text          公式說明
    """
    cost = Decimal(str(cost))
    useful_years = int(useful_years)
    used_years = int(used_years)
    used_months = int(used_months)

    if useful_years <= 0:
        raise ValueError("耐用年數必須大於 0")
    if cost <= 0:
        raise ValueError("取得成本必須大於 0")
    if used_months < 0 or used_months > 11:
        raise ValueError("使用月數須為 0~11")

    if method == "平均法":
        return _straight_line(cost, useful_years, used_years, used_months, residual)
    elif method == "定率遞減法":
        return _declining_balance(cost, useful_years, used_years, used_months, residual)
    else:
        raise ValueError(f"不支援的折舊方法: {method!r}（請輸入 '平均法' 或 '定率遞減法'）")


def _round2(d: Decimal) -> Decimal:
    """四捨五入至小數第二位"""
    return d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _round0(d: Decimal) -> int:
    """四捨五入至整數"""
    return int(d.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


# ──────────────────────────────────────────────────────────────────────────────
# 平均法 (Straight-line)
# ──────────────────────────────────────────────────────────────────────────────

def _straight_line(
    cost: Decimal,
    useful_years: int,
    used_years: int,
    used_months: int,
    residual: Optional[int | float],
) -> dict:
    """平均法折舊計算。

    殘價 = 成本 / (耐用年數 + 1)
    每年折舊 = (成本 - 殘價) / 耐用年數
    不足一年按月比例計算。
    """
    if residual is not None:
        res_val = Decimal(str(residual))
    else:
        res_val = _round2(cost / (useful_years + 1))

    annual_dep = _round2((cost - res_val) / useful_years)

    schedule: list[dict] = []
    accumulated = Decimal("0")
    book_value = cost

    total_periods = used_years + (1 if used_months > 0 else 0)

    for yr in range(1, total_periods + 1):
        is_partial = (yr == total_periods and used_months > 0 and yr > used_years) or \
                     (used_years == 0 and yr == 1 and used_months > 0)

        # 判斷是否為不足一年的最後一期
        if yr == total_periods and used_months > 0 and yr > used_years:
            dep_this_year = _round2(annual_dep * used_months / 12)
        elif used_years == 0 and used_months > 0:
            dep_this_year = _round2(annual_dep * used_months / 12)
        else:
            dep_this_year = annual_dep

        # 不超過可折舊總額
        max_dep = cost - res_val - accumulated
        if dep_this_year > max_dep:
            dep_this_year = max_dep

        if dep_this_year <= 0:
            break

        accumulated += dep_this_year
        book_value = cost - accumulated

        schedule.append({
            "year": yr,
            "depreciation": float(dep_this_year),
            "accumulated": float(accumulated),
            "book_value": float(book_value),
            "months": used_months if (yr == total_periods and used_months > 0 and yr > used_years) else 12,
        })

    # 若 used_years > 0 且 used_months > 0，先跑整年再跑零頭
    if used_years > 0 and used_months > 0 and len(schedule) <= used_years:
        partial_dep = _round2(annual_dep * used_months / 12)
        max_dep = cost - res_val - accumulated
        partial_dep = min(partial_dep, max_dep)
        if partial_dep > 0:
            accumulated += partial_dep
            book_value = cost - accumulated
            schedule.append({
                "year": used_years + 1,
                "depreciation": float(partial_dep),
                "accumulated": float(accumulated),
                "book_value": float(book_value),
                "months": used_months,
            })

    formula = (
        f"平均法折舊\n"
        f"殘價 = {cost} ÷ ({useful_years} + 1) = {res_val}\n"
        f"每年折舊額 = ({cost} - {res_val}) ÷ {useful_years} = {annual_dep}\n"
        f"使用期間：{used_years} 年 {used_months} 月"
    )

    return {
        "method": "平均法",
        "residual_value": float(res_val),
        "annual_depreciation": float(annual_dep),
        "depreciation_rate": None,
        "accumulated_depreciation": float(accumulated),
        "current_value": float(book_value),
        "depreciation_schedule": schedule,
        "formula_text": formula,
    }


# ──────────────────────────────────────────────────────────────────────────────
# 定率遞減法 (Declining balance)
# ──────────────────────────────────────────────────────────────────────────────

def _declining_balance(
    cost: Decimal,
    useful_years: int,
    used_years: int,
    used_months: int,
    residual: Optional[int | float],
) -> dict:
    """定率遞減法折舊計算。

    折舊率 = 1 - (殘價/成本)^(1/耐用年數)
    每年折舊 = 期初帳面價值 × 折舊率
    累積折舊不超過成本 90%。
    """
    if residual is not None:
        res_val = Decimal(str(residual))
    else:
        # 法定殘價 = 成本 10%
        res_val = _round2(cost * Decimal("0.1"))

    # 折舊率 = 1 - (殘價/成本)^(1/耐用年數)
    ratio = float(res_val / cost)
    if ratio <= 0:
        raise ValueError("殘價必須大於 0 才能使用定率遞減法")
    dep_rate = Decimal(str(1.0 - ratio ** (1.0 / useful_years)))
    dep_rate = _round2(dep_rate)

    max_total_dep = _round2(cost * Decimal("0.9"))  # 累積折舊不超過 90%

    schedule: list[dict] = []
    accumulated = Decimal("0")
    book_value = cost

    total_periods = used_years + (1 if used_months > 0 else 0)

    for yr in range(1, used_years + 1):
        dep_this_year = _round2(book_value * dep_rate)

        if accumulated + dep_this_year > max_total_dep:
            dep_this_year = max_total_dep - accumulated

        if dep_this_year <= 0:
            break

        accumulated += dep_this_year
        book_value = cost - accumulated

        schedule.append({
            "year": yr,
            "depreciation": float(dep_this_year),
            "accumulated": float(accumulated),
            "book_value": float(book_value),
            "months": 12,
        })

    # 不足一年的零頭
    if used_months > 0 and accumulated < max_total_dep:
        dep_partial = _round2(book_value * dep_rate * used_months / 12)
        if accumulated + dep_partial > max_total_dep:
            dep_partial = max_total_dep - accumulated
        if dep_partial > 0:
            accumulated += dep_partial
            book_value = cost - accumulated
            schedule.append({
                "year": used_years + 1,
                "depreciation": float(dep_partial),
                "accumulated": float(accumulated),
                "book_value": float(book_value),
                "months": used_months,
            })

    formula = (
        f"定率遞減法折舊\n"
        f"殘價 = {res_val}（成本之 {float(res_val/cost)*100:.1f}%）\n"
        f"折舊率 = 1 - ({res_val}/{cost})^(1/{useful_years}) ≈ {dep_rate}\n"
        f"累積折舊上限 = {cost} × 90% = {max_total_dep}\n"
        f"使用期間：{used_years} 年 {used_months} 月"
    )

    return {
        "method": "定率遞減法",
        "residual_value": float(res_val),
        "annual_depreciation": None,
        "depreciation_rate": float(dep_rate),
        "accumulated_depreciation": float(accumulated),
        "current_value": float(book_value),
        "depreciation_schedule": schedule,
        "formula_text": formula,
    }


if __name__ == "__main__":
    print("=== 平均法範例 ===")
    r1 = calc_depreciation("平均法", cost=100000, useful_years=5, used_years=3, used_months=6)
    print(r1["formula_text"])
    print(f"累積折舊: {r1['accumulated_depreciation']}")
    print(f"帳面價值: {r1['current_value']}")
    for row in r1["depreciation_schedule"]:
        print(f"  第{row['year']}年: 折舊 {row['depreciation']}, 累積 {row['accumulated']}, 帳面 {row['book_value']}")

    print()
    print("=== 定率遞減法範例 ===")
    r2 = calc_depreciation("定率遞減法", cost=100000, useful_years=5, used_years=3, used_months=0)
    print(r2["formula_text"])
    print(f"累積折舊: {r2['accumulated_depreciation']}")
    print(f"帳面價值: {r2['current_value']}")
    for row in r2["depreciation_schedule"]:
        print(f"  第{row['year']}年: 折舊 {row['depreciation']}, 累積 {row['accumulated']}, 帳面 {row['book_value']}")
