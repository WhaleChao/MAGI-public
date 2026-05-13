#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""繼承系統表試算 (GDGT19)

依民法第 1138～1144 條規定，建立繼承系統表並計算各繼承人之應繼分。
支援代位繼承（民法§1140）。
"""

from __future__ import annotations

from fractions import Fraction
from typing import Optional, Union

from .date_utils import roc_to_date, date_to_roc_display


# ─── 繼承順位分類 ──────────────────────────────────────────────────────────────

# 關係 → 繼承順位
_RELATION_ORDER = {
    # 第一順位：直系血親卑親屬
    "長子": 1, "次子": 1, "三子": 1, "四子": 1, "五子": 1,
    "長女": 1, "次女": 1, "三女": 1, "四女": 1, "五女": 1,
    "子": 1, "女": 1, "養子": 1, "養女": 1,
    # 第二順位：父母
    "父": 2, "母": 2, "養父": 2, "養母": 2,
    # 第三順位：兄弟姊妹
    "兄": 3, "弟": 3, "姊": 3, "妹": 3,
    # 第四順位：祖父母
    "祖父": 4, "祖母": 4, "外祖父": 4, "外祖母": 4,
}

_ORDER_NAMES = {
    1: "直系血親卑親屬",
    2: "父母",
    3: "兄弟姊妹",
    4: "祖父母",
}


def _is_spouse(relation: str) -> bool:
    return relation in ("配偶", "妻", "夫")


def _get_order(relation: str) -> Optional[int]:
    """取得繼承順位，配偶回傳 None"""
    if _is_spouse(relation):
        return None
    return _RELATION_ORDER.get(relation)


def _is_predeceased(heir: dict, decedent_death: Optional[str]) -> bool:
    """判斷繼承人是否先於被繼承人死亡（即需要代位繼承）"""
    if "death" not in heir or heir["death"] is None:
        return False
    if decedent_death is None:
        return False
    try:
        heir_death = roc_to_date(heir["death"]) if isinstance(heir["death"], str) else heir["death"]
        dec_death = roc_to_date(decedent_death) if isinstance(decedent_death, str) else decedent_death
        return heir_death <= dec_death
    except Exception:
        return False


def calc_inheritance(
    decedent: dict,
    heirs: list[dict],
) -> dict:
    """繼承系統表試算。

    Parameters
    ----------
    decedent : dict
        被繼承人 {"name": str, "birth": str, "death": str}
        日期格式為民國年 (YYY/MM/DD)
    heirs : list[dict]
        繼承人列表，每人：
        {
            "name": str,
            "relation": str,  # 配偶/長子/長女/父/母/兄/弟/姊/妹/祖父/祖母 等
            "birth": str,
            "death": str (optional),  # 若已歿
            "children": list[dict] (optional)  # 代位繼承人
                每個 child: {"name": str, "birth": str}
        }

    Returns
    -------
    dict
        inheritance_order: int 適用之繼承順位
        legal_shares: dict[str, str]  各人應繼分（分數字串）
        chart_text: str 繼承系統表文字
        steps: list[str] 計算步驟說明
    """
    dec_name = decedent["name"]
    dec_death = decedent.get("death")
    steps: list[str] = []

    # 分類繼承人
    spouse: Optional[dict] = None
    by_order: dict[int, list[dict]] = {1: [], 2: [], 3: [], 4: []}

    for h in heirs:
        rel = h["relation"]
        if _is_spouse(rel):
            # 配偶是否已歿
            if _is_predeceased(h, dec_death):
                steps.append(f"{h['name']}（{rel}）先於被繼承人死亡，不列為繼承人")
            else:
                spouse = h
        else:
            order = _get_order(rel)
            if order is None:
                steps.append(f"無法辨識 {h['name']} 之關係「{rel}」，跳過")
                continue
            by_order.setdefault(order, []).append(h)

    # 依序尋找有效繼承人
    active_order = None
    active_heirs: list[dict] = []

    for order in [1, 2, 3, 4]:
        candidates = by_order.get(order, [])
        effective: list[dict] = []
        for c in candidates:
            if _is_predeceased(c, dec_death):
                # 代位繼承（僅第一順位適用，民法§1140）
                children = c.get("children", [])
                if order == 1 and children:
                    steps.append(
                        f"{c['name']}（{c['relation']}）先於被繼承人死亡，"
                        f"由其直系血親卑親屬 {', '.join(ch['name'] for ch in children)} 代位繼承"
                    )
                    effective.append({
                        **c,
                        "_representation": True,
                        "_rep_children": children,
                    })
                else:
                    steps.append(
                        f"{c['name']}（{c['relation']}）先於被繼承人死亡，"
                        f"非第一順位或無代位繼承人，不列入"
                    )
            else:
                effective.append(c)

        if effective:
            active_order = order
            active_heirs = effective
            break

    if active_order is None and spouse is None:
        return {
            "inheritance_order": None,
            "legal_shares": {},
            "chart_text": f"被繼承人 {dec_name} 無繼承人",
            "steps": steps,
        }

    steps.append(
        f"適用繼承順位：第 {active_order} 順位（{_ORDER_NAMES.get(active_order, '—')}）"
        if active_order else "僅配偶繼承"
    )

    # ─── 計算應繼分 ──────────────────────────────────────────────────────────
    shares: dict[str, Fraction] = {}

    if active_order is None:
        # 僅配偶
        if spouse:
            shares[spouse["name"]] = Fraction(1)
            steps.append(f"配偶 {spouse['name']} 為唯一繼承人，應繼分 = 全部")
    else:
        # 計算同一順位的有效人頭數（代位繼承人算原本那 1 份）
        head_count = len(active_heirs)
        total_heads = head_count + (1 if spouse else 0)

        if active_order == 1:
            # 配偶與第一順位平均分配
            if spouse:
                per_share = Fraction(1, total_heads)
                shares[spouse["name"]] = per_share
                steps.append(
                    f"配偶 {spouse['name']} 與第一順位繼承人共 {total_heads} 人平均分配，"
                    f"各得 1/{total_heads}"
                )
            else:
                per_share = Fraction(1, head_count)

            for h in active_heirs:
                if h.get("_representation"):
                    # 代位繼承：該份由子女均分
                    rep_children = h["_rep_children"]
                    parent_share = per_share
                    child_share = parent_share / len(rep_children)
                    for ch in rep_children:
                        shares[ch["name"]] = child_share
                        steps.append(
                            f"  {ch['name']}（代位 {h['name']}）："
                            f"應繼分 = {parent_share} / {len(rep_children)} = {child_share}"
                        )
                else:
                    shares[h["name"]] = per_share

        elif active_order == 2:
            # 配偶 1/2，其餘平分另 1/2
            if spouse:
                shares[spouse["name"]] = Fraction(1, 2)
                rest = Fraction(1, 2)
                steps.append(f"配偶 {spouse['name']}：應繼分 = 1/2")
            else:
                rest = Fraction(1)
            per_share = rest / head_count
            for h in active_heirs:
                shares[h["name"]] = per_share

        elif active_order == 3:
            # 配偶 1/2，其餘平分另 1/2
            if spouse:
                shares[spouse["name"]] = Fraction(1, 2)
                rest = Fraction(1, 2)
                steps.append(f"配偶 {spouse['name']}：應繼分 = 1/2")
            else:
                rest = Fraction(1)
            per_share = rest / head_count
            for h in active_heirs:
                shares[h["name"]] = per_share

        elif active_order == 4:
            # 配偶 2/3，其餘平分 1/3
            if spouse:
                shares[spouse["name"]] = Fraction(2, 3)
                rest = Fraction(1, 3)
                steps.append(f"配偶 {spouse['name']}：應繼分 = 2/3")
            else:
                rest = Fraction(1)
            per_share = rest / head_count
            for h in active_heirs:
                shares[h["name"]] = per_share

        # 非代位的普通繼承人加入步驟說明
        for h in active_heirs:
            if not h.get("_representation") and h["name"] in shares:
                steps.append(f"  {h['name']}（{h['relation']}）：應繼分 = {shares[h['name']]}")

    # ─── 繼承系統表文字 ──────────────────────────────────────────────────────
    chart_lines = _build_chart(decedent, spouse, active_heirs, active_order, shares)

    # 轉換 shares 為字串
    legal_shares = {name: str(frac) for name, frac in shares.items()}

    return {
        "inheritance_order": active_order,
        "inheritance_order_name": _ORDER_NAMES.get(active_order, "—") if active_order else "僅配偶",
        "legal_shares": legal_shares,
        "chart_text": "\n".join(chart_lines),
        "steps": steps,
    }


def _build_chart(
    decedent: dict,
    spouse: Optional[dict],
    active_heirs: list[dict],
    order: Optional[int],
    shares: dict[str, Fraction],
) -> list[str]:
    """建立繼承系統表文字"""
    lines: list[str] = []
    dec = decedent
    lines.append("┌─────────────────────────────────┐")
    lines.append("│          繼 承 系 統 表          │")
    lines.append("└─────────────────────────────────┘")
    lines.append("")

    # 被繼承人
    dec_birth = dec.get("birth", "")
    dec_death = dec.get("death", "")
    lines.append(f"被繼承人：{dec['name']}")
    if dec_birth:
        lines.append(f"  出生：{dec_birth}")
    if dec_death:
        lines.append(f"  死亡：{dec_death}")
    lines.append("")

    # 配偶
    if spouse:
        sp_share = shares.get(spouse["name"], "—")
        lines.append(f"配偶：{spouse['name']}（應繼分 {sp_share}）")
        lines.append("")

    # 繼承人
    if active_heirs:
        order_name = _ORDER_NAMES.get(order, "")
        lines.append(f"第 {order} 順位繼承人（{order_name}）：")
        for h in active_heirs:
            if h.get("_representation"):
                lines.append(f"  ├ {h['name']}（{h['relation']}）── 已歿，代位繼承：")
                for ch in h["_rep_children"]:
                    ch_share = shares.get(ch["name"], "—")
                    lines.append(f"  │   └ {ch['name']}（應繼分 {ch_share}）")
            else:
                h_share = shares.get(h["name"], "—")
                death_mark = ""
                if h.get("death"):
                    death_mark = "（已歿）"
                lines.append(f"  ├ {h['name']}（{h['relation']}）{death_mark}"
                             f"── 應繼分 {h_share}")

    return lines


# ─── CLI 測試 ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    result = calc_inheritance(
        decedent={"name": "王大明", "birth": "040/05/15", "death": "114/01/10"},
        heirs=[
            {"name": "李美麗", "relation": "配偶", "birth": "042/08/20"},
            {"name": "王小明", "relation": "長子", "birth": "065/03/01"},
            {"name": "王小華", "relation": "次子", "birth": "067/11/15",
             "death": "110/06/01",
             "children": [
                 {"name": "王孫一", "birth": "090/04/10"},
                 {"name": "王孫二", "birth": "092/07/22"},
             ]},
            {"name": "王美玲", "relation": "長女", "birth": "070/02/28"},
        ],
    )

    print("=== 繼承系統表試算 ===")
    print(result["chart_text"])
    print()
    print(f"繼承順位：第 {result['inheritance_order']} 順位"
          f"（{result['inheritance_order_name']}）")
    print("應繼分：")
    for name, share in result["legal_shares"].items():
        print(f"  {name}：{share}")
    print("\n計算步驟：")
    for s in result["steps"]:
        print(f"  {s}")

    print("\n\n=== 僅配偶繼承 ===")
    result2 = calc_inheritance(
        decedent={"name": "張三", "birth": "050/01/01", "death": "115/01/01"},
        heirs=[
            {"name": "李四", "relation": "配偶", "birth": "052/06/15"},
        ],
    )
    print(result2["chart_text"])
    print(f"應繼分：{result2['legal_shares']}")
