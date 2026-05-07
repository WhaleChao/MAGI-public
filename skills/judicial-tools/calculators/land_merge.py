#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""土地合併 (GDGT15) 與部分維持共有 (GDGT14) 試算

GDGT15：多筆土地合併為一筆，重新計算各共有人之應有部分。
GDGT14：一筆土地中部分共有人維持共有、其餘分割取得，重新計算持分。
"""

from __future__ import annotations

from .date_utils import frac_simplify, frac_add, gcd


def calc_land_merge(parcels: list[dict]) -> dict:
    """土地合併試算 (GDGT15)。

    多筆共有土地合併為一筆時，各共有人在新地號的應有部分。

    Parameters
    ----------
    parcels : list[dict]
        各筆土地，每筆包含：
        - id : str           地號
        - area : float       面積（平方公尺）
        - owners : list[dict]
            - name : str
            - numerator : int   應有部分分子
            - denominator : int 應有部分分母

    Returns
    -------
    dict
        total_area: 合併後總面積
        new_shares: {name: {"numerator": int, "denominator": int, "display": str}}
        steps: 計算步驟說明
    """
    total_area = sum(p["area"] for p in parcels)
    if total_area == 0:
        return {"error": "總面積為零，無法計算"}

    # 每人的「加權面積」 = Σ(各筆面積 × 持分比例)
    weighted: dict[str, float] = {}
    steps: list[str] = []

    for p in parcels:
        pid = p["id"]
        area = p["area"]
        for o in p["owners"]:
            name = o["name"]
            n, d = o["numerator"], o["denominator"]
            contrib = area * n / d
            weighted[name] = weighted.get(name, 0.0) + contrib
            steps.append(
                f"  {name} 在 {pid}：{area} m² × {n}/{d} = {contrib:.4f} m²"
            )

    steps.insert(0, f"合併後總面積：{total_area} m²")
    steps.append("")

    # 新持分 = weighted / total_area，轉為分數
    # 為求精確，使用整數運算：以所有分母的最小公倍數為基底
    # 但面積可能含小數，故先以高精度浮點計算比例，再找近似分數
    new_shares: dict[str, dict] = {}
    for name, w in sorted(weighted.items()):
        ratio = w / total_area
        # 近似分數：分母取合理值（總面積 * 所有分母的 LCM）
        # 簡化方式：用 10000 為公分母再約分
        big_d = 10000000
        big_n = round(ratio * big_d)
        n_s, d_s = frac_simplify(big_n, big_d)
        new_shares[name] = {
            "numerator": n_s,
            "denominator": d_s,
            "display": f"{n_s}/{d_s}",
            "weighted_area": round(w, 4),
            "ratio": round(ratio, 8),
        }
        steps.append(
            f"  {name}：加權面積 {w:.4f} m² / {total_area} m² "
            f"= {ratio:.8f} ≈ {n_s}/{d_s}"
        )

    # 驗算
    check = sum(s["ratio"] for s in new_shares.values())
    steps.append(f"\n驗算：各人比例合計 = {check:.8f}")

    return {
        "total_area": total_area,
        "new_shares": new_shares,
        "steps": steps,
    }


def calc_land_partial_share(
    total_area: float,
    price_per_sqm: float,
    owners: list[dict],
    shared_group: list[str],
    split_group: list[str],
) -> dict:
    """部分維持共有試算 (GDGT14)。

    一筆土地中，部分共有人維持共有（shared_group），
    其餘共有人分割取得各自部分（split_group）。

    Parameters
    ----------
    total_area : float
        原始總面積
    price_per_sqm : float
        每平方公尺單價
    owners : list[dict]
        全部共有人，每人 {"name", "numerator", "denominator"}
    shared_group : list[str]
        維持共有之共有人姓名
    split_group : list[str]
        分割取得之共有人姓名

    Returns
    -------
    dict
        shared_portion: 共有部分面積與新持分
        split_portions: 各分割人取得面積
        steps: 計算步驟
    """
    steps: list[str] = []
    owner_map = {o["name"]: o for o in owners}

    # 計算各群組的持分合計
    def group_ratio(names: list[str]) -> float:
        return sum(
            owner_map[n]["numerator"] / owner_map[n]["denominator"]
            for n in names if n in owner_map
        )

    shared_ratio = group_ratio(shared_group)
    split_ratio = group_ratio(split_group)
    total_ratio = shared_ratio + split_ratio

    steps.append(f"總面積：{total_area} m²，單價：{price_per_sqm} 元/m²")
    steps.append(f"維持共有群組持分合計：{shared_ratio:.8f}")
    steps.append(f"分割群組持分合計：{split_ratio:.8f}")

    # 共有部分面積
    shared_area = total_area * shared_ratio / total_ratio
    steps.append(f"共有部分面積：{total_area} × {shared_ratio:.8f} / {total_ratio:.8f} "
                 f"= {shared_area:.4f} m²")

    # 維持共有者的新持分（在共有部分內）
    shared_new: list[dict] = []
    for name in shared_group:
        if name not in owner_map:
            continue
        o = owner_map[name]
        old_ratio = o["numerator"] / o["denominator"]
        new_ratio = old_ratio / shared_ratio  # 在共有部分中的比例
        big_d = 10000000
        big_n = round(new_ratio * big_d)
        n_s, d_s = frac_simplify(big_n, big_d)
        shared_new.append({
            "name": name,
            "old_share": f"{o['numerator']}/{o['denominator']}",
            "new_share": f"{n_s}/{d_s}",
            "new_numerator": n_s,
            "new_denominator": d_s,
        })
        steps.append(f"  {name}：新持分 {old_ratio:.8f} / {shared_ratio:.8f} "
                     f"= {new_ratio:.8f} ≈ {n_s}/{d_s}")

    # 分割者各自取得面積
    split_portions: list[dict] = []
    for name in split_group:
        if name not in owner_map:
            continue
        o = owner_map[name]
        old_ratio = o["numerator"] / o["denominator"]
        alloc_area = total_area * old_ratio / total_ratio
        alloc_value = alloc_area * price_per_sqm
        split_portions.append({
            "name": name,
            "old_share": f"{o['numerator']}/{o['denominator']}",
            "allocated_area": round(alloc_area, 4),
            "allocated_value": round(alloc_value, 2),
        })
        steps.append(f"  {name}：分割面積 {alloc_area:.4f} m² "
                     f"（價值 {alloc_value:,.0f} 元）")

    return {
        "shared_portion": {
            "area": round(shared_area, 4),
            "value": round(shared_area * price_per_sqm, 2),
            "owners": shared_new,
        },
        "split_portions": split_portions,
        "steps": steps,
    }


# ─── CLI 測試 ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # GDGT15：合併
    print("=== 土地合併試算 (GDGT15) ===")
    result = calc_land_merge([
        {
            "id": "仁愛段 100",
            "area": 600.0,
            "owners": [
                {"name": "甲", "numerator": 1, "denominator": 2},
                {"name": "乙", "numerator": 1, "denominator": 2},
            ],
        },
        {
            "id": "仁愛段 101",
            "area": 400.0,
            "owners": [
                {"name": "甲", "numerator": 1, "denominator": 4},
                {"name": "丙", "numerator": 3, "denominator": 4},
            ],
        },
    ])
    print(f"合併後總面積：{result['total_area']} m²")
    for name, info in result["new_shares"].items():
        print(f"  {name}：{info['display']}（加權面積 {info['weighted_area']} m²）")
    for s in result["steps"]:
        print(s)

    print("\n=== 部分維持共有試算 (GDGT14) ===")
    result2 = calc_land_partial_share(
        total_area=1000.0,
        price_per_sqm=30000.0,
        owners=[
            {"name": "甲", "numerator": 1, "denominator": 4},
            {"name": "乙", "numerator": 1, "denominator": 4},
            {"name": "丙", "numerator": 1, "denominator": 4},
            {"name": "丁", "numerator": 1, "denominator": 4},
        ],
        shared_group=["甲", "乙"],
        split_group=["丙", "丁"],
    )
    print(f"共有部分面積：{result2['shared_portion']['area']} m²")
    for o in result2["shared_portion"]["owners"]:
        print(f"  {o['name']}：{o['old_share']} → {o['new_share']}")
    for sp in result2["split_portions"]:
        print(f"  {sp['name']}：分得 {sp['allocated_area']} m²"
              f"（{sp['allocated_value']:,.0f} 元）")
