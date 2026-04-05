"""
Iron Dome Distributed Sync (鐵穹分散式同步)
===========================================
When one MAGI node updates Iron Dome rules, notify all other nodes to sync.

Architecture:
- Each node exposes /api/iron_dome/sync endpoint
- On local update, broadcast to all known nodes
- Receivers pull latest patterns from Keeper (central source of truth)
"""

import os
_MAGI_ROOT = os.path.abspath(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
import json
import hashlib
import logging
import subprocess
import requests
from datetime import datetime
from typing import List, Dict, Tuple

logger = logging.getLogger("IronDomeSync")

# NOTE:
# - Casper/Melchior/Balthasar 之間會透過 Tailscale 互通。
# - 某些節點可能尚未部署 /api/iron_dome/* 端點；此時應標示為 UNSUPPORTED，而不是 ERROR 以免誤報。

def _env_str(key: str, default: str = "") -> str:
    v = (os.environ.get(key) or "").strip()
    return v if v else default


def _env_int(key: str, default: int) -> int:
    raw = (os.environ.get(key) or "").strip()
    if not raw:
        return int(default)
    try:
        return int(raw)
    except Exception:
        return int(default)


def _tailscale_ip() -> str:
    """
    Best-effort: resolve local Tailscale IPv4.
    Avoids hardcoding Casper 的 127.0.0.1 造成其他節點拉不到 patterns。
    """
    try:
        r = subprocess.run(["tailscale", "ip", "-4"], capture_output=True, text=True, timeout=1.5)
        ips = [ln.strip() for ln in (r.stdout or "").splitlines() if ln.strip()]
        return ips[0] if ips else ""
    except Exception:
        return ""


def _advertise_ip() -> str:
    # Allow explicit override first.
    ip = _env_str("MAGI_ADVERTISE_IP", "")
    if ip:
        return ip
    # Tailscale if present.
    ip = _tailscale_ip()
    if ip:
        return ip
    # Fallback to node registry for current node.
    info = MAGI_NODES.get(CURRENT_NODE) or {}
    return str(info.get("ip") or "")


def _node_ip_or(name: str, fallback: str) -> str:
    try:
        from api.routing.node_registry import get_node_ip
        return get_node_ip(name) or fallback
    except Exception:
        return fallback


# Node Registry (Tailscale IPs)
MAGI_NODES = {
    "casper": {"ip": _env_str("MAGI_CASPER_IP", "127.0.0.1"), "port": _env_int("MAGI_CASPER_PORT", 5002), "role": "Governor"},
    "melchior": {"ip": _env_str("MAGI_MELCHIOR_IP", _node_ip_or("melchior", "100.116.54.16")), "port": _env_int("MAGI_MELCHIOR_PORT", 5002), "role": "Scientist"},
    "balthasar": {"ip": _env_str("MAGI_BALTHASAR_IP", _node_ip_or("balthasar", "100.118.235.126")), "port": _env_int("MAGI_BALTHASAR_PORT", 5002), "role": "Coordinator"},
}

# Current node identity
CURRENT_NODE = os.environ.get("MAGI_NODE", "casper")

PATTERNS_CACHE_FILE = f"{_MAGI_ROOT}/static/iron_dome_patterns.json"


def _normalize_pattern_list(items) -> List[str]:
    return [p for p in (items or []) if isinstance(p, str) and p.strip()]


def _load_pattern_lists() -> Tuple[List[str], List[str]]:
    """
    Load canonical pattern lists from skills.iron_dome.core.
    Keep backward-compat fallback for older deployments.
    """
    try:
        from skills.iron_dome.core import STATIC_RULE_SETS  # type: ignore
        if isinstance(STATIC_RULE_SETS, dict):
            inj = _normalize_pattern_list(STATIC_RULE_SETS.get("PROMPT_INJECTION"))
            danger = _normalize_pattern_list(STATIC_RULE_SETS.get("DESTRUCTIVE_COMMAND"))
            if not danger:
                danger = _normalize_pattern_list(STATIC_RULE_SETS.get("DANGEROUS_COMMAND"))
            if inj or danger:
                return inj, danger
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 101, exc_info=True)

    try:
        from skills.iron_dome.core import PROMPT_INJECTION_PATTERNS, DESTRUCTIVE_PATTERNS  # type: ignore
        return _normalize_pattern_list(PROMPT_INJECTION_PATTERNS), _normalize_pattern_list(DESTRUCTIVE_PATTERNS)
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 107, exc_info=True)

    # Last-resort compatibility for legacy nodes still exposing bridge constants.
    try:
        from skills.bridge.iron_dome import PROMPT_INJECTION_PATTERNS, DANGEROUS_COMMAND_PATTERNS  # type: ignore
        return _normalize_pattern_list(PROMPT_INJECTION_PATTERNS), _normalize_pattern_list(DANGEROUS_COMMAND_PATTERNS)
    except Exception:
        return [], []


def get_patterns_hash() -> str:
    """Generate hash of current Iron Dome patterns for change detection."""
    try:
        inj, danger = _load_pattern_lists()
        payload = {
            "prompt_injection": inj,
            "dangerous_commands": danger,
        }
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        return hashlib.md5(raw).hexdigest()
    except Exception:
        return ""


def export_patterns() -> Dict:
    """Export current patterns to shareable format."""
    try:
        prompt_injection_patterns, dangerous_command_patterns = _load_pattern_lists()
        
        patterns = {
            "version": datetime.now().isoformat(),
            "source_node": CURRENT_NODE,
            "hash": get_patterns_hash(),
            "patterns": {
                "prompt_injection": prompt_injection_patterns,
                "dangerous_commands": dangerous_command_patterns
            }
        }
        
        # Cache locally
        with open(PATTERNS_CACHE_FILE, 'w') as f:
            json.dump(patterns, f, indent=2, ensure_ascii=False)
        
        return patterns
    except Exception as e:
        logger.error(f"❌ Export patterns error: {e}")
        return {}


def broadcast_update():
    """
    Broadcast Iron Dome update to all other MAGI nodes.
    Called when local patterns are modified.
    """
    logger.info(f"📡 Broadcasting Iron Dome update from {CURRENT_NODE}...")
    
    current_hash = get_patterns_hash()
    results = {}
    
    for node_name, node_info in MAGI_NODES.items():
        if node_name == CURRENT_NODE:
            continue  # Skip self
        
        try:
            url = f"http://{node_info['ip']}:{node_info['port']}/api/iron_dome/notify"
            payload = {
                "source": CURRENT_NODE,
                # Provide routable address for peers to pull patterns from Casper.
                "source_ip": _advertise_ip(),
                "source_port": int(MAGI_NODES.get(CURRENT_NODE, {}).get("port") or 5002),
                "hash": current_hash,
                "timestamp": datetime.now().isoformat()
            }
            
            response = requests.post(url, json=payload, timeout=10)
            
            if response.status_code == 200:
                logger.info(f"✅ Notified {node_name}")
                results[node_name] = "OK"
            elif response.status_code == 404:
                # Node doesn't support this endpoint yet.
                logger.info(f"ℹ️ {node_name} does not support iron_dome notify (HTTP 404)")
                # Fallback: use skills sync (Melchior supports /api/skills/sync) to propagate latest iron_dome.py.
                if node_name == "melchior" and CURRENT_NODE == "casper":
                    try:
                        from skills.bridge.melchior_manager import sync_skills_to_melchior

                        ss = sync_skills_to_melchior(force=True, smoke_test=False)
                        if ss.get("success"):
                            results[node_name] = "UNSUPPORTED_SYNCED_VIA_SKILLS"
                        else:
                            results[node_name] = f"UNSUPPORTED_SYNC_SKILLS_FAILED: {ss.get('error','')}"
                    except Exception as e:
                        results[node_name] = f"UNSUPPORTED_SYNC_SKILLS_ERROR: {e}"
                else:
                    results[node_name] = "UNSUPPORTED"
            else:
                logger.warning(f"⚠️ {node_name} returned {response.status_code}")
                results[node_name] = f"HTTP {response.status_code}"
                
        except requests.exceptions.ConnectionError:
            logger.warning(f"⚠️ {node_name} offline")
            results[node_name] = "OFFLINE"
        except Exception as e:
            logger.error(f"❌ Error notifying {node_name}: {e}")
            results[node_name] = str(e)
    
    return results


def receive_update_notification(source_node: str, source_hash: str) -> Dict:
    """
    Handle update notification from another node.
    Compare hashes and pull if different.
    """
    logger.info(f"📥 Received Iron Dome update notification from {source_node}")
    
    local_hash = get_patterns_hash()
    
    if local_hash == source_hash:
        logger.info("✅ Already in sync")
        return {"status": "SYNCED", "action": "none"}
    
    # Hash differs - need to pull
    logger.info(f"🔄 Hash mismatch. Local: {local_hash[:8]}... Remote: {source_hash[:8]}...")
    
    # Pull from source node
    result = pull_patterns_from(source_node)
    
    return {
        "status": "UPDATED" if result else "FAILED",
        "action": "pulled",
        "from": source_node
    }


def pull_patterns_from(node_name: str) -> bool:
    """Pull Iron Dome patterns from specified node."""
    if node_name not in MAGI_NODES:
        logger.error(f"❌ Unknown node: {node_name}")
        return False
    
    node_info = MAGI_NODES[node_name]
    
    try:
        url = f"http://{node_info['ip']}:{node_info['port']}/api/iron_dome/patterns"
        response = requests.get(url, timeout=30)
        
        if response.status_code == 200:
            patterns = response.json()
            
            # Save to cache. iron_dome.py 會在執行中自動讀取 PATTERNS_CACHE_FILE 並熱更新 regex。
            with open(PATTERNS_CACHE_FILE, 'w') as f:
                json.dump(patterns, f, indent=2, ensure_ascii=False)
            
            logger.info(f"✅ Pulled patterns from {node_name}")
            logger.info("♻️ 已更新 patterns cache；服務端將自動套用（hot-reload）。")
            
            return True
        else:
            logger.error(f"❌ Failed to pull from {node_name}: HTTP {response.status_code}")
            return False
            
    except Exception as e:
        logger.error(f"❌ Pull error: {e}")
        return False


def get_sync_status() -> Dict:
    """Get current sync status with all nodes."""
    local_hash = get_patterns_hash()
    status = {
        "node": CURRENT_NODE,
        "local_hash": local_hash,
        "timestamp": datetime.now().isoformat(),
        "nodes": {}
    }
    
    for node_name, node_info in MAGI_NODES.items():
        if node_name == CURRENT_NODE:
            status["nodes"][node_name] = {"status": "SELF", "hash": local_hash}
            continue
        
        try:
            url = f"http://{node_info['ip']}:{node_info['port']}/api/iron_dome/hash"
            response = requests.get(url, timeout=5)
            
            if response.status_code == 200:
                remote_hash = response.json().get("hash", "")
                synced = remote_hash == local_hash
                status["nodes"][node_name] = {
                    "status": "SYNCED" if synced else "OUTDATED",
                    "hash": remote_hash,
                    "synced": synced
                }
            elif response.status_code == 404:
                status["nodes"][node_name] = {"status": "UNSUPPORTED", "http": 404}
            else:
                status["nodes"][node_name] = {"status": "ERROR", "http": response.status_code}
                
        except requests.exceptions.ConnectionError:
            status["nodes"][node_name] = {"status": "OFFLINE"}
        except Exception as e:
            status["nodes"][node_name] = {"status": "ERROR", "error": str(e)}
    
    return status


# Flask routes (to be integrated into server.py)
def register_iron_dome_routes(app):
    """Register Iron Dome sync routes with Flask app."""
    from flask import jsonify, request
    
    @app.route('/api/iron_dome/hash', methods=['GET'])
    def iron_dome_hash():
        return jsonify({"hash": get_patterns_hash(), "node": CURRENT_NODE})
    
    @app.route('/api/iron_dome/patterns', methods=['GET'])
    def iron_dome_patterns():
        return jsonify(export_patterns())
    
    @app.route('/api/iron_dome/notify', methods=['POST'])
    def iron_dome_notify():
        data = request.get_json()
        source = data.get('source', 'unknown')
        source_hash = data.get('hash', '')
        result = receive_update_notification(source, source_hash)
        return jsonify(result)
    
    @app.route('/api/iron_dome/status', methods=['GET'])
    def iron_dome_status():
        return jsonify(get_sync_status())
    
    @app.route('/api/iron_dome/broadcast', methods=['POST'])
    def iron_dome_broadcast():
        result = broadcast_update()
        return jsonify({"broadcast_results": result})
    
    logger.info("🛡️ Iron Dome Sync routes registered")


if __name__ == "__main__":
    # Test
    print("🛡️ Iron Dome Sync Test")
    print(f"Current Node: {CURRENT_NODE}")
    print(f"Local Hash: {get_patterns_hash()}")
    print(f"Status: {json.dumps(get_sync_status(), indent=2)}")
