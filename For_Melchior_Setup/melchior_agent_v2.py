import os
import sys
import time
import subprocess
import logging
import json
import hashlib
import hmac
import zipfile
import shutil
from flask import Flask, request, jsonify
import requests
import platform
from pathlib import Path

def _load_env_file(path: str) -> None:
    """
    Minimal .env loader (no external deps). Only sets keys not already in os.environ.
    """
    p = (path or "").strip()
    if not p or not os.path.exists(p):
        return
    try:
        for raw in open(p, "r", encoding="utf-8", errors="replace").read().splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            if not k:
                continue
            if k not in os.environ:
                os.environ[k] = v
    except Exception:
        return


# ---------------- CONFIGURATION ----------------
SCRIPT_DIR = str(Path(__file__).resolve().parent)
_load_env_file(os.path.join(SCRIPT_DIR, "melchior.env"))

PORT = int(os.environ.get("MELCHIOR_AGENT_PORT", "5002"))
OLLAMA_URL = os.environ.get("MELCHIOR_OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
DEFAULT_MODEL = os.environ.get("MELCHIOR_DEFAULT_MODEL", "qwen3:30b").strip() or "qwen3:30b"

# OpenAI-compatible /v1 server (llama.cpp llama-server) in distributed mode.
LLAMA_SERVER_BIN = os.environ.get("MELCHIOR_LLAMA_SERVER_BIN", r"C:\AI\llama.cpp\bin\llama-server.exe")
LLAMA_MODEL_PATH = os.environ.get("MELCHIOR_LLAMA_MODEL_PATH", r"C:\AI\models\qwen3-30b.Q4_K_M.gguf")
LLAMA_V1_PORT = int(os.environ.get("MELCHIOR_LLAMA_V1_PORT", "8080"))
RPC_PORT = str(os.environ.get("MELCHIOR_RPC_PORT", "50052"))
RPC_HOST = os.environ.get("MELCHIOR_RPC_HOST", "0.0.0.0")
LLAMA_CTX = int(os.environ.get("MELCHIOR_LLAMA_CTX", "8192"))
LLAMA_NGL = int(os.environ.get("MELCHIOR_LLAMA_NGL", "60"))  # tune for VRAM
LLAMA_THREADS = int(os.environ.get("MELCHIOR_LLAMA_THREADS", "0"))  # 0 means auto
LLAMA_BATCH = int(os.environ.get("MELCHIOR_LLAMA_BATCH", "0"))      # 0 means default

SKILLS_DIR = os.environ.get("MELCHIOR_SKILLS_DIR", r"C:\AI\MAGI\skills")
# ------------------------------------------------

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("MelchiorAgent")

# Global Process Holder
rpc_process = None
_CAP_CACHE = {"ts": 0.0, "data": {}}
CAP_CACHE_TTL_SEC = int(os.environ.get("MELCHIOR_CAP_CACHE_TTL_SEC", "10"))
STOP_OLLAMA_IN_DISTRIBUTED = os.environ.get("MELCHIOR_STOP_OLLAMA_IN_DISTRIBUTED", "1").strip().lower() in {"1", "true", "yes", "on"}
OPS_TOKEN = os.environ.get("MELCHIOR_OPS_TOKEN", "").strip()

# ---------------- Iron Dome (Distributed Safety Rules) ----------------
# Melchior 端最少要提供 /api/iron_dome/hash|patterns|notify|status，讓 Casper 能做同步狀態與規則散播。
# 這裡採「檔案快取」模式，避免依賴 Casper 端的 skills 套件結構。
IRON_DOME_DIR = os.path.join(SCRIPT_DIR, "static")
IRON_DOME_CACHE_FILE = os.environ.get(
    "MELCHIOR_IRON_DOME_CACHE_FILE",
    os.path.join(IRON_DOME_DIR, "iron_dome_patterns.json"),
)


def _iron_dome_hash() -> str:
    try:
        with open(IRON_DOME_CACHE_FILE, "rb") as f:
            return hashlib.md5(f.read()).hexdigest()
    except Exception:
        return ""


def _iron_dome_read() -> dict:
    try:
        if not os.path.exists(IRON_DOME_CACHE_FILE):
            return {}
        with open(IRON_DOME_CACHE_FILE, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def _iron_dome_write(data: dict) -> None:
    os.makedirs(IRON_DOME_DIR, exist_ok=True)
    tmp = IRON_DOME_CACHE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data or {}, f, ensure_ascii=False, indent=2)
    os.replace(tmp, IRON_DOME_CACHE_FILE)


def _pull_iron_dome_patterns(source_ip: str, source_port: int) -> dict:
    url = f"http://{source_ip}:{int(source_port)}/api/iron_dome/patterns"
    r = requests.get(url, timeout=20)
    if r.status_code != 200:
        raise RuntimeError(f"pull failed: HTTP {r.status_code}")
    data = r.json() or {}
    if not isinstance(data, dict):
        raise RuntimeError("pull failed: invalid json")
    return data

# ---------------------------------------------------------------------


def _is_ollama_running() -> bool:
    try:
        # Windows-only best effort
        out = subprocess.check_output(["tasklist"], text=True, errors="ignore")
        return ("ollama.exe" in out.lower()) or ("ollama_app.exe" in out.lower())
    except Exception:
        return False


def _stop_ollama() -> None:
    try:
        subprocess.run(["taskkill", "/F", "/IM", "ollama.exe"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["taskkill", "/F", "/IM", "ollama_app.exe"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def _start_ollama() -> None:
    try:
        if _is_ollama_running():
            return
        subprocess.Popen(["ollama", "serve"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def _safe_get(url: str, timeout: int = 3):
    try:
        r = requests.get(url, timeout=timeout)
        return r.status_code, r.text, (r.json() if "application/json" in (r.headers.get("content-type") or "") else None)
    except Exception as e:
        return 0, str(e), None


def _ollama_version() -> str:
    code, _text, data = _safe_get(f"{OLLAMA_URL}/api/version", timeout=2)
    if code == 200 and isinstance(data, dict):
        return str(data.get("version") or "")
    return ""


def _ollama_models() -> list:
    try:
        r = requests.get(f"{OLLAMA_URL}/api/tags", timeout=3)
        if r.status_code != 200:
            return []
        items = r.json().get("models", []) if isinstance(r.json(), dict) else []
        out = []
        for it in items:
            if isinstance(it, dict) and it.get("name"):
                out.append(str(it["name"]).strip())
        return sorted(set(out))
    except Exception:
        return []


def _openai_v1_models() -> list:
    # Only available when llama-server is running on port 8080 in distributed mode.
    try:
        r = requests.get("http://127.0.0.1:8080/v1/models", timeout=2)
        if r.status_code != 200:
            return []
        data = r.json() or {}
        items = data.get("data") or []
        out = []
        for it in items:
            if isinstance(it, dict) and it.get("id"):
                out.append(str(it["id"]).strip())
        return sorted(set(out))
    except Exception:
        return []


def _ops_auth_ok(req) -> bool:
    """
    Optional auth gate for remote repair operations.
    - If MELCHIOR_OPS_TOKEN is unset: allow (trusted tailnet deployment).
    - If set: require exact header match.
    """
    if not OPS_TOKEN:
        return True
    got = str(req.headers.get("X-MAGI-OPS-TOKEN") or "").strip()
    if not got:
        return False
    try:
        return bool(hmac.compare_digest(got, OPS_TOKEN))
    except Exception:
        return got == OPS_TOKEN


def _wait_v1_ready(wait_sec: int = 180) -> dict:
    """
    Wait until local llama-server /v1/models is ready.
    """
    start = time.time()
    deadline = start + max(15, int(wait_sec))
    checks = 0
    last = "unknown"
    while time.time() < deadline:
        checks += 1
        try:
            r = requests.get(f"http://127.0.0.1:{LLAMA_V1_PORT}/v1/models", timeout=4)
            if r.status_code == 200:
                return {
                    "ready": True,
                    "checks": checks,
                    "elapsed_ms": int((time.time() - start) * 1000),
                    "last_status": "200",
                }
            if r.status_code == 503:
                # Expected while loading model.
                try:
                    d = r.json() if "application/json" in (r.headers.get("content-type") or "") else {}
                    msg = str(((d or {}).get("error") or {}).get("message") or "")
                    last = f"503:{msg or 'loading'}"
                except Exception:
                    last = "503"
            else:
                last = f"HTTP {r.status_code}"
        except Exception as e:
            last = str(e)
        time.sleep(2.0)
    return {
        "ready": False,
        "checks": checks,
        "elapsed_ms": int((time.time() - start) * 1000),
        "last_status": last,
    }

def stop_rpc():
    """Stops the Distributed Llama Server."""
    global rpc_process
    if rpc_process:
        logger.info("🛑 Stopping Distributed RPC Server...")
        rpc_process.terminate()
        try:
            rpc_process.wait(timeout=5)
        except Exception:
            rpc_process.kill()
        rpc_process = None
        return True
    return False


def _check_local_v1() -> bool:
    try:
        r = requests.get(f"http://127.0.0.1:{LLAMA_V1_PORT}/v1/models", timeout=2)
        return r.status_code == 200
    except Exception:
        return False

def start_rpc():
    """Starts the Distributed Llama Server."""
    global rpc_process
    if rpc_process:
        return True # Already running
        
    if not os.path.exists(LLAMA_SERVER_BIN):
        logger.error(f"❌ Llama Server binary not found at: {LLAMA_SERVER_BIN}")
        return False

    if not os.path.exists(LLAMA_MODEL_PATH):
        logger.error(f"❌ Model file not found: {LLAMA_MODEL_PATH}")
        return False
        
    logger.info("🚀 Starting Distributed RPC Server...")
    try:
        # Start in background
        cmd = [
            LLAMA_SERVER_BIN,
            "-m",
            LLAMA_MODEL_PATH,
            "--host",
            "0.0.0.0",
            "--port",
            str(LLAMA_V1_PORT),
            "--rpc-server-host",
            RPC_HOST,
            "--rpc-server-port",
            RPC_PORT,
            "-c",
            str(LLAMA_CTX),
            "-ngl",
            str(LLAMA_NGL),
        ]
        if LLAMA_THREADS > 0:
            cmd += ["-t", str(LLAMA_THREADS)]
        if LLAMA_BATCH > 0:
            cmd += ["-b", str(LLAMA_BATCH)]

        logger.info(f"   cmd: {cmd}")
        rpc_process = subprocess.Popen(cmd)
        return True
    except Exception as e:
        logger.error(f"❌ Failed to start RPC: {e}")
        return False

@app.route('/health', methods=['GET'])
def health():
    return jsonify({
        "status": "online", 
        "mode": "distributed" if rpc_process else "engineer",
        "service": "melchior",
        "ollama_url": OLLAMA_URL,
        "v1_port": LLAMA_V1_PORT,
        "v1_online": _check_local_v1() if rpc_process else False,
    })

@app.route('/api/capabilities', methods=['GET'])
def capabilities():
    """
    Capability probe for CASPER dynamic routing.
    This endpoint is designed to be dependency-free and best-effort.
    """
    now = time.time()
    if _CAP_CACHE.get("data") and (now - float(_CAP_CACHE.get("ts") or 0.0)) < CAP_CACHE_TTL_SEC:
        return jsonify(_CAP_CACHE["data"]), 200

    mode = "distributed" if rpc_process else "engineer"
    models = _ollama_models()
    v1_models = _openai_v1_models() if mode == "distributed" else []
    payload = {
        "ok": True,
        "service": "melchior",
        "mode": mode,
        "agent": {"port": PORT},
        "ollama": {
            "reachable": bool(models),
            "url": OLLAMA_URL,
            "version": _ollama_version(),
            "models": models,
            "default_model": DEFAULT_MODEL,
        },
        "openai_v1": {
            "reachable": bool(v1_models),
            "base": "http://127.0.0.1:8080/v1",
            "models": v1_models,
        },
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "python": platform.python_version(),
        },
        "ts": int(now),
    }
    _CAP_CACHE["ts"] = now
    _CAP_CACHE["data"] = payload
    return jsonify(payload), 200

@app.route('/api/warmup', methods=['POST'])
def warmup():
    """
    Warm up Ollama model (keep_alive) to reduce first-request latency.
    Payload: {"model": "...", "timeout": 60}
    """
    data = request.json or {}
    model = str(data.get("model") or DEFAULT_MODEL).strip()
    timeout = int(data.get("timeout") or 60)
    t0 = time.time()
    try:
        # If we're in distributed mode and /v1 is available, warm up llama-server first.
        if rpc_process:
            try:
                payload_v1 = {
                    "model": model,
                    "messages": [{"role": "user", "content": "Reply with exactly: warm"}],
                    "temperature": 0.0,
                    "max_tokens": 8,
                    "stream": False,
                }
                r0 = requests.post(f"http://127.0.0.1:{LLAMA_V1_PORT}/v1/chat/completions", json=payload_v1, timeout=max(5, timeout))
                if r0.status_code == 200:
                    return jsonify({"success": True, "backend": "llama_server_v1", "model": model, "ms": int((time.time() - t0) * 1000)}), 200
            except Exception:
                pass

        payload = {
            "model": model,
            "prompt": "Reply with exactly: warm",
            "stream": False,
            "keep_alive": str(data.get("keep_alive") or "10m"),
            "options": data.get("options") or {},
        }
        r = requests.post(f"{OLLAMA_URL}/api/generate", json=payload, timeout=max(5, timeout))
        ok = (r.status_code == 200)
        text = ""
        if ok:
            try:
                text = (r.json() or {}).get("response", "") or ""
            except Exception:
                text = r.text[:200]
        return jsonify({"success": ok, "model": model, "ms": int((time.time() - t0) * 1000), "response": text[:200]}), (200 if ok else 500)
    except requests.exceptions.Timeout:
        return jsonify({"success": False, "model": model, "ms": int((time.time() - t0) * 1000), "error": f"timeout({timeout}s)"}), 504
    except Exception as e:
        return jsonify({"success": False, "model": model, "ms": int((time.time() - t0) * 1000), "error": str(e)}), 500

@app.route('/api/brain/switch', methods=['POST'])
def switch_brain():
    """
    Handles Casper's Mode Switch Request.
    Payload: {"mode": "distributed"} or {"mode": "local"/"engineer"}
    """
    data = request.json
    mode = data.get("mode", "engineer")
    logger.info(f"📡 Received Mode Switch: {mode}")
    
    if mode == "distributed":
        # 1. Stop Ollama? (Optional, usually fine to keep running if VRAM allows)
        # 2. Start RPC Server
        if STOP_OLLAMA_IN_DISTRIBUTED:
            _stop_ollama()
        if start_rpc():
            return jsonify({"status": "Switched to Big Brain Mode", "success": True, "config": "Anti-Freeze Enabled (Background)", "pid": rpc_process.pid})
        else:
            return jsonify({"status": "failed", "error": "binary_missing"}), 500
            
    elif mode in ["local", "engineer"]:
        # 1. Stop RPC Server
        stop_rpc()
        # 2. Ensure Ollama is reachable
        _start_ollama()
        try:
            requests.get(f"{OLLAMA_URL}/api/tags", timeout=2)
            ollama_status = "ready"
        except Exception:
            ollama_status = "ollama_down"
            
        return jsonify({"status": "Switched to Engineer Mode", "ollama": ollama_status, "success": True})
        
    return jsonify({"error": "invalid_mode"}), 400


@app.route('/api/brain/recover', methods=['POST'])
def recover_brain():
    """
    Remote self-heal entry point for CASPER.
    Payload:
      {
        "target": "distributed",
        "model": "qwen3:30b",
        "wait_sec": 240,
        "force_cycle": true
      }
    """
    if not _ops_auth_ok(request):
        return jsonify({"success": False, "error": "unauthorized"}), 403

    data = request.get_json(silent=True) or {}
    target = str(data.get("target") or "distributed").strip().lower()
    model = str(data.get("model") or DEFAULT_MODEL).strip() or DEFAULT_MODEL
    wait_sec = max(30, min(int(data.get("wait_sec") or 240), 900))
    force_cycle = bool(data.get("force_cycle", True))

    if target not in {"distributed", "big_brain", "big-brain", "big"}:
        return jsonify({"success": False, "error": f"unsupported_target:{target}"}), 400

    out = {
        "success": False,
        "target": "distributed",
        "model": model,
        "wait_sec": wait_sec,
        "steps": {},
    }

    # 1) Optional hard cycle through engineer for clean process state.
    if force_cycle:
        try:
            stop_rpc()
            _start_ollama()
            out["steps"]["to_engineer"] = {"ok": True}
        except Exception as e:
            out["steps"]["to_engineer"] = {"ok": False, "error": str(e)}

    # 2) Start distributed mode.
    try:
        if STOP_OLLAMA_IN_DISTRIBUTED:
            _stop_ollama()
        started = bool(start_rpc())
        out["steps"]["start_distributed"] = {"ok": started, "pid": (rpc_process.pid if rpc_process else None)}
        if not started:
            out["error"] = "start_rpc_failed"
            return jsonify(out), 500
    except Exception as e:
        out["steps"]["start_distributed"] = {"ok": False, "error": str(e)}
        out["error"] = "start_distributed_exception"
        return jsonify(out), 500

    # 3) Wait for /v1 to become ready.
    ready_info = _wait_v1_ready(wait_sec=wait_sec)
    out["steps"]["wait_v1_ready"] = ready_info
    if not bool(ready_info.get("ready")):
        out["error"] = f"v1_not_ready:{ready_info.get('last_status')}"
        return jsonify(out), 200

    # 4) Warmup main model on /v1.
    t0 = time.time()
    try:
        payload_v1 = {
            "model": model,
            "messages": [{"role": "user", "content": "Reply with exactly: OK"}],
            "temperature": 0.0,
            "max_tokens": 32,
            "stream": False,
        }
        r = requests.post(f"http://127.0.0.1:{LLAMA_V1_PORT}/v1/chat/completions", json=payload_v1, timeout=max(20, min(wait_sec, 120)))
        ok = (r.status_code == 200)
        out["steps"]["warmup_v1"] = {
            "ok": ok,
            "status_code": r.status_code,
            "elapsed_ms": int((time.time() - t0) * 1000),
        }
        if not ok:
            out["error"] = f"warmup_v1_http_{r.status_code}"
            return jsonify(out), 200
    except Exception as e:
        out["steps"]["warmup_v1"] = {"ok": False, "error": str(e), "elapsed_ms": int((time.time() - t0) * 1000)}
        out["error"] = "warmup_v1_exception"
        return jsonify(out), 200

    out["success"] = True
    out["status"] = "distributed_ready"
    return jsonify(out), 200

@app.route('/api/chat', methods=['POST'])
def chat():
    """
    Proxy chat requests to Ollama (For Night Talk Engineer Mode).
    Accepts dynamic timeout from Casper.
    """
    data = request.json
    prompt = data.get("prompt", "")
    model = data.get("model", DEFAULT_MODEL)
    options = data.get("options", {})
    timeout = int(data.get("timeout", 600)) # Default 600s, but can be overridden
    keep_alive = str(data.get("keep_alive") or "10m")
    
    # Forward to Ollama
    try:
        payload = {
            "model": model,
            "prompt": prompt,
            "stream": False,
            "keep_alive": keep_alive,
            "options": options,
        }
        # Use the dynamic timeout for the request to Ollama
        resp = requests.post(f"{OLLAMA_URL}/api/generate", json=payload, timeout=timeout)
        if resp.status_code == 200:
            return jsonify({"response": resp.json().get("response", "")})
        else:
            return jsonify({"response": f"Error: Ollama {resp.status_code}"}), 500
    except requests.exceptions.Timeout:
        return jsonify({"response": f"Error: Ollama Request Timed Out (Limit {timeout}s)"}), 504
    except Exception as e:
        return jsonify({"response": f"Error: {str(e)}"}), 500

@app.route('/api/update_agent', methods=['POST'])
def update_agent():
    """
    Self-Update Endpoint: HITL Enforced.
    Prevents automated script pulling/overwriting. Requires manual administrator review.
    """
    return jsonify({
        "success": False, 
        "error": "IRON DOME HITL APPROVAL REQUIRED: Automated fundamental system updates are restricted. Please review the new script manually and deploy via SSH/RDP."
    }), 403


# ---------------- Iron Dome Routes ----------------
@app.route('/api/iron_dome/hash', methods=['GET'])
def iron_dome_hash():
    return jsonify({"hash": _iron_dome_hash(), "service": "melchior"}), 200


@app.route('/api/iron_dome/patterns', methods=['GET'])
def iron_dome_patterns():
    data = _iron_dome_read()
    if not data:
        return jsonify({"hash": _iron_dome_hash(), "patterns": {}, "service": "melchior"}), 200
    # Ensure hash is present for peers.
    data.setdefault("hash", _iron_dome_hash())
    data.setdefault("service", "melchior")
    return jsonify(data), 200


@app.route('/api/iron_dome/notify', methods=['POST'])
def iron_dome_notify():
    """
    Payload example:
      {"source":"casper","source_ip":"100.x.y.z","source_port":5002,"hash":"...","timestamp":"..."}
    """
    data = request.get_json(silent=True) or {}
    source_ip = str(data.get("source_ip") or "").strip()
    source_port = int(data.get("source_port") or 5002)
    remote_hash = str(data.get("hash") or "").strip()
    local_hash = _iron_dome_hash()

    if remote_hash and local_hash and (remote_hash == local_hash):
        return jsonify({"status": "SYNCED", "action": "none", "hash": local_hash}), 200

    if not source_ip:
        # Without routable source, we can only acknowledge the notification.
        return jsonify({"status": "NEED_SOURCE", "action": "none", "hash": local_hash}), 200

    try:
        patterns = _pull_iron_dome_patterns(source_ip, source_port)
        _iron_dome_write(patterns)
        return jsonify({"status": "UPDATED", "action": "pulled", "hash": _iron_dome_hash()}), 200
    except Exception as e:
        return jsonify({"status": "FAILED", "action": "pulled", "error": str(e), "hash": local_hash}), 200


@app.route('/api/iron_dome/status', methods=['GET'])
def iron_dome_status():
    return jsonify({"ok": True, "service": "melchior", "hash": _iron_dome_hash()}), 200
# --------------------------------------------------

@app.route('/api/skills/sync', methods=['POST'])
def sync_skills():
    """
    Receives a ZIP file of skills from Casper and extracts them.
    """
    if 'file' not in request.files:
        return jsonify({"success": False, "error": "No file part"}), 400
        
    file = request.files['file']
    if file.filename == '':
        return jsonify({"success": False, "error": "No selected file"}), 400
        
    if file:
        try:
            # 1. Save ZIP
            zip_path = os.path.join(os.getcwd(), "skills_update.zip")
            file.save(zip_path)
            logger.info(f"📦 Received Skills Update: {zip_path}")
            
            # 2. Extract
            if not os.path.exists(SKILLS_DIR):
                os.makedirs(SKILLS_DIR)
                
            with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                zip_ref.extractall(SKILLS_DIR)
                
            logger.info(f"✅ Skills Extracted to {SKILLS_DIR}")
            return jsonify({"success": True, "message": "Skills Synced"})
            
        except Exception as e:
            logger.error(f"❌ Skill Sync Failed: {e}")
            return jsonify({"success": False, "error": str(e)}), 500

if __name__ == '__main__':
    logger.info(f"🤖 Melchior Agent v2 Listening on Port {PORT}")
    app.run(host='0.0.0.0', port=PORT)
