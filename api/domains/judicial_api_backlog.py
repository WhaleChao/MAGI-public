from __future__ import annotations

import math
from typing import Any, Dict, List


def _i(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def format_count(value: Any) -> str:
    return f"{_i(value):,}"


def format_duration_hours(hours: Any) -> str:
    h = max(0.0, _f(hours))
    if h >= 48:
        return f"{h / 24:.1f} 天"
    if h >= 1:
        return f"{h:.1f} 小時"
    return f"{round(h * 60)} 分鐘"


def build_backlog_interpretation(
    *,
    backlog_before: Any = 0,
    backlog_remaining: Any = 0,
    handled: Any = 0,
    db_upserts: Any = 0,
    archive_upserts: Any = 0,
    vector_ingested: Any = 0,
    summarized: Any = 0,
    usable_summaries: Any = None,
    rejected_summaries: Any = None,
    summary_mode: str = "",
    vector_enabled: Any = None,
    errors: Any = 0,
    oldest_age_hours: Any = 0.0,
    newest_age_hours: Any = 0.0,
    raw_total: Any = 0,
    unreadable_count: Any = 0,
    skipped_low_value: Any = 0,
    skipped_missing_text: Any = 0,
    max_docs: Any = 0,
    runs_per_day: Any = 0,
    cache_root: str = "",
) -> Dict[str, Any]:
    before = _i(backlog_before)
    remaining = _i(backlog_remaining)
    done = _i(handled)
    db = _i(db_upserts)
    archive = _i(archive_upserts)
    vectors = _i(vector_ingested)
    summaries = _i(summarized)
    usable = summaries if usable_summaries is None else _i(usable_summaries)
    rejected = max(0, summaries - usable) if rejected_summaries is None else _i(rejected_summaries)
    err = _i(errors)
    raw = _i(raw_total)
    unreadable = _i(unreadable_count)
    low_value = _i(skipped_low_value)
    missing_text = _i(skipped_missing_text)
    batch_size = _i(max_docs) or max(done, 1)
    daily_runs = max(0, _i(runs_per_day))
    oldest = _f(oldest_age_hours)
    newest = _f(newest_age_hours)
    reduced = max(0, before - remaining)

    runs_left = int(math.ceil(remaining / max(done, 1))) if remaining > 0 and done > 0 else 0
    configured_runs_left = int(math.ceil(remaining / max(batch_size, 1))) if remaining > 0 else 0

    if err:
        status = "PROCESS_ERROR"
        headline = "司法院裁判資料整理有錯誤，需優先看錯誤紀錄"
    elif remaining <= 0:
        status = "CLEAR"
        headline = "司法院裁判資料整理序列已清空"
    elif oldest >= 24 * 7:
        status = "STALE"
        headline = "司法院裁判資料嚴重積壓，見解庫的新鮮度已落後"
    elif oldest >= 24:
        status = "AGING"
        headline = "司法院裁判資料有跨日積壓，最新見解可能延遲出現"
    else:
        status = "CATCHING_UP"
        headline = "司法院裁判資料仍有待整理量，但正在正常處理"

    mode_label = {
        "extractive": "快速原文擷取",
        "llm": "模型摘要",
        "none": "不產生摘要",
        "auto": "自動選擇",
    }.get(str(summary_mode or "").strip().lower(), str(summary_mode or "未標示"))
    if err:
        round_result = f"本輪發生 {format_count(err)} 個錯誤，未完成項目會保留待重試"
    elif done:
        round_result = f"本輪已讀取 {format_count(done)} 件；待整理量 {format_count(before)} → {format_count(remaining)}"
    else:
        round_result = f"本輪沒有讀取新資料；目前仍有 {format_count(remaining)} 件待整理"

    lines: List[str] = [
        f"- 結果：{round_result}",
        f"- 目前：{headline}",
        f"- 可用內容：完整資料入庫 {format_count(min(db, archive))} 件；摘要嘗試 {format_count(summaries)}、通過 {format_count(usable)}、淘汰 {format_count(rejected)}",
        f"- 檢索：向量新增 {format_count(vectors)} 件（模式：{mode_label}）",
    ]
    if str(summary_mode or "").strip().lower() == "extractive" and not bool(vector_enabled):
        lines[-1] = (
            f"- 檢索：向量新增 {format_count(vectors)} 件；快速原文擷取模式依設定不建立向量，"
            "不是向量服務故障"
        )
    if raw:
        lines.append(f"- 資料檔：總數 {format_count(raw)}；不可讀 {format_count(unreadable)}")
    if low_value or missing_text:
        lines.append(
            f"- 排除：低價值程序文書 {format_count(low_value)} 件；無全文 {format_count(missing_text)} 件"
        )
    if remaining > 0:
        if done > 0:
            estimate = f"- 剩餘：依本輪速度約需 {format_count(runs_left)} 輪；依設定批量約需 {format_count(configured_runs_left)} 輪"
            if daily_runs > 0 and configured_runs_left > 0:
                estimate += f"（每日 {daily_runs} 輪約 {math.ceil(configured_runs_left / daily_runs)} 天）"
            lines.append(estimate)
        else:
            lines.append("- 剩餘：本輪沒有成功消化，暫時無法估算")

    suggestions: List[str] = []
    if err:
        suggestions.append("先查問題紀錄與錯誤輸出，避免錯誤重跑造成同一批卡住。")
    if remaining > 0 and oldest >= 24 * 7:
        suggestions.append("啟動或加密度執行裁判資料整理補跑任務；目前不是缺資料，而是整理速度不足。")
    elif remaining > 0 and done <= 0:
        suggestions.append("檢查本輪未讀取資料的原因；不要把這次結果視為已完成。")
    if unreadable:
        suggestions.append("抽查不可讀資料檔，避免壞檔讓待整理量永遠不歸零。")
    if not suggestions:
        suggestions.append("維持目前排程。")

    return {
        "status": status,
        "headline": headline,
        "backlog_before": before,
        "backlog_remaining": remaining,
        "handled": done,
        "reduced": reduced,
        "usable_summaries": usable,
        "rejected_summaries": rejected,
        "runs_left_at_current_rate": runs_left,
        "runs_left_at_configured_batch": configured_runs_left,
        "lines": lines,
        "suggestions": suggestions,
    }


def format_backlog_notice(title: str, interpretation: Dict[str, Any]) -> str:
    lines = [title]
    lines.extend(interpretation.get("lines") or [])
    suggestions = [str(x).strip() for x in (interpretation.get("suggestions") or []) if str(x).strip()]
    if suggestions:
        lines.append("- 建議：" + "；".join(suggestions))
    return "\n".join(lines)
