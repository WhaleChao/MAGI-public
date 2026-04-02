import logging

import sys
import os
import json
import traceback
import subprocess
from datetime import datetime

# --- Load .env for subprocess/cron credential access ---
try:
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', '.env'))
except Exception:
    logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 13, exc_info=True)


# Ensure project root is in path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))

try:
    from skills.bridge.melchior_client import chat, generate_code
except ImportError:
    print("CRITICAL: Could not import Melchior Client.")
    sys.exit(1)

# Config Paths
CONFIG_PATH = os.path.join(os.path.dirname(__file__), '../../../code/config.json')

def load_config():
    """Load config from osc.py's config.json"""
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            print(f"Error loading config: {e}")
    return {}

def run_judicial_task(config):
    """Run Judicial Automation Task"""
    print("\n--- Running Judicial Automation ---")
    from skills.legal.judicial import LawyerSSO, CourtRecordDownloader
    
    # Updated to read from "judicial" block
    jid_config = config.get("judicial", {})
    username = os.environ.get('MAGI_JUDICIAL_RECORD_USERNAME', '') or jid_config.get('record_username', '')
    password = os.environ.get('MAGI_JUDICIAL_RECORD_PASSWORD', '') or jid_config.get('record_password', '')
    
    if not username or not password:
        print("MISSING CREDENTIALS: Please set 'judicial.record_username' and 'judicial.record_password' in config.json")
        # For demo/dev purposes, we might just return True or raise a specific error
        raise ValueError("Missing Credentials in 'judicial' config block")

    # Example workflow: Login and Check
    sso = LawyerSSO(username, password, headless=True)
    if sso.login():
        print("SSO Login Successful")
        sso.close()
        return True
    else:
        raise RuntimeError("SSO Login Failed")

def run_laf_task(config):
    """Run LAF Automation Task"""
    print("\n--- Running LAF Automation ---")
    from skills.legal.laf import LAFWebAutomation
    
    # Updated to read from "laf" block
    laf_config = config.get("laf", {})
    username = os.environ.get('MAGI_LAF_USERNAME', '') or laf_config.get('username', '')
    password = os.environ.get('MAGI_LAF_PASSWORD', '') or laf_config.get('password', '')
    
    if not username or not password:
        raise ValueError("Missing Credentials in 'laf' config block")

    laf = LAFWebAutomation(username, password, download_folder='./downloads')
    
    if laf.login():
        print("LAF Login Successful")
        laf.close()
        return True
    else:
        laf.close()
        raise RuntimeError("LAF Login Failed")

def diagnose_and_heal(error_msg, file_path):
    """
    Ask Melchior to diagnose the error and suggest a fix.
    """
    print(f"\n🚑 [SELF-HEALING] Diagnosing error in {file_path}...")
    
    # Read the file content
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            code_content = f.read()
    except Exception as e:
        print(f"Could not read source code: {e}")
        return

    # Construct Prompt
    prompt = f"""
I encountered a runtime error in my Python script.
File: {os.path.basename(file_path)}

Error Traceback:
{error_msg}

Source Code (Truncated or Full):
```python
{code_content[-4000:]} 
```
(Note: Only showing last 4000 chars roughly. If the error line is earlier, I might need more context, but try to fix based on this.)

Task:
1. Analyze the error.
2. Provide a FIXED version of the specific function or block that caused the error.
3. Return valid Python code replacement.
"""
    
    print("Sending diagnostic request to Melchior...")
    response = chat(prompt, model="taide-12b") # diagnostic fallback — local model
    suggestion = response.get("response", "")
    
    print("\n--- Melchior's Diagnosis ---")
    print(suggestion)
    print("----------------------------")
    
    # In a fully autonomous mode, we would parse this and apply the patch.
    # For now, we log it for the user.
    log_file = f"error_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
    with open(log_file, 'w', encoding='utf-8') as f:
        f.write(f"# Error Report\n\n## Traceback\n{error_msg}\n\n## Melchior Suggestion\n{suggestion}")
    print(f"Report saved to {log_file}")

def main():
    if len(sys.argv) < 2:
        print("Usage: python runner.py [judicial|laf]")
        sys.exit(1)
        
    task_name = sys.argv[1]
    config = load_config()
    
    target_file = ""
    if task_name == 'judicial':
        target_file = os.path.join(os.path.dirname(__file__), 'judicial.py')
    elif task_name == 'laf':
        target_file = os.path.join(os.path.dirname(__file__), 'laf.py')
    
    try:
        if task_name == 'judicial':
            run_judicial_task(config)
        elif task_name == 'laf':
            run_laf_task(config)
        else:
            print("Unknown task")
            
    except Exception:
        # Catch ALL errors
        error_msg = traceback.format_exc()
        print(f"\n❌ Execution Failed:\n{error_msg}")
        
        # Trigger Self-Healing
        if target_file and os.path.exists(target_file):
            diagnose_and_heal(error_msg, target_file)

if __name__ == "__main__":
    main()
