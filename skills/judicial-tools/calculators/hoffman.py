#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""霍夫曼一次給付試算 (GDGT03)

霍夫曼計算法（Hoffman method）以單利折現方式，將未來定期給付
折算為一次給付之現值。常用於扶養費、勞動能力減損等一次性賠償計算。

公式：
  第 k 期霍夫曼係數 = 1 / (1 + r × k)
  總現值 = 每期金額 × Σ(k=0 ~ n-1) [1/(1+r×k)]
  不足一期之零頭 f：加計 金額 × f / (1 + r × n)

參考：司法院霍夫曼試算器 GDGT03
"""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import Optional


def calc_hoffman(
    amount: int | float,
    rate: float,
    years: int,
    months: int = 0,
    period_type: str = "月",
    supporters: int = 1,
    first_period_discount: bool = False,
) -> dict:
    """霍夫曼一次給付試算。

    Args:
        amount: 每期給付金額
        rate: 年利率（百分比，例如 5 表示 5%）
        years: 給付年數
        months: 給付月數（加上年數的零頭）
        period_type: "月" 或 "年"，決定每期長度
        supporters: 扶養義務人人數（結果除以此數）
        first_period_discount: True 表示第一期也折現（k 從 1 開始）

    Returns:
        dict 包含：
          total_periods       總期數
          fractional_period   不足一期之比例
          hoffman_coefficient 霍夫曼係數合計
          present_value       折現後總金額（已除以扶養人數）
          present_value_before_split  除以扶養人數前之金額
          supporters          扶養義務人人數
          detail              逐期明細（每 N 期彙總）
          formula_text        公式說明
    """
    amt = Decimal(str(amount))
    r = Decimal(str(rate)) / Decimal("100")
    supporters = max(1, int(supporters))

    if r <= 0:
        raise ValueError("利率必須大於 0")
    if amt <= 0:
        raise ValueError("每期金額必須大於 0")

    # 計算總期數
    if period_type == "月":
        total_full_periods = years * 12 + months
        fractional = Decimal("0")
        period_rate = r / 12  # 月利率
    elif period_type == "年":
        total_full_periods = years
        if months > 0:
            fractional = Decimal(str(months)) / Decimal("12")
        else:
            fractional = Decimal("0")
        period_rate = r
    else:
        raise ValueError(f"不支援的期別: {period_type!r}（請輸入 '月' 或 '年'）")

    # 計算霍夫曼係數
    coeff_sum = Decimal("0")
    detail: list[dict] = []
    start_k = 1 if first_period_discount else 0

    for k in range(start_k, total_full_periods + start_k):
        denominator = 1 + period_rate * k
        coeff_k = Decimal("1") / denominator
        coeff_sum += coeff_k
        detail.append({
            "period": k if first_period_discount else k + 1,
            "k": k,
            "coefficient": float(_round6(coeff_k)),
            "cumulative_coefficient": float(_round6(coeff_sum)),
        })

    # 不足一期之零頭
    frac_coeff = Decimal("0")
    if fractional > 0:
        n = total_full_periods + start_k
        denominator = 1 + period_rate * n
        frac_coeff = fractional / denominator
        coeff_sum += frac_coeff

    pv_before_split = _round2(amt * coeff_sum)
    pv = _round2(pv_before_split / supporters)

    # 公式文字
    period_label = "月" if period_type == "月" else "年"
    rate_label = f"{rate}%" if period_type == "年" else f"{rate}%÷12"

    formula_parts = [
        f"霍夫曼一次給付試算",
        f"每{period_label}金額：{amount}",
        f"年利率：{rate}%（每期利率 = {rate_label}）",
        f"期數：{total_full_periods} {period_label}" + (
            f" + {float(fractional):.4f} {period_label}" if fractional > 0 else ""
        ),
        f"霍夫曼係數合計：{float(_round6(coeff_sum))}",
        f"折現金額：{amount} × {float(_round6(coeff_sum))} = {float(pv_before_split)}",
    ]
    if supporters > 1:
        formula_parts.append(f"扶養義務人：{supporters} 人")
        formula_parts.append(f"每人分擔：{float(pv_before_split)} ÷ {supporters} = {float(pv)}")

    return {
        "total_periods": total_full_periods,
        "fractional_period": float(fractional),
        "hoffman_coefficient": float(_round6(coeff_sum)),
        "present_value": float(pv),
        "present_value_before_split": float(pv_before_split),
        "supporters": supporters,
        "detail": detail,
        "formula_text": "\n".join(formula_parts),
    }


def _round2(d: Decimal) -> Decimal:
    return d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _round6(d: Decimal) -> Decimal:
    return d.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)


if __name__ == "__main__":
    print("=== 霍夫曼試算範例：月付 ===")
    r1 = calc_hoffman(amount=20000, rate=5, years=10, months=0, period_type="月")
    print(r1["formula_text"])
    print(f"霍夫曼係數: {r1['hoffman_coefficient']}")
    print(f"一次給付金額: {r1['present_value']}")
    print(f"總期數: {r1['total_periods']}")
    print()

    print("=== 霍夫曼試算範例：年付 + 扶養人數 ===")
    r2 = calc_hoffman(amount=240000, rate=5, years=15, months=6, period_type="年", supporters=3)
    print(r2["formula_text"])
    print(f"一次給付金額（每人分擔）: {r2['present_value']}")
    print()

    print("=== 前5期明細 ===")
    for row in r2["detail"][:5]:
        print(f"  第{row['period']}期: 係數={row['coefficient']:.6f}, 累計={row['cumulative_coefficient']:.6f}")
