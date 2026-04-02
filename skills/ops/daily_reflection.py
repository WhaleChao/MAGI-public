#!/usr/bin/env python3
import logging
# -*- coding: utf-8 -*-
"""
skills/ops/daily_reflection.py

Daily summarization of usage patterns, errors, and user corrections.
Parses OpenClaw JSONL session files over the last 24 hours and uses TAIDE
to synthesize a reflection summary for self-evolution.
"""

import os
import sys
import json
import time
from datetime import datetime
from pathlib import Path

_MAGI_ROOT = Path(__file__).resolve().parents[2]
if str(_MAGI_ROOT) not in sys.path:
    sys.path.insert(0, str(_MAGI_ROOT))

from api.runtime_paths import ensure_orch_on_sys_path, get_orch_dir

MAGI_DIR = str(_MAGI_ROOT)
CODE_DIR = str(get_orch_dir())
ensure_orch_on_sys_path()

from skills.bridge.inference_gateway import InferenceGateway

def parse_openclaw_sessions(hours: int = 24) -> str:
    """Read OpenClaw main agent sessions modified within `hours`."""
    base_dir = Path("/Users/ai/.openclaw/agents/main/sessions")
    if not base_dir.exists():
        return ""
    
    cutoff = time.time() - (hours * 3600)
    dialogues = []
    
    # Process jsonl files modified within the timeframe
    for p in base_dir.glob("*.jsonl"):
        try:
            if p.stat().st_mtime >= cutoff:
                lines = p.read_text(encoding="utf-8").splitlines()
                session_text = []
                for ln in lines:
                    if not ln.strip():
                        continue
                    try:
                        obj = json.loads(ln)
                        item_type = obj.get("type")
                        if item_type == "message":
                            msg_data = obj.get("message", {})
                            role = str(msg_data.get("role") or "").upper()
                            
                            content_blocks = msg_data.get("content", [])
                            content = ""
                            if isinstance(content_blocks, list):
                                for block in content_blocks:
                                    if block.get("type") == "text":
                                        content += block.get("text", "") + "\n"
                            
                            content = content.strip()
                            if content:
                                session_text.append(f"{role}: {content}")
                        elif item_type == "action":
                            action_name = str(obj.get("action") or "").strip()
                            if action_name:
                                session_text.append(f"ACTION: {action_name}")
                        elif item_type == "error":
                            err_msg = str(obj.get("error") or "").strip()
                            if err_msg:
                                session_text.append(f"ERROR: {err_msg}")
                    except Exception:
                        continue
                if session_text:
                    dialogues.append("\n".join(session_text))
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 77, exc_info=True)

    return "\n\n---\n\n".join(dialogues)

def run_reflection() -> dict:
    conversation = parse_openclaw_sessions(hours=96)
    if not conversation.strip():
        return {"success": False, "error": "No recent conversation logs found."}

    # Limit prompt size (TAIDE has 8192 context, leaving room for generation)
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
    model_hint = os.environ.get("MAGI_REFLECTION_MODEL", "cwchang/llama3-taide-lx-8b-chat-alpha1:latest")
    
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
        learnings_path = Path("/Users/ai/.openclaw/workspace/.learnings/LEARNINGS.md")
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
        print("✅ Daily reflection complete:")
        print(res.get("response"))
        if res.get("note"):
            print(f"Note: {res.get('note')}")
    else:
        print(f"❌ Failed: {res.get('error')}")
        sys.exit(1)
