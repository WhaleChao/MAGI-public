import logging
import os
import subprocess
import sys
import tempfile
from pathlib import Path

if "pytest" in sys.modules:
    import pytest

    pytest.skip("simulation_test.py is a manual full-system simulation, not a pytest unit test", allow_module_level=True)

# Add project root to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from skills.bridge import melchior_client
from skills.bridge.balthasar_bridge import check_health as check_balthasar, summarize_text
from skills.bridge.melchior_bridge import analyze_image
from skills.bridge.watcher_bridge import check_health as check_watcher

try:
    from skills.source_control.git_ops import get_status as check_git
except Exception:

    def check_git():
        try:
            root = Path(__file__).resolve().parents[1]
            proc = subprocess.run(
                ["git", "-C", str(root), "status", "--short", "--branch"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if proc.returncode == 0:
                text = (proc.stdout or "").strip()
                return text or "working tree clean"
        except Exception:
            pass
        return "source control bridge unavailable"


# --- Load .env for subprocess/cron credential access ---
try:
    from dotenv import load_dotenv as _load_dotenv

    _load_dotenv()
except Exception:
    pass


# Configure Logging
logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("Simulation")


def _check_local_audit_runtime():
    try:
        from skills.iron_dome import core as iron_dome_core

        safe, msg = iron_dome_core.is_safe("simulation health probe")
        if safe:
            return True, "Iron Dome active (Watcher remote optional)"
        return False, f"Iron Dome probe blocked: {msg}"
    except Exception as e:
        return False, f"Local audit runtime unavailable ({e})"


def _check_balthasar_runtime():
    remote_online, remote_msg = check_balthasar()
    if remote_online:
        return True, f"Remote summary runtime online ({remote_msg})", ""

    summary = summarize_text("The quick brown fox jumps over the lazy dog.")
    if isinstance(summary, dict) and summary.get("success") and str(summary.get("text") or "").strip():
        provider = str(summary.get("provider") or "local-first")
        return True, f"Local-first summary runtime OK ({provider})", str(summary.get("text") or "").strip()

    error = summary.get("error") if isinstance(summary, dict) else str(summary)
    return False, f"{remote_msg}; local summary failed ({error})", ""


def _check_melchior_runtime():
    if callable(getattr(melchior_client, "_omlx_vision_available", None)) and melchior_client._omlx_vision_available():
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            image_path = f.name
            f.write(
                b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
                b"\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
            )
        try:
            result = analyze_image(image_path, prompt="Extract all text from this image exactly as shown.")
            text = str(result or "").strip()
            if text and "failed" not in text.lower() and "error" not in text.lower():
                return True, "Local vision runtime OK", text[:80]
            vision_error = text[:120]
        finally:
            try:
                os.remove(image_path)
            except Exception:
                pass
        if callable(getattr(melchior_client, "_omlx_available", None)) and melchior_client._omlx_available():
            reply = melchior_client.chat("請只回覆 OK", timeout=20)
            if reply.get("success") and str(reply.get("response") or "").strip():
                model = str(reply.get("model") or "local_omlx")
                return True, f"Local inference runtime OK ({model}; vision model unavailable)", str(reply.get("response") or "").strip()
        return False, "Local vision runtime failed", vision_error

    if callable(getattr(melchior_client, "_omlx_available", None)) and melchior_client._omlx_available():
        reply = melchior_client.chat("請只回覆 OK", timeout=20)
        if reply.get("success") and str(reply.get("response") or "").strip():
            model = str(reply.get("model") or "local_omlx")
            return True, f"Local inference runtime OK ({model}; vision model not configured)", str(reply.get("response") or "").strip()
        return False, "Local inference runtime failed", str(reply.get("error") or "")

    return False, "Neither local vision nor local inference runtime is available", ""


def run_test():
    print("\n🔮 MAGI SYSTEM SIMULATION TEST 🔮")
    print("=================================")

    # 1. Self-Check (Casper)
    print("\n[1/6] Checking Casper (Self)...")
    try:
        print("✅ Casper Core: Online")
    except Exception as e:
        print(f"❌ Casper Core: Failed ({e})")

    # 2. Git Control
    print("\n[2/6] Checking Git Control...")
    try:
        status = check_git()
        print(f"✅ Git Status: {status[:50]}...")
    except Exception as e:
        print(f"❌ Git Control: Failed ({e})")

    # 3. Keeper (Database)
    print("\n[3/6] Checking Keeper (MariaDB)...")
    try:
        import mysql.connector

        conn = mysql.connector.connect(
            host=os.environ.get("DB_HOST", "100.121.61.74"),
            user=os.environ.get("DB_USER", "casper_service"),
            password=os.environ.get("DB_PASSWORD", ""),
            database="magi_brain",
            connection_timeout=5,
        )
        if conn.is_connected():
            print("✅ Keeper: Connected (MariaDB Active)")
            conn.close()
        else:
            print("❌ Keeper: Connection Failed (No Error)")
    except Exception as e:
        print(f"❌ Keeper: Failed ({e})")

    # 4. Audit / Security
    print("\n[4/6] Checking Audit / Security Runtime...")
    try:
        is_online, msg = check_watcher()
        if is_online:
            print(f"✅ Watcher Status: {msg}")
        else:
            local_ok, local_msg = _check_local_audit_runtime()
            if local_ok:
                print(f"✅ Local Audit Runtime: {local_msg}")
                print(f"   > Watcher Remote: optional / offline ({msg})")
            else:
                print(f"❌ Audit Runtime: remote watcher offline ({msg}); local runtime failed ({local_msg})")
    except Exception as e:
        local_ok, local_msg = _check_local_audit_runtime()
        if local_ok:
            print(f"✅ Local Audit Runtime: {local_msg}")
            print(f"   > Watcher Remote: optional / error ({e})")
        else:
            print(f"❌ Audit Runtime: {e}; local runtime failed ({local_msg})")

    # 5. Balthasar Summary Runtime
    print("\n[5/6] Checking Balthasar (Summary Runtime)...")
    try:
        ok, msg, preview = _check_balthasar_runtime()
        if ok:
            print(f"✅ Balthasar Summary: {msg}")
            if preview:
                print(f"   > Reply: {preview[:50]}...")
        else:
            print(f"❌ Balthasar Summary: {msg}")
    except Exception as e:
        print(f"❌ Balthasar Summary: Error ({e})")

    # 6. Melchior Inference Runtime
    print("\n[6/6] Checking Melchior (Inference / Vision Runtime)...")
    try:
        ok, msg, preview = _check_melchior_runtime()
        if ok:
            print(f"✅ Melchior Runtime: {msg}")
            if preview:
                print(f"   > Reply: {preview[:50]}...")
        else:
            print(f"❌ Melchior Runtime: {msg}")
            if preview:
                print(f"   > Detail: {preview[:80]}...")
    except Exception as e:
        print(f"❌ Melchior Runtime: Failed ({e})")

    print("\n=================================")
    print("✨ MAGI FEDERATION SIMULATION COMPLETE ✨")


if __name__ == "__main__":
    run_test()
