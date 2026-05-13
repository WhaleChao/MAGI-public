#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cmd/export_cmd.py — Excel/CSV 匯出指令
"""
from __future__ import annotations

import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

from data.perf_tracker import _DEFAULT_MODEL_PARAMS, _load_perf
from data.watchlist import WatchItem, _watchlist_from_state
from predict.predict_engine import _predict_one

# ── 路徑推導 ─────────────────────────────────────────────────────
_SKILL_DIR = Path(__file__).resolve().parent.parent  # market-briefing/
_MAGI_ROOT = _SKILL_DIR.parents[1]


def _tz_now() -> datetime:
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Asia/Taipei"))
    except Exception:
        return datetime.now()


def _skill_python() -> str:
    import sys
    return (os.environ.get("MAGI_SKILL_PYTHON") or sys.executable or "python3").strip() or "python3"


def _cmd_export(state: Dict[str, Any], mode: str = "deep") -> str:
    """匯出追蹤清單分析為 Excel (.xlsx)。"""
    items = _watchlist_from_state(state)
    if not items:
        return "⚠️ 追蹤清單為空，請先設定追蹤股票。"

    perf = _load_perf()
    params = perf.get("model_params") if isinstance(perf.get("model_params"), dict) else dict(_DEFAULT_MODEL_PARAMS)

    with ThreadPoolExecutor(max_workers=min(len(items), 6)) as pool:
        futures = {pool.submit(_predict_one, it, params, mode): it for it in items}
        rows: List[Dict[str, Any]] = []
        for fut in as_completed(futures):
            try:
                rows.append(fut.result(timeout=60))
            except Exception:
                it = futures[fut]
                rows.append({"symbol": it.symbol, "label": it.label, "market": it.market, "ok": False})

    order = {it.symbol: i for i, it in enumerate(items)}
    rows.sort(key=lambda r: order.get(r.get("symbol", ""), 999))

    now = _tz_now()
    date_str = now.strftime("%Y%m%d")
    export_dir = _MAGI_ROOT / "static" / "exports"
    export_dir.mkdir(parents=True, exist_ok=True)
    xlsx_path = str(export_dir / f"market_briefing_{date_str}.xlsx")

    mode_tag = {"quick": "快速", "technical": "技術分析", "deep": "深度"}.get(mode, mode)

    header = ["代號", "名稱", "市場", "昨收", "預估價", "預估漲跌%", "信心%", "趨勢(EMA)", "動量5日%", "波動率"]
    if mode in {"technical", "deep"}:
        header += ["RSI(14)", "MACD柱", "BBands %B"]
    if mode == "deep":
        header += ["支撐", "阻力", "量能", "ADX", "趨勢強度"]

    data_rows: List[List[Any]] = []
    for r in rows:
        if not r.get("ok"):
            data_rows.append([r.get("symbol", ""), r.get("label", ""), r.get("market", ""), "資料取得失敗"] + [""] * (len(header) - 4))
            continue
        row = [
            r.get("symbol", ""),
            r.get("label", ""),
            r.get("market", ""),
            round(float(r.get("last") or 0), 2),
            round(float(r.get("pred_price") or 0), 2),
            round(float(r.get("pred_pct") or 0), 2),
            int(r.get("confidence") or 0),
            round(float(r.get("trend") or 0), 3),
            round(float(r.get("mom5") or 0), 3),
            round(float(r.get("vol") or 0), 3),
        ]
        if mode in {"technical", "deep"}:
            row += [
                round(float(r.get("rsi") or 0), 1) if r.get("rsi") is not None else "",
                round(float(r.get("macd_hist") or 0), 4) if r.get("macd_hist") is not None else "",
                round(float(r.get("bb_pct") or 0), 1) if r.get("bb_pct") is not None else "",
            ]
        if mode == "deep":
            row += [
                round(float(r.get("support") or 0), 2) if r.get("support") is not None else "",
                round(float(r.get("resistance") or 0), 2) if r.get("resistance") is not None else "",
                str(r.get("volume_trend") or ""),
                round(float(r.get("adx") or 0), 1) if r.get("adx") is not None else "",
                str(r.get("trend_strength") or ""),
            ]
        data_rows.append(row)

    # Try using xlsx skill, fallback to direct openpyxl
    try:
        import subprocess as _sp
        xlsx_skill = str(_MAGI_ROOT / "skills" / "xlsx" / "action.py")
        py = _skill_python()
        if os.path.exists(xlsx_skill):
            payload = {
                "sheets": [{
                    "name": f"股市分析_{mode_tag}",
                    "headers": header,
                    "rows": data_rows,
                }],
                "output_path": xlsx_path,
            }
            payload_str = json.dumps(payload, ensure_ascii=False)
            result = _sp.run(
                [py, xlsx_skill, "--task", "create", "--text", payload_str],
                capture_output=True, timeout=30, text=True,
                cwd=str(_MAGI_ROOT),
            )
            if result.returncode == 0 and os.path.exists(xlsx_path):
                return f"✅ Excel 報表已匯出：{xlsx_path}\n共 {len(rows)} 檔標的，{mode_tag}模式。"
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s", __name__, exc_info=True)

    # Fallback: direct openpyxl
    try:
        import openpyxl
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = f"股市分析_{mode_tag}"

        ws.append([f"MAGI 股市分析報表 — {mode_tag}模式（{now.strftime('%Y-%m-%d %H:%M')}）"])
        ws.append([])
        ws.append(header)

        for dr in data_rows:
            ws.append(dr)

        for col_idx, col_name in enumerate(header, 1):
            ws.column_dimensions[openpyxl.utils.get_column_letter(col_idx)].width = max(len(str(col_name)) * 1.5, 10)

        wb.save(xlsx_path)
        return f"✅ Excel 報表已匯出：{xlsx_path}\n共 {len(rows)} 檔標的，{mode_tag}模式。"
    except ImportError:
        csv_path = xlsx_path.replace(".xlsx", ".csv")
        with open(csv_path, "w", encoding="utf-8-sig") as f:
            f.write(",".join(header) + "\n")
            for dr in data_rows:
                f.write(",".join(str(x) for x in dr) + "\n")
        return f"✅ CSV 報表已匯出（openpyxl 未安裝）：{csv_path}\n共 {len(rows)} 檔標的，{mode_tag}模式。"
