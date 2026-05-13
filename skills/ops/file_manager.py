# -*- coding: utf-8 -*-
"""
File Manager Skill (檔案管理)
Based on ClawHub community skill: file-management
Iron Dome Audit: ⚠️ RESTRICTED — Sandboxed to ALLOWED_PATHS only

Provides: File listing, search, read, size info
Does NOT provide: delete, move, write (requires admin token)
"""

import os
import logging
from datetime import datetime

logger = logging.getLogger("FileManager")

from api.runtime_paths import get_legacy_code_root, get_magi_root_dir, legacy_code_enabled

# Iron Dome: Sandbox paths
MAGI_ROOT = str(get_magi_root_dir())
ALLOWED_PATHS = [
    MAGI_ROOT,
    os.path.join(MAGI_ROOT, "skills"),
]
if legacy_code_enabled():
    ALLOWED_PATHS.append(str(get_legacy_code_root()))

# Iron Dome: Blocked extensions
BLOCKED_EXTENSIONS = [
    ".env", ".pem", ".key", ".p12", ".pfx",
    ".sqlite", ".db", ".credentials",
]


def _is_allowed(path):
    """Iron Dome sandbox check."""
    abs_path = os.path.abspath(path)
    return any(abs_path.startswith(p) for p in ALLOWED_PATHS)


def list_directory(path, show_hidden=False):
    """
    List files in a directory with details.
    """
    if not _is_allowed(path):
        return f"🛡️ Iron Dome: 存取被拒 — `{path}` 不在允許範圍內。"
    
    if not os.path.isdir(path):
        return f"❌ 路徑不存在: `{path}`"
    
    try:
        items = []
        for name in sorted(os.listdir(path)):
            if not show_hidden and name.startswith('.'):
                continue
            
            full_path = os.path.join(path, name)
            is_dir = os.path.isdir(full_path)
            
            if is_dir:
                count = len(os.listdir(full_path))
                items.append(f"📂 `{name}/` ({count} items)")
            else:
                size = os.path.getsize(full_path)
                size_str = _format_size(size)
                items.append(f"📄 `{name}` ({size_str})")
        
        header = f"📁 **{path}** ({len(items)} items)\n\n"
        return header + "\n".join(items)
    except Exception as e:
        return f"❌ 錯誤: {e}"


def search_files(directory, pattern, extension=None):
    """
    Search for files matching a pattern.
    """
    if not _is_allowed(directory):
        return f"🛡️ Iron Dome: 存取被拒 — `{directory}` 不在允許範圍內。"
    
    results = []
    pattern_lower = pattern.lower()
    
    for root, dirs, files in os.walk(directory):
        # Skip hidden dirs
        dirs[:] = [d for d in dirs if not d.startswith('.')]
        
        for fname in files:
            if pattern_lower in fname.lower():
                if extension and not fname.endswith(extension):
                    continue
                
                full_path = os.path.join(root, fname)
                rel_path = os.path.relpath(full_path, directory)
                size = os.path.getsize(full_path)
                results.append(f"- `{rel_path}` ({_format_size(size)})")
                
                if len(results) >= 30:
                    results.append("... (結果已截斷)")
                    break
        
        if len(results) >= 30:
            break
    
    if not results:
        return f"🔍 在 `{directory}` 中找不到符合 `{pattern}` 的檔案。"
    
    return f"🔍 **搜尋結果**: `{pattern}` in `{directory}`\n\n" + "\n".join(results)


def file_info(path):
    """
    Get detailed information about a file.
    """
    if not _is_allowed(path):
        return f"🛡️ Iron Dome: 存取被拒"
    
    if not os.path.exists(path):
        return f"❌ 檔案不存在: `{path}`"
    
    try:
        stat = os.stat(path)
        modified = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        
        info = f"""📄 **檔案資訊**
- 路徑: `{path}`
- 大小: {_format_size(stat.st_size)}
- 修改時間: {modified}
- 類型: {'目錄' if os.path.isdir(path) else os.path.splitext(path)[1] or '無副檔名'}
"""
        
        # If it's a text file, show first few lines
        if not os.path.isdir(path) and stat.st_size < 50000:
            ext = os.path.splitext(path)[1].lower()
            if ext not in BLOCKED_EXTENSIONS and ext in ['.py', '.md', '.txt', '.json', '.yml', '.yaml', '.cfg', '.ini', '.sh']:
                try:
                    with open(path, 'r', encoding='utf-8') as f:
                        preview = f.read(500)
                    info += f"\n📝 **預覽**:\n```\n{preview}\n```"
                except Exception:
                    logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 140, exc_info=True)
        
        return info.strip()
    except Exception as e:
        return f"❌ 錯誤: {e}"


def _format_size(size):
    """Format file size."""
    if size < 1024:
        return f"{size}B"
    elif size < 1024**2:
        return f"{size/1024:.1f}KB"
    elif size < 1024**3:
        return f"{size/(1024**2):.1f}MB"
    else:
        return f"{size/(1024**3):.1f}GB"


if __name__ == "__main__":
    print(list_directory(str(MAGI_ROOT)))
    print()
    print(search_files(str(MAGI_ROOT), "orchestrator"))
