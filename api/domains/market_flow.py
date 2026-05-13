"""
Market watchlist operations extracted from Orchestrator.

All functions accept an `orch` parameter (the Orchestrator instance)
instead of `self`, keeping the same logic but as standalone functions.
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys

logger = logging.getLogger("Orchestrator")

_MAGI_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_market_watch_state(orch) -> dict:
    path = os.path.join(orch._agent_dir, "market_watchlist.json")
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, "load_market_watch_state", exc_info=True)
    return {}


def is_stock_like_token(token: str) -> bool:
    t = str(token or "").strip()
    if not t:
        return False
    up = t.upper()
    if re.fullmatch(r"\d{4}(?:\.(?:TW|TWO))?", up):
        return True
    if re.fullmatch(r"[A-Z]{1,6}", up):
        return True
    if re.fullmatch(r"[\u4e00-\u9fff]{2,8}", t):
        banned = {
            "追蹤", "股票", "清單", "設定", "新增", "移除", "刪除",
            "今天", "明天", "可以", "幫我", "請問", "謝謝", "收到",
        }
        return t not in banned
    return False


def looks_like_market_watchlist_reply(message: str) -> bool:
    raw = str(message or "").strip()
    if not raw or len(raw) > 160:
        return False
    raw = re.sub(
        r"^(?:追蹤股票|追蹤清單|我要追蹤|設定追蹤|更新追蹤|新增追蹤|增加追蹤)\s*[:：]?\s*",
        "",
        raw,
        flags=re.IGNORECASE,
    ).strip()
    for sep in ["和", "與", "及", " plus ", " PLUS ", "+", "\uff0b"]:
        raw = raw.replace(sep, " ")
    parts = [
        p.strip(" \t\r\n,\uff0c\u3001;\uff1b|/()[]{}\"'`")
        for p in re.split(r"[\s,\uff0c\u3001;\uff1b|/]+", raw)
        if p.strip(" \t\r\n,\uff0c\u3001;\uff1b|/()[]{}\"'`")
    ]
    if not parts or len(parts) > 12:
        return False
    filler = {
        "\u8acb", "\u9ebb\u7169", "\u5e6b\u6211", "\u8b1d\u8b1d", "\u611f\u8b1d", "\u6536\u5230",
        "\u8ffd\u8e64", "\u80a1\u7968", "\u6e05\u55ae", "\u8a2d\u5b9a", "\u65b0\u589e", "\u79fb\u9664", "\u522a\u9664",
        "THANKS", "THX", "PLEASE",
    }
    good = 0
    bad = 0
    for p in parts:
        if p.upper() in filler or p in filler:
            continue
        if is_stock_like_token(p):
            good += 1
        else:
            bad += 1
    return good > 0 and bad == 0


# ---------------------------------------------------------------------------
# Main entry points
# ---------------------------------------------------------------------------

def try_market_watchlist_quick_set(orch, message: str, platform: str = "") -> tuple[bool, str]:
    """
    Fallback for Telegram/LINE quick replies after the first stock prompt:
    if watchlist is empty and user replies with plain symbols/names, treat it as market_set.
    """
    text = str(message or "").strip()
    if not text or text.startswith("/") or text.startswith("!"):
        return False, ""

    st = load_market_watch_state(orch)
    watch = st.get("watchlist") if isinstance(st.get("watchlist"), list) else []
    first_prompt_date = str(st.get("first_prompt_date") or "").strip()

    if watch or not first_prompt_date:
        return False, ""
    if not looks_like_market_watchlist_reply(text):
        return False, ""

    py = os.environ.get("MAGI_SKILL_PYTHON", f"{_MAGI_ROOT}/venv/bin/python3").strip()
    if not py or not os.path.exists(py):
        py = sys.executable or "python3"
    skill_script = f"{_MAGI_ROOT}/skills/market-briefing/action.py"
    if not os.path.exists(skill_script):
        return False, ""

    try:
        proc = subprocess.run(
            [py, skill_script, "--task", "set", "--text", text],
            capture_output=True,
            text=True,
            timeout=90,
            cwd=_MAGI_ROOT,
            env=os.environ.copy(),
        )
        out = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()
        if proc.returncode != 0:
            logger.warning(f"market quick-set failed rc={proc.returncode}: {(err or out)[:240]}")
            return False, ""
        if not out or "\u26a0\ufe0f \u6211\u6c92\u89e3\u6790\u5230\u80a1\u7968\u4ee3\u865f" in out:
            return False, ""
        logger.info("\u2705 market quick-set captured plain watchlist reply")
        # Use handler for postprocessing
        from api.handlers import text_processing_handler as _tp
        return True, _tp.postprocess_router_reply(out, platform)
    except Exception as e:
        logger.warning(f"market quick-set exception: {e}")
        return False, ""


def run_stock_briefing_command(orch, message: str) -> str:
    skill_script = f"{_MAGI_ROOT}/skills/market-briefing/action.py"
    if not os.path.exists(skill_script):
        return "\u274c \u627e\u4e0d\u5230\u80a1\u5e02\u8ffd\u8e64 skill\u3002"
    text = str(message or "").strip()

    # Capability question check
    from api.handlers import text_processing_handler as _tp
    if _looks_like_capability_question(text):
        return (
            "\u2705 **\u6211\u53ef\u4ee5\u5e6b\u60a8\u8ffd\u8e64\u80a1\u7968\u8207\u7522\u751f\u6668\u5831\uff01**\n\n"
            "\u2022 \u8a2d\u5b9a\uff1a`\u8ffd\u8e64\u80a1\u7968 \u53f0\u7a4d\u96fb AAPL`\n"
            "\u2022 \u6e05\u55ae\uff1a`\u8ffd\u8e64\u6e05\u55ae`\n"
            "\u2022 \u6668\u5831\uff1a`\u80a1\u5e02\u6668\u5831`"
        )
    msg_lower = text.lower()
    if any(k in text for k in ["\u76ee\u524d\u8ffd\u8e64", "\u8ffd\u8e64\u6e05\u55ae"]) or "watchlist" in msg_lower:
        task = "list"
        payload = ""
    elif any(k in text for k in ["\u79fb\u9664\u8ffd\u8e64", "\u522a\u9664\u8ffd\u8e64", "\u53d6\u6d88\u8ffd\u8e64"]) or "remove" in msg_lower:
        task = "remove"
        payload = _strip_intent_prefixes(
            text,
            [r"^(?:\u5e6b\u6211|\u8acb|\u9ebb\u7169|\u5354\u52a9\u6211|\u53ef\u4ee5\u5e6b\u6211)?\s*", r"^(?:\u79fb\u9664\u8ffd\u8e64|\u522a\u9664\u8ffd\u8e64|\u53d6\u6d88\u8ffd\u8e64)\s*"],
        )
    elif any(k in text for k in ["\u8ffd\u8e64\u4ee5\u4e0b\u80a1\u7968", "\u8ffd\u8e64\u80a1\u7968", "\u8a2d\u5b9a\u8ffd\u8e64", "\u65b0\u589e\u8ffd\u8e64", "\u589e\u52a0\u8ffd\u8e64"]) or any(k in msg_lower for k in ["track ", "watch "]):
        task = "set" if any(k in text for k in ["\u8ffd\u8e64\u4ee5\u4e0b\u80a1\u7968", "\u8ffd\u8e64\u80a1\u7968", "\u8a2d\u5b9a\u8ffd\u8e64"]) else "add"
        payload = _strip_intent_prefixes(
            text,
            [r"^(?:\u5e6b\u6211|\u8acb|\u9ebb\u7169|\u5354\u52a9\u6211|\u53ef\u4ee5\u5e6b\u6211)?\s*", r"^(?:\u8ffd\u8e64\u4ee5\u4e0b\u80a1\u7968|\u8ffd\u8e64\u80a1\u7968|\u8a2d\u5b9a\u8ffd\u8e64|\u65b0\u589e\u8ffd\u8e64|\u589e\u52a0\u8ffd\u8e64)\s*"],
        )
    else:
        task = "briefing"
        payload = ""
    if any(k in text for k in ["\u5feb\u901f\u6a21\u5f0f", "\u7c21\u5831"]) or "quick" in msg_lower:
        mode = "quick"
    elif any(k in text for k in ["\u6280\u8853\u5206\u6790", "MACD", "RSI", "\u5e03\u6797\u901a\u9053"]) or any(k in msg_lower for k in ["technical", "macd", "rsi"]):
        mode = "technical"
    else:
        mode = "deep"
    cmd = [sys.executable, skill_script, "--task", task, "--mode", mode]
    if payload:
        cmd.extend(["--text", payload])
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=180,
            cwd=_MAGI_ROOT,
            env=os.environ.copy(),
        )
        out = (proc.stdout or "").strip()
        if proc.returncode != 0 or not out:
            err = ((proc.stderr or out or "unknown").strip())[:300]
            return f"\u274c \u80a1\u5e02\u8ffd\u8e64\u5931\u6557\uff1a{err}"
        return out
    except Exception as e:
        return f"\u274c \u80a1\u5e02\u8ffd\u8e64\u932f\u8aa4\uff1a{e}"


# ---------------------------------------------------------------------------
# Private helpers (inlined from orchestrator)
# ---------------------------------------------------------------------------

def _looks_like_capability_question(message: str) -> bool:
    text = str(message or "").strip()
    if not text:
        return False
    if not re.search(r"[\u55ce\u561b\u5462\uff1f\?]$", text):
        return False
    if not re.search(r"(\u53ef\u4ee5|\u53ef\u4e0d\u53ef\u4ee5|\u80fd\u4e0d\u80fd|\u6703\u4e0d\u6703|\u6703|\u5982\u4f55|\u600e\u9ebc|\u6709\u6c92\u6709\u8fa6\u6cd5|\u80fd\u5426|\u53ef\u5426)", text, re.IGNORECASE):
        return False
    has_payload = bool(
        re.search(r"https?://", text, re.IGNORECASE)
        or re.search(r"[A-Za-z]{4,}", text)
        or re.search(r"\d{4,}", text)
        or re.search(r"[\u3002\uff1b;\uff0c,]", text)
    )
    return len(text) <= 36 or not has_payload


def _strip_intent_prefixes(text: str, patterns: list[str]) -> str:
    from api.handlers import text_processing_handler as _tp
    return _tp.strip_intent_prefixes(text, patterns)
