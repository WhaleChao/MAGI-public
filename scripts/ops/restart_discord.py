
import os
import signal
import subprocess
import threading
import time
import sys
_MAGI_ROOT = os.path.abspath(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

def restart_discord_bot():
    print("🔄 Restarting Discord Bot...")
    
    # 1. Kill existing discord_bot.py using Process Guardian
    try:
        # Add project root to sys.path to find skills
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
        from skills.ops.process_guardian import force_kill_all
        
        print("🛡️ Invoking Process Guardian...")
        # Check if Daemon is running
        from skills.ops.process_guardian import is_daemon_running, force_kill_all
        
        has_daemon = is_daemon_running()
        if has_daemon:
            print("⚠️ Daemon Supervisor detected!")
            print("🛑 Killing Discord Bot and letting Daemon restart it...")
            print(force_kill_all("api/discord_bot.py"))
            print("✅ Done. Daemon should restart it within 10 seconds.")
            return # Exit, do not start manually
            
        # No daemon, proceed with manual kill
        report = force_kill_all("api/discord_bot.py")
        print(report)
        
    except Exception as e:
        print(f"⚠️ Process Guardian failed, falling back to pkill: {e}")
        subprocess.run(["pkill", "-f", "api/discord_bot.py"])

    time.sleep(2)

    # 2. Start new instance (Only if no daemon)
    log_path = os.path.join(_MAGI_ROOT, "discord.log")
    print(f"Starting: discord_bot.py → {log_path}")

    log_fh = open(log_path, "a")
    try:
        _p = subprocess.Popen(
            [os.path.join(_MAGI_ROOT, "venv", "bin", "python3"),
             os.path.join(_MAGI_ROOT, "api", "discord_bot.py")],
            stdout=log_fh, stderr=log_fh,
            cwd=_MAGI_ROOT,
            start_new_session=True,  # detach from parent (replaces nohup + &)
        )
    except Exception:
        log_fh.close()
        raise
    threading.Thread(target=_p.wait, daemon=True).start()
    
    print("✅ Discord Bot Restarted.")
    print("Check logs with: tail -f discord.log")

if __name__ == "__main__":
    restart_discord_bot()
