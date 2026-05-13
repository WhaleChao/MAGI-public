# -*- coding: utf-8 -*-
"""
quicklook.py
============
macOS Quick Look 文件縮圖產生模組。

利用 qlmanage 產生文件預覽縮圖，不需自己渲染 PDF。
用於 Dashboard 文件預覽、案件列表縮圖。

整合點：
- Dashboard / OSC：文件列表顯示縮圖
- skills/pdf-namer/：命名後產生縮圖供確認
"""
from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path
from typing import Optional

logger = logging.getLogger("QuickLook")

DEFAULT_THUMBNAIL_DIR = "/tmp/magi-thumbnails"
DEFAULT_SIZE = 300


def generate_thumbnail(
    file_path: str,
    size: int = DEFAULT_SIZE,
    output_dir: str = DEFAULT_THUMBNAIL_DIR,
) -> Optional[str]:
    """
    利用 macOS Quick Look 產生文件縮圖。

    Args:
        file_path: 來源檔案路徑（PDF, DOCX, 圖片等）
        size: 縮圖最大邊長（像素）
        output_dir: 輸出目錄

    Returns:
        縮圖檔案路徑，或 None（失敗時）
    """
    if not os.path.isfile(file_path):
        logger.warning("QuickLook: file not found: %s", file_path)
        return None

    os.makedirs(output_dir, exist_ok=True)

    try:
        result = subprocess.run(
            ["qlmanage", "-t", "-s", str(size), "-o", output_dir, file_path],
            capture_output=True, text=True, timeout=15,
        )

        if result.returncode != 0:
            logger.warning("qlmanage failed (rc=%d): %s", result.returncode, result.stderr.strip())
            return None

        # qlmanage 輸出檔名為 原始檔名.png
        basename = os.path.basename(file_path)
        thumbnail_name = f"{basename}.png"
        thumbnail_path = os.path.join(output_dir, thumbnail_name)

        if os.path.isfile(thumbnail_path):
            return thumbnail_path

        # 有時候 qlmanage 用不同的命名（如加上 _thumb）
        for f in os.listdir(output_dir):
            if f.startswith(basename) and f.endswith(".png"):
                return os.path.join(output_dir, f)

        logger.warning("QuickLook: thumbnail not found after qlmanage")
        return None

    except subprocess.TimeoutExpired:
        logger.warning("qlmanage timed out for: %s", file_path)
        return None
    except FileNotFoundError:
        logger.error("qlmanage not found — not running on macOS?")
        return None


def generate_thumbnails_batch(
    file_paths: list[str],
    size: int = DEFAULT_SIZE,
    output_dir: str = DEFAULT_THUMBNAIL_DIR,
) -> dict[str, Optional[str]]:
    """
    批次產生文件縮圖。

    Args:
        file_paths: 來源檔案路徑列表
        size: 縮圖最大邊長
        output_dir: 輸出目錄

    Returns:
        {file_path: thumbnail_path_or_None}
    """
    results = {}
    for fp in file_paths:
        results[fp] = generate_thumbnail(fp, size, output_dir)
    return results


def cleanup_thumbnails(
    output_dir: str = DEFAULT_THUMBNAIL_DIR,
    max_age_hours: float = 24,
) -> int:
    """
    清理過期的縮圖檔案。

    Args:
        output_dir: 縮圖目錄
        max_age_hours: 保留時間（小時）

    Returns:
        刪除的檔案數
    """
    import time

    if not os.path.isdir(output_dir):
        return 0

    cutoff = time.time() - (max_age_hours * 3600)
    deleted = 0

    for f in os.listdir(output_dir):
        fp = os.path.join(output_dir, f)
        if os.path.isfile(fp) and os.path.getmtime(fp) < cutoff:
            try:
                os.remove(fp)
                deleted += 1
            except OSError:
                pass

    if deleted:
        logger.info("QuickLook: cleaned up %d old thumbnails", deleted)
    return deleted


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python quicklook.py <file_path> [size]")
        sys.exit(1)

    file_path = sys.argv[1]
    size = int(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_SIZE

    thumb = generate_thumbnail(file_path, size)
    if thumb:
        print(f"Thumbnail: {thumb}")
        print(f"Size: {os.path.getsize(thumb)} bytes")
    else:
        print("Failed to generate thumbnail")
