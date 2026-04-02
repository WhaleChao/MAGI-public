#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
judicial-tools/action.py

司法院辦案小工具集 — 純 Python 離線實作
涵蓋 17 項計算工具：規費、上訴期間、折舊、霍夫曼、利息違約金、
刑度加重減輕、土地分割/合併、不當得利、繼承系統表等

來源: https://gdgt.judicial.gov.tw/judtool/MAINPAGE.htm
"""

from __future__ import annotations

import argparse
import json
import sys
import os
import re
from typing import Any, Dict

# ─── 動態 import path ─────────────────────────────────────────────────────────
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

# ─── 工具指令表 ─────────────────────────────────────────────────────────────

TOOLS = {
    # 通用
    "judicial_fee":      {"code": "GDGT08", "name": "司法規費試算（114年新法）",       "category": "通用"},
    "judicial_fee_old":  {"code": "GDGT24", "name": "司法規費試算（113年底前舊法）",   "category": "通用"},
    "appeal_period":     {"code": "GDGT01", "name": "上訴抗告再審期間試算",           "category": "通用"},
    "elapsed_time":      {"code": "GDGT09", "name": "經過時間試算",                   "category": "通用"},
    # 民事
    "depreciation":      {"code": "GDGT02", "name": "折舊自動試算表",                 "category": "民事"},
    "hoffman":           {"code": "GDGT03", "name": "霍夫曼一次給付試算",             "category": "民事"},
    "severance":         {"code": "GDGT04", "name": "資遣費試算",                     "category": "民事"},
    "annual_leave":      {"code": "GDGT07", "name": "特休日數試算",                   "category": "民事"},
    "interest":          {"code": "GDGT12", "name": "利息及違約金試算",               "category": "民事"},
    "co_owner_share":    {"code": "GDGT20", "name": "共有人應有部分比例",             "category": "民事"},
    # 刑事
    "sentence":          {"code": "GDGT22", "name": "法定刑度加重減輕試算",           "category": "刑事"},
    # 其他
    "land_division":     {"code": "GDGT13", "name": "土地分割共有物面積與地價之試算", "category": "其他"},
    "land_partial":      {"code": "GDGT14", "name": "土地單筆部分維持共用/應有部分",  "category": "其他"},
    "land_merge":        {"code": "GDGT15", "name": "土地數筆合併後應有部分之試算",   "category": "其他"},
    "unjust_enrichment": {"code": "GDGT16", "name": "相當租金不當得利之試算",         "category": "其他"},
    "penalty_interest":  {"code": "GDGT18", "name": "違約金與利息之試算",             "category": "其他"},
    "inheritance":       {"code": "GDGT19", "name": "繼承系統表",                     "category": "其他"},
}


# ─── Dispatch ─────────────────────────────────────────────────────────────────

def dispatch(command: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """根據指令名稱分派到對應計算模組"""

    # ── 通用 ──
    if command in ("judicial_fee", "judicial_fee_old"):
        from calculators.judicial_fee import calc_judicial_fee
        law = "new" if command == "judicial_fee" else "old"
        return calc_judicial_fee(
            category=params.get("category", "民事"),
            procedure=params.get("procedure", "訴訟事件"),
            statute=params.get("statute", ""),
            amount=float(params.get("amount", 0)),
            level=params.get("level", "一審"),
            law=law,
        )

    if command == "appeal_period":
        from calculators.appeal_period import calc_appeal_period
        return calc_appeal_period(
            case_type=params.get("case_type", "民事"),
            court=params.get("court", "TPD"),
            appeal_type=params.get("appeal_type", "上訴"),
            serve_date=params.get("serve_date", ""),
            serve_method=params.get("serve_method", "一般"),
            location_type=params.get("location_type", "臺灣地區"),
            extra_transit_days=int(params.get("extra_transit_days", 0)),
        )

    if command == "elapsed_time":
        from calculators.elapsed_time import calc_elapsed_time
        return calc_elapsed_time(
            start_str=params.get("start", ""),
            end_str=params.get("end", ""),
        )

    # ── 民事 ──
    if command == "depreciation":
        from calculators.depreciation import calc_depreciation
        return calc_depreciation(
            method=params.get("method", "平均法"),
            cost=float(params.get("cost", 0)),
            useful_years=int(params.get("useful_years", 1)),
            used_years=int(params.get("used_years", 0)),
            used_months=int(params.get("used_months", 0)),
            residual=params.get("residual"),
        )

    if command == "hoffman":
        from calculators.hoffman import calc_hoffman
        return calc_hoffman(
            amount=float(params.get("amount", params.get("monthly_amount", 0))),
            rate=float(params.get("rate", 5)),
            years=int(params.get("years", 0)),
            months=int(params.get("months", 0)),
            period_type=params.get("period_type", "月"),
            supporters=int(params.get("supporters", 1)),
            first_period_discount=params.get("first_period_discount", False),
        )

    if command == "severance":
        return _delegate_labor("資遣費", params)

    if command == "annual_leave":
        return _delegate_labor("特休", params)

    if command == "interest":
        from calculators.interest import calc_interest
        return calc_interest(
            principal=float(params.get("principal", 0)),
            annual_rate=float(params.get("rate", 5)),
            start_str=params.get("start", ""),
            end_str=params.get("end", ""),
            calc_type=params.get("type", "利息"),
            monthly_payment=float(params.get("monthly_payment", 0)),
        )

    if command == "co_owner_share":
        from calculators.co_owner import calc_co_owner_share
        return calc_co_owner_share(owners=params.get("owners", []))

    # ── 刑事 ──
    if command == "sentence":
        from calculators.sentence import calc_sentence
        min_v = float(params.get("min_years", params.get("min_val", 0)))
        max_v = float(params.get("max_years", params.get("max_val", 0)))
        min_u = params.get("min_unit", "年")
        max_u = params.get("max_unit", "年")
        stype = params.get("type", "有期徒刑")
        # 有期徒刑內部以月為單位
        if stype == "有期徒刑":
            if min_u == "年":
                min_v = min_v * 12
                min_u = "月"
            if max_u == "年":
                max_v = max_v * 12
                max_u = "月"
        return calc_sentence(
            sentence_type=stype,
            min_val=min_v,
            max_val=max_v,
            min_unit=min_u,
            max_unit=max_u,
            aggravations=params.get("aggravations", []),
            mitigations=params.get("mitigations", []),
        )

    # ── 其他 ──
    if command == "land_division":
        from calculators.land_division import calc_land_division
        parcels = params.get("parcels", [])
        # 允許簡寫 key 名
        for p in parcels:
            if "area" in p and "total_area" not in p:
                p["total_area"] = p.pop("area")
            if "price" in p and "price_per_sqm" not in p:
                p["price_per_sqm"] = p.pop("price")
        return calc_land_division(parcels=parcels)

    if command == "land_partial":
        from calculators.land_merge import calc_land_partial_share
        return calc_land_partial_share(
            total_area=float(params.get("total_area", 0)),
            price=float(params.get("price", 0)),
            owners=params.get("owners", []),
            shared_group=params.get("shared_group", []),
            split_group=params.get("split_group", []),
        )

    if command == "land_merge":
        from calculators.land_merge import calc_land_merge
        return calc_land_merge(parcels=params.get("parcels", []))

    if command == "unjust_enrichment":
        from calculators.unjust_enrichment import calc_unjust_enrichment
        lv = params.get("land_values", params.get("land_value", 0))
        if isinstance(lv, (int, float)):
            lv = [{"year": 0, "value": float(lv)}]
        return calc_unjust_enrichment(
            land_values=lv,
            area=float(params.get("area", 0)),
            share_n=int(params.get("share_n", 1)),
            share_d=int(params.get("share_d", 1)),
            rate=float(params.get("rate", 5)),
            start_str=params.get("start", ""),
            end_str=params.get("end", ""),
        )

    if command == "penalty_interest":
        from calculators.interest import calc_interest
        return calc_interest(
            principal=float(params.get("principal", 0)),
            annual_rate=float(params.get("rate", 5)),
            start_str=params.get("start", ""),
            end_str=params.get("end", ""),
            calc_type=params.get("type", "違約金"),
            monthly_payment=float(params.get("monthly_payment", 0)),
        )

    if command == "inheritance":
        from calculators.inheritance import calc_inheritance
        return calc_inheritance(
            decedent=params.get("decedent", {}),
            heirs=params.get("heirs", []),
        )

    return {"error": f"未知指令: {command}"}


def _delegate_labor(keyword: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """委派至 labor-law-calculator skill"""
    labor_action = os.path.join(
        os.path.dirname(_HERE), "labor-law-calculator", "action.py"
    )
    if not os.path.exists(labor_action):
        return {"error": "labor-law-calculator skill 不存在，無法計算資遣費/特休"}

    import subprocess
    task_parts = [keyword]
    if "salary" in params or "monthly_salary" in params:
        sal = params.get("salary", params.get("monthly_salary", ""))
        task_parts.append(f"月薪{sal}")
    if "start" in params or "hire_date" in params:
        task_parts.append(f"到職日{params.get('hire_date', params.get('start', ''))}")
    if "end" in params or "leave_date" in params:
        task_parts.append(f"離職日{params.get('leave_date', params.get('end', ''))}")

    task_str = "，".join(task_parts)
    try:
        r = subprocess.run(
            [sys.executable, labor_action, "--task", task_str],
            capture_output=True, text=True, timeout=30,
        )
        return {"delegated_to": "labor-law-calculator", "output": r.stdout.strip(), "stderr": r.stderr.strip()}
    except Exception as e:
        return {"error": f"委派 labor-law-calculator 失敗: {e}"}


# ─── Help ─────────────────────────────────────────────────────────────────────

def print_help():
    print("=" * 70)
    print("  司法院辦案小工具集 (judicial-tools)")
    print("  來源: https://gdgt.judicial.gov.tw/judtool/MAINPAGE.htm")
    print("=" * 70)
    print()

    current_cat = None
    for cmd, info in TOOLS.items():
        if info["category"] != current_cat:
            current_cat = info["category"]
            print(f"【{current_cat}】")
        print(f"  {cmd:<22s} {info['code']}  {info['name']}")
    print()
    print("用法:")
    print('  python action.py --task \'<指令> {"param1":"value1",...}\'')
    print('  python action.py --task "help"')
    print()
    print("範例:")
    print('  python action.py --task \'judicial_fee {"category":"民事","procedure":"訴訟事件","amount":1000000}\'')
    print('  python action.py --task \'elapsed_time {"start":"1140101","end":"1150312"}\'')
    print('  python action.py --task \'depreciation {"method":"平均法","cost":500000,"useful_years":5,"used_years":3}\'')
    print()
    print("※ 所有試算結果僅供參考，實際仍應以法院裁判結果為準")


# ─── Parse task ───────────────────────────────────────────────────────────────

def parse_task(task: str):
    """Parse task string into (command, params_dict)"""
    task = task.strip()

    if task.lower() in ("help", "--help", "?", "說明"):
        return "help", {}

    # Try to find JSON block
    json_match = re.search(r'\{.*\}', task, re.DOTALL)
    if json_match:
        command = task[:json_match.start()].strip()
        try:
            params = json.loads(json_match.group())
        except json.JSONDecodeError:
            return None, {"error": f"JSON 解析失敗: {json_match.group()[:100]}"}
    else:
        parts = task.split(None, 1)
        command = parts[0]
        params = {}

    # Normalize command
    command = command.lower().replace("-", "_")

    # Allow GDGT code as command
    code_map = {v["code"].lower(): k for k, v in TOOLS.items()}
    if command in code_map:
        command = code_map[command]

    return command, params


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="司法院辦案小工具集")
    parser.add_argument("--task", type=str, default="help", help="指令與參數")
    parser.add_argument("task_pos", nargs="?", default=None, help="指令 (positional)")
    args = parser.parse_args()

    task = args.task if args.task != "help" else (args.task_pos or "help")

    command, params = parse_task(task)

    if command is None:
        print(json.dumps(params, ensure_ascii=False, indent=2))
        sys.exit(1)

    if command == "help":
        print_help()
        sys.exit(0)

    if command not in TOOLS:
        print(json.dumps({
            "error": f"未知指令: {command}",
            "available": list(TOOLS.keys()),
        }, ensure_ascii=False, indent=2))
        sys.exit(1)

    try:
        result = dispatch(command, params)
        result["_tool"] = TOOLS[command]["name"]
        result["_code"] = TOOLS[command]["code"]
        result["_disclaimer"] = "本試算結果僅供參考，實際仍應以法院裁判結果為準"
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    except Exception as e:
        print(json.dumps({
            "error": str(e),
            "command": command,
            "params": params,
        }, ensure_ascii=False, indent=2))
        sys.exit(1)


if __name__ == "__main__":
    main()
