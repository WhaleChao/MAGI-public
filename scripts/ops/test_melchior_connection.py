import requests
import time
import json

MELCHIOR_IP = "100.116.54.16"
AGENT_PORT = 5002
BASE_URL = f"http://{MELCHIOR_IP}:{AGENT_PORT}"

def check_health():
    try:
        url = f"{BASE_URL}/health"
        print(f"Checking health at {url}...")
        resp = requests.get(url, timeout=5)
        print(f"Status: {resp.status_code}")
        print(f"Response: {resp.text}")
        return resp.json()
    except Exception as e:
        print(f"Error checking health: {e}")
        return None

def switch_mode(mode):
    try:
        url = f"{BASE_URL}/api/brain/switch"
        print(f"Switching to mode: {mode} at {url}...")
        payload = {"mode": mode}
        resp = requests.post(url, json=payload, timeout=10)
        print(f"Status: {resp.status_code}")
        print(f"Response: {resp.text}")
        return resp.json()
    except Exception as e:
        print(f"Error switching mode: {e}")
        return None

def main():
    print("=== Testing Melchior Connection & Mode Switching ===\n")

    # 1. Check Initial Status
    print("--- 1. Initial Health Check ---")
    initial_status = check_health()
    if not initial_status:
        print("CRITICAL: Melchior is unreachable!")
        return

    initial_mode = initial_status.get("mode")
    print(f"Current Mode: {initial_mode}\n")

    # 2. Switch to Distributed (Connect)
    print("--- 2. Switching to Distributed Mode (Connect) ---")
    switch_mode("distributed")
    time.sleep(2)
    
    # Verify
    print("Verifying...")
    status_dist = check_health()
    if status_dist and status_dist.get("mode") == "distributed":
        print("SUCCESS: Switched to Distributed Mode.\n")
    else:
        print("FAILURE: Could not switch to Distributed Mode.\n")

    # 3. Switch to Engineer (Disconnect)
    print("--- 3. Switching to Engineer Mode (Disconnect) ---")
    switch_mode("engineer")
    time.sleep(2)
    
    # Verify
    print("Verifying...")
    status_eng = check_health()
    if status_eng and status_eng.get("mode") == "engineer":
        print("SUCCESS: Switched to Engineer Mode.\n")
    else:
        print("FAILURE: Could not switch to Engineer Mode.\n")

    print("=== Test Complete ===")

if __name__ == "__main__":
    main()
