#!/usr/bin/env python3
"""
MAGI to Star-Office-UI Status Sync Bridge
Watches HEARTBEAT.md and pushes updates to the pixel office API.
"""
import os
import time
import json
import re
import urllib.request
import urllib.error

# Paths
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
HEARTBEAT_FILE = os.path.expanduser("~/.openclaw/workspace/HEARTBEAT.md")
STAR_OFFICE_API = "http://localhost:18791/agent-push"
# 確保 CASPER 可以打 localhost，如果被綁定的話

# Previous states to only send on change
last_states = {
    "Casper": None,
    "Melchior": None,
    "Balthasar": None
}

def parse_heartbeat():
    if not os.path.exists(HEARTBEAT_FILE):
        return {}
        
    states = {}
    with open(HEARTBEAT_FILE, "r", encoding="utf-8") as f:
        content = f.read()
        
        # Example: * **Balthasar (Planning/Coding)**: 🟢 Active (Idle)
        # Parse the lines
        for line in content.split("\n"):
            line = line.strip()
            if line.startswith("* **"):
                match = re.search(r'\*\*\s*(\w+)\s*\(.*?\)\*\*:\s*([^<]+)', line)
                if match:
                    name = match.group(1)
                    raw_status = match.group(2).strip()
                    states[name] = raw_status

    return states

def translate_status_to_office(raw_status):
    state = "idle"
    detail = "待命中"
    
    raw_lower = raw_status.lower()
    
    if "active" in raw_lower or "working" in raw_lower or "executing" in raw_lower:
        state = "writing"
        detail = "處理任務中..."
    elif "idle" in raw_lower or "sleeping" in raw_lower or "waiting" in raw_lower:
        state = "idle"
        detail = "待命中..."
    elif "error" in raw_lower or "alert" in raw_lower or "🔴" in raw_lower:
        state = "error"
        detail = "發生異常！"
    elif "researching" in raw_lower or "reading" in raw_lower or "analyzing" in raw_lower:
        state = "researching"
        detail = "資料檢索與分析中..."
    # Icon based mapping
    elif "🟢" in raw_lower:
        state = "writing"
        detail = "主要進程運行中..."
    elif "🟡" in raw_lower:
        state = "researching"
        detail = "分析整理中..."
        
    return state, detail

def push_to_office(agent_id, state, detail):
    data = json.dumps({
        "agentId": agent_id,
        "joinKey": "", # no auth checking needed if skipped or empty in newer backend versions if allowed 
        "state": state,
        "detail": detail
    }).encode("utf-8")
    
    req = urllib.request.Request(STAR_OFFICE_API, data=data, headers={"Content-Type": "application/json"})
    
    try:
        with urllib.request.urlopen(req) as resp:
            pass # Ignore success response to save logs
    except Exception as e:
        # Fails silently if office UI is offline
        pass

def sync_loop():
    print("Starting Star-Office-UI Sync Daemon...")
    while True:
        try:
            current_states = parse_heartbeat()
            for agent, raw_status in current_states.items():
                if agent not in last_states:
                    continue
                    
                state, detail = translate_status_to_office(raw_status)
                
                # Push always or only on change to animate? Star-Office has a 5 min idle auto-fallback
                # So we should ping it at least every 4 mins to keep them active.
                push_to_office(agent, state, detail)
                last_states[agent] = state
                
        except Exception as e:
            print(f"Sync error: {e}")
            
        # Pings every 5 seconds to keep the sprites active if they are working
        time.sleep(5)

if __name__ == "__main__":
    sync_loop()
