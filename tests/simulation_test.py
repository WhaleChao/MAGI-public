import logging
import os
import sys
import time

if "pytest" in sys.modules:
    import pytest

    pytest.skip("simulation_test.py is a manual full-system simulation, not a pytest unit test", allow_module_level=True)

# Add project root to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from skills.bridge.balthasar_bridge import check_health as check_balthasar, summarize_text
from skills.bridge.melchior_bridge import analyze_image
from skills.bridge.watcher_bridge import check_health as check_watcher

try:
    from skills.source_control.git_ops import get_status as check_git
except Exception:
    def check_git():
        return "source control bridge unavailable"

# --- Load .env for subprocess/cron credential access ---
try:
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv()
except Exception:
    pass


# Configure Logging
logging.basicConfig(level=logging.INFO, format='%(message)s')
logger = logging.getLogger("Simulation")

def run_test():
    print("\n🔮 MAGI SYSTEM SIMULATION TEST 🔮")
    print("=================================")
    
    # 1. Self-Check (Casper)
    print("\n[1/5] Checking Casper (Self)...")
    try:
        # Simple check if we are running
        print("✅ Casper Core: Online")
    except Exception as e:
        print(f"❌ Casper Core: Failed ({e})")

    # 2. Git Control
    print("\n[2/5] Checking Git Control...")
    try:
        status = check_git()
        print(f"✅ Git Status: {status[:50]}...")
    except Exception as e:
        print(f"❌ Git Control: Failed ({e})")

    # 3. Keeper (Database)
    print("\n[3/5] Checking Keeper (MariaDB)...")
    try:
        import mysql.connector
        conn = mysql.connector.connect(
            host=os.environ.get("DB_HOST", "100.121.61.74"),
            user=os.environ.get("DB_USER", "casper_service"),
            password=os.environ.get("DB_PASSWORD", ""),
            database='magi_brain',
            connection_timeout=5
        )
        if conn.is_connected():
            print("✅ Keeper: Connected (MariaDB Active)")
            conn.close()
        else:
            print("❌ Keeper: Connection Failed (No Error)")
    except Exception as e:
        print(f"❌ Keeper: Failed ({e})")

    # 4. Watcher (Security)
    print("\n[4/5] Checking Watcher (Auditor)...")
    try:
        is_online, msg = check_watcher()
        if is_online:
            print(f"✅ Watcher Status: {msg}")
        else:
            print(f"⚠️ Watcher: Offline or Unreachable ({msg})")
    except Exception as e:
        print(f"❌ Watcher: Error ({e})")

    # 5. Balthasar (Coordinator)
    print("\n[5/5] Checking Balthasar (Coordinator)...")
    try:
        is_online, msg = check_balthasar()
        if is_online:
            print(f"✅ Balthasar Status: Online")
            # Simulation: Summarize Text
            print("   > Sending Test Summary Request...")
            # We expect Balthasar to be running server.py with /apple/summarize
            # To avoid hanging, we set a timeout in the bridge if possible, or just catch it here.
            try:
                summary = summarize_text("The quick brown fox jumps over the lazy dog.")
                print(f"   > Reply: {str(summary)[:50]}...")
            except Exception as e:
                print(f"   ⚠️ Summary Service Unreachable: {e}")
        else:
            print(f"❌ Balthasar: Offline ({msg})")
    except Exception as e:
        print(f"❌ Balthasar: Error ({e})")

    # 6. Melchior (Vision AI)
    print("\n[6/5] Checking Melchior (Vision AI)...")
    # Generate valid blank image (PNG header)
    dummy_img = "test_image.png"
    # Minimal 1x1 PNG
    with open(dummy_img, "wb") as f:
        f.write(b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82')
            
    try:
        print(f"   > Sending {dummy_img} to Melchior...")
        # Check connection implicitly via analysis call
        # This might fail if Melchior is not running the exact endpoint or model
        result = analyze_image(dummy_img) 
        print(f"   > Reply: {str(result)[:50]}...")
    except Exception as e:
        print(f"❌ Melchior: Failed ({e})")
        
    print("\n=================================")
    print("✨ MAGI FEDERATION SIMULATION COMPLETE ✨")

if __name__ == "__main__":
    run_test()
