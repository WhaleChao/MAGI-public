"""
Melchior Update Skill
=====================
Automates the process of updating the Melchior Agent on the Windows machine.
Uploads the local `melchior_agent_v2.py` to the running agent's self-update endpoint.

Usage:
    python3 skills/casper/melchior_update.py
"""

import os
import sys

# Add project root to path
current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(os.path.dirname(current_dir))
sys.path.append(project_root)

from skills.bridge import melchior_client

AGENT_SOURCE_PATH = os.path.join(project_root, "For_Melchior_Setup", "melchior_agent_v2.py")

def run_update():
    print("🚀 Initiating Melchior Agent Update...")
    print(f"📂 Source: {AGENT_SOURCE_PATH}")
    
    if not os.path.exists(AGENT_SOURCE_PATH):
        print("❌ Error: Source agent file not found!")
        return
        
    print("📤 Uploading to Melchior...")
    result = melchior_client.update_agent(AGENT_SOURCE_PATH)
    
    if result.get("success"):
        print("✅ Update Successful!")
        print("⚠️  IMPORTANT: You must restart the Melchior Agent script on Windows for changes to take effect.")
    else:
        print(f"❌ Update Failed: {result.get('error')}")
        print("ℹ️  Note: If this fails, the running agent might be too old to support self-update.")
        print("    You will need to manually copy the file one last time.")

if __name__ == "__main__":
    run_update()
