
import logging
import time
from datetime import datetime
from skills.brain_manager.action import switch_brain_mode

# Logging
logger = logging.getLogger("SunriseProtocol")
if not logging.getLogger().handlers:
    logging.basicConfig(level=logging.INFO)

def execute_sunrise_protocol():
    """
    Executes the MAGI Sunrise Protocol.
    This transitions the system from 'Local Mode' (Night Talk) back to 'Distributed Mode' (Day Mode).
    """
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M')
    logger.info(f"🌅 Initiating Sunrise Protocol at {timestamp}...")
    
    report = f"# 🌅 Sunrise Protocol Report ({timestamp})\n\n"
    
    # 1. Health Check (Optional, but good practice)
    # We can add checks for sensor data or node status here in the future.
    logger.info("1. Pre-Flight System Check...")
    
    # 2. Brain Mode Switch
    logger.info("2. Switching Brain Mode to DISTRIBUTED...")
    try:
        # success, message = switch_brain_mode("distributed", force=True) # switch_brain_mode returns a string or tuple? 
        # Checking night_talk.py usages: res = switch_brain_mode("distributed", force=True)
        # It seems it returns a string explanation based on night_talk.py line 261.
        
        result = switch_brain_mode("distributed", force=True)
        logger.info(f"✅ Switch Result: {result}")
        report += f"- **Mode Switch**: {result}\n"
        
        if "Failed" in str(result):
             report += "⚠️ **Warning**: Mode switch reported failure. Check logs.\n"
        else:
             report += "✅ **Success**: System is now in Distributed Mode.\n"

    except Exception as e:
        logger.error(f"❌ Critical Failure during Mode Switch: {e}")
        report += f"❌ **Critical Error**: {e}\n"
        return report

    # 3. Verify Melchior (Bridge Check)
    # This is a 'soft' check. If Melchior is restarting, it might fail, but that's expected.
    logger.info("3. Verifying Melchior Connection...")
    try:
        from skills.bridge import melchior_client
        if melchior_client:
            # Give it a moment to stabilize if it just switched
            time.sleep(2)
            health = melchior_client.check_health()
            if health.get('online'):
                mode = health.get('mode', 'Unknown')
                logger.info(f"✅ Melchior Online. Mode: {mode}")
                report += f"- **Melchior Status**: Online (Mode: {mode})\n"
            else:
                 logger.warning("⚠️ Melchior is offline or unresponsive.")
                 report += "- **Melchior Status**: ⚠️ Offline/Unresponsive (Expected if restarting)\n"
    except ImportError:
        logger.warning("Melchior Bridge not available for verification.")
        report += "- **Melchior Verification**: Skipped (Bridge not imported)\n"
    except Exception as e:
        logger.warning(f"Melchior verification failed: {e}")
        report += f"- **Melchior Verification**: ⚠️ Failed ({e})\n"

    logger.info("☀️ Sunrise Protocol Complete.")
    return report

if __name__ == "__main__":
    print(execute_sunrise_protocol())
