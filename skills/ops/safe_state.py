"""
safe_state — 安全的 JSON state file 讀寫工具

- 讀取時自動處理損壞的 JSON（備份損壞檔、回傳空預設值）
- 寫入時使用 atomic write（tempfile + os.replace）防止中途斷電損壞
"""
import json
import logging
import os
import tempfile
from datetime import datetime
from typing import Any, Dict

logger = logging.getLogger("safe_state")


def safe_load_json(path: str, default: Any = None) -> Any:
    """
    安全讀取 JSON 檔案。

    - 檔案不存在 → 回傳 default（預設 {}）
    - JSON parse 失敗 → 備份損壞檔為 .corrupt.YYYYMMDD_HHMMSS，回傳 default
    - 編碼錯誤 → 同上
    """
    if default is None:
        default = {}
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        if not content.strip():
            return default
        return json.loads(content)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        # 備份損壞檔案
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup = f"{path}.corrupt.{ts}"
        try:
            os.rename(path, backup)
            logger.warning("State file corrupted, backed up: %s → %s (error: %s)", path, backup, e)
        except Exception:
            logger.error("State file corrupted and backup failed: %s (error: %s)", path, e)
        return default
    except Exception as e:
        logger.error("Failed to load state file %s: %s", path, e)
        return default


def safe_save_json(path: str, data: Any, **kwargs) -> bool:
    """
    Atomic 寫入 JSON 檔案（tempfile + os.replace）。

    kwargs 直接傳給 json.dump（例如 ensure_ascii, indent, default）。
    """
    dir_path = os.path.dirname(path)
    if dir_path:
        os.makedirs(dir_path, exist_ok=True)

    dump_kwargs = {"ensure_ascii": False, "indent": 2}
    dump_kwargs.update(kwargs)

    fd, tmp = tempfile.mkstemp(dir=dir_path, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, **dump_kwargs)
        os.replace(tmp, path)
        return True
    except Exception as e:
        logger.error("Failed to save state file %s: %s", path, e)
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False
