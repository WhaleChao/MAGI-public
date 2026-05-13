import requests
import json
import time

MELCHIOR_HOST = ""
PORT = 5002
URL = f"http://{MELCHIOR_HOST}:{PORT}/api/generate_image"

def test_payload(name, payload):
    print(f"\n🧪 Testing {name}...")
    try:
        start = time.time()
        response = requests.post(URL, json=payload, timeout=10)
        duration = time.time() - start
        
        print(f"   Status: {response.status_code}")
        print(f"   Time: {duration:.2f}s")
        
        try:
            data = response.json()
            print(f"   Response: {json.dumps(data, indent=2, ensure_ascii=False)}")
        except Exception:
            print(f"   Raw Text: {response.text}")
            
    except Exception as e:
        print(f"   ❌ Connection Error: {e}")

print(f"📡 CASPER DIAGNOSTICS PROTOCOL -> MELCHIOR ({MELCHIOR_HOST})")
print("==================================================")

# Test 1: Full Payload (The one failing)
test_payload("Full Payload", {
    "prompt": "test",
    "negative_prompt": "blur",
    "steps": 20,
    "width": 512,
    "height": 512,
    "cfg_scale": 7,
    "model": "realisticVisionV51.safetensors"
})

# Test 2: Minimal Payload
test_payload("Minimal Payload", {
    "prompt": "test"
})

# Test 3: No Model Payload
test_payload("No Model Payload", {
    "prompt": "test",
    "steps": 10
})

# Test 4: String Model (Just to check override settings)
test_payload("String Model Payload", {
    "prompt": "test",
    "override_settings": "THIS IS A STRING" 
})

# Test 5: Empty Payload
test_payload("Empty Payload", {})

# Test 6: Health Check
print("\n🏥 Checking Health...")
try:
    r = requests.get(f"http://{MELCHIOR_HOST}:{PORT}/health", timeout=5)
    print(f"   Health: {r.status_code} - {r.text}")
except Exception:
    print("   Health: OFFLINE")

# Test 7: Iron Dome Check (To verify app is same)
print("\n🛡️ Checking Iron Dome Status...")
try:
    r = requests.get(f"http://{MELCHIOR_HOST}:{PORT}/api/iron_dome/status", timeout=5)
    print(f"   Iron Dome: {r.status_code}")
    print(f"   Response: {r.text[:200]}")
except Exception:
    print("   Iron Dome: OFFLINE")
