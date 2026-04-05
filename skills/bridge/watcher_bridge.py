"""
WATCHER BRIDGE (MAGI-00 Interface)
==================================
Bridge module for communicating with the Watcher node.
Provides health check, status retrieval, and audit queries.
"""

import requests
import logging
import os
import sys

from skills.bridge.http_pool import get_session as _get_session

# Configuration — Watcher 跑在 NAS 上，需動態解析 IP（LAN → Tailscale fallback）
def _resolve_watcher_host() -> str:
    explicit = os.environ.get("WATCHER_HOST", "").strip()
    if explicit:
        return explicit
    try:
        from api.nas_mount_guard import resolve_nas_host
        return resolve_nas_host()
    except Exception:
        try:
            from api.routing.node_registry import get_node as _gn
            _nas = _gn("nas")
            return (_nas.lan_ip if _nas else None) or "192.168.1.3"
        except Exception:
            return "192.168.1.3"

WATCHER_HOST = _resolve_watcher_host()
WATCHER_API_PORT = int(os.environ.get("WATCHER_PORT", "5010"))

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("WatcherBridge")


def check_health():
    """Checks if Watcher is online via its API or TCP ping."""
    # Try Watcher status API first
    try:
        response = _get_session().get(f"http://{WATCHER_HOST}:{WATCHER_API_PORT}/status", timeout=5)
        if response.status_code == 200:
            return True, "Online (API OK)"
        return False, f"Status {response.status_code}"
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 31, exc_info=True)
    # Fallback: TCP connectivity check on Tailscale
    import socket
    try:
        s = socket.create_connection((WATCHER_HOST, WATCHER_API_PORT), timeout=3)
        s.close()
        return True, "Online (TCP OK, API unresponsive)"
    except Exception as e:
        return False, str(e)


def get_watcher_status():
    """
    Get detailed Watcher status.
    Tries to connect to Watcher's local status API if available.
    """
    # First, basic health check
    is_online, msg = check_health()
    
    if not is_online:
        return {
            "online": False,
            "message": msg,
            "daemon_running": False
        }
    
    # Try to get daemon status (if Watcher API is running)
    try:
        response = _get_session().get(f"http://{WATCHER_HOST}:{WATCHER_API_PORT}/status", timeout=3)
        if response.status_code == 200:
            data = response.json()
            return {
                "online": True,
                "daemon_running": True,
                "last_pull": data.get("last_pull"),
                "total_archived": data.get("total_archived", 0),
                "open_anomalies": data.get("open_anomalies", 0),
                "message": "Daemon Active"
            }
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 71, exc_info=True)  # Daemon API not available, that's okay
    
    return {
        "online": True,
        "daemon_running": False,
        "message": "Node Online (Daemon Status Unknown)"
    }


def query_archived_logs(limit=50, days=7):
    """
    Query archived audit logs from Watcher.
    
    Note: This requires Watcher API to be running.
    Falls back to local data if Watcher is unavailable.
    """
    try:
        response = _get_session().get(
            f"http://{WATCHER_HOST}:{WATCHER_API_PORT}/audit_archive",
            params={"limit": limit, "days": days},
            timeout=10
        )
        if response.status_code == 200:
            return response.json()
    except Exception as e:
        logger.warning(f"Could not query Watcher archive: {e}")
    
    return {"error": "Watcher archive unavailable", "entries": []}


def get_anomalies(unresolved_only=True):
    """
    Get detected anomalies from Watcher.
    
    Args:
        unresolved_only: If True, only return unresolved anomalies
    """
    try:
        response = _get_session().get(
            f"http://{WATCHER_HOST}:{WATCHER_API_PORT}/anomalies",
            params={"unresolved": unresolved_only},
            timeout=5
        )
        if response.status_code == 200:
            return response.json()
    except Exception as e:
        logger.warning(f"Could not query Watcher anomalies: {e}")
    
    return {"error": "Watcher anomaly data unavailable", "anomalies": []}


def trigger_pull():
    """
    Trigger an immediate audit log pull on Watcher.
    
    Note: Requires Watcher API to support this endpoint.
    """
    try:
        response = _get_session().post(
            f"http://{WATCHER_HOST}:{WATCHER_API_PORT}/pull",
            timeout=30
        )
        if response.status_code == 200:
            return response.json()
    except Exception as e:
        logger.error(f"Failed to trigger Watcher pull: {e}")
    
    return {"error": "Could not trigger Watcher pull"}


# OpenClaw Tool Definition
TOOL_DEFINITION = {
    "name": "watcher_status",
    "description": "Check the status of the Watcher audit node. Use when asked about audit logs, evidence, or system integrity.",
    "endpoint": "bridge",
    "function": "get_watcher_status",
    "parameters": {
        "type": "object",
        "properties": {},
        "required": []
    }
}


if __name__ == "__main__":
    print("🔍 Watcher Bridge Self-Test")
    print("-" * 40)
    
    status, msg = check_health()
    print(f"Health Check: {'✅' if status else '❌'} {msg}")
    
    full_status = get_watcher_status()
    print(f"Full Status: {full_status}")
