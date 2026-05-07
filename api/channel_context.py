"""
Channel-aware routing utilities for MAGI.

Provides context-based routing for different platforms (Telegram, Discord, LINE)
and topic-specific fast-path routing.
"""

import json
import logging
import os
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class ChannelContext:
    """Context information for a message channel.

    Attributes:
        topic_key: Canonical topic identifier (e.g., "laf", "filereview", "judgment").
        channel_id: Discord channel ID or empty string.
        thread_id: Telegram message_thread_id or None.
        platform: Platform identifier ("Telegram", "Discord", "LINE").
    """
    topic_key: str = ""
    channel_id: str = ""
    thread_id: Optional[int] = None
    platform: str = ""


# Topics that have dedicated fast-path handlers
TOPIC_FAST_PATH_ENABLED = {
    "laf",
    "filereview",
    "judgment",
    "transcript",
    "translation",
    "summary",
    "market",
}


def _get_magi_root(magi_root: str = "") -> str:
    """Auto-detect MAGI root directory based on file location.

    Args:
        magi_root: Optional explicit MAGI root path. If provided, used as-is.

    Returns:
        Path to MAGI root directory.
    """
    if magi_root:
        return magi_root

    # This file is at mnt/MAGI/api/channel_context.py
    # So parent of parent of parent of this file is mnt/MAGI root
    current_file = os.path.abspath(__file__)
    api_dir = os.path.dirname(current_file)  # mnt/MAGI/api
    magi_dir = os.path.dirname(api_dir)      # mnt/MAGI
    return magi_dir


def reverse_lookup_telegram_topic(
    thread_id: Optional[int], magi_root: str = ""
) -> str:
    """Reverse-lookup Telegram topic_key from message_thread_id.

    Loads .agent/telegram_channel_state.json, reads the topicMap,
    and returns the canonical topic key for the given thread_id.

    Args:
        thread_id: Telegram message_thread_id (e.g., 9, 7, etc.).
        magi_root: Optional MAGI root path for testing/override.

    Returns:
        Topic key (e.g., "laf", "filereview") or empty string if no match.
    """
    if thread_id is None:
        logger.debug("thread_id is None, returning empty topic_key")
        return ""

    magi_root = _get_magi_root(magi_root)
    state_file = os.path.join(magi_root, ".agent", "telegram_channel_state.json")

    try:
        if not os.path.exists(state_file):
            logger.warning(f"Telegram channel state file not found: {state_file}")
            return ""

        with open(state_file, "r", encoding="utf-8") as f:
            state = json.load(f)

        topic_map = state.get("topicMap", {})

        # Reverse lookup: find key where value == thread_id
        for topic_key, mapped_thread_id in topic_map.items():
            if mapped_thread_id == thread_id:
                logger.debug(
                    f"Reverse lookup for thread_id={thread_id} found topic_key={topic_key}"
                )
                return topic_key

        logger.debug(f"No topic_key found for thread_id={thread_id}")
        return ""

    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse {state_file}: {e}")
        return ""
    except Exception as e:
        logger.error(f"Error reading telegram channel state: {e}")
        return ""


def reverse_lookup_discord_channel(channel_id: str, magi_root: str = "") -> str:
    """Reverse-lookup Discord topic_key from channel_id.

    Loads .agent/discord_channel_map.json and reverse-looks up the channel_id.
    Returns the base topic by stripping suffixes (e.g., "laf_dispatch" → "laf").

    Args:
        channel_id: Discord channel ID (e.g., "456").
        magi_root: Optional MAGI root path for testing/override.

    Returns:
        Topic key (e.g., "laf", "filereview") or empty string if no match.
    """
    if not channel_id:
        logger.debug("channel_id is empty, returning empty topic_key")
        return ""

    magi_root = _get_magi_root(magi_root)
    channel_map_file = os.path.join(magi_root, ".agent", "discord_channel_map.json")

    try:
        if not os.path.exists(channel_map_file):
            logger.warning(f"Discord channel map file not found: {channel_map_file}")
            return ""

        with open(channel_map_file, "r", encoding="utf-8") as f:
            channel_map = json.load(f)

        # Reverse lookup: find key where value == channel_id
        for full_key, mapped_channel_id in channel_map.items():
            if str(mapped_channel_id) == str(channel_id):
                # Strip suffix (e.g., "laf_dispatch" → "laf")
                base_topic = full_key.split("_")[0]
                logger.debug(
                    f"Reverse lookup for channel_id={channel_id} found full_key={full_key}, base_topic={base_topic}"
                )
                return base_topic

        logger.debug(f"No topic_key found for channel_id={channel_id}")
        return ""

    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse {channel_map_file}: {e}")
        return ""
    except Exception as e:
        logger.error(f"Error reading discord channel map: {e}")
        return ""


def should_skip_nl_router(ctx: ChannelContext) -> bool:
    """Check if NL router should be skipped for this context.

    In the new design, NL Router keyword interception is disabled everywhere.
    Skills are now reached via slash commands, EmbeddingRouter, or topic fast-path.

    Args:
        ctx: Channel context.

    Returns:
        Always True in the new design.
    """
    # NL Router is universally disabled in the new design
    logger.debug("NL router skipped (disabled in new design)")
    return True
