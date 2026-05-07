
import os
import glob
import logging

from api.runtime_paths import get_legacy_code_root, get_magi_root_dir, legacy_code_enabled

# Configuration
MAGI_ROOT = str(get_magi_root_dir())
LEGACY_CODE_ROOT = str(get_legacy_code_root())
ALLOWED_PATHS = [MAGI_ROOT]
if legacy_code_enabled():
    ALLOWED_PATHS.append(LEGACY_CODE_ROOT)

logger = logging.getLogger("CodeAnalysis")

def _resolve_alias(directory: str) -> str:
    raw = (directory or "").strip()
    if not raw:
        return MAGI_ROOT
    key = raw.lower()
    if key in {"magi", "workspace", "code"}:
        return MAGI_ROOT
    if key in {"legacy", "legacy_code", "archive"} and legacy_code_enabled():
        return LEGACY_CODE_ROOT
    return os.path.abspath(raw)


def list_files(directory):
    """
    Lists files in a given directory if allowed.
    """
    # Normalize path
    if not os.path.isabs(directory):
        directory = _resolve_alias(directory)
             
    # Security Check
    allowed = False
    for p in ALLOWED_PATHS:
        if directory.startswith(p):
            allowed = True
            break
            
    if not allowed:
        return {"success": False, "error": f"Access Denied: {directory} is not in allowed paths."}
        
    try:
        files = []
        for file in os.listdir(directory):
            if file.endswith(".py") or file.endswith(".json") or file.endswith(".md"):
                 files.append(file)
        return {"success": True, "files": files, "path": directory}
    except Exception as e:
        return {"success": False, "error": str(e)}

def read_codebase(directory_keyword="magi"):
    """
    Reads .py files with intelligent selection to fit context window.
    """
    # Resolve Directory
    target_dir = _resolve_alias(directory_keyword)
        
    logger.info(f"📂 Reading codebase from: {target_dir}")
    
    # List all candidate files
    result = list_files(target_dir)
    if not result["success"]:
        return result
        
    all_files = [f for f in result["files"] if f.endswith(".py")]
    
    # Priority Files (Entry points & Core logic)
    priority_names = ["main.py", "app.py", "server.py", "orchestrator.py", "discord_bot.py", "manage_meetings.py"]
    
    selected_files = []
    
    # 1. Add Priority Files first
    for f in all_files:
        if f.lower() in priority_names or any(p in f.lower() for p in ["api", "core", "skill"]):
             if f not in selected_files:
                 selected_files.append(f)
    
    # 2. Add remaining files up to a limit
    for f in all_files:
        if f not in selected_files:
            selected_files.append(f)
            
    # Read Content with Character Limit (approx 15k tokens max -> ~60k chars)
    MAX_CHARS = 50000 
    current_chars = 0
    file_contents = ""
    read_files_list = []
    
    for filename in selected_files:
        filepath = os.path.join(target_dir, filename)
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                content = f.read()
                
                # Skip very large files or lock files
                if len(content) > 20000: 
                    content = content[:2000] + "\n... [Truncated] ..."
                
                if current_chars + len(content) > MAX_CHARS:
                    # If we hit the limit, stop adding full files
                    # But maybe mention we skipped others?
                    break
                
                current_chars += len(content)
                file_contents += f"\n\n--- FILE: {filename} ---\n{content}"
                read_files_list.append(filename)
                
        except Exception as e:
            logger.error(f"Error reading {filename}: {e}")
            
    if not file_contents:
        return {"success": False, "error": "No .py files found or readable."}
        
    # Append summary of skipped files
    skipped_count = len(all_files) - len(read_files_list)
    if skipped_count > 0:
        file_contents += f"\n\n--- NOTE: {skipped_count} other files were detected but skipped to save memory. ---"
        
    return {
        "success": True, 
        "content": file_contents, 
        "file_list": read_files_list,
        "base_path": target_dir,
        "total_files": len(all_files)
    }

def analyze_code(directory_keyword="magi", instructions="Analyze the relationship between these files."):
    """
    Reads code and sends to LLM for analysis.
    """
    read_result = read_codebase(directory_keyword)
    if not read_result["success"]:
        return f"❌ Error: {read_result['error']}"
        
    code_content = read_result["content"]
    file_list = ", ".join(read_result["file_list"])
    total_count = read_result.get("total_files", 0)
    
    prompt = f"""
    You are an expert software architect.
    
    Context:
    I have read {len(read_result["file_list"])} key Python files from a total of {total_count} files in {read_result['base_path']}.
    Files Read: {file_list}
    
    User Instruction:
    {instructions}
    
    Code Content:
    {code_content}
    
    Please provide a comprehensive report focusing on the architecture and relationships of these key files.
    """
    
    # Use Casper (Self) via dedicated analysis function
    try:
        from skills.bridge.grounded_ai import analyze_content
        logger.info("🧠 Sending optimized code context to Casper...")
        response = analyze_content(prompt, timeout=600) # 10 minutes timeout
        return response
    except Exception as e:
        return f"❌ Analysis Failed: {e}"

def estimate_effort(directory_keyword="magi"):
    """
    Returns an estimated time and file count.
    """
    read_result = read_codebase(directory_keyword)
    if not read_result["success"]:
        return {"success": False, "message": "無法讀取目錄"}
        
    file_count = len(read_result["file_list"])
    total_chars = len(read_result["content"])
    
    # Rough estimate: 1000 chars takes ~5 seconds processing + overhead
    # 50k chars -> 250s -> ~4 mins
    estimated_seconds = (total_chars / 1000) * 5 + 30
    estimated_minutes = max(1, int(estimated_seconds / 60))
    
    return {
        "success": True,
        "file_count": file_count,
        "total_files": read_result.get("total_files", file_count),
        "estimated_minutes": f"{estimated_minutes}-{estimated_minutes+2}"
    }
