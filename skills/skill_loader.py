"""
MAGI Skill Loader
=================
在 Orchestrator 啟動時呼叫，將現有的硬編碼 handler 遷移至 SkillRegistry，
並執行技能自動發現。

用法（在 orchestrator.py __init__ 中）：
    from skills.skill_loader import load_all_skills
    load_all_skills(self)
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass  # Orchestrator type hint avoided to prevent circular import

logger = logging.getLogger("SkillLoader")


def load_all_skills(orchestrator: object) -> None:
    """
    One-time initialization: register all skill handlers into the global
    SkillRegistry, then run auto-discovery.

    Called from Orchestrator.__init__() after all handler methods are available.
    """
    from skills.plugin import skill_registry

    # ── 1. Register direct handlers (migrate from hardcoded dict) ─────
    _register_direct_handlers(orchestrator, skill_registry)

    # ── 2. Auto-discover skills from SKILL.md ─────────────────────────
    count = skill_registry.discover()
    logger.info(
        "SkillLoader: %d skills discovered, %d plugins, %d direct handlers",
        count, skill_registry.plugin_count, skill_registry.handler_count,
    )


def _register_direct_handlers(orch: object, registry) -> None:
    """
    Register orchestrator methods as direct handlers in the registry.
    This replaces the hardcoded `direct_handlers` dict in
    _dispatch_safe_semantic_skill().
    """

    # ── Judgment ──────────────────────────────────────────────────
    _judgment_guide = (
        "✅ **我可以幫您查判決！**\n\n"
        "• 直接輸入：`查判決 傷害`\n"
        "• 也可提供案號：`查判決 113年度上訴字第12號`"
    )
    registry.register_handler(
        "judgment_search",
        lambda: orch._run_judgment_collector_command(orch._last_dispatch_message, notify=False),
        capability_guide=_judgment_guide,
        aliases=["run_judgment_collector"],
    )

    # ── Labor law ─────────────────────────────────────────────────
    registry.register_handler(
        "labor_law_calc",
        lambda: orch._run_labor_law_command(orch._last_dispatch_message),
        capability_guide=(
            "✅ **我可以幫您計算勞基法相關金額！**\n\n"
            "**加班費**：`月薪 50000，休息日加班 3 小時`\n"
            "**特休假**：`到職日 2020-03-01，我有幾天特休`\n"
            "**資遣費**：`月薪 45000，到職 2018-01-01，現在資遣費多少`"
        ),
    )

    # ── Translation ───────────────────────────────────────────────
    _translate_guide = (
        "✅ **我可以幫您翻譯！**\n\n"
        "• 翻譯文字：直接輸入 `翻譯 [文字/網址]`\n"
        "• 翻譯檔案：上傳 PDF/TXT/DOCX 後在留言打 `翻譯`\n"
        "• 支援中英日韓等多語系，本地 oMLX 引擎處理！"
    )
    registry.register_handler(
        "tri_sage_translate",
        lambda: orch._run_inline_translation_command(orch._last_dispatch_user_id, orch._last_dispatch_message),
        capability_guide=_translate_guide,
        aliases=["translate_document"],
    )

    # ── Summarize ─────────────────────────────────────────────────
    _summary_guide = (
        "✅ **我可以幫您做摘要！**\n\n"
        "• 網頁摘要：`摘要 [網址]`\n"
        "• 文字摘要：`摘要 [一段文字]`\n"
        "• 也可以上傳檔案請我整理重點"
    )
    registry.register_handler(
        "summarize_text",
        lambda: orch._run_inline_summary_command(orch._last_dispatch_message),
        capability_guide=_summary_guide,
        aliases=["pdf_summarize"],
    )

    # ── Stock briefing ────────────────────────────────────────────
    registry.register_handler(
        "stock_briefing",
        lambda: orch._run_stock_briefing_command(orch._last_dispatch_message),
        capability_guide=(
            "✅ **我可以幫您追蹤股票與產生晨報！**\n\n"
            "• 設定：`追蹤股票 台積電 AAPL`\n"
            "• 清單：`追蹤清單`\n"
            "• 晨報：`股市晨報`"
        ),
    )

    # ── Court hearing ─────────────────────────────────────────────
    registry.register_handler(
        "court_hearing",
        lambda: orch._run_court_hearing_command(orch._last_dispatch_message),
        capability_guide=(
            "✅ **我可以幫您查開庭排程！**\n\n"
            "• 查看排程：`最近有什麼庭`\n"
            "• 庭前準備：`準備 XXX 案的開庭資料`"
        ),
    )

    # ── Judgment trend ────────────────────────────────────────────
    registry.register_handler(
        "judgment_trend",
        lambda: orch._run_judgment_trend_command(orch._last_dispatch_message),
        capability_guide=(
            "✅ **我可以分析判決趨勢！**\n\n"
            "• 總覽：`判決趨勢`\n"
            "• 特定案由：`判決趨勢 詐欺`"
        ),
    )

    # ── Transcription ─────────────────────────────────────────────
    _transcribe_guide = (
        "✅ **我可以幫您處理語音！**\n\n"
        "直接上傳錄音檔（MP3/WAV/M4A），我就會自動產生逐字稿。\n"
        "• 加上 `翻譯` → 翻譯逐字稿\n"
        "• 加上 `摘要` → 摘要逐字稿"
    )
    registry.register_handler(
        "tri_sage_transcribe",
        lambda: orch._run_transcribe_guidance(orch._last_dispatch_message),
        capability_guide=_transcribe_guide,
        aliases=["audio_transcribe"],
    )

    # ── Web search ────────────────────────────────────────────────
    _search_guide = (
        "✅ **我可以幫您搜尋網路！**\n\n"
        "• 基本搜尋：`搜尋 [關鍵字]`\n"
        "• 深度研究：`網路研究 [主題]`\n"
        "• 自動整理摘要與參考來源"
    )
    registry.register_handler(
        "web_search",
        lambda: orch._run_embedding_web_search(orch._last_dispatch_message),
        capability_guide=_search_guide,
        aliases=["deep_research"],
    )

    logger.info("Registered %d direct handlers (with aliases)", registry.handler_count)
