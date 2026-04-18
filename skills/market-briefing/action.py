#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
market-briefing/action.py  (thin launcher)

每日台股/美股追蹤與晨報（08:30）
- 第一天先詢問追蹤清單
- 設定追蹤後，隔天開始晨報
- 之後可隨時新增/移除追蹤
- 報告含：近期價格趨勢 + 財報/申報摘要（台股公開資訊、SEC 申報）
"""

from __future__ import annotations
import logging

import argparse
import json
import os
import sys
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

_SKILL_DIR = Path(__file__).resolve().parent
_MAGI_ROOT = _SKILL_DIR.parent.parent
if str(_MAGI_ROOT) not in sys.path:
    sys.path.insert(0, str(_MAGI_ROOT))
if str(_SKILL_DIR) not in sys.path:
    sys.path.insert(0, str(_SKILL_DIR))

from committee import HedgeFundCommittee
from models.signals import TradingAction


try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None


def _tz_now() -> datetime:
    if ZoneInfo:
        return datetime.now(ZoneInfo("Asia/Taipei"))
    return datetime.now()


def _skill_python() -> str:
    """Return the interpreter used to run this skill, with a safe fallback."""
    return (os.environ.get("MAGI_SKILL_PYTHON") or sys.executable or "python3").strip() or "python3"


_DEFAULT_MAGI_ROOT = Path(__file__).resolve().parents[2]
MAGI_ROOT = Path(os.environ.get("MAGI_ROOT", str(_DEFAULT_MAGI_ROOT)))
if str(MAGI_ROOT) not in sys.path:
    sys.path.insert(0, str(MAGI_ROOT))
AGENT_DIR = MAGI_ROOT / ".agent"
STATE_PATH = AGENT_DIR / "market_watchlist.json"
CACHE_PATH = AGENT_DIR / "market_data_cache.json"
PERF_PATH = AGENT_DIR / "market_perf_history.json"
NOTIFY_LOG_PATH = MAGI_ROOT / "static" / "market_briefing_notify.log"

_DEFAULT_STATE = {
    "watchlist": [],
    "first_prompt_date": "",
    "active_from_date": "",
    "last_report_date": "",
    "updated_at": "",
}

_DEFAULT_MODEL_PARAMS = {
    "w_trend": 0.55,
    "w_mom": 0.45,
    "w_vol": 0.18,
    "bias": 0.0,
    "updated_at": "",
}

COMMON_TW_ALIASES = {
    "台積電": "2330",
    "聯發科": "2454",
    "鴻海": "2317",
    "廣達": "2382",
    "台達電": "2308",
    "中華電": "2412",
    "兆豐金": "2886",
    "富邦金": "2881",
    "國泰金": "2882",
    "台新金": "2887",
}

STOP_WORDS = {
    "請", "幫我", "幫", "設定", "新增", "增加", "追蹤", "股票", "名單", "清單", "移除", "刪除",
    "減少", "不要", "再", "報", "預測", "晨報", "今日", "台灣", "美國", "台股", "美股",
    "和", "以及", "還有", "與", "請問", "我要", "可以", "先", "開始", "隔天", "每天",
}

# ── Sub-module imports ─────────────────────────────────────────────
from data.indicators import (
    _ema, _pct, _clamp, _rsi, _macd, _bbands,
    _support_resistance, _volume_trend, _adx_approx, _safe_mean,
)
from data.fetcher import (
    _http_json, _yahoo_history,
    _get_twse_lookup, _get_sec_tickers,
    _latest_tw_financials, _latest_us_filing,
)
from data.perf_tracker import (
    _load_json, _save_json, _notify_log as _notify_log_impl,
    _load_cache, _save_cache,
    _load_perf, _save_perf,
    _parse_ymd, _next_trade_date, _utc_ts_to_date, _actual_close_on_or_after,
    _sign, _predict_pct_by_params, _solve_linear_4x4,
    _fit_params_from_samples, _mae_for_params,
    _refresh_metrics, _resolve_records_and_tune,
    _upsert_prediction_records, _format_perf_lines,
)
from data.watchlist import (
    WatchItem,
    _unique, _tokenize, _resolve_tokens,
    _load_state, _save_state, _watchlist_from_state,
    _first_prompt_message, _format_watchlist,
)
from predict.predict_engine import _predict_one as _predict_one_impl, _render_report
from mbcmd.backtest_cmd import _cmd_backtest as _cmd_backtest_impl
from mbcmd.sector_cmd import (
    _TWSE_SECTOR_NAMES, _resolve_sector_name,
    _get_twse_sector_map, _find_peers,
    _cmd_sector as _cmd_sector_impl,
)
from mbcmd.comps_cmd import _fetch_comps_metrics, _cmd_comps as _cmd_comps_impl
from mbcmd.export_cmd import _cmd_export as _cmd_export_impl


# ── Thin wrappers to preserve original call signatures ────────────

def _notify_log(event: str, detail: str = "") -> None:
    _notify_log_impl(event, detail, notify_log_path=NOTIFY_LOG_PATH)


def _cmd_backtest() -> str:
    return _cmd_backtest_impl()


def _cmd_sector(text: str, mode: str = "deep") -> str:
    return _cmd_sector_impl(text, mode=mode)


def _cmd_comps(text: str) -> str:
    return _cmd_comps_impl(text)


def _cmd_export(state: Dict[str, Any], mode: str = "deep") -> str:
    return _cmd_export_impl(state, mode=mode)


def _predict_one(item: WatchItem, params: Dict[str, float], mode: str = "quick") -> Dict[str, Any]:
    """Wrapper that injects committee callback for deep mode."""
    def _committee_cb(wi: WatchItem, m: str, market_data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        try:
            committee = HedgeFundCommittee()
            state_res = committee.run_analysis(wi.symbol, wi.label, market_data)
            final = state_res.final_decision
            if final:
                action_icons = {"BUY": "🚀 [看多]", "SELL": "🩸 [看空]", "HOLD": "⚖️ [持平]", "NEUTRAL": "⚪ [觀察]"}
                icon = action_icons.get(final.action.value, "⚪")
                return {
                    "committee_verdict": final.action.value,
                    "line": (
                        f"{icon} {wi.label}：委員會綜合評級【{final.action.value}】 (信心 {final.confidence*100:.0f}%)\n"
                        f"  委員會論點：{final.reasoning[:120]}..."
                    ),
                }
        except Exception as ce:
            logging.getLogger(__name__).error("Committee analysis failed for %s: %s", wi.symbol, ce)
        return None

    return _predict_one_impl(item, params, mode, committee_callback=_committee_cb if mode == "deep" else None)


def _maybe_notify(text: str, notify: bool) -> bool:
    if not notify:
        return False
    for p in (str(MAGI_ROOT), str(MAGI_ROOT.parent)):
        if p and p not in sys.path:
            sys.path.insert(0, p)
    try:
        from skills.ops.red_phone import alert_admin  # type: ignore

        r = alert_admin(
            text,
            severity="info",
            source="market_briefing",
            topic_key="market",
        ) or {}
        ok = bool(r.get("telegram") or r.get("line") or r.get("discord"))
        if not ok:
            _notify_log("notify_failed", json.dumps(r, ensure_ascii=False)[:1200])
        return ok
    except Exception as e:
        _notify_log("notify_exception", f"{type(e).__name__}: {e}; {traceback.format_exc(limit=2)}")
        return False


def _cmd_prompt(state: Dict[str, Any], notify: bool) -> str:
    today = _tz_now().strftime("%Y-%m-%d")
    if not state.get("first_prompt_date"):
        state["first_prompt_date"] = today
        _save_state(state)
    msg = _first_prompt_message()
    _maybe_notify(msg, notify=notify)
    return msg


def _cmd_list(state: Dict[str, Any]) -> str:
    items = _watchlist_from_state(state)
    active_from = str(state.get("active_from_date") or "").strip()
    lines = ["📌 目前追蹤股票：", _format_watchlist(items)]
    if active_from:
        lines.append(f"晨報啟用日：{active_from} 08:30")
    return "\n".join(lines)


def _register_financial_crawl_targets(items: "List[WatchItem]") -> None:
    """Register financial report URLs for watchlist items into the crawler-targets skill."""
    try:
        import subprocess as _sp
        crawler_script = str(MAGI_ROOT / "skills" / "crawler-targets" / "action.py")
        if not os.path.exists(crawler_script):
            return
        py = _skill_python()
        for item in items:
            sym = item.symbol.upper().split(".")[0]
            if item.market == "US":
                slug_map = {
                    "AAPL": "apple", "TSLA": "tesla", "MSFT": "microsoft",
                    "GOOG": "alphabet", "GOOGL": "alphabet", "AMZN": "amazon",
                    "META": "meta-platforms", "NVDA": "nvidia", "QQQ": "invesco-qqq-trust",
                }
                slug = slug_map.get(sym, sym.lower())
                url = f"https://www.macrotrends.net/stocks/charts/{sym}/{slug}/financial-statements"
                note = f"{sym} 財報"
            elif item.market == "TW":
                code = sym
                url = f"https://mops.twse.com.tw/mops/web/t05st09_new?id={code}"
                note = f"{item.label}({code}) 重大訊息"
            else:
                continue
            payload = json.dumps({"url": url, "note": note}, ensure_ascii=False)
            _sp.run(
                [py, crawler_script, "--task", f"add {payload}"],
                capture_output=True, timeout=10,
                cwd=str(MAGI_ROOT),
            )
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, "_register_financial_crawl_targets", exc_info=True)


def _update_watchlist(state: Dict[str, Any], mode: str, text: str) -> str:
    existing = _watchlist_from_state(state)
    parsed = _resolve_tokens(text)
    if mode in {"set", "add"} and not parsed:
        return (
            "⚠️ 我沒解析到股票代號，請用這種格式：\n"
            "- 追蹤股票：台積電、聯發科、AAPL、MSFT"
        )

    if mode == "set":
        new_items = parsed
    elif mode == "add":
        new_items = _unique(existing + parsed)
    elif mode == "remove":
        if not parsed:
            tokens = {x.upper() for x in _tokenize(text)}
        else:
            tokens = {x.symbol.upper() for x in parsed} | {x.label.upper() for x in parsed}
        kept: List[WatchItem] = []
        for it in existing:
            key = it.symbol.upper()
            if key in tokens or it.label.upper() in tokens:
                continue
            rm = False
            for tk in tokens:
                if tk and (tk in key or tk in it.label.upper()):
                    rm = True
                    break
            if not rm:
                kept.append(it)
        new_items = kept
    else:
        return "⚠️ 不支援的模式"

    today = _tz_now().date()
    old_empty = len(existing) == 0

    state["watchlist"] = [x.to_dict() for x in new_items]
    if old_empty and new_items:
        state["active_from_date"] = (today + timedelta(days=1)).strftime("%Y-%m-%d")
    elif not new_items:
        state["active_from_date"] = ""

    _save_state(state)
    _register_financial_crawl_targets(new_items)

    if mode == "set":
        prefix = "✅ 已更新追蹤清單"
    elif mode == "add":
        prefix = "✅ 已新增追蹤股票"
    else:
        prefix = "✅ 已更新（移除）追蹤清單"

    lines = [prefix, _format_watchlist(new_items)]
    if state.get("active_from_date"):
        lines.append(f"我會從 {state['active_from_date']} 08:30 開始回報每日預測。")
    elif not new_items:
        lines.append("目前清單為空，我會先停止晨報。")
    return "\n".join(lines)


def _cmd_briefing(state: Dict[str, Any], notify: bool, force: bool = False, mode: str = "quick") -> str:
    items = _watchlist_from_state(state)
    today = _tz_now().strftime("%Y-%m-%d")

    if not items:
        msg = _cmd_prompt(state, notify=notify)
        return msg

    active_from = str(state.get("active_from_date") or "").strip()
    if active_from and not force:
        try:
            if datetime.strptime(today, "%Y-%m-%d").date() < datetime.strptime(active_from, "%Y-%m-%d").date():
                msg = (
                    f"✅ 已收到追蹤清單，將從 {active_from} 08:30 開始晨報。\n"
                    f"目前清單：\n{_format_watchlist(items)}"
                )
                _maybe_notify(msg, notify=notify)
                return msg
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, "_cmd_briefing", exc_info=True)

    perf = _load_perf()
    resolve_info = _resolve_records_and_tune(perf)
    params = perf.get("model_params") if isinstance(perf.get("model_params"), dict) else dict(_DEFAULT_MODEL_PARAMS)

    with ThreadPoolExecutor(max_workers=min(len(items), 6)) as pool:
        futures = {pool.submit(_predict_one, it, params, mode): it for it in items}
        rows = []
        for fut in as_completed(futures):
            try:
                rows.append(fut.result(timeout=60))
            except Exception:
                it = futures[fut]
                rows.append({"symbol": it.symbol, "label": it.label, "market": it.market, "ok": False, "line": f"{it.label}({it.symbol})：資料取得逾時", "mode": mode})
    # Preserve original order
    order = {it.symbol: i for i, it in enumerate(items)}
    rows.sort(key=lambda r: order.get(r.get("symbol", ""), 999))
    new_count = _upsert_prediction_records(perf, rows, today)
    _save_perf(perf)

    report = _render_report(items, rows, mode=mode)
    perf_info = dict(resolve_info)
    perf_info["new_count"] = int(new_count)
    report = report + "\n\n" + "\n".join(_format_perf_lines(perf, perf_info))
    state["last_report_date"] = today
    _save_state(state)
    _maybe_notify(report, notify=notify)
    return report


def _cmd_performance() -> str:
    perf = _load_perf()
    resolve_info = _resolve_records_and_tune(perf)
    _save_perf(perf)
    metrics = resolve_info.get("metrics") if isinstance(resolve_info.get("metrics"), dict) else {}
    recs = perf.get("records") if isinstance(perf.get("records"), list) else []
    params = perf.get("model_params") if isinstance(perf.get("model_params"), dict) else dict(_DEFAULT_MODEL_PARAMS)
    logs = perf.get("tuning_log") if isinstance(perf.get("tuning_log"), list) else []
    last_tune = logs[-1] if logs else {}

    lines = [
        "📈 MAGI 股市模型績效",
        f"- 累計預測筆數：{len(recs)}",
        f"- 已解算筆數：{int(metrics.get('resolved') or 0)}（最近視窗 {int(metrics.get('window') or 0)}）",
        f"- 近期方向命中率：{float(metrics.get('hit_rate') or 0.0):.1f}%",
        f"- 近期 MAE：{float(metrics.get('mae_pct_point') or 0.0):.3f} pct",
        f"- 最近解算日：{str(metrics.get('last_resolved') or 'n/a')}",
        (
            f"- 目前權重：trend={float(params.get('w_trend') or _DEFAULT_MODEL_PARAMS['w_trend']):.3f}, "
            f"mom={float(params.get('w_mom') or _DEFAULT_MODEL_PARAMS['w_mom']):.3f}, "
            f"vol={float(params.get('w_vol') or _DEFAULT_MODEL_PARAMS['w_vol']):.3f}, "
            f"bias={float(params.get('bias') or _DEFAULT_MODEL_PARAMS['bias']):+.3f}"
        ),
    ]
    if last_tune:
        lines.append(
            f"- 最近校準：{str(last_tune.get('ts') or '')}｜"
            f"MAE {float(last_tune.get('old_mae') or 0.0):.3f} → {float(last_tune.get('new_mae') or 0.0):.3f}"
        )
    if int(resolve_info.get("resolved_now") or 0) > 0:
        lines.append(f"- 本次補解算：{int(resolve_info.get('resolved_now') or 0)} 筆")
    if bool(resolve_info.get("tune_applied")) and str(resolve_info.get("tune_msg") or "").strip():
        lines.append(f"- 本次校準：{str(resolve_info.get('tune_msg'))}")

    solved = [r for r in recs if isinstance(r, dict) and r.get("resolved_date")]
    if solved:
        sym_stats: Dict[str, Dict[str, Any]] = {}
        for r in solved[-180:]:
            sym = str(r.get("symbol") or "?")
            if sym not in sym_stats:
                sym_stats[sym] = {"hits": 0, "total": 0, "errs": []}
            sym_stats[sym]["total"] += 1
            if r.get("sign_hit"):
                sym_stats[sym]["hits"] += 1
            sym_stats[sym]["errs"].append(float(r.get("abs_err_pct") or 0))
        if sym_stats:
            lines.append("")
            lines.append("📊 個股績效：")
            for sym, st in sorted(sym_stats.items()):
                hr = st["hits"] / st["total"] * 100 if st["total"] else 0
                mae = sum(st["errs"]) / len(st["errs"]) if st["errs"] else 0
                lines.append(f"  {sym}: 命中率 {hr:.0f}%（{st['hits']}/{st['total']}）MAE {mae:.3f}")

    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="MAGI 市場晨報技能")
    ap.add_argument("--task", default="briefing", help="prompt|list|set|add|remove|briefing|performance|comps|sector|export")
    ap.add_argument("--text", default="", help="自然語句或股票清單")
    ap.add_argument("--notify", default="0", help="1=同步推播 TG")
    ap.add_argument("--force", default="0", help="1=忽略 active_from_date 直接產生")
    ap.add_argument("--mode", default="deep", help="quick|technical|deep（分析模式）")
    args = ap.parse_args()

    task = str(args.task or "briefing").strip().lower()
    if task == "help":
        import json as _j
        print(_j.dumps({"skill": "market-briefing", "tasks": ["prompt", "list", "set", "add", "remove", "briefing", "performance", "backtest", "comps", "sector", "export"], "description": "MAGI 市場晨報 — 股市資訊收集與分析"}, ensure_ascii=False, indent=2))
        return 0
    text = str(args.text or "").strip()
    notify = str(args.notify or "0").strip().lower() in {"1", "true", "yes", "on"}
    force = str(args.force or "0").strip().lower() in {"1", "true", "yes", "on"}
    mode = str(args.mode or "deep").strip().lower()
    if mode not in {"quick", "technical", "deep"}:
        mode = "deep"

    state = _load_state()

    if task in {"prompt", "ask"}:
        print(_cmd_prompt(state, notify=notify))
        return 0
    if task == "list":
        print(_cmd_list(state))
        return 0
    if task in {"set", "add", "remove"}:
        print(_update_watchlist(state, task, text))
        return 0
    if task in {"brief", "briefing", "report", "daily"}:
        print(_cmd_briefing(state, notify=notify, force=force, mode=mode))
        return 0
    if task in {"perf", "performance", "metrics"}:
        print(_cmd_performance())
        return 0
    if task in {"backtest", "bt"}:
        print(_cmd_backtest())
        return 0
    if task in {"comps", "comp", "同業比較"}:
        print(_cmd_comps(text))
        return 0
    if task in {"sector", "產業分析", "板塊"}:
        print(_cmd_sector(text, mode=mode))
        return 0
    if task in {"export", "xlsx", "excel"}:
        print(_cmd_export(state, mode=mode))
        return 0

    print("⚠️ 不支援的 task，請使用：prompt|list|set|add|remove|briefing|performance|backtest|comps|sector|export")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
