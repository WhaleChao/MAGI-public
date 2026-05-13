# -*- coding: utf-8 -*-
"""
apple_commands.py
=================
Apple 原生功能相關指令的 CommandRegistry 註冊模組。

透過 CommandRegistry 擴展指令，不修改 command_dispatch.py 的既有邏輯。
在 Orchestrator 初始化時 import 即自動註冊。

指令清單：
- !開庭 [案號] [日期] [時間] — 建立開庭行事曆事件 + 提醒
- 搜檔 / 找檔 [關鍵字] — Spotlight 全文檢索（精確查詢不走 GPU）
- 通知測試 — 測試 macOS 桌面通知
"""
from __future__ import annotations

import logging
import os
import re
from typing import Optional

logger = logging.getLogger("AppleCommands")


def register_apple_commands(registry) -> None:
    """
    將所有 Apple 相關指令註冊到 CommandRegistry。

    在 orchestrator.py 初始化時呼叫：
        from api.commands.apple_commands import register_apple_commands
        register_apple_commands(_cmd_registry)
    """

    # ------------------------------------------------------------------
    # !開庭 — 建立開庭行事曆事件 + 提醒
    # ------------------------------------------------------------------
    def _handle_trial_event(ctx) -> Optional[str]:
        msg = ctx.message.strip()

        # 必須以 !開庭 或 ！開庭 開頭
        if not re.match(r"[!！]開庭", msg):
            return None

        try:
            from skills.apple.eventkit_bridge import parse_trial_command, create_trial_events
        except ImportError as e:
            logger.warning("eventkit_bridge not available: %s", e)
            return "⚠️ Apple 行事曆模組未安裝。"

        parsed = parse_trial_command(msg.replace("！", "!"))
        if not parsed:
            return (
                "📅 **開庭指令格式**\n\n"
                "`!開庭 [案號] [日期] [時間]`\n\n"
                "範例：\n"
                "• `!開庭 113勞訴19 2026-05-01 09:30`\n"
                "• `!開庭 114年度訴字第88號 2026-06-15`（預設 09:30）"
            )

        results = create_trial_events(
            case_number=parsed["case_number"],
            trial_date=parsed["trial_date"],
        )

        if results:
            items = "\n".join(f"  ✅ {r}" for r in results)
            return f"📅 **開庭事件已建立**\n\n案號：{parsed['case_number']}\n日期：{parsed['trial_date'].strftime('%Y-%m-%d %H:%M')}\n\n{items}"
        else:
            return f"⚠️ 建立開庭事件失敗，請檢查 Apple Calendar 權限。"

    registry.register(
        _handle_trial_event,
        name="trial_event",
        keywords=["!開庭", "！開庭"],
        pattern=r"[!！]開庭",
        priority=20,
    )

    # ------------------------------------------------------------------
    # 搜檔 / 找檔 — Spotlight 全文檢索
    # ------------------------------------------------------------------
    def _handle_spotlight_search(ctx) -> Optional[str]:
        msg = ctx.message.strip()

        # 提取搜尋關鍵字
        m = re.match(r"(?:搜檔|找檔|搜尋檔案|查檔案|查檔)\s*[:：]?\s*(.+)", msg)
        if not m:
            return None

        query = m.group(1).strip()
        if not query:
            return "請提供搜尋關鍵字。例：`搜檔 113勞訴19`"

        try:
            from skills.ops.spotlight_search import (
                spotlight_search, spotlight_search_case, is_exact_query
            )
        except ImportError as e:
            logger.warning("spotlight_search not available: %s", e)
            return "⚠️ Spotlight 搜尋模組未安裝。"

        # 判斷搜尋類型
        nas_folder = "/Volumes/homes"
        if not os.path.isdir(nas_folder):
            nas_folder = None

        if is_exact_query(query):
            results = spotlight_search_case(query, case_folder=nas_folder)
            search_type = "案號搜尋（Spotlight）"
        else:
            results = spotlight_search(query, folder=nas_folder, file_type="pdf")
            search_type = "全文檢索（Spotlight）"

        if not results:
            return f"🔍 {search_type}：未找到符合「{query}」的檔案。"

        lines = [f"🔍 **{search_type}** — 找到 {len(results)} 筆\n"]
        for i, r in enumerate(results[:10], 1):
            size_kb = r.get("size", 0) / 1024
            lines.append(f"{i}. `{r['name']}` ({size_kb:.0f} KB)")
            # 顯示路徑最後兩層
            parts = r["path"].split("/")
            if len(parts) >= 3:
                lines.append(f"   📁 .../{'/'.join(parts[-3:])}")

        if len(results) > 10:
            lines.append(f"\n（還有 {len(results) - 10} 筆未顯示）")

        return "\n".join(lines)

    registry.register(
        _handle_spotlight_search,
        name="spotlight_search",
        keywords=["搜檔", "找檔", "搜尋檔案", "查檔案", "查檔"],
        pattern=r"(?:搜檔|找檔|搜尋檔案|查檔案|查檔)\s*[:：]?",
        priority=25,
    )

    # ------------------------------------------------------------------
    # 通知測試
    # ------------------------------------------------------------------
    def _handle_notify_test(ctx) -> Optional[str]:
        msg = ctx.message.strip()
        if msg not in ("通知測試", "測試通知", "test notification"):
            return None

        try:
            from skills.ops.macos_notify import send_notification, HAS_TERMINAL_NOTIFIER
        except ImportError:
            return "⚠️ 通知模組未安裝。"

        ok = send_notification(
            title="MAGI 通知測試",
            message="通知功能正常運作！",
            subtitle="由使用者手動觸發",
        )

        backend = "terminal-notifier" if HAS_TERMINAL_NOTIFIER else "osascript"
        if ok:
            return f"✅ 桌面通知已發送（使用 {backend}）。請檢查通知中心。"
        else:
            return f"⚠️ 通知發送失敗。可能需要檢查 macOS 通知權限。"

    registry.register(
        _handle_notify_test,
        name="notify_test",
        keywords=["通知測試", "測試通知", "test notification"],
        priority=30,
    )

    logger.info("Apple commands registered: trial_event, spotlight_search, notify_test")
