"""
debug_capture.py — 統一 debug 截圖/HTML 輸出管理
所有模組的 debug 截圖改為呼叫此 helper，輸出到 .runtime/debug_screenshots/
每次寫入時同步追加 MD 紀錄，PNG/HTML 保留 48 小時後由清理 job 刪除。
"""
import os, time, logging
from pathlib import Path
from datetime import datetime

logger = logging.getLogger("DebugCapture")

_MAGI_ROOT = Path(os.environ.get("MAGI_ROOT_DIR", Path(__file__).resolve().parent.parent))
DEBUG_DIR = _MAGI_ROOT / ".runtime" / "debug_screenshots"
DEBUG_MD = _MAGI_ROOT / ".runtime" / "debug_archive" / "debug_log.md"

def _ensure_dirs():
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    DEBUG_MD.parent.mkdir(parents=True, exist_ok=True)

def save_debug_screenshot(driver, prefix: str, context: str = "") -> str:
    """截圖並記錄到 MD。回傳截圖路徑。不改變任何流程邏輯。"""
    _ensure_dirs()
    ts = int(time.time())
    filename = f"{prefix}_{ts}.png"
    filepath = str(DEBUG_DIR / filename)
    try:
        driver.save_screenshot(filepath)
        _append_md(filename, "截圖", context)
        logger.debug("Debug screenshot: %s", filepath)
    except Exception as e:
        logger.warning("Debug screenshot failed: %s", e)
    return filepath

def save_debug_html(driver_or_html, prefix: str, context: str = "", html_str: str = "") -> str:
    """儲存 HTML 快照並記錄到 MD。回傳路徑。"""
    _ensure_dirs()
    ts = int(time.time())
    filename = f"{prefix}_{ts}.html"
    filepath = str(DEBUG_DIR / filename)
    try:
        content = html_str or (driver_or_html.page_source if hasattr(driver_or_html, 'page_source') else str(driver_or_html))
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(content)
        _append_md(filename, "HTML", context)
    except Exception as e:
        logger.warning("Debug HTML save failed: %s", e)
    return filepath

def _append_md(filename: str, ftype: str, context: str):
    """追加一行到 debug_log.md"""
    try:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"| {ts} | {ftype} | `{filename}` | {context} |\n"
        with open(DEBUG_MD, "a", encoding="utf-8") as f:
            if f.tell() == 0:
                f.write("# MAGI Debug Log\n\n| 時間 | 類型 | 檔名 | 上下文 |\n|------|------|------|--------|\n")
            f.write(line)
    except Exception:
        pass

def cleanup_old(max_age_hours: int = 48):
    """刪除超過 max_age_hours 的 debug 檔案。由 cron 呼叫。"""
    if not DEBUG_DIR.exists():
        return 0
    cutoff = time.time() - max_age_hours * 3600
    deleted = 0
    for f in DEBUG_DIR.iterdir():
        if f.suffix in ('.png', '.html') and f.stat().st_mtime < cutoff:
            f.unlink(missing_ok=True)
            deleted += 1
    return deleted
