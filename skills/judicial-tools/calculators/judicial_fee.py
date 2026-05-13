#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""司法規費試算 — 新法（114年1月1日起）及舊法費率計算

依據民事訴訟法第77條之13（114年新法）及修正前舊法，
計算民事、家事、行政訴訟之裁判費。

新法費率（114/1/1 起適用）:
    ≤ 100萬: 1%
    100萬~1000萬: 超過部分 0.9%
    1000萬~1億: 超過部分 0.8%
    1億~10億: 超過部分 0.7%
    > 10億: 超過部分 0.6%
    最低徵收 1,000 元

舊法費率:
    ≤ 10萬: 1,000 元
    10萬~100萬: 超過部分 1% + 1,000
    100萬~1000萬: 超過部分 0.9% + 10,000
    1000萬~1億: 超過部分 0.8% + 91,000
    > 1億: 超過部分 0.7% + 811,000
"""

from __future__ import annotations

import math
from typing import Optional


# ─── 費率表 ─────────────────────────────────────────────────────────────────────

# 新法級距: (上限, 費率)，金額超過前一級的部分按該費率計算
_NEW_BRACKETS = [
    (1_000_000,      0.01),   # ≤ 100萬: 1%
    (10_000_000,     0.009),  # 100萬~1000萬: 0.9%
    (100_000_000,    0.008),  # 1000萬~1億: 0.8%
    (1_000_000_000,  0.007),  # 1億~10億: 0.7%
    (None,           0.006),  # > 10億: 0.6%
]

_NEW_MIN_FEE = 1_000  # 最低徵收額

# 上訴審加倍倍率
_LEVEL_MULTIPLIER_NEW = {
    "一審": 1.0,
    "二審": 1.5,
    "三審": 2.5,
}

_LEVEL_MULTIPLIER_OLD = {
    "一審": 1.0,
    "二審": 1.5,
    "三審": 2.5,
}

# 非訟事件固定費用
_NON_LITIGATION_FEE_OLD = 1_000


# ─── 核心計算 ─────────────────────────────────────────────────────────────────

def _calc_bracket_fee(amount: int, brackets: list, min_fee: int = 0) -> int:
    """依級距表計算費用"""
    if amount <= 0:
        return min_fee
    fee = 0.0
    prev_limit = 0
    for ceiling, rate in brackets:
        if ceiling is None:
            # 最高級距，無上限
            fee += (amount - prev_limit) * rate
            break
        if amount <= ceiling:
            fee += (amount - prev_limit) * rate
            break
        else:
            fee += (ceiling - prev_limit) * rate
            prev_limit = ceiling
    return max(math.ceil(fee), min_fee)


def calc_judicial_fee_new(amount: int, level: str = "一審") -> int:
    """新法（114年起）裁判費計算

    Args:
        amount: 訴訟標的金額（新台幣元）
        level: 審級，一審/二審/三審

    Returns:
        裁判費金額（元）
    """
    if amount <= 0:
        return _NEW_MIN_FEE

    multiplier = _LEVEL_MULTIPLIER_NEW.get(level, 1.0)
    base_fee = _calc_bracket_fee(amount, _NEW_BRACKETS, _NEW_MIN_FEE)
    fee = base_fee * multiplier
    return max(math.ceil(fee), _NEW_MIN_FEE)


def calc_judicial_fee_old(amount: int, level: str = "一審") -> int:
    """舊法裁判費計算

    Args:
        amount: 訴訟標的金額（新台幣元）
        level: 審級，一審/二審/三審

    Returns:
        裁判費金額（元）
    """
    if amount <= 0:
        return 1_000

    # 舊法級距直接計算
    if amount <= 100_000:
        base_fee = 1_000
    elif amount <= 1_000_000:
        base_fee = math.ceil((amount - 100_000) * 0.01) + 1_000
    elif amount <= 10_000_000:
        base_fee = math.ceil((amount - 1_000_000) * 0.009) + 10_000
    elif amount <= 100_000_000:
        base_fee = math.ceil((amount - 10_000_000) * 0.008) + 91_000
    else:
        base_fee = math.ceil((amount - 100_000_000) * 0.007) + 811_000

    multiplier = _LEVEL_MULTIPLIER_OLD.get(level, 1.0)
    fee = base_fee * multiplier
    return max(math.ceil(fee), 1_000)


def calc_judicial_fee(
    category: str = "民事",
    procedure: str = "訴訟事件",
    statute: str = "",
    amount: int = 0,
    level: str = "一審",
    law: str = "new",
) -> dict:
    """司法規費試算主入口

    Args:
        category: 案件類別 — 民事 / 家事 / 行政
        procedure: 程序類型 — 訴訟事件 / 非訟事件
        statute: 法條依據說明（選填）
        amount: 訴訟標的金額（新台幣元）
        level: 審級 — 一審 / 二審 / 三審
        law: 適用法律 — "new" (114年新法) / "old" (舊法)

    Returns:
        dict 包含 fee, amount, level, law, category, procedure, breakdown
    """
    # 非訟事件
    if procedure == "非訟事件":
        if law == "old":
            fee = _NON_LITIGATION_FEE_OLD
            breakdown = "非訟事件固定費用 1,000 元（舊法）"
        else:
            # 新法非訟事件仍依標的金額計算，但最低 1,000 元
            fee = calc_judicial_fee_new(amount, "一審")
            breakdown = f"非訟事件依新法費率計算，標的金額 {amount:,} 元 → 費用 {fee:,} 元"
        return {
            "fee": fee,
            "amount": amount,
            "level": level,
            "law": "新法" if law == "new" else "舊法",
            "category": category,
            "procedure": procedure,
            "statute": statute,
            "breakdown": breakdown,
        }

    # 訴訟事件
    if law == "new":
        fee = calc_judicial_fee_new(amount, level)
        law_label = "新法（114年1月1日起）"
    else:
        fee = calc_judicial_fee_old(amount, level)
        law_label = "舊法（113年12月31日前）"

    # 建立計算說明
    multiplier_map = _LEVEL_MULTIPLIER_NEW if law == "new" else _LEVEL_MULTIPLIER_OLD
    mult = multiplier_map.get(level, 1.0)

    if law == "new":
        base = calc_judicial_fee_new(amount, "一審")
    else:
        base = calc_judicial_fee_old(amount, "一審")

    parts = [f"標的金額: {amount:,} 元"]
    parts.append(f"一審裁判費: {base:,} 元")
    if level != "一審":
        parts.append(f"{level}倍率: ×{mult}")
        parts.append(f"{level}裁判費: {fee:,} 元")

    breakdown = "；".join(parts)

    return {
        "fee": fee,
        "amount": amount,
        "level": level,
        "law": law_label,
        "category": category,
        "procedure": procedure,
        "statute": statute,
        "breakdown": breakdown,
    }


# ─── 測試 ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 60)
    print("司法規費試算測試")
    print("=" * 60)

    test_amounts = [50_000, 500_000, 3_000_000, 50_000_000, 200_000_000, 2_000_000_000]

    for amt in test_amounts:
        new_fee = calc_judicial_fee_new(amt, "一審")
        old_fee = calc_judicial_fee_old(amt, "一審")
        print(f"\n標的金額 {amt:>15,} 元")
        print(f"  新法一審: {new_fee:>10,} 元")
        print(f"  舊法一審: {old_fee:>10,} 元")

    print("\n" + "-" * 60)
    print("審級加倍測試（新法, 標的 500 萬）")
    for lvl in ["一審", "二審", "三審"]:
        r = calc_judicial_fee("民事", "訴訟事件", "", 5_000_000, lvl, "new")
        print(f"  {lvl}: {r['fee']:,} 元 — {r['breakdown']}")

    print("\n" + "-" * 60)
    print("非訟事件測試")
    r = calc_judicial_fee("民事", "非訟事件", "", 500_000, "一審", "old")
    print(f"  舊法非訟: {r['fee']:,} 元 — {r['breakdown']}")
    r = calc_judicial_fee("民事", "非訟事件", "", 500_000, "一審", "new")
    print(f"  新法非訟: {r['fee']:,} 元 — {r['breakdown']}")
