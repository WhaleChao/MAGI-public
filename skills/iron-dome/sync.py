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
from typing import List, Dict

try:
    from skills.iron_dome.core import STATIC_RULE_SETS, get_all_patterns
except ImportError:
    # Fallback during migration
    STATIC_RULE_SETS = {}
    def get_all_patterns(): return []

logger = logging.getLogger("IronDomeSync")

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
    """Resolve local Tailscale IPv4."""
    try:
        r = subprocess.run(["tailscale", "ip", "-4"], capture_output=True, text=True, timeout=1.5)
        ips = [ln.strip() for ln in (r.stdout or "").splitlines() if ln.strip()]
        return ips[0] if ips else ""
    except Exception:
        return ""


def _advertise_ip() -> str:
    ip = _env_str("MAGI_ADVERTISE_IP", "")
    if ip: return ip
    ip = _tailscale_ip()
    if ip: return ip
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

CURRENT_NODE = os.environ.get("MAGI_NODE", "casper")
PATTERNS_CACHE_FILE = f"{_MAGI_ROOT}/static/iron_dome_patterns.json"


def get_patterns_hash() -> str:
    """Generate hash of current active patterns."""
    try:
        patterns = get_all_patterns()
        dump = json.dumps(patterns, sort_keys=True)
        return hashlib.md5(dump.encode('utf-8')).hexdigest()
    except Exception:
        return ""


def export_patterns() -> Dict:
    """Export current patterns for sync."""
    try:
        patterns = {
            "version": datetime.now().isoformat(),
            "source_node": CURRENT_NODE,
            "hash": get_patterns_hash(),
            "patterns": {
                "prompt_injection": STATIC_RULE_SETS.get("PROMPT_INJECTION", []),
                "dangerous_commands": STATIC_RULE_SETS.get("DESTRUCTIVE_COMMAND", []) + STATIC_RULE_SETS.get("SENSITIVE_DATA", []),
                # Include dynamic ones if needed, but usually they are separate
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
    """Broadcast Iron Dome update to all other MAGI nodes."""
    logger.info(f"📡 Broadcasting Iron Dome update from {CURRENT_NODE}...")
    
    current_hash = get_patterns_hash()
    results = {}
    
    for node_name, node_info in MAGI_NODES.items():
        if node_name == CURRENT_NODE: continue
        
        try:
            url = f"http://{node_info['ip']}:{node_info['port']}/api/iron_dome/notify"
            payload = {
                "source": CURRENT_NODE,
                "source_ip": _advertise_ip(),
                "source_port": int(MAGI_NODES.get(CURRENT_NODE, {}).get("port") or 5002),
                "hash": current_hash,
                "timestamp": datetime.now().isoformat()
            }
            
            response = requests.post(url, json=payload, timeout=5)
            if response.status_code == 200:
                results[node_name] = "OK"
            else:
                results[node_name] = f"HTTP {response.status_code}"
                
        except Exception as e:
            results[node_name] = str(e)
    
    return results


def receive_update_notification(source_node: str, source_hash: str) -> Dict:
    logger.info(f"📥 Received Iron Dome update notification from {source_node}")
    local_hash = get_patterns_hash()
    
    if local_hash == source_hash:
        return {"status": "SYNCED", "action": "none"}
    
    # Enable pulling logic if needed (requires implementation in core to accept external updates)
    # properly. For now, we acknowledge.
    return {"status": "ACK", "action": "none", "note": "Sync logic pending migration"}


def get_sync_status() -> Dict:
    local_hash = get_patterns_hash()
    return {"node": CURRENT_NODE, "hash": local_hash, "nodes": MAGI_NODES}


# ---------------------------------------------------------------------------
# GAP-6: External upstream rule update
# ---------------------------------------------------------------------------
# Set IRON_DOME_UPSTREAM_URL in .env to a trusted HTTPS URL that serves a
# JSON object:  {"version": "YYYY-MM-DD", "rules": [{"pattern":"...","reason":"..."},...]}
#
# Self-scan safety:  each fetched pattern is tested against Iron Dome's own
# static rules before being stored.  Any pattern that itself triggers a
# violation (e.g. contains `rm -rf`) is rejected with reason logged.
# ---------------------------------------------------------------------------

_UPSTREAM_URL_ENV = "IRON_DOME_UPSTREAM_URL"
_UPSTREAM_TIMEOUT  = 20   # seconds
_MAX_UPSTREAM_RULES = 200  # hard cap – refuse oversized payloads
_UPSTREAM_LAST_FETCH_FILE = f"{_MAGI_ROOT}/static/iron_dome_upstream_last.json"


def fetch_upstream_rules(broadcast: bool = True, dry_run: bool = False) -> Dict:
    """
    Pull security rules from a trusted upstream URL, self-scan each pattern,
    merge new ones into local dynamic rules, then broadcast to other nodes.

    Returns a result dict with keys:
        ok, fetched, added, skipped, rejected, errors
    """
    upstream_url = os.environ.get(_UPSTREAM_URL_ENV, "").strip()
    if not upstream_url:
        return {
            "ok": False,
            "error": f"{_UPSTREAM_URL_ENV} not set — add it to MAGI/.env to enable upstream rule updates",
            "added": 0,
        }

    logger.info(f"🛡️ Fetching Iron Dome upstream rules from {upstream_url} ...")

    # --- Fetch ---
    try:
        resp = requests.get(upstream_url, timeout=_UPSTREAM_TIMEOUT,
                            headers={"User-Agent": "MAGI-IronDome/1.0"})
        resp.raise_for_status()
        payload = resp.json()
    except Exception as e:
        logger.error(f"❌ Upstream fetch failed: {e}")
        return {"ok": False, "error": f"fetch_failed: {e}", "added": 0}

    if not isinstance(payload, dict) or not isinstance(payload.get("rules"), list):
        return {"ok": False, "error": "upstream_payload_invalid: expected {rules:[...]}", "added": 0}

    rules = payload["rules"]
    if len(rules) > _MAX_UPSTREAM_RULES:
        return {"ok": False, "error": f"upstream_payload_too_large: {len(rules)} rules (max {_MAX_UPSTREAM_RULES})", "added": 0}

    upstream_version = str(payload.get("version", "unknown"))
    logger.info(f"📦 Upstream version: {upstream_version}, rules: {len(rules)}")

    # --- Self-scan & Merge ---
    added = 0
    skipped = 0
    rejected = 0
    errors = []

    for rule in rules:
        pattern = str(rule.get("pattern") or "").strip()
        reason  = str(rule.get("reason")  or "upstream").strip()[:200]
        if not pattern:
            skipped += 1
            continue

        # Self-scan: make sure the pattern string itself does not trigger
        # our own destructive-command / injection rules.
        try:
            from skills.iron_dome.core import is_safe as _is_safe
            safe, why = _is_safe(pattern)
        except Exception:
            safe, why = True, "core_unavailable"

        if not safe:
            rejected += 1
            reason_msg = f"rejected_self_scan({why}): {pattern[:80]}"
            logger.warning(f"⚠️ {reason_msg}")
            errors.append(reason_msg)
            continue

        if dry_run:
            added += 1
            continue

        try:
            from skills.iron_dome.core import add_pattern as _add_pattern
            res = _add_pattern(pattern, reason=reason, source=f"upstream:{upstream_version}")
            if res.get("success"):
                added += 1
            else:
                skipped += 1  # already exists
        except Exception as e:
            errors.append(f"add_failed({pattern[:60]}): {e}")
            skipped += 1

    logger.info(f"✅ Upstream sync: added={added} skipped={skipped} rejected={rejected}")

    # --- Record last fetch ---
    try:
        os.makedirs(os.path.dirname(_UPSTREAM_LAST_FETCH_FILE), exist_ok=True)
        with open(_UPSTREAM_LAST_FETCH_FILE, "w", encoding="utf-8") as f:
            json.dump({
                "url": upstream_url,
                "version": upstream_version,
                "fetched_at": datetime.now().isoformat(),
                "added": added,
                "skipped": skipped,
                "rejected": rejected,
            }, f, ensure_ascii=False, indent=2)
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 271, exc_info=True)

    # --- Broadcast to cluster ---
    broadcast_result = {}
    if broadcast and added > 0 and not dry_run:
        try:
            broadcast_result = broadcast_update()
        except Exception as e:
            broadcast_result = {"error": str(e)}

    return {
        "ok": True,
        "upstream_version": upstream_version,
        "fetched": len(rules),
        "added": added,
        "skipped": skipped,
        "rejected": rejected,
        "errors": errors[:10],
        "broadcast": broadcast_result,
    }
