"""
User Activity Beacon — 使用者活動信標

當使用者透過任何通訊頻道（LINE / Discord / Telegram）發訊息時，
記錄時間戳。夜間自動任務在啟動 LLM 重度操作前檢查此信標，
若使用者近期活躍則自動延後，確保使用者體驗優先。
"""

import json
import os
import time
import logging

logger = logging.getLogger("UserActivityBeacon")

_MAGI_ROOT = os.environ.get("MAGI_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_BEACON_PATH = os.path.join(_MAGI_ROOT, ".agent", "user_activity_beacon.json")
# 預設：最近 5 分鐘內有使用者活動，就視為「活躍」
_DEFAULT_ACTIVE_THRESHOLD_SEC = int(os.environ.get("MAGI_USER_ACTIVE_THRESHOLD_SEC", "300"))


def touch(user_id: str = "", platform: str = "") -> None:
    """使用者發訊息時呼叫，寫入活動時間戳。"""
    try:
        os.makedirs(os.path.dirname(_BEACON_PATH), exist_ok=True)
        data = {
            "last_activity": time.time(),
            "user_id": str(user_id or ""),
            "platform": str(platform or ""),
            "iso": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        tmp = _BEACON_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, _BEACON_PATH)
    except Exception as e:
        logger.debug("beacon write failed: %s", e)


def is_user_active(threshold_sec: int = 0) -> bool:
    """檢查使用者是否近期活躍。"""
    threshold = threshold_sec or _DEFAULT_ACTIVE_THRESHOLD_SEC
    try:
        with open(_BEACON_PATH) as f:
            data = json.load(f)
        elapsed = time.time() - float(data.get("last_activity", 0))
        return elapsed < threshold
    except Exception:
        return False


def seconds_since_last_activity() -> float:
    """回傳距離上次使用者活動的秒數，無紀錄回傳 inf。"""
    try:
        with open(_BEACON_PATH) as f:
            data = json.load(f)
        return time.time() - float(data.get("last_activity", 0))
    except Exception:
        return float("inf")


def last_activity_info() -> dict:
    """回傳完整信標資訊，供 debug 用。"""
    try:
        with open(_BEACON_PATH) as f:
            data = json.load(f)
        data["elapsed_sec"] = round(time.time() - float(data.get("last_activity", 0)), 1)
        data["is_active"] = data["elapsed_sec"] < _DEFAULT_ACTIVE_THRESHOLD_SEC
        return data
    except Exception:
        return {"is_active": False, "elapsed_sec": float("inf")}
