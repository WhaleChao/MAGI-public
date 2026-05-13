# -*- coding: utf-8 -*-
"""
finder_ops.py
=============
Finder AppleScript 檔案操作模組。

SMB 網路磁碟上的檔案操作用 Finder AppleScript 比 Python shutil 更穩定。
提供移動、複製、重命名等操作的 Finder 原生實作。

整合點：
- skills/pdf-namer/：歸檔時用 Finder 移動（對 SMB 更穩定）
- casper_ecosystem/safe_fs.py：安全檔案操作的底層
"""
from __future__ import annotations

import logging
import os
import subprocess
from typing import Optional

logger = logging.getLogger("FinderOps")


def _run_osascript(script: str, timeout: int = 15) -> tuple[bool, str]:
    """執行 AppleScript。"""
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=timeout,
        )
        if result.returncode == 0:
            return True, result.stdout.strip()
        return False, result.stderr.strip()
    except subprocess.TimeoutExpired:
        return False, "timeout"
    except FileNotFoundError:
        return False, "osascript not found"


def _escape(text: str) -> str:
    """轉義 AppleScript 字串。"""
    return text.replace("\\", "\\\\").replace('"', '\\"')


# ---------------------------------------------------------------------------
# Finder 檔案操作
# ---------------------------------------------------------------------------

def move_file(src: str, dst_folder: str) -> bool:
    """
    透過 Finder 移動檔案，對 SMB 網路磁碟更穩定。

    Args:
        src: 來源檔案的完整路徑
        dst_folder: 目標資料夾路徑

    Returns:
        True if moved successfully
    """
    if not os.path.isfile(src):
        logger.warning("FinderOps: source file not found: %s", src)
        return False
    if not os.path.isdir(dst_folder):
        logger.warning("FinderOps: destination folder not found: %s", dst_folder)
        return False

    src_esc = _escape(src)
    dst_esc = _escape(dst_folder)

    script = f'''
    tell application "Finder"
        move POSIX file "{src_esc}" to POSIX file "{dst_esc}" with replacing
    end tell
    '''
    ok, output = _run_osascript(script)
    if ok:
        logger.info("FinderOps: moved %s → %s", os.path.basename(src), dst_folder)
    else:
        logger.error("FinderOps: move failed: %s", output)
    return ok


def copy_file(src: str, dst_folder: str) -> bool:
    """
    透過 Finder 複製檔案。

    Args:
        src: 來源檔案的完整路徑
        dst_folder: 目標資料夾路徑

    Returns:
        True if copied successfully
    """
    if not os.path.isfile(src):
        logger.warning("FinderOps: source file not found: %s", src)
        return False
    if not os.path.isdir(dst_folder):
        logger.warning("FinderOps: destination folder not found: %s", dst_folder)
        return False

    src_esc = _escape(src)
    dst_esc = _escape(dst_folder)

    script = f'''
    tell application "Finder"
        duplicate POSIX file "{src_esc}" to POSIX file "{dst_esc}" with replacing
    end tell
    '''
    ok, output = _run_osascript(script)
    if ok:
        logger.info("FinderOps: copied %s → %s", os.path.basename(src), dst_folder)
    else:
        logger.error("FinderOps: copy failed: %s", output)
    return ok


def rename_file(file_path: str, new_name: str) -> bool:
    """
    透過 Finder 重新命名檔案。

    Args:
        file_path: 檔案完整路徑
        new_name: 新檔案名稱（不含路徑）

    Returns:
        True if renamed successfully
    """
    if not os.path.isfile(file_path):
        logger.warning("FinderOps: file not found: %s", file_path)
        return False

    path_esc = _escape(file_path)
    name_esc = _escape(new_name)

    script = f'''
    tell application "Finder"
        set theFile to POSIX file "{path_esc}" as alias
        set name of theFile to "{name_esc}"
    end tell
    '''
    ok, output = _run_osascript(script)
    if ok:
        logger.info("FinderOps: renamed → %s", new_name)
    else:
        logger.error("FinderOps: rename failed: %s", output)
    return ok


def create_folder(folder_path: str) -> bool:
    """
    透過 Finder 建立資料夾（含巢狀路徑）。

    Args:
        folder_path: 要建立的資料夾完整路徑

    Returns:
        True if created (or already exists)
    """
    if os.path.isdir(folder_path):
        return True

    # 確保父目錄存在
    parent = os.path.dirname(folder_path)
    if not os.path.isdir(parent):
        logger.warning("FinderOps: parent folder not found: %s", parent)
        return False

    parent_esc = _escape(parent)
    name = os.path.basename(folder_path)
    name_esc = _escape(name)

    script = f'''
    tell application "Finder"
        make new folder at POSIX file "{parent_esc}" with properties {{name:"{name_esc}"}}
    end tell
    '''
    ok, output = _run_osascript(script)
    if ok:
        logger.info("FinderOps: created folder %s", folder_path)
    else:
        # 可能資料夾已存在（race condition）
        if os.path.isdir(folder_path):
            return True
        logger.error("FinderOps: create folder failed: %s", output)
    return ok


def reveal_in_finder(path: str) -> bool:
    """
    在 Finder 中顯示檔案或資料夾。

    Args:
        path: 檔案或資料夾路徑
    """
    path_esc = _escape(path)
    script = f'''
    tell application "Finder"
        reveal POSIX file "{path_esc}"
        activate
    end tell
    '''
    ok, _ = _run_osascript(script)
    return ok


def get_file_info(file_path: str) -> Optional[dict]:
    """
    透過 Finder 取得檔案資訊。

    Returns:
        {"name": str, "size": int, "kind": str, "creation_date": str}
    """
    if not os.path.exists(file_path):
        return None

    path_esc = _escape(file_path)
    script = f'''
    tell application "Finder"
        set theFile to POSIX file "{path_esc}" as alias
        set fName to name of theFile
        set fSize to size of theFile
        set fKind to kind of theFile
        set fDate to creation date of theFile as text
        return fName & "||" & (fSize as text) & "||" & fKind & "||" & fDate
    end tell
    '''
    ok, output = _run_osascript(script)
    if ok and output:
        parts = output.split("||")
        if len(parts) >= 4:
            return {
                "name": parts[0],
                "size": int(parts[1]) if parts[1].isdigit() else 0,
                "kind": parts[2],
                "creation_date": parts[3],
            }
    return None


def move_to_trash(file_path: str) -> bool:
    """
    透過 Finder 將檔案移至垃圾桶（安全刪除）。

    注意：此操作可復原（與 os.remove 不同）。

    Args:
        file_path: 檔案路徑

    Returns:
        True if moved to trash
    """
    if not os.path.exists(file_path):
        return False

    path_esc = _escape(file_path)
    script = f'''
    tell application "Finder"
        delete POSIX file "{path_esc}"
    end tell
    '''
    ok, output = _run_osascript(script)
    if ok:
        logger.info("FinderOps: moved to trash: %s", os.path.basename(file_path))
    return ok


# ---------------------------------------------------------------------------
# 批次操作
# ---------------------------------------------------------------------------

def move_files_batch(
    files: list[str],
    dst_folder: str,
    use_finder: bool = True,
) -> dict[str, bool]:
    """
    批次移動檔案。

    Args:
        files: 來源檔案路徑列表
        dst_folder: 目標資料夾
        use_finder: True 用 Finder（SMB 穩定），False 用 shutil

    Returns:
        {file_path: success}
    """
    results = {}
    for f in files:
        if use_finder:
            results[f] = move_file(f, dst_folder)
        else:
            import shutil
            try:
                shutil.move(f, dst_folder)
                results[f] = True
            except Exception as e:
                logger.error("shutil.move failed for %s: %s", f, e)
                results[f] = False
    return results


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys

    if "--info" in sys.argv and len(sys.argv) > 2:
        idx = sys.argv.index("--info")
        path = sys.argv[idx + 1]
        info = get_file_info(path)
        if info:
            print(f"Name: {info['name']}")
            print(f"Size: {info['size']:,} bytes")
            print(f"Kind: {info['kind']}")
            print(f"Created: {info['creation_date']}")
        else:
            print("File not found or error")

    elif "--reveal" in sys.argv and len(sys.argv) > 2:
        idx = sys.argv.index("--reveal")
        path = sys.argv[idx + 1]
        reveal_in_finder(path)

    else:
        print("Usage:")
        print("  python finder_ops.py --info <path>    # Get file info")
        print("  python finder_ops.py --reveal <path>  # Show in Finder")
