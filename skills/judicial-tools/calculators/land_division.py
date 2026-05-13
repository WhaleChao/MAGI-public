#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""土地分割共有物面積與地價之試算 (GDGT13)

計算共有土地分割時，各共有人應有面積、實際分配面積與應互為補償之金額。
"""

from __future__ import annotations

from .date_utils import frac_simplify


def calc_land_division(parcels: list[dict]) -> dict:
    """土地分割共有物面積與地價之試算。

    Parameters
    ----------
    parcels : list[dict]
        地號列表，每筆包含：
        - id : str            地號
        - total_area : float  總面積（平方公尺）
        - price_per_sqm : float  每平方公尺單價
        - owners : list[dict]  共有人列表
            - name : str         姓名
            - numerator : int    應有部分分子
            - denominator : int  應有部分分母
            - allocated_area : float  實際分配面積（平方公尺）

    Returns
    -------
    dict
        parcels: 各地號計算結果
        total_compensation: 全部補償金額彙總
        explanation: 說明文字
    """
    all_results: list[dict] = []
    # 跨地號彙總：每人應收 / 應付金額
    global_balance: dict[str, float] = {}

    for parcel in parcels:
        pid = parcel["id"]
        total_area = parcel["total_area"]
        price = parcel["price_per_sqm"]
        owners = parcel["owners"]

        owner_results: list[dict] = []
        for o in owners:
            name = o["name"]
            n, d = o["numerator"], o["denominator"]
            allocated = o["allocated_area"]

            entitled_area = total_area * n / d
            entitled_value = entitled_area * price
            allocated_value = allocated * price
            diff_area = allocated - entitled_area
            compensation = diff_area * price  # 正值＝多得應付，負值＝少得應收

            n_s, d_s = frac_simplify(n, d)

            owner_results.append({
                "name": name,
                "share": f"{n_s}/{d_s}",
                "entitled_area": round(entitled_area, 4),
                "entitled_value": round(entitled_value, 2),
                "allocated_area": round(allocated, 4),
                "allocated_value": round(allocated_value, 2),
                "diff_area": round(diff_area, 4),
                "compensation": round(compensation, 2),
                "note": "應付補償" if compensation > 0 else (
                    "應收補償" if compensation < 0 else "無需補償"
                ),
            })

            global_balance[name] = global_balance.get(name, 0.0) + compensation

        all_results.append({
            "parcel_id": pid,
            "total_area": total_area,
            "price_per_sqm": price,
            "owners": owner_results,
        })

    # 整理全域補償表
    compensation_summary: list[dict] = []
    for name, amount in sorted(global_balance.items(), key=lambda x: -x[1]):
        compensation_summary.append({
            "name": name,
            "net_compensation": round(amount, 2),
            "direction": "應付" if amount > 0 else ("應收" if amount < 0 else "平衡"),
        })

    # 驗算：全部補償加總應為 0（或極小浮點誤差）
    total_check = sum(global_balance.values())

    explanation_lines = [
        "計算方式：",
        "  應有面積 = 總面積 × 應有部分",
        "  應有價值 = 應有面積 × 每坪單價",
        "  補償金額 = (實際分配面積 − 應有面積) × 每坪單價",
        "  正值 → 多得者應付補償；負值 → 少得者應收補償",
    ]

    return {
        "parcels": all_results,
        "compensation_summary": compensation_summary,
        "balance_check": round(total_check, 2),
        "explanation": "\n".join(explanation_lines),
    }


# ─── CLI 測試 ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    result = calc_land_division([
        {
            "id": "台北市大安區仁愛段 100 地號",
            "total_area": 1000.0,
            "price_per_sqm": 50000.0,
            "owners": [
                {"name": "甲", "numerator": 1, "denominator": 3,
                 "allocated_area": 350.0},
                {"name": "乙", "numerator": 1, "denominator": 3,
                 "allocated_area": 330.0},
                {"name": "丙", "numerator": 1, "denominator": 3,
                 "allocated_area": 320.0},
            ],
        },
    ])

    print("=== 土地分割試算 ===")
    for p in result["parcels"]:
        print(f"\n地號：{p['parcel_id']}（總面積 {p['total_area']} m², "
              f"單價 {p['price_per_sqm']} 元/m²）")
        for o in p["owners"]:
            print(f"  {o['name']}（{o['share']}）："
                  f"應有 {o['entitled_area']} m² / "
                  f"實分 {o['allocated_area']} m² / "
                  f"差額 {o['diff_area']} m² / "
                  f"補償 {o['compensation']:+,.0f} 元 ({o['note']})")
    print(f"\n補償彙總（驗算差額={result['balance_check']}）：")
    for c in result["compensation_summary"]:
        print(f"  {c['name']}：{c['net_compensation']:+,.0f} 元（{c['direction']}）")
