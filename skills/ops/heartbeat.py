import logging
import json
import subprocess
import time
import os
import requests
from datetime import datetime
_MAGI_ROOT = os.path.abspath(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))


def _node_ip_or(name: str, fallback: str) -> str:
    try:
        from api.routing.node_registry import get_node_ip
        return get_node_ip(name) or fallback
    except Exception:
        return fallback


# Node Configuration
NODES = {
    "casper": {
        "ip": "127.0.0.1",
        "name": "Casper",
        "role": "Decision & Governor",
        "type": "omlx",
        "port": 8080,
        "openclaw_config": "/Users/ai/.openclaw/openclaw.json"
    },
    "balthasar": {
        "ip": _node_ip_or("balthasar", "100.118.235.126"),
        "name": "Balthasar",
        "role": "Coordinator (Mobile)",
        "type": "flask",
        "port": 5002
    },
    "keeper": {
        "ip": _node_ip_or("nas", "100.121.61.74"),
        "name": "Keeper",
        "role": "Database (Iron Dome)",
        "type": "db",
        "port": 3306,
        "model": "MariaDB 10.11"
    },
    "melchior": {
        "ip": _node_ip_or("melchior", "100.116.54.16"),
        "name": "Melchior",
        "role": "Scientist (Code)",
        "type": "api",
        "port": 8080,
        "gpu": "RTX 3060"
    },
}

STATUS_FILE = f"{_MAGI_ROOT}/static/magi_status.json"

# ── Tailscale Serve Guard ──
# Ensure external traffic goes through Caddy (18790), never directly to OpenClaw (18789).
TAILSCALE_EXPECTED_PORT = 18790
TAILSCALE_FORBIDDEN_PORT = 18789

def guard_tailscale_serve():
    """Disabled — nodes are on LAN (192.168.1.x), no longer need Tailscale serve."""
    pass

# ── TAIDE Model Resident Guard ──
# Ollama retired — TAIDE now runs on oMLX as TAIDE-12b-Chat-mlx-4bit.
TAIDE_MODEL = os.environ.get("MAGI_TEXT_PRIMARY_MODEL", "")

def guard_taide_resident():
    """No-op: Ollama retired. TAIDE runs on oMLX (managed by launchd)."""
    pass

def check_omlx_health():
    """Check if oMLX server is running and responsive."""
    omlx_port = int(os.environ.get("MAGI_OMLX_PORT", "8080"))
    try:
        r = requests.get(f"http://127.0.0.1:{omlx_port}/v1/models", timeout=3)
        if r.status_code == 200:
            models = [m.get("id", "") for m in r.json().get("data", [])]
            print(f"  ✅ oMLX OK — {len(models)} models: {', '.join(models[:5])}")
            return True
        print(f"  ⚠️ oMLX HTTP {r.status_code}")
        return False
    except Exception as e:
        print(f"  ❌ oMLX unreachable: {e}")
        return False

def check_ping(ip):
    try:
        # Increased to 3000ms (3s) to allow for busy nodes
        subprocess.check_output(["/sbin/ping", "-c", "1", "-W", "3000", ip], stderr=subprocess.STDOUT)
        return True
    except subprocess.CalledProcessError:
        return False

def get_node_model(ip, port=8080):
    """Query API for currently loaded model (oMLX /v1/models or Ollama /api/ps)."""
    try:
        # Try OpenAI-compatible /v1/models first
        response = requests.get(f"http://{ip}:{port}/v1/models", timeout=5)
        if response.status_code == 200:
            data = response.json()
            models = data.get("data") or []
            if models:
                # 優先回傳主對話模型（TAIDE），Qwen 只負責 code
                main_model = os.environ.get("MAGI_MAIN_MODEL", "TAIDE")
                for m in models:
                    mid = m.get("id", "")
                    if main_model.lower().split("-")[0] in mid.lower():
                        return mid
                return models[0].get("id", "Unknown")
            return "Idle"
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 97, exc_info=True)
    try:
        # Fallback: Ollama /api/ps (for remote nodes that still run Ollama)
        response = requests.get(f"http://{ip}:{port}/api/ps", timeout=5)
        if response.status_code == 200:
            data = response.json()
            if data.get("models") and len(data["models"]) > 0:
                return data["models"][0].get("name", "Unknown")
        return "Idle"
    except requests.exceptions.Timeout:
        return "Busy (Timeout)"
    except Exception:
        return "Unreachable"

def get_openclaw_default_model(config_path):
    """Read OpenClaw config for default model"""
    try:
        with open(config_path, 'r') as f:
            config = json.load(f)
            return config.get("agents", {}).get("defaults", {}).get("model", {}).get("primary", "Unknown")
    except Exception:
        return "Unknown"

def update_status():
    status_data = {
        "timestamp": datetime.now().isoformat(),
        "nodes": {}
    }
    
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Checking nodes...")
    
    for key, node in NODES.items():
        # 1. Base Ping Check
        is_online = check_ping(node.get("ip"))
        
        # 2. Service Check (if online)
        model = node.get("model", "N/A")
        
        if is_online:
            node_type = node.get("type", "ping")
            port = node.get("port")
            
            if node_type == "omlx" and port:
                # Casper now runs on oMLX
                try:
                    r = requests.get(f"http://{node['ip']}:{port}/v1/models", timeout=3)
                    if r.status_code == 200:
                        omlx_models = [m.get("id", "") for m in r.json().get("data", [])]
                        if omlx_models:
                            # 顯示主對話模型（TAIDE），Qwen 只負責 code
                            main_kw = os.environ.get("MAGI_MAIN_MODEL", "TAIDE").lower().split("-")[0]
                            primary = next((m for m in omlx_models if main_kw in m.lower()), omlx_models[0])
                            model = f"oMLX: {primary}"
                        else:
                            model = "oMLX (no models)"
                    else:
                        model = "oMLX (error)"
                except Exception:
                    model = "oMLX (unreachable)"

            elif node_type == "api" and port:
                model = get_node_model(node["ip"], port)
                    
            elif node_type == "flask" and port:
                # Check Flask Health Endpoint
                try:
                    r = requests.get(f"http://{node['ip']}:{port}/health", timeout=2)
                    if r.status_code == 200:
                        model = "Active"
                    else:
                        model = f"HTTP {r.status_code}"
                except Exception:
                    model = "Service Down"
                    
            elif node_type == "db":
                # Checking port open (using nc logic or just assume online if ping works for now)
                # For heartbeat simplicity we assume Ping + Static Model is enough for Keeper
                pass

        status_data["nodes"][key] = {
            "online": is_online,
            "ip": node.get("ip"),
            "name": node.get("name"),
            "role": node.get("role"),
            "model": model,
            "gpu": node.get("gpu", None),
            "last_check": time.time()
        }
        print(f"  - {node['name']} ({node['ip']}): {'ONLINE' if is_online else 'OFFLINE'} | Model: {model}")

    # 3. Skill Checks
    skills = {
        "memory": {"status": "OFFLINE", "details": "DB Unreachable"},
        "research": {"status": "OFFLINE", "details": "No Internet"},
        "genesis": {"status": "OFFLINE", "details": "Engine Down"}
    }

    # Memory Check (Keeper + Fulltext)
    if status_data["nodes"]["keeper"]["online"]:
        try:
            # We can't easily import mysql here due to env, but we can assume if Keeper is online, Memory is ACTIVE
            # Ideally we'd run a quick query. For now, link to Keeper status.
            skills["memory"] = {"status": "ACTIVE", "details": "Ready"}
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 195, exc_info=True)
            
    # Research Check (Ping 8.8.8.8)
    if check_ping("8.8.8.8"):
        skills["research"] = {"status": "ACTIVE", "details": "Internet OK"}
        
    # Genesis Check (Decoupled from Distributed Melchior)
    if status_data["nodes"]["casper"]["online"]:
        skills["genesis"] = {"status": "ACTIVE", "details": "Local Engine Active"}
        
    status_data["skills"] = skills

    # 4. Task Check (Read from TaskTracker)
    try:
        task_file = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "active_tasks.json")
        if os.path.exists(task_file):
            with open(task_file, "r") as f:
                status_data["tasks"] = json.load(f)
        else:
            status_data["tasks"] = {}
    except Exception as e:
        status_data["tasks"] = {}
        print(f"Task Read Error: {e}")

    # 5. Obsidian Vault Status
    try:
        _agent_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), ".agent")
        _obs_cfg_path = os.path.join(_agent_dir, "obsidian_vault_config.json")
        _obs_idx_path = os.path.join(_agent_dir, "obsidian_index.json")
        obs_info = {"vault_configured": False}
        if os.path.exists(_obs_cfg_path):
            with open(_obs_cfg_path, "r") as f:
                _obs_cfg = json.load(f)
            vp = _obs_cfg.get("vault_path", "")
            obs_info["vault_configured"] = bool(vp and os.path.isdir(vp))
            obs_info["vault_name"] = _obs_cfg.get("vault_name", "")
        if os.path.exists(_obs_idx_path):
            with open(_obs_idx_path, "r") as f:
                _obs_idx = json.load(f)
            obs_info["notes_indexed"] = len(_obs_idx.get("notes", {}))
            obs_info["last_update"] = _obs_idx.get("updated_at", "")
        status_data["obsidian"] = obs_info
    except Exception as e:
        status_data["obsidian"] = {"vault_configured": False}
        print(f"Obsidian Status Error: {e}")

    with open(STATUS_FILE, "w") as f:
        json.dump(status_data, f, indent=2)
    
    print(f"Updated {STATUS_FILE} with Skills")

_ts_guard_counter = 0
_TS_GUARD_EVERY_N = 6    # Check Tailscale every ~60s (6 × 10s)
_TAIDE_GUARD_EVERY_N = 30  # Check TAIDE every ~5min (30 × 10s)

if __name__ == "__main__":
    print("💗 MAGI Heartbeat Monitor Started (v5 - Dual Engine: Ollama + oMLX)")
    # Initial guards on startup
    guard_tailscale_serve()
    guard_taide_resident()
    check_omlx_health()
    _taide_guard_counter = 0
    while True:
        update_status()
        _ts_guard_counter += 1
        _taide_guard_counter += 1
        if _ts_guard_counter >= _TS_GUARD_EVERY_N:
            _ts_guard_counter = 0
            guard_tailscale_serve()
        if _taide_guard_counter >= _TAIDE_GUARD_EVERY_N:
            _taide_guard_counter = 0
            guard_taide_resident()
            check_omlx_health()
        time.sleep(10)  # Update every 10 seconds
