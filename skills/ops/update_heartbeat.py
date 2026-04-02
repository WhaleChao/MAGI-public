#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
_MAGI_ROOT = os.path.abspath(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

MAGI_DIR = _MAGI_ROOT
if MAGI_DIR not in sys.path:
    sys.path.insert(0, MAGI_DIR)

from skills.bridge import melchior_client

def get_balthasar_status():
    return "🟢 Active"

def get_casper_status():
    return "🟢 Active"

def get_melchior_status():
    r = melchior_client.get_capabilities()
    if r:
        m = getattr(r, "get", lambda x, y: y)("model", "Unknown")
        return f"🟢 Active (Model: {m})"
    return "🔴 Offline"

def generate_heartbeat():
    heartbeat_path = "/Users/ai/.openclaw/workspace/HEARTBEAT.md"
    
    status_content = f"""# MAGI System Heartbeat

## System Philosophers
* **Balthasar (Planning/Coding)**: {get_balthasar_status()}
* **Casper (Memory/Context)**: {get_casper_status()}
* **Melchior (Evaluation/Chat)**: {get_melchior_status()}
"""
    
    print(status_content)
    
if __name__ == "__main__":
    generate_heartbeat()
