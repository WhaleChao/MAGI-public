#!/usr/bin/env python3
"""
smoke_external_chat.py - Phase C external chat observability smoke test
Calls /osc/external/chat to verify cold start and degraded performance levels.
"""

import sys
import json
import time
import requests
import os

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

def main():
    api_key = os.environ.get("OSC_EXTERNAL_API_KEY", "openclaw2026")
    port = os.environ.get("MAGI_HTTP_PORT", "5003")
    url = f"http://127.0.0.1:{port}/osc/external/chat"
    
    headers = {
        "X-API-Key": api_key,
        "Content-Type": "application/json"
    }

    # Complex Query (forces Graph-RAG / heavy search usually)
    complex_payload = {
        "user_id": "smoke_tester",
        "platform": "smoke",
        "message": "這是一個複雜問題：請幫我整理最近的存證信函範本與寫法，以及相關的法條。",
        "role": "user",
        "timeout_sec": 30
    }
    
    # Simple Small-Talk (forces fast path)
    simple_payload = {
        "user_id": "smoke_tester",
        "platform": "smoke",
        "message": "早安，今天精神如何？",
        "role": "user",
        "timeout_sec": 10
    }

    print(f"[*] Starting Smoke Test for {url}")
    print("--------------------------------------------------")

    for name, payload in [("SIMPLE", simple_payload), ("COMPLEX", complex_payload)]:
        print(f"[*] Testing {name} Tier Route...")
        try:
            t0 = time.time()
            resp = requests.post(url, headers=headers, json=payload, timeout=400)
            t1 = time.time()
            
            data = resp.json()
            is_success = data.get("success", False)
            is_degraded = data.get("degraded", False)
            meta = data.get("meta", {})
            
            print(f"   [HTTP {resp.status_code}] Success: {is_success} | Degraded: {is_degraded}")
            print(f"   Response time: {round(t1 - t0, 3)}s")
            print(f"   Metadata: {json.dumps(meta, ensure_ascii=False)}")
            
            if is_degraded:
                print("   [!] WARNING: Route is degraded (timeout breached or overloaded).")
                print(f"   reply: {data.get('reply')}")
            else:
                reply = data.get('reply', '')
                print(f"   [OK] Reply len={len(reply)} | Preview: {reply[:60].replace(chr(10), ' ')}")
                
        except Exception as e:
            print(f"   [X] Request failed: {e}")
        
        print("--------------------------------------------------")

if __name__ == "__main__":
    main()
