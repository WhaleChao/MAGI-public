#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""共用日期工具 — 民國年轉換、假日判定、在途期間等"""

from __future__ import annotations
import calendar
from datetime import date, timedelta
from typing import Optional, Tuple

# ─── 民國年 ↔ 西元年 ─────────────────────────────────────────────────────────

def roc_to_date(roc_str: str) -> date:
    """民國年字串 (YYYMMDD 或 YYY/MM/DD 或 YYY-MM-DD) → date"""
    s = roc_str.replace("/", "").replace("-", "").strip()
    if len(s) == 7:
        y, m, d = int(s[:3]), int(s[3:5]), int(s[5:7])
    elif len(s) == 8:  # 西元年 YYYYMMDD
        y, m, d = int(s[:4]) - 1911, int(s[4:6]), int(s[6:8])
    elif len(s) == 6:
        y, m, d = int(s[:2]), int(s[2:4]), int(s[4:6])
        y += 100 if y < 50 else 0  # 兩位數民國年
    else:
        raise ValueError(f"無法解析民國年日期: {roc_str!r}")
    return date(y + 1911, m, d)


def date_to_roc(d: date) -> str:
    """date → 民國年字串 YYYMMDD"""
    return f"{d.year - 1911:03d}{d.month:02d}{d.day:02d}"


def date_to_roc_display(d: date) -> str:
    """date → 民國年顯示格式 YYY/MM/DD"""
    return f"{d.year - 1911}/{d.month:02d}/{d.day:02d}"


# ─── 假日判定 ─────────────────────────────────────────────────────────────────

# 中華民國固定國定假日 (月, 日)
_FIXED_HOLIDAYS = [
    (1, 1),   # 元旦
    (2, 28),  # 和平紀念日
    (4, 4),   # 兒童節
    (4, 5),   # 清明節 (近似, 實際依天文計算)
    (5, 1),   # 勞動節
    (10, 10), # 國慶日
]


def is_weekend(d: date) -> bool:
    return d.weekday() >= 5


def is_holiday(d: date) -> bool:
    """簡易假日判定（週末 + 固定國定假日）"""
    if is_weekend(d):
        return True
    if (d.month, d.day) in _FIXED_HOLIDAYS:
        return True
    return False


def next_business_day(d: date) -> date:
    """若 d 為假日，順延至下一個工作日"""
    while is_holiday(d):
        d += timedelta(days=1)
    return d


# ─── 經過時間計算 ─────────────────────────────────────────────────────────────

def elapsed_days(start: date, end: date) -> int:
    """計算經過天數（起算日計入、終止日不計入）"""
    return (end - start).days


def elapsed_months_detail(start: date, end: date) -> Tuple[int, int, int]:
    """計算經過月數 → (整月數, 殘餘日數, 殘餘月分母日數)"""
    if end < start:
        raise ValueError("終止日不可早於起算日")

    months = (end.year - start.year) * 12 + (end.month - start.month)
    # 回推：start + months 個月
    try:
        checkpoint = _add_months(start, months)
    except ValueError:
        months -= 1
        checkpoint = _add_months(start, months)

    if checkpoint > end:
        months -= 1
        checkpoint = _add_months(start, months)

    remainder_days = (end - checkpoint).days
    # 分母：該月份總天數
    denom = calendar.monthrange(checkpoint.year, checkpoint.month)[1]
    return months, remainder_days, denom


def elapsed_years_detail(start: date, end: date) -> Tuple[int, int, int]:
    """計算經過年數 → (整年數, 殘餘日數, 殘餘年度總日數)"""
    if end < start:
        raise ValueError("終止日不可早於起算日")

    years = end.year - start.year
    try:
        checkpoint = start.replace(year=start.year + years)
    except ValueError:  # 2/29
        checkpoint = start.replace(year=start.year + years, day=28)

    if checkpoint > end:
        years -= 1
        try:
            checkpoint = start.replace(year=start.year + years)
        except ValueError:
            checkpoint = start.replace(year=start.year + years, day=28)

    remainder_days = (end - checkpoint).days
    # 年度總日數
    yr = checkpoint.year
    year_days = 366 if calendar.isleap(yr) else 365
    return years, remainder_days, year_days


def _add_months(d: date, months: int) -> date:
    """日期 + N 個月"""
    m = d.month - 1 + months
    y = d.year + m // 12
    m = m % 12 + 1
    max_day = calendar.monthrange(y, m)[1]
    return date(y, m, min(d.day, max_day))


# ─── 分數運算 ─────────────────────────────────────────────────────────────────

def gcd(a: int, b: int) -> int:
    while b:
        a, b = b, a % b
    return a


def frac_add(n1: int, d1: int, n2: int, d2: int) -> Tuple[int, int]:
    """分數加法，回傳約分後 (分子, 分母)"""
    n = n1 * d2 + n2 * d1
    d = d1 * d2
    g = gcd(abs(n), abs(d))
    return n // g, d // g


def frac_simplify(n: int, d: int) -> Tuple[int, int]:
    """約分"""
    if d == 0:
        raise ValueError("分母不可為零")
    g = gcd(abs(n), abs(d))
    return n // g, d // g
