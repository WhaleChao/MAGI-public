#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""經過時間試算 — 計算兩個日期間的天數、月數、年數

常見用途：
- 計算僱傭期間（資遣費、退休金年資）
- 計算遲延利息天數
- 計算消滅時效經過期間
"""

from __future__ import annotations

from .date_utils import (
    roc_to_date,
    date_to_roc_display,
    elapsed_days,
    elapsed_months_detail,
    elapsed_years_detail,
)


def calc_elapsed_time(start_str: str, end_str: str) -> dict:
    """計算兩個民國年日期之間的經過時間

    Args:
        start_str: 起始日期（民國年 YYYMMDD 或 YYY/MM/DD）
        end_str: 終止日期（民國年 YYYMMDD 或 YYY/MM/DD）

    Returns:
        dict 包含:
            total_days: 總天數
            months: 月數（小數）
            years: 年數（小數）
            detail: 明細分解
            start_date: 起始日（民國年顯示格式）
            end_date: 終止日（民國年顯示格式）
            start_date_ad: 起始日（西元 ISO 格式）
            end_date_ad: 終止日（西元 ISO 格式）
    """
    start = roc_to_date(start_str)
    end = roc_to_date(end_str)

    if end < start:
        raise ValueError(
            f"終止日 ({date_to_roc_display(end)}) 不可早於起始日 ({date_to_roc_display(start)})"
        )

    # 總天數
    total_days = elapsed_days(start, end)

    # 月數分解
    full_months, rem_days_m, denom_m = elapsed_months_detail(start, end)
    months_decimal = round(full_months + (rem_days_m / denom_m if denom_m else 0), 4)

    # 年數分解
    full_years, rem_days_y, denom_y = elapsed_years_detail(start, end)
    years_decimal = round(full_years + (rem_days_y / denom_y if denom_y else 0), 4)

    # 明細文字
    detail_parts = [
        f"起始日: {date_to_roc_display(start)}（{start.isoformat()}）",
        f"終止日: {date_to_roc_display(end)}（{end.isoformat()}）",
        f"總天數: {total_days} 天",
        f"月數: {full_months} 個月又 {rem_days_m} 天（{months_decimal} 個月）",
        f"年數: {full_years} 年又 {rem_days_y} 天（{years_decimal} 年）",
    ]

    return {
        "total_days": total_days,
        "months": months_decimal,
        "months_full": full_months,
        "months_remainder_days": rem_days_m,
        "years": years_decimal,
        "years_full": full_years,
        "years_remainder_days": rem_days_y,
        "detail": "；".join(detail_parts),
        "start_date": date_to_roc_display(start),
        "end_date": date_to_roc_display(end),
        "start_date_ad": start.isoformat(),
        "end_date_ad": end.isoformat(),
    }


# ─── 測試 ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 60)
    print("經過時間試算測試")
    print("=" * 60)

    # 測試 1: 一般日期
    r = calc_elapsed_time("110/01/15", "115/03/12")
    print(f"\n【{r['start_date']} → {r['end_date']}】")
    print(f"  總天數: {r['total_days']} 天")
    print(f"  月數: {r['months']} 個月（{r['months_full']} 月又 {r['months_remainder_days']} 天）")
    print(f"  年數: {r['years']} 年（{r['years_full']} 年又 {r['years_remainder_days']} 天）")

    # 測試 2: 短期間
    r = calc_elapsed_time("115/02/01", "115/03/01")
    print(f"\n【{r['start_date']} → {r['end_date']}】")
    print(f"  總天數: {r['total_days']} 天")
    print(f"  月數: {r['months']} 個月")

    # 測試 3: 跨年
    r = calc_elapsed_time("113/06/01", "115/06/01")
    print(f"\n【{r['start_date']} → {r['end_date']}】")
    print(f"  總天數: {r['total_days']} 天")
    print(f"  年數: {r['years']} 年")

    # 測試 4: 同一天
    r = calc_elapsed_time("115/03/12", "115/03/12")
    print(f"\n【{r['start_date']} → {r['end_date']}（同日）】")
    print(f"  總天數: {r['total_days']} 天")
