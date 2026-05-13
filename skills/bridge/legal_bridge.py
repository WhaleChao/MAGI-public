import sys
import os
import subprocess
import logging
import json
from pathlib import Path

_MAGI_ROOT = Path(__file__).resolve().parents[2]
if str(_MAGI_ROOT) not in sys.path:
    sys.path.insert(0, str(_MAGI_ROOT))

from api.runtime_paths import get_orch_dir

# Configure Logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("LegalBridge")

# Paths to Legacy Scripts
CODE_DIR = str(get_orch_dir())
SCRIPTS = {
    "laf-monitor": os.path.join(CODE_DIR, "laf_automation_v2.py"),
    "file-review": os.path.join(CODE_DIR, "file_review_automation.py"),
    "paperclip-control": os.path.join(CODE_DIR, "judicial_automation_v2.py"), # Transcript Download
    "meetings": str(_MAGI_ROOT / "skills" / "law_firm" / "manage_meetings.py"), # New standardized one
}

def execute_skill(skill_name, args=[]):
    """
    Executes a legacy script as a subprocess.
    """
    if skill_name not in SCRIPTS:
        return f"❌ Skill '{skill_name}' not found."

    script_path = SCRIPTS[skill_name]
    
    if not os.path.exists(script_path):
        return f"❌ Script not found: {script_path}"

    logger.info(f"⚖️ Executing Legal Skill: {skill_name} with args: {args}")
    
    try:
        # Construct command
        # Most of these scripts might take CLI args. We need to know their interface.
        # Assuming they might run without args or with specific flags.
        # For now, we run them and capture stdout.
        
        cmd = ["python3", script_path] + args
        
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        
        if result.returncode == 0:
            logger.info(f"✅ Excution Success: {skill_name}")
            return f"✅ {skill_name} Executed Successfully.\nOutput:\n{result.stdout[:500]}..." # Truncate
        else:
            logger.error(f"❌ Execution Failed: {skill_name}")
            return f"❌ Error executing {skill_name}:\n{result.stderr}"

    except subprocess.TimeoutExpired:
        return f"⏳ Execution timed out for {skill_name}."
    except Exception as e:
        return f"💥 System Error: {str(e)}"

# Test
if __name__ == "__main__":
    if len(sys.argv) > 1:
        print(execute_skill(sys.argv[1], sys.argv[2:]))
    else:
        print("Usage: python legal_bridge.py [skill_name] [args]")
        print("Available Skills:", list(SCRIPTS.keys()))
