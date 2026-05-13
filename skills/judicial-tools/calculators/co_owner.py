#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""共有人應有部分比例試算 (GDGT20)

計算多位共有人之應有部分比例，並驗證合計是否等於 1。
支援分數輸入與約分、通分。

參考：司法院共有人應有部分試算器 GDGT20
"""

from __future__ import annotations

from .date_utils import frac_add, frac_simplify, gcd


def calc_co_owner_share(owners: list[dict]) -> dict:
    """共有人應有部分比例試算。

    Args:
        owners: 共有人清單，每筆格式：
            {"name": str, "numerator": int, "denominator": int}
            例如 {"name": "王大明", "numerator": 1, "denominator": 4}

    Returns:
        dict 包含：
          owners_detail   各共有人明細 (含約分後比例、小數百分比)
          total_numerator   合計分子（通分後）
          total_denominator 合計分母（通分後）
          total_decimal     合計小數值
          is_complete       合計是否等於 1
          common_denominator  最小公分母
          formula_text      說明文字
    """
    if not owners:
        raise ValueError("至少須有一位共有人")

    # 驗證輸入
    for i, o in enumerate(owners):
        if "name" not in o or "numerator" not in o or "denominator" not in o:
            raise ValueError(f"第 {i+1} 筆共有人資料缺少必要欄位 (name, numerator, denominator)")
        if o["denominator"] == 0:
            raise ValueError(f"第 {i+1} 筆共有人 {o['name']} 的分母不可為零")
        if o["numerator"] < 0:
            raise ValueError(f"第 {i+1} 筆共有人 {o['name']} 的分子不可為負數")

    # 計算最小公分母 (LCM of all denominators)
    common_denom = owners[0]["denominator"]
    for o in owners[1:]:
        common_denom = _lcm(common_denom, o["denominator"])

    # 計算各人明細
    owners_detail: list[dict] = []
    sum_n = 0
    sum_d = 1

    for o in owners:
        n, d = frac_simplify(o["numerator"], o["denominator"])
        # 通分
        unified_n = n * (common_denom // d)
        decimal_pct = float(n / d) * 100

        owners_detail.append({
            "name": o["name"],
            "original": f"{o['numerator']}/{o['denominator']}",
            "simplified": f"{n}/{d}",
            "unified": f"{unified_n}/{common_denom}",
            "decimal_percent": round(decimal_pct, 4),
        })

        # 累加分數
        sum_n, sum_d = frac_add(sum_n, sum_d, n, d)

    # 約分合計
    sum_n, sum_d = frac_simplify(sum_n, sum_d)
    total_decimal = float(sum_n / sum_d)
    is_complete = (sum_n == sum_d)

    # 通分後的合計
    total_unified_n = sum_n * (common_denom // sum_d) if sum_d != 0 and common_denom % sum_d == 0 else sum_n
    total_unified_d = common_denom if sum_d != 0 and common_denom % sum_d == 0 else sum_d

    # 公式文字
    lines = [f"共有人應有部分比例試算", f"共有人數：{len(owners)} 人", ""]
    for od in owners_detail:
        lines.append(f"  {od['name']}：{od['original']}"
                     + (f" = {od['simplified']}" if od['original'] != od['simplified'] else "")
                     + f"（{od['decimal_percent']:.2f}%）")

    lines.append("")
    lines.append(f"合計：{sum_n}/{sum_d}" + (f" = {total_unified_n}/{total_unified_d}" if sum_d != common_denom else ""))
    lines.append(f"合計比例：{total_decimal*100:.4f}%")

    if is_complete:
        lines.append("✓ 合計等於 1（比例完整）")
    else:
        diff_n = sum_d - sum_n
        lines.append(f"✗ 合計不等於 1（差額 {diff_n}/{sum_d}）")

    return {
        "owners_detail": owners_detail,
        "total_numerator": sum_n,
        "total_denominator": sum_d,
        "total_decimal": total_decimal,
        "is_complete": is_complete,
        "common_denominator": common_denom,
        "formula_text": "\n".join(lines),
    }


def _lcm(a: int, b: int) -> int:
    """最小公倍數"""
    return abs(a * b) // gcd(a, b)


if __name__ == "__main__":
    print("=== 共有人應有部分（合計 = 1）===")
    r1 = calc_co_owner_share([
        {"name": "王大明", "numerator": 1, "denominator": 4},
        {"name": "王小美", "numerator": 1, "denominator": 4},
        {"name": "王建國", "numerator": 2, "denominator": 4},
    ])
    print(r1["formula_text"])
    print()

    print("=== 共有人應有部分（合計 ≠ 1）===")
    r2 = calc_co_owner_share([
        {"name": "張三", "numerator": 1, "denominator": 3},
        {"name": "李四", "numerator": 1, "denominator": 6},
        {"name": "王五", "numerator": 1, "denominator": 4},
    ])
    print(r2["formula_text"])
    print(f"\n合計: {r2['total_numerator']}/{r2['total_denominator']} = {r2['total_decimal']:.6f}")
    print(f"完整: {r2['is_complete']}")
