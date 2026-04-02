
import os
import sys
import time
import subprocess
import socket
import logging
import threading
from flask import Flask, request, jsonify

# ==============================================================================
# MELCHIOR AGENT (The Cerebellum)
# ==============================================================================
# This script runs on the Windows PC (Melchior).
# It listens for commands from Casper to switch "Brain Modes".
#
# Roles:
# 1. Distributed Worker (RPC Mode): Contributes GPU to Casper's 70B Model.
# 2. Engineer (Local Mode): Runs local Ollama for Image Generation / Heavy Tasks.
# ==============================================================================

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')
logger = logging.getLogger("MelchiorAgent")

# CONFIGURATION (Adjust for Windows Environment)
RPC_BINARY_PATH = r"C:\AI\llama.cpp\bin\rpc-server.exe"  # Adjust path!
RPC_PORT = "50052"
RPC_HOST = "0.0.0.0"

# GLOBAL STATE
rpc_process = None
state_lock = threading.Lock()


def is_ollama_running():
    try:
        return "ollama.exe" in subprocess.check_output(["tasklist"]).decode(errors="ignore")
    except Exception:
        return False

def stop_rpc():
    """Stops the RPC Server."""
    global rpc_process
    with state_lock:
        if rpc_process:
            logger.info("🛑 Stopping RPC Server...")
            rpc_process.terminate()
            rpc_process = None
    
    # Force kill just in case (Windows)
    subprocess.run(["taskkill", "/F", "/IM", "rpc-server.exe"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def start_rpc():
    """Starts the RPC Server."""
    global rpc_process
    stop_rpc() # Ensure clean slate
    
    logger.info("🚀 Starting RPC Server (Contributing GPU)...")
    try:
        # Start rpc-server.exe in background
        # -H 0.0.0.0 -p 50052
        cmd = [RPC_BINARY_PATH, "-H", RPC_HOST, "-p", RPC_PORT]
        with state_lock:
            rpc_process = subprocess.Popen(cmd)
        logger.info(f"✅ RPC Server Running on {RPC_PORT}")
        return True
    except Exception as e:
        logger.error(f"❌ Failed to start RPC: {e}")
        return False

def stop_ollama():
    """Stops Ollama (to free VRAM for RPC)."""
    logger.info("🛑 Stopping Ollama...")
    # Windows: taskkill
    subprocess.run(["taskkill", "/F", "/IM", "ollama.exe"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["taskkill", "/F", "/IM", "ollama_app.exe"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def start_ollama():
    """Ensures Ollama is running."""
    logger.info("🟢 Ensuring Ollama is running...")
    if not is_ollama_running():
        subprocess.Popen(["ollama", "serve"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

@app.route('/api/brain/switch', methods=['POST'])
def switch_mode():
    data = request.json or {}
    mode = str(data.get("mode", "engineer")).strip().lower()
    if mode in ["local", "independent", "fallback"]:
        mode = "engineer"
    
    logger.info(f"🔄 CONFIGURING MODE: {mode.upper()}")
    
    if mode == "distributed":
        # 1. Stop Ollama (Free VRAM)
        # stop_ollama() # Optional: if VRAM is tight (12GB is tight for 70B+Img)
        # Actually for 70B we definitely need VRAM.
        # But we might keep it running if users use it for small models?
        # DECISION: Stop strict VRAM conflict sources.
        
        # 2. Start RPC
        start_rpc()
        status = "Distributed Mode Active (RPC Running)"
        
    elif mode == "engineer":
        # 1. Stop RPC (Release GPU)
        stop_rpc()
        
        # 2. Start Ollama (if not running)
        start_ollama()
        
        status = "Engineer Mode Active (Ollama Ready)"
        
    else:
        return jsonify({"error": "Unknown mode"}), 400
        
    return jsonify({"status": status, "mode": mode, "rpc_active": rpc_process is not None, "ollama_active": is_ollama_running()})

@app.route('/status', methods=['GET'])
def status():
    global rpc_process
    return jsonify({
        "rpc_active": rpc_process is not None,
        "rpc_pid": rpc_process.pid if rpc_process else None,
        "ollama_active": is_ollama_running()
    })


@app.route('/health', methods=['GET'])
def health():
    return jsonify({
        "ok": True,
        "rpc_active": rpc_process is not None,
        "ollama_active": is_ollama_running()
    })

if __name__ == '__main__':
    # Run on all interfaces, port 5002
    print("🤖 Melchior Agent Listening on Port 5002...")
    app.run(host='0.0.0.0', port=5002)
