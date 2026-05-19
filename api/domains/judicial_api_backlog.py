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
        headline = "整理有錯誤，需優先看 traceback"
    elif remaining <= 0:
        status = "CLEAR"
        headline = "已清空，最新裁判可進入見解流程"
    elif oldest >= 24 * 7:
        status = "STALE"
        headline = "嚴重積壓，見解庫的新鮮度已落後"
    elif oldest >= 24:
        status = "AGING"
        headline = "有跨日積壓，最新見解可能延遲出現"
    else:
        status = "CATCHING_UP"
        headline = "有待消化 backlog，但仍在正常消化"

    lines: List[str] = [
        f"- 狀態：{headline}",
        f"- 本輪：待處理 {format_count(before)} → {format_count(remaining)}（消化 {format_count(reduced)}，處理 {format_count(done)}）",
        f"- 入庫：court_judgments {format_count(db)} / archive {format_count(archive)} / 摘要 {format_count(summaries)} / 向量 {format_count(vectors)}",
        f"- 老化：最老 {format_duration_hours(oldest)}；最新 {format_duration_hours(newest)}",
    ]
    if raw:
        lines.append(f"- Raw：總檔 {format_count(raw)}；不可讀 {format_count(unreadable)}")
    if low_value or missing_text:
        lines.append(
            f"- 品質閘門：低價值程序文書略過 {format_count(low_value)}；無全文略過 {format_count(missing_text)}"
        )
    if remaining > 0:
        if done > 0:
            estimate = f"- 預估：照本輪速度約 {format_count(runs_left)} 輪清空；照設定批量約 {format_count(configured_runs_left)} 輪"
            if daily_runs > 0 and configured_runs_left > 0:
                estimate += f"（每日 {daily_runs} 輪約 {math.ceil(configured_runs_left / daily_runs)} 天）"
            lines.append(estimate)
        else:
            lines.append("- 預估：本輪未消化成功，無法估算清空時間")
    if cache_root:
        lines.append(f"- 快取：{cache_root}")

    suggestions: List[str] = []
    if err:
        suggestions.append("先查 issue_agenda / stderr，避免錯誤重跑造成同一批卡住。")
    if remaining > 0 and oldest >= 24 * 7:
        suggestions.append("啟動或加密度執行 backlog_clear；目前不是缺資料，而是消化速度不足。")
    elif remaining > 0:
        suggestions.append("維持 night pull，白天整理可提高批量或增加補跑輪次。")
    if unreadable:
        suggestions.append("抽查不可讀 raw 檔，避免壞檔讓 backlog 永遠不歸零。")
    if not suggestions:
        suggestions.append("維持目前排程。")

    return {
        "status": status,
        "headline": headline,
        "backlog_before": before,
        "backlog_remaining": remaining,
        "handled": done,
        "reduced": reduced,
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
