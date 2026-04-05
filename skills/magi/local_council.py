"""
MAGI Local Council (本地三哲人決議系統)
=======================================
Run the Three Magi consensus on a SINGLE Mac Mini using local Ollama.

Architecture:
  - Melchior  (MAGI-02 / Scientist)  → Technical analysis
  - Balthasar (MAGI-03 / Pragmatist) → UX & operational impact
  - Casper    (MAGI-01 / Stabilizer) → Safety audit, final ruling

All three agents share the same local model (taide-12b) but receive
different system prompts derived from their SOUL files.

Usage:
  from skills.magi.local_council import convene_council
  result = convene_council("Should we upgrade the database schema?")
  print(result["final_ruling"])
"""

import json
import logging
import os
import time
from datetime import datetime
from typing import Dict, List, Optional

import requests

# --- Load .env for subprocess/cron credential access ---
try:
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv()
except Exception:
    logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 33, exc_info=True)

logger = logging.getLogger("MagiCouncil")
if not logging.getLogger().handlers:
    logging.basicConfig(level=logging.INFO)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
OLLAMA_BASE = os.environ.get("MAGI_COUNCIL_OLLAMA_BASE", os.environ.get("OMLX_URL", "http://127.0.0.1:8080")).rstrip("/")
COUNCIL_MODEL = os.environ.get("MAGI_COUNCIL_MODEL", os.environ.get("MAGI_TEXT_PRIMARY_MODEL", ""))
COUNCIL_TIMEOUT = int(os.environ.get("MAGI_COUNCIL_TIMEOUT", "120"))
COUNCIL_NUM_CTX = int(os.environ.get("MAGI_COUNCIL_NUM_CTX", "4096"))
COUNCIL_MAX_TOKENS = int(os.environ.get("MAGI_COUNCIL_MAX_TOKENS", "800"))
COUNCIL_TEMPERATURE = float(os.environ.get("MAGI_COUNCIL_TEMPERATURE", "0.4"))

# Discord (optional)
DISCORD_WEBHOOK_URL = (
    os.environ.get("MAGI_DISCORD_WEBHOOK")
    or os.environ.get("DISCORD_WEBHOOK_URL")
    or ""
).strip()

# ---------------------------------------------------------------------------
# Soul Prompts (condensed from SOUL_*.md)
# ---------------------------------------------------------------------------
SOUL_MELCHIOR = """你是 Melchior (MAGI-02)，MAGI 系統中的「科學家/工程師」。
角色定位：技術實作、邏輯分析、效能最佳化。
行為準則：
- 語氣：精確、資料導向、客觀。
- 思維角度：微觀實作。「這如何運作？」「能否更快？」
- 面對變化：適應性強。「讓我們測試這個假設。」
- 專長：程式碼審查、Legacy 重構、工具打造。
限制：不得直接 DELETE 資料庫。所有 UPDATE 前必須記錄 BEFORE/AFTER 狀態。
請用繁體中文回答，技術術語可用英文。回答請簡潔（300 字內）。"""

SOUL_BALTHASAR = """你是 Balthasar (MAGI-03)，MAGI 系統中的「行動秘書/執行者」。
角色定位：協調、使用者體驗、實際執行效率。
行為準則：
- 語氣：友善、積極、有同理心。
- 思維角度：使用者導向。「這有用嗎？」「這會不會造成困擾？」
- 面對變化：熱情。「來試試看！」
- 專長：通知管理、任務同步、草稿撰寫。
限制：主要為 READ-ONLY。若需寫入 osc 須經由 Melchior。不嘗試重構程式碼。
請用繁體中文回答。回答請簡潔（300 字內）。"""

SOUL_CASPER = """你是 Casper (MAGI-01)，MAGI 系統中的「總理/仲裁者」，也是議長。
角色定位：安全把關、風險評估、最終裁決。
行為準則：
- 語氣：正式、冷靜、權威、簡潔。
- 思維角度：宏觀風險。「這有必要嗎？」「這安全嗎？」
- 面對變化：保持懷疑。安全 > 效率。
- 專長：否決權（Veto）、Git 回復、業務監督。
限制：不得 DELETE 或直接寫入 osc 資料庫。
你需要綜合 Melchior 和 Balthasar 的意見做出最終裁決。
如果有安全風險，你 **必須** 行使否決權。
請用繁體中文回答。最終裁決格式：
✅ 通過 / 🚫 否決 / ⚠️ 有條件通過
並附上理由（200 字內）。"""


# ---------------------------------------------------------------------------
# Core Functions
# ---------------------------------------------------------------------------

def _warmup_model(model: str = ""):
    """Send a tiny prompt to pre-load the model into VRAM."""
    use_model = model or COUNCIL_MODEL
    try:
        logger.info(f"  ☕ 預熱模型 ({use_model})...")
        resp = requests.post(
            f"{OLLAMA_BASE}/v1/chat/completions",
            json={
                "model": use_model,
                "messages": [{"role": "user", "content": "ping"}],
                "max_tokens": 1,
                "stream": False,
            },
            timeout=60,
        )
        if resp.status_code == 200:
            logger.info(f"  ☕ 模型已就緒")
            return True
    except Exception as e:
        logger.warning(f"  ☕ 預熱失敗: {e}")
    return False

def _ollama_chat(
    system_prompt: str,
    user_prompt: str,
    model: str = "",
    timeout: int = 0,
    retries: int = 2,
) -> Dict:
    """Send a chat completion request to local oMLX (OpenAI-compatible API)."""
    use_model = model or COUNCIL_MODEL
    use_timeout = timeout or COUNCIL_TIMEOUT
    payload = {
        "model": use_model,
        "messages": [
            {"role": "system", "content": system_prompt.strip()},
            {"role": "user", "content": user_prompt.strip()},
        ],
        "stream": False,
        "temperature": COUNCIL_TEMPERATURE,
        "max_tokens": COUNCIL_MAX_TOKENS,
    }
    last_error = ""
    for attempt in range(max(1, retries)):
        try:
            resp = requests.post(
                f"{OLLAMA_BASE}/v1/chat/completions",
                json=payload,
                timeout=use_timeout,
            )
            resp.raise_for_status()
            data = resp.json()
            choices = data.get("choices") or []
            content = ""
            if choices:
                content = ((choices[0].get("message") or {}).get("content") or "").strip()
            if content:
                return {"success": True, "response": content, "model": use_model}
            last_error = "empty_response"
            logger.warning(f"    ⚠️ 空回應 (attempt {attempt+1}/{retries})")
        except Exception as e:
            last_error = str(e)
            logger.warning(f"    ⚠️ 請求失敗 (attempt {attempt+1}/{retries}): {e}")
    return {"success": False, "response": "", "error": last_error}


def _post_discord(content: str) -> bool:
    """Post to Discord webhook if configured."""
    if not DISCORD_WEBHOOK_URL:
        return False
    try:
        from urllib import request as urlrequest
        payload = json.dumps({"content": content[:1950]}).encode("utf-8")
        req = urlrequest.Request(
            DISCORD_WEBHOOK_URL,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlrequest.urlopen(req, timeout=10) as resp:
            return getattr(resp, "status", 0) in (200, 204)
    except Exception as e:
        logger.warning(f"Discord post failed: {e}")
        return False


# ---------------------------------------------------------------------------
# Council Session
# ---------------------------------------------------------------------------

def convene_council(
    question: str,
    context: str = "",
    notify_discord: bool = True,
    model: str = "",
) -> Dict:
    """
    Convene the Three Magi for a local council session.

    Args:
        question: The topic / question to deliberate.
        context:  Optional background context.
        notify_discord: Whether to post results to Discord.
        model:    Override the default model.

    Returns:
        Dict with keys: success, melchior, balthasar, casper, final_ruling,
                        votes, passed, timestamp, duration_sec
    """
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    t0 = time.time()

    prompt_base = question.strip()
    if context:
        prompt_base = f"背景資訊：\n{context.strip()}\n\n議題：\n{question.strip()}"

    logger.info(f"🏛️ [MAGI Council] 開始決議：{question[:60]}...")

    # --- Warmup: pre-load model to avoid cold-start empty responses ---
    _warmup_model(model=model)

    # --- Agent 1: Melchior (The Scientist) ---
    logger.info("  🔬 Melchior 分析中...")
    m_prompt = (
        f"以下是需要你進行技術分析的議題：\n\n{prompt_base}\n\n"
        "請提供：\n"
        "1. 技術可行性分析\n"
        "2. 推薦的實作方案\n"
        "3. 潛在的技術風險\n"
        "4. 你的投票：贊成(Yes) 或 反對(No)，附上理由"
    )
    melchior = _ollama_chat(SOUL_MELCHIOR, m_prompt, model=model)

    # --- Agent 2: Balthasar (The Pragmatist) ---
    logger.info("  🍏 Balthasar 評估中...")
    b_prompt = (
        f"以下是需要你評估的議題：\n\n{prompt_base}\n\n"
        f"Melchior 的技術分析：\n{melchior.get('response', '(無回應)')[:600]}\n\n"
        "請提供：\n"
        "1. 使用者體驗影響\n"
        "2. 執行效率與成本\n"
        "3. 你的投票：贊成(Yes) 或 反對(No)，附上理由"
    )
    balthasar = _ollama_chat(SOUL_BALTHASAR, b_prompt, model=model)

    # --- Agent 3: Casper (The Stabilizer / Chairman) ---
    logger.info("  👻 Casper 仲裁中...")
    c_prompt = (
        f"以下是本次決議的議題：\n\n{prompt_base}\n\n"
        f"【Melchior 意見】\n{melchior.get('response', '(無回應)')[:600]}\n\n"
        f"【Balthasar 意見】\n{balthasar.get('response', '(無回應)')[:600]}\n\n"
        "作為議長，請：\n"
        "1. 綜合雙方意見\n"
        "2. 進行安全風險評估\n"
        "3. 做出最終裁決：✅ 通過 / 🚫 否決 / ⚠️ 有條件通過\n"
        "4. 附上你的理由"
    )
    casper = _ollama_chat(SOUL_CASPER, c_prompt, model=model)

    duration = round(time.time() - t0, 1)

    # --- Parse votes ---
    def _extract_vote(text: str) -> str:
        t = (text or "").lower()
        if "🚫" in text or "否決" in t or "veto" in t:
            return "No"
        if "✅" in text or "通過" in t:
            return "Yes"
        if "⚠️" in text or "有條件" in t:
            return "Conditional"
        if "yes" in t or "贊成" in t:
            return "Yes"
        if "no" in t or "反對" in t:
            return "No"
        return "Abstain"

    m_vote = _extract_vote(melchior.get("response", ""))
    b_vote = _extract_vote(balthasar.get("response", ""))
    c_vote = _extract_vote(casper.get("response", ""))

    votes = {"melchior": m_vote, "balthasar": b_vote, "casper": c_vote}

    # Casper has veto power
    if c_vote == "No":
        passed = False
    elif c_vote == "Conditional":
        passed = "conditional"
    else:
        yes_count = sum(1 for v in votes.values() if v == "Yes")
        passed = yes_count >= 2  # majority rule (2/3)

    result = {
        "success": True,
        "question": question,
        "melchior": melchior.get("response", ""),
        "balthasar": balthasar.get("response", ""),
        "casper": casper.get("response", ""),
        "final_ruling": casper.get("response", ""),
        "votes": votes,
        "passed": passed,
        "timestamp": timestamp,
        "duration_sec": duration,
        "model": model or COUNCIL_MODEL,
    }

    logger.info(
        f"🏛️ [MAGI Council] 決議完成 ({duration}s) — "
        f"Melchior:{m_vote} Balthasar:{b_vote} Casper:{c_vote} → "
        f"{'通過' if passed is True else '有條件通過' if passed == 'conditional' else '否決'}"
    )

    # --- Discord Notification ---
    if notify_discord and DISCORD_WEBHOOK_URL:
        status = "✅ 通過" if passed is True else "⚠️ 有條件通過" if passed == "conditional" else "🚫 否決"
        dc_msg = (
            f"🏛️ **MAGI 三哲人決議** ({timestamp})\n"
            f"📋 **議題**: {question[:100]}\n\n"
            f"🔬 **Melchior [{m_vote}]**: {melchior.get('response', '')[:300]}\n\n"
            f"🍏 **Balthasar [{b_vote}]**: {balthasar.get('response', '')[:300]}\n\n"
            f"👻 **Casper [{c_vote}]**: {casper.get('response', '')[:300]}\n\n"
            f"📊 **結果**: {status} (⏱ {duration}s)"
        )
        _post_discord(dc_msg)

    return result


def format_council_result(result: Dict) -> str:
    """Format council result for human-readable display."""
    if not result.get("success"):
        return f"❌ 決議失敗：{result.get('error', 'unknown')}"

    votes = result.get("votes", {})
    passed = result.get("passed")
    status = "✅ 通過" if passed is True else "⚠️ 有條件通過" if passed == "conditional" else "🚫 否決"

    lines = [
        f"🏛️ **MAGI 三哲人決議結果** ({result.get('timestamp', '')})",
        f"📋 議題：{result.get('question', '')[:100]}",
        "",
        f"🔬 **Melchior (科學家)** [{votes.get('melchior', '?')}]",
        result.get("melchior", ""),
        "",
        f"🍏 **Balthasar (執行者)** [{votes.get('balthasar', '?')}]",
        result.get("balthasar", ""),
        "",
        f"👻 **Casper (仲裁者)** [{votes.get('casper', '?')}]",
        result.get("casper", ""),
        "",
        f"📊 **最終結果**：{status}",
        f"⏱ 耗時：{result.get('duration_sec', '?')} 秒",
        f"🤖 模型：{result.get('model', '?')}",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI Interface
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="MAGI Local Council")
    parser.add_argument("--task", type=str, help="The question/topic to deliberate")
    parser.add_argument("--context", type=str, default="", help="Additional context")
    parser.add_argument("--model", type=str, default="", help="Override model")
    parser.add_argument("--no-discord", action="store_true", help="Skip Discord notification")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    args = parser.parse_args()

    if args.debug:
        logging.basicConfig(level=logging.DEBUG)

    if not args.task:
        args.task = input("請輸入議題：").strip()
        if not args.task:
            print("❌ 未輸入議題")
            exit(1)

    result = convene_council(
        question=args.task,
        context=args.context,
        notify_discord=not args.no_discord,
        model=args.model,
    )
    print(format_council_result(result))
