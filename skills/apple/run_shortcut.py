import subprocess
import sys
import json

def list_shortcuts():
    try:
        result = subprocess.run(['shortcuts', 'list'], capture_output=True, text=True)
        return result.stdout.splitlines()
    except Exception as e:
        return []

def run_shortcut(name, input_text=None):
    cmd = ['shortcuts', 'run', name]
    if input_text:
        cmd.extend(['-i', input_text])
        
    try:
        print(f"🍎 Running shortcut: '{name}' (Input: {input_text})...")
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        
        if result.returncode == 0:
            return {"success": True, "output": result.stdout.strip()}
        else:
            return {"success": False, "error": result.stderr.strip()}
            
    except subprocess.TimeoutExpired:
         return {"success": False, "error": "Timeout"}
    except Exception as e:
        return {"success": False, "error": str(e)}

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python run_shortcut.py <shortcut_name> [input_text]")
        sys.exit(1)
        
    name = sys.argv[1]
    input_val = sys.argv[2] if len(sys.argv) > 2 else None
    
    # Validation if needed
    # valid_shortcuts = list_shortcuts()
    # if name not in valid_shortcuts: ...
    
    res = run_shortcut(name, input_val)
    print(json.dumps(res))
