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
_MAGI_ROOT = os.path.abspath(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))))
import json
import hashlib
import logging
import requests
from datetime import datetime
from typing import List, Dict

logger = logging.getLogger("IronDomeSync")

# Node Registry (Tailscale IPs)
MAGI_NODES = {
    "casper": {"ip": "127.0.0.1", "port": 5002, "role": "Governor"},
    "melchior": {"ip": "100.116.54.16", "port": 5002, "role": "Scientist"},
    "balthasar": {"ip": "100.118.235.126", "port": 5002, "role": "Coordinator"},
}

# Current node identity
CURRENT_NODE = os.environ.get("MAGI_NODE", "casper")

# Iron Dome patterns file
IRON_DOME_FILE = f"{_MAGI_ROOT}/skills/bridge/iron_dome.py"
PATTERNS_CACHE_FILE = f"{_MAGI_ROOT}/static/iron_dome_patterns.json"


def get_patterns_hash() -> str:
    """Generate hash of current Iron Dome patterns for change detection."""
    try:
        with open(IRON_DOME_FILE, 'rb') as f:
            return hashlib.md5(f.read()).hexdigest()
    except:
        return ""


def export_patterns() -> Dict:
    """Export current patterns to shareable format."""
    try:
        from skills.bridge.iron_dome import PROMPT_INJECTION_PATTERNS, DANGEROUS_COMMAND_PATTERNS
        
        patterns = {
            "version": datetime.now().isoformat(),
            "source_node": CURRENT_NODE,
            "hash": get_patterns_hash(),
            "patterns": {
                "prompt_injection": PROMPT_INJECTION_PATTERNS,
                "dangerous_commands": DANGEROUS_COMMAND_PATTERNS
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
                "hash": current_hash,
                "timestamp": datetime.now().isoformat()
            }
            
            response = requests.post(url, json=payload, timeout=10)
            
            if response.status_code == 200:
                logger.info(f"✅ Notified {node_name}")
                results[node_name] = "OK"
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
            
            # Save to cache (for now, manual deployment required for actual .py update)
            with open(PATTERNS_CACHE_FILE, 'w') as f:
                json.dump(patterns, f, indent=2, ensure_ascii=False)
            
            logger.info(f"✅ Pulled patterns from {node_name}")
            
            # TODO: Hot-reload iron_dome.py (requires careful implementation)
            # For safety, just cache and log for now
            logger.warning("⚠️ Cached new patterns. Manual review recommended before hot-reload.")
            
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
            else:
                status["nodes"][node_name] = {"status": "ERROR"}
                
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
