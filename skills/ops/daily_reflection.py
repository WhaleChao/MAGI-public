#!/usr/bin/env python3
import logging
# -*- coding: utf-8 -*-
"""
skills/ops/daily_reflection.py

Daily summarization of usage patterns, errors, and user corrections.
Parses OpenClaw JSONL session files over the last 24 hours and uses the local oMLX
chat model to synthesize a reflection summary for self-evolution.
"""

import os
import sys
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

_MAGI_ROOT = Path(__file__).resolve().parents[2]
if str(_MAGI_ROOT) not in sys.path:
    sys.path.insert(0, str(_MAGI_ROOT))

from api.runtime_paths import ensure_orch_on_sys_path, get_agent_dir, get_orch_dir, get_runtime_dir

MAGI_DIR = str(_MAGI_ROOT)
CODE_DIR = str(get_orch_dir())
ensure_orch_on_sys_path()

from skills.bridge.inference_gateway import InferenceGateway

def parse_v3_conversation_history(hours: int = 24, limit: int = 500) -> str:
    """Read recent V3 chat history through a strictly read-only SQLite handle."""

    db_path = get_runtime_dir() / "conversation_history.sqlite3"
    if not db_path.is_file():
        return ""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=max(1, int(hours)))).isoformat()
    uri = f"file:{db_path}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=2)
        try:
            rows = conn.execute(
                "SELECT session_id, role, content FROM conversation_history "
                "WHERE ts >= ? ORDER BY id DESC LIMIT ?",
                (cutoff, max(1, min(2000, int(limit)))),
            ).fetchall()
        finally:
            conn.close()
    except Exception:
        logging.getLogger(__name__).debug("V3 conversation reflection probe failed", exc_info=True)
        return ""

    dialogues = []
    for session_id, role, content in reversed(rows):
        text = str(content or "").strip()
        if not text:
            continue
        dialogues.append(
            f"SESSION {str(session_id or '')[:16]} {str(role or '').upper()}: {text[:4000]}"
        )
    return "\n".join(dialogues)

def run_reflection() -> dict:
    conversation = parse_v3_conversation_history(hours=96)
    if not conversation.strip():
        return {
            "success": True,
            "skipped": True,
            "reason": "no_recent_v3_conversation_logs",
            "response": "",
        }

    # Limit prompt size (Gemma E4B has 8192 context, leaving room for generation)
    char_limit = 20000
    if len(conversation) > char_limit:
        conversation = conversation[-char_limit:]
        
    prompt = f"""請以「繁體中文」為基礎，擔任自我反省日誌撰寫專家。
分析以下過去 24 小時內的對話紀錄（包含使用者指令、系統回覆、調用的工具與發生的錯誤），並總結這四個面向：
1. 今天的對話方向 / 系統被如何使用 (Usage Directions)
2. 遇到的困難、錯誤 (Errors encountered)
3. 使用者的指正或不滿 (User Corrections)
4. 具體的自我改進建議 (Suggestions for self-improvement)

請直接給出結構化的 Markdown。若某項沒有明顯案例，請直接回答「無明顯案例」。
請勿在結語加入任何其他冗長的客套話。

【對話紀錄開始】
{conversation}
【對話紀錄結束】
"""
    model_hint = os.environ.get("MAGI_REFLECTION_MODEL", "gemma-4-e4b-it-4bit")
    
    try:
        gateway = InferenceGateway()
        r = gateway.dispatch(
            prompt=prompt,
            task_type="reflection",
            timeout=300,
            force_quality=os.environ.get("MAGI_REFLECTION_FORCE_QUALITY", "0").strip().lower() in {"1", "true", "yes", "on"},
        )
        if not r.get("success"):
            return {"success": False, "error": r.get("error") or "Unknown error"}
            
        out = (r.get("response") or "").strip()
        if not out:
            return {"success": False, "error": "Empty response from self-improvement summary."}
            
        today_str = datetime.now().strftime("%Y-%m-%d")
        summary_md = f"## [{today_str}] Self-Evolution Daily Reflection\n\n{out}\n\n---\n"
        
        # Save to learnings
        learnings_path = get_agent_dir() / "learnings" / "LEARNINGS.md"
        learnings_path.parent.mkdir(parents=True, exist_ok=True)
        
        content = ""
        if learnings_path.exists():
            content = learnings_path.read_text(encoding="utf-8")
        learnings_path.write_text(summary_md + "\n\n" + content, encoding="utf-8")
        
        # Also store into CASPER event memory
        ensure_orch_on_sys_path()
            
        try:
            import magi_eventlog
            magi_eventlog.remember_event(
                f"self_reflection:{today_str}",
                ok=True,
                source="magi_autopilot_reflection",
                payload={"summary": out, "date": today_str},
                tags={"task": "daily_reflection", "ok": "1"},
            )
        except Exception as e:
            return {"success": True, "note": f"Saved to LEARNINGS.md but vector DB failed: {e}", "response": summary_md}

        return {"success": True, "response": summary_md}

    except Exception as e:
        return {"success": False, "error": str(e)}

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    args = parser.parse_args()
    
    print("Running self-evolution reflection...", flush=True)
    res = run_reflection()
    if res.get("success"):
        if res.get("skipped"):
            print("⏭️ Daily reflection skipped: no recent V3 conversation logs.")
        else:
            print("✅ Daily reflection complete:")
            print(res.get("response"))
        if res.get("note"):
            print(f"Note: {res.get('note')}")
    else:
        print(f"❌ Failed: {res.get('error')}")
        sys.exit(1)
