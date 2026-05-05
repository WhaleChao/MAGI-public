#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Delivery helpers for market briefing reports.

The market report can be much longer than chat-platform limits.  Keep the chat
message readable and place the complete report in an exported text file.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


CHAT_INLINE_LIMIT = 1800
SUMMARY_LINE_LIMIT = 18
STOCK_LINE_LIMIT = 190
STOCK_SECTIONS = {"【台股】", "【美股】", "【其他】"}


def _compact_lines(lines: Iterable[str]) -> List[str]:
    out: List[str] = []
    previous_blank = False
    for raw in lines:
        line = str(raw or "").rstrip()
        if not line:
            if not previous_blank:
                out.append("")
            previous_blank = True
            continue
        out.append(line)
        previous_blank = False
    while out and not out[-1]:
        out.pop()
    return out


def _shorten_line(line: str, limit: int = STOCK_LINE_LIMIT) -> str:
    text = str(line or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(40, limit - 1)].rstrip() + "…"


def _is_stock_summary_line(line: str, in_stock_section: bool) -> bool:
    if not line.startswith("- "):
        return False
    if "今日無" in line:
        return False
    if "預估" in line or "資料取得" in line:
        return True
    if not in_stock_section:
        return False
    return True


def build_market_chat_summary(report: str, export_info: Optional[Dict[str, Any]] = None) -> str:
    """Build a short, non-truncated chat summary for LINE/TG/DC.

    The summary keeps the title, key stock lines, overall direction, model
    performance, and a pointer to the full report.
    """
    text = str(report or "").strip()
    if not text:
        return "📊 MAGI 股市晨報：本次沒有可用內容。"

    lines = _compact_lines(text.splitlines())
    title = lines[0] if lines else "📊 MAGI 股市晨報"

    stock_lines: List[str] = []
    overall_lines: List[str] = []
    perf_lines: List[str] = []
    in_stock_section = False
    for line in lines[1:]:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped in STOCK_SECTIONS:
            in_stock_section = True
            continue
        if stripped.startswith("整體偏向"):
            in_stock_section = False
        if _is_stock_summary_line(stripped, in_stock_section):
            stock_lines.append(stripped)
            continue
        if stripped.startswith("整體偏向") or stripped.startswith("註："):
            overall_lines.append(stripped)
            continue
        if stripped.startswith("- 校準") or stripped.startswith("- 近期") or stripped.startswith("- 本次") or stripped.startswith("- 已解算"):
            perf_lines.append(stripped)

    summary: List[str] = [title, "", "重點摘要："]
    if stock_lines:
        max_stock_lines = min(6, max(4, SUMMARY_LINE_LIMIT - 6))
        summary.extend(_shorten_line(line) for line in stock_lines[:max_stock_lines])
        if len(stock_lines) > max_stock_lines:
            summary.append(f"- 另有 {len(stock_lines) - max_stock_lines} 個標的，請看完整報告。")
    else:
        summary.append("- 本次沒有成功產生個股摘要。")

    if overall_lines:
        summary.extend(["", *overall_lines[:2]])
    if perf_lines:
        summary.extend(["", "模型狀態：", *perf_lines[:3]])

    if export_info:
        url = str(export_info.get("url") or "").strip()
        path = str(export_info.get("path") or "").strip()
        summary.append("")
        if url:
            summary.append(f"完整報告：{url}")
        elif path:
            summary.append(f"完整報告已輸出：{path}")

    result = "\n".join(summary).strip()
    if len(result) <= CHAT_INLINE_LIMIT:
        return result

    pointer = ""
    if export_info:
        pointer = str(export_info.get("url") or export_info.get("path") or "").strip()
    suffix = f"\n\n完整報告：{pointer}" if pointer else "\n\n完整報告已輸出，請查看 MAGI static/exports。"
    return result[: max(200, CHAT_INLINE_LIMIT - len(suffix) - 1)].rstrip() + "…" + suffix


def export_market_report(report: str) -> Dict[str, Any]:
    """Export a full market report to static/exports as TXT."""
    try:
        from skills.ops.export_text import export_txt

        return export_txt(report, prefix="market_briefing")
    except Exception as exc:
        return {"success": False, "error": str(exc)}


def deliver_market_report(report: str, *, notify: bool, magi_root: Path, notify_log) -> bool:
    """Notify chat channels with a concise summary and attach/export full text."""
    if not notify:
        return False

    export_info = export_market_report(report)
    summary = build_market_chat_summary(report, export_info if export_info.get("success") else None)

    ok = False
    try:
        from skills.ops.red_phone import alert_admin

        result = alert_admin(
            summary,
            severity="info",
            source="market_briefing",
            topic_key="market",
        ) or {}
        ok = bool(result.get("telegram") or result.get("line") or result.get("discord"))
        if not ok:
            notify_log("notify_failed", str(result)[:1200])
    except Exception as exc:
        notify_log("notify_exception", f"{type(exc).__name__}: {exc}")

    # Telegram can carry the full TXT as a document.  LINE will receive the URL
    # or local path in the summary; Discord market mirror is intentionally off.
    if export_info.get("success") and export_info.get("path"):
        try:
            from skills.ops.red_phone import send_file_admin

            file_result = send_file_admin(
                str(export_info["path"]),
                caption="MAGI 股市晨報完整報告",
                topic_key="market",
            )
            if file_result.get("ok"):
                ok = True
            else:
                notify_log("file_send_failed", str(file_result)[:1200])
        except Exception as exc:
            notify_log("file_send_exception", f"{type(exc).__name__}: {exc}")

    return ok
