#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""法定刑度加重減輕試算 (GDGT22)

根據刑法第 33 條、第 64~68 條規定，計算法定刑度經加重或減輕後之範圍。
"""

from __future__ import annotations

import math
from typing import Any


# ─── 常數 ─────────────────────────────────────────────────────────────────────

MAX_IMPRISONMENT_YEARS = 30       # 有期徒刑上限 30 年 (刑法§33)
MIN_IMPRISONMENT_MONTHS = 2       # 有期徒刑下限 2 月
MIN_DETENTION_DAYS = 1            # 拘役下限 1 日
MAX_DETENTION_DAYS = 120          # 拘役上限 120 日 (加重可至 180 日)
MAX_DETENTION_DAYS_AGG = 180      # 拘役加重上限

SENTENCE_TYPES = ("死刑", "無期徒刑", "有期徒刑", "拘役", "罰金")


# ─── 輔助 ─────────────────────────────────────────────────────────────────────

def _ym_to_months(years: float, months: float) -> float:
    """年+月 → 總月數"""
    return years * 12 + months


def _months_to_ym(total: float) -> dict:
    """總月數 → {'years': int, 'months': int}"""
    total = max(total, 0)
    y = int(total) // 12
    m = int(total) % 12
    # 處理因乘除產生的小數：四捨五入到整月
    remainder = total - int(total)
    if remainder >= 0.5:
        m += 1
        if m >= 12:
            y += 1
            m -= 12
    return {"years": y, "months": m}


def _half_up(val: float) -> float:
    """乘除後取整（刑法規定不滿一月以一月計，即無條件進位）"""
    return math.ceil(val)


# ─── 加重 ─────────────────────────────────────────────────────────────────────

def _aggravate_once(stype: str, min_val: float, max_val: float,
                    min_unit: str, max_unit: str) -> tuple:
    """執行一次加重，回傳 (stype, min_val, max_val, min_unit, max_unit, explanation)"""
    if stype in ("死刑", "無期徒刑"):
        return stype, min_val, max_val, min_unit, max_unit, f"{stype}不得加重"

    if stype == "有期徒刑":
        new_max = max_val * 1.5
        cap = MAX_IMPRISONMENT_YEARS * 12  # 以月計
        if new_max > cap:
            new_max = cap
        explanation = (
            f"有期徒刑加重 1/2：上限 {max_val} 月 × 1.5 = {max_val * 1.5} 月"
            f"（上限 {MAX_IMPRISONMENT_YEARS} 年）→ {new_max} 月"
        )
        return stype, min_val, new_max, min_unit, max_unit, explanation

    if stype == "拘役":
        new_max = min(max_val * 1.5, MAX_DETENTION_DAYS_AGG)
        explanation = (
            f"拘役加重 1/2：上限 {max_val} 日 × 1.5 = {max_val * 1.5} 日"
            f"（上限 {MAX_DETENTION_DAYS_AGG} 日）→ {new_max} 日"
        )
        return stype, min_val, new_max, min_unit, max_unit, explanation

    if stype == "罰金":
        new_max = max_val * 1.5
        explanation = f"罰金加重 1/2：上限 {max_val} × 1.5 = {new_max}"
        return stype, min_val, new_max, min_unit, max_unit, explanation

    return stype, min_val, max_val, min_unit, max_unit, "未知刑種，無法加重"


# ─── 減輕 ─────────────────────────────────────────────────────────────────────

def _mitigate_once(stype: str, min_val: float, max_val: float,
                   min_unit: str, max_unit: str, rule: str) -> tuple:
    """執行一次減輕，回傳 (stype, min_val, max_val, min_unit, max_unit, explanation)"""
    if stype == "死刑":
        # 死刑減為無期徒刑，或 15~20 年有期徒刑
        explanation = "死刑減輕 → 無期徒刑，或有期徒刑 15 年以上 20 年以下"
        return "有期徒刑", 15 * 12, 20 * 12, "月", "月", explanation

    if stype == "無期徒刑":
        explanation = "無期徒刑減輕 → 有期徒刑 15 年以上 20 年以下"
        return "有期徒刑", 15 * 12, 20 * 12, "月", "月", explanation

    if stype == "有期徒刑":
        new_min = max(min_val * 0.5, MIN_IMPRISONMENT_MONTHS)
        new_max = max_val * 0.5
        if new_max < MIN_IMPRISONMENT_MONTHS:
            new_max = MIN_IMPRISONMENT_MONTHS
        explanation = (
            f"有期徒刑減輕 1/2：下限 {min_val} 月 × 0.5 = {min_val * 0.5} 月"
            f"（不得低於 {MIN_IMPRISONMENT_MONTHS} 月）→ 下限 {new_min} 月，"
            f"上限 {max_val} 月 × 0.5 = {new_max} 月"
        )
        return stype, new_min, new_max, min_unit, max_unit, explanation

    if stype == "拘役":
        new_min = max(min_val * 0.5, MIN_DETENTION_DAYS)
        new_max = max_val * 0.5
        if new_max < MIN_DETENTION_DAYS:
            new_max = MIN_DETENTION_DAYS
        explanation = (
            f"拘役減輕 1/2：下限 {min_val} 日 × 0.5 = {min_val * 0.5} 日"
            f"（不得低於 {MIN_DETENTION_DAYS} 日）→ {new_min} 日"
        )
        return stype, new_min, new_max, min_unit, max_unit, explanation

    if stype == "罰金":
        new_min = min_val * 0.5
        explanation = f"罰金減輕 1/2：下限 {min_val} × 0.5 = {new_min}"
        return stype, new_min, max_val, min_unit, max_unit, explanation

    return stype, min_val, max_val, min_unit, max_unit, "未知刑種，無法減輕"


# ─── 主函式 ───────────────────────────────────────────────────────────────────

def calc_sentence(
    sentence_type: str,
    min_val: float,
    max_val: float,
    min_unit: str = "月",
    max_unit: str = "月",
    aggravations: list[dict[str, Any]] | None = None,
    mitigations: list[dict[str, Any]] | None = None,
) -> dict:
    """法定刑度加重減輕試算。

    Parameters
    ----------
    sentence_type : str
        刑種：死刑 / 無期徒刑 / 有期徒刑 / 拘役 / 罰金
    min_val, max_val : float
        原始刑度之最低、最高值。
        有期徒刑以「月」為單位（例：6 月～5 年 → 6, 60）。
        拘役以「日」為單位。罰金以「元」為單位。
    min_unit, max_unit : str
        單位標示（月/年/日/元），用於顯示。
    aggravations : list[dict]
        加重事由列表，每項 {"rule": str, "count": int}。
    mitigations : list[dict]
        減輕事由列表，每項 {"rule": str, "count": int}。
        rule 含「第59條」時視為酌減（效果同減輕 1/2）。

    Returns
    -------
    dict
        original: 原始範圍
        adjusted: 調整後範圍
        steps: 各步驟說明
    """
    if aggravations is None:
        aggravations = []
    if mitigations is None:
        mitigations = []

    steps: list[str] = []
    stype = sentence_type
    lo, hi = float(min_val), float(max_val)
    u_lo, u_hi = min_unit, max_unit

    original = {
        "sentence_type": stype,
        "min": lo, "max": hi,
        "min_unit": u_lo, "max_unit": u_hi,
    }

    # 先加重，再減輕（刑法§71）
    for agg in aggravations:
        count = agg.get("count", 1)
        rule_name = agg.get("rule", "加重")
        for i in range(count):
            stype, lo, hi, u_lo, u_hi, expl = _aggravate_once(
                stype, lo, hi, u_lo, u_hi
            )
            steps.append(f"[加重] {rule_name}（第 {i+1} 次）：{expl}")

    for mit in mitigations:
        count = mit.get("count", 1)
        rule_name = mit.get("rule", "減輕")
        for i in range(count):
            stype, lo, hi, u_lo, u_hi, expl = _mitigate_once(
                stype, lo, hi, u_lo, u_hi, rule_name
            )
            steps.append(f"[減輕] {rule_name}（第 {i+1} 次）：{expl}")

    adjusted = {
        "sentence_type": stype,
        "min": lo, "max": hi,
        "min_unit": u_lo, "max_unit": u_hi,
    }

    # 可讀描述
    if stype == "有期徒刑":
        lo_ym = _months_to_ym(lo)
        hi_ym = _months_to_ym(hi)
        adjusted["display"] = (
            f"有期徒刑 {lo_ym['years']} 年 {lo_ym['months']} 月"
            f" 以上 {hi_ym['years']} 年 {hi_ym['months']} 月 以下"
        )
    elif stype == "拘役":
        adjusted["display"] = f"拘役 {lo} 日 以上 {hi} 日 以下"
    elif stype == "罰金":
        adjusted["display"] = f"罰金 {lo:,.0f} 元 以上 {hi:,.0f} 元 以下"
    else:
        adjusted["display"] = stype

    return {
        "original": original,
        "adjusted": adjusted,
        "steps": steps,
    }


# ─── CLI 測試 ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # 範例：竊盜罪 (§320) 有期徒刑 5 年以下，加重 1 次，酌減 1 次
    result = calc_sentence(
        sentence_type="有期徒刑",
        min_val=2,        # 2 月
        max_val=60,       # 5 年
        min_unit="月",
        max_unit="月",
        aggravations=[{"rule": "累犯加重 (§47)", "count": 1}],
        mitigations=[{"rule": "第59條酌減", "count": 1}],
    )
    print("=== 竊盜罪加重減輕試算 ===")
    print(f"原始：{result['original']}")
    print(f"調整：{result['adjusted']}")
    for s in result["steps"]:
        print(f"  {s}")
    print()

    # 範例：殺人罪 (§271) 死刑/無期徒刑，減輕 1 次
    result2 = calc_sentence(
        sentence_type="死刑",
        min_val=0, max_val=0,
        mitigations=[{"rule": "自首減輕 (§62)", "count": 1}],
    )
    print("=== 殺人罪死刑減輕 ===")
    print(f"原始：{result2['original']}")
    print(f"調整：{result2['adjusted']}")
    for s in result2["steps"]:
        print(f"  {s}")
