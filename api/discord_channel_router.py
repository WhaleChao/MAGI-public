"""
Discord 多頻道路由器
===================
依 topic_key 將通知分流到不同 Discord 子頻道。

頻道映射可透過以下方式設定（優先級由高至低）：
1. 環境變數 MAGI_DC_CHANNEL_MAP (JSON)
2. .agent/discord_channel_map.json
3. config.json → discord.channelMap
4. !magi setup_channels 自動建立

映射格式: { "topic_key": "channel_id", ... }

預設頻道規劃（業務＋動作）:
  閱卷-繳費   filereview_payment     繳費通知、逾期提醒
  閱卷-下載   filereview_download    卷宗/繳費單下載完成
  閱卷-聲請   filereview_apply       聲請閱卷進度
  筆錄-通知   transcript             筆錄下載完成
  法扶-派案   laf_dispatch           新案派案通知、審查結果
  法扶-結案   laf_closing            結案回報、酬金領款
  逐字稿      verbatim               音訊轉文字、逐字稿產出
  摘要        summary                文件/PDF 摘要產出
  翻譯        translation            文件翻譯產出
  一般        general                預設 fallback 頻道
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Optional

logger = logging.getLogger("discord_channel_router")

_MAGI_ROOT = os.path.abspath(os.path.dirname(os.path.dirname(__file__)))
_AGENT_DIR = os.path.join(_MAGI_ROOT, ".agent")
_CHANNEL_MAP_FILE = os.path.join(_AGENT_DIR, "discord_channel_map.json")

# ───────── topic_key → sub_topic 映射 ─────────
# red_phone.py 已定義 canonical topic_key (filereview, transcript, laf, ...),
# 這裡進一步細分為 sub_topic 用於 DC 頻道路由。

# 從 red_phone 借用 canonical 化函數
try:
    from skills.ops.red_phone import _canonical_topic_key
except ImportError:
    def _canonical_topic_key(key: str) -> str:
        return str(key or "").strip().lower()


# ───────── 訊息內容 → 細分 sub_topic ─────────

def _infer_sub_topic(message: str, topic_key: str, source: str = "") -> str:
    """
    根據 topic_key + 訊息內容，推斷更細的 sub_topic 用於頻道路由。

    Returns: sub_topic string (e.g. "filereview_payment", "laf_dispatch")
    """
    canonical = _canonical_topic_key(topic_key)
    s = str(message or "").lower()
    src = str(source or "").lower()

    if canonical in ("filereview", "filereview_payment", "filereview_download", "filereview_apply"):
        # 已經有明確 sub_topic 的直接返回
        if canonical in ("filereview_payment", "filereview_download", "filereview_apply"):
            return canonical
        # 閱卷類：依動作細分
        if any(k in s for k in ["繳費", "逾期", "到期", "待繳", "payment"]):
            return "filereview_payment"
        if any(k in s for k in ["下載完成", "已下載", "download", "歸檔"]):
            return "filereview_download"
        if any(k in s for k in ["聲請", "apply", "申請閱卷"]):
            return "filereview_apply"
        if any(k in s for k in ["信箱檢查完成", "閱卷信箱"]):
            return "filereview_download"
        return "filereview_download"

    if canonical == "laf":
        # 法扶類：依動作細分
        if any(k in s for k in ["派案", "dispatch", "新案"]):
            return "laf_dispatch"
        if any(k in s for k in ["審查結果", "review_result", "准予扶助"]):
            return "laf_dispatch"
        if any(k in s for k in ["結案", "closing", "酬金", "領款"]):
            return "laf_closing"
        if any(k in s for k in ["開辦", "go_live", "go-live"]):
            return "laf_go_live"
        if any(k in s for k in ["重試", "retry", "exhausted", "達上限"]):
            return "laf_closing"
        return "laf"

    if canonical == "transcript":
        return "transcript"

    if canonical == "verbatim":
        return "verbatim"

    if canonical == "summary":
        return "summary"

    if canonical == "translation":
        return "translation"

    if canonical == "filing":
        return "filing"

    # 其他 topic 直接返回 canonical
    return canonical or "general"


# ───────── sub_topic → fallback chain ─────────
# 當特定 sub_topic 的頻道沒設定時，回退到更廣的 topic

_FALLBACK_CHAIN: dict[str, list[str]] = {
    "filereview_payment": ["filereview", "general"],
    "filereview_download": ["filereview", "general"],
    "filereview_apply": ["filereview", "general"],
    "filereview": ["general"],
    "laf_dispatch": ["laf", "general"],
    "laf_go_live": ["laf_dispatch", "laf", "general"],
    "laf_closing": ["laf", "general"],
    "laf": ["general"],
    "transcript": ["general"],
    "verbatim": ["general"],
    "summary": ["general"],
    "translation": ["general"],
    "judgment": ["general"],
    "alert": ["general"],
    "check": ["general"],
    "nightly": ["general"],
    "market": ["general"],
    "filing": ["general"],
}


# ───────── Channel Map 載入/儲存 ─────────

def _load_channel_map() -> dict[str, str]:
    """
    從多個來源載入 DC 頻道映射（merge, 後面的覆蓋前面的）。
    Returns: { sub_topic_or_topic: "channel_id_string", ... }
    """
    merged: dict[str, str] = {}

    # 1. config.json → discord.channelMap
    try:
        from api.runtime_paths import get_config_path
        cfg_path = str(get_config_path("config.json"))
        if os.path.exists(cfg_path):
            with open(cfg_path, "r", encoding="utf-8") as f:
                cfg = json.load(f) or {}
            dc = cfg.get("discord") or {}
            raw = dc.get("channelMap") or {}
            if isinstance(raw, dict):
                for k, v in raw.items():
                    ck = str(k or "").strip()
                    cv = str(v or "").strip()
                    if ck and cv:
                        merged[ck] = cv
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 161, exc_info=True)

    # 2. .agent/discord_channel_map.json
    try:
        if os.path.exists(_CHANNEL_MAP_FILE):
            with open(_CHANNEL_MAP_FILE, "r", encoding="utf-8") as f:
                raw = json.load(f) or {}
            if isinstance(raw, dict):
                for k, v in raw.items():
                    if k.startswith("_"):  # skip _mirror, _servers metadata
                        continue
                    ck = str(k or "").strip()
                    cv = str(v or "").strip() if isinstance(v, str) else ""
                    if ck and cv:
                        merged[ck] = cv
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 175, exc_info=True)

    # 3. 環境變數 MAGI_DC_CHANNEL_MAP
    env_json = os.environ.get("MAGI_DC_CHANNEL_MAP", "").strip()
    if env_json:
        try:
            raw = json.loads(env_json)
            if isinstance(raw, dict):
                for k, v in raw.items():
                    ck = str(k or "").strip()
                    cv = str(v or "").strip()
                    if ck and cv:
                        merged[ck] = cv
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 189, exc_info=True)

    return merged


def save_channel_map(channel_map: dict[str, str]) -> str:
    """儲存頻道映射到 .agent/discord_channel_map.json。"""
    os.makedirs(_AGENT_DIR, exist_ok=True)
    clean = {}
    for k, v in (channel_map or {}).items():
        ck = str(k or "").strip()
        cv = str(v or "").strip()
        if ck and cv:
            clean[ck] = cv
    with open(_CHANNEL_MAP_FILE, "w", encoding="utf-8") as f:
        json.dump(clean, f, ensure_ascii=False, indent=2)
    return _CHANNEL_MAP_FILE


# ───────── 主路由函數 ─────────

def resolve_discord_channel(
    message: str,
    *,
    topic_key: str = "",
    source: str = "",
    fallback_channel_id: str = "",
) -> tuple[str, str]:
    """
    解析訊息應該發送到哪個 DC 頻道。

    Returns: (resolved_sub_topic, channel_id)
        - channel_id 可能為空字串（表示未設定，使用預設頻道）
    """
    sub_topic = _infer_sub_topic(message, topic_key, source)
    cmap = _load_channel_map()

    if not cmap:
        return sub_topic, fallback_channel_id

    # 嘗試 sub_topic → fallback chain
    if sub_topic in cmap:
        return sub_topic, cmap[sub_topic]

    for fb in _FALLBACK_CHAIN.get(sub_topic, []):
        if fb in cmap:
            return sub_topic, cmap[fb]

    return sub_topic, cmap.get("general", fallback_channel_id)


# ───────── 預設頻道定義 (for auto-setup) ─────────

DEFAULT_CHANNELS: list[dict] = [
    {
        "name": "閱卷-繳費",
        "key": "filereview_payment",
        "topic": "繳費通知、逾期提醒、繳費單下載",
    },
    {
        "name": "閱卷-下載",
        "key": "filereview_download",
        "topic": "卷宗下載完成、歸檔結果、信箱掃描報告",
    },
    {
        "name": "閱卷-聲請",
        "key": "filereview_apply",
        "topic": "聲請閱卷進度與結果",
    },
    {
        "name": "筆錄-通知",
        "key": "transcript",
        "topic": "筆錄下載完成、筆錄摘要",
    },
    {
        "name": "法扶-派案",
        "key": "laf_dispatch",
        "topic": "新案派案通知、審查結果通知",
    },
    {
        "name": "法扶-開辦",
        "key": "laf_go_live",
        "topic": "開辦回報進度、開辦確認、開辦通知書",
    },
    {
        "name": "法扶-結案",
        "key": "laf_closing",
        "topic": "結案回報、酬金領款、附件重試狀態",
    },
    {
        "name": "逐字稿",
        "key": "verbatim",
        "topic": "音訊轉文字、逐字稿產出結果",
    },
    {
        "name": "摘要",
        "key": "summary",
        "topic": "文件摘要、PDF 摘要產出結果",
    },
    {
        "name": "翻譯",
        "key": "translation",
        "topic": "文件翻譯產出結果",
    },
    {
        "name": "一般",
        "key": "general",
        "topic": "系統狀態、其他通知",
    },
]


def get_mirror_channel_id(sub_topic: str) -> str:
    """
    查詢 mirror（測試伺服器）對應的頻道 ID。
    用於雙伺服器同時發送：正式伺服器 + 測試伺服器。

    Returns: mirror channel_id string, or "" if no mirror configured.
    """
    try:
        if os.path.exists(_CHANNEL_MAP_FILE):
            with open(_CHANNEL_MAP_FILE, "r", encoding="utf-8") as f:
                raw = json.load(f) or {}
            mirror = raw.get("_mirror")
            if isinstance(mirror, dict):
                # Try exact sub_topic, then fallback chain
                if sub_topic in mirror:
                    return str(mirror[sub_topic])
                for fb in _FALLBACK_CHAIN.get(sub_topic, []):
                    if fb in mirror:
                        return str(mirror[fb])
                return str(mirror.get("general", ""))
    except Exception:
        pass
    return ""


async def auto_setup_channels(guild, category_name: str = "📋 MAGI 通知") -> dict[str, str]:
    """
    在 Discord guild 中自動建立分類與子頻道。

    Parameters:
        guild: discord.Guild 物件
        category_name: 分類名稱

    Returns: { sub_topic: channel_id_str, ... }
    """
    import discord  # noqa

    # 找或建分類
    category = None
    for cat in guild.categories:
        if cat.name == category_name:
            category = cat
            break
    if category is None:
        category = await guild.create_category(category_name)
        logger.info("✅ Created Discord category: %s", category_name)

    channel_map: dict[str, str] = {}
    existing_names = {ch.name: ch for ch in category.text_channels}

    for ch_def in DEFAULT_CHANNELS:
        name = ch_def["name"]
        key = ch_def["key"]
        topic = ch_def.get("topic", "")

        if name in existing_names:
            ch = existing_names[name]
            logger.info("  ↳ Channel already exists: #%s (id=%s)", name, ch.id)
        else:
            ch = await guild.create_text_channel(
                name=name,
                category=category,
                topic=topic,
            )
            logger.info("  ✅ Created channel: #%s (id=%s)", name, ch.id)

        channel_map[key] = str(ch.id)

    # 儲存映射
    save_channel_map(channel_map)
    logger.info("✅ Discord channel map saved: %s", channel_map)
    return channel_map
