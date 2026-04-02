import os
import sys
import logging
import zipfile
import shutil
from flask import Flask, request, jsonify
import requests

# ---------------- CONFIGURATION ----------------
PORT = 5002
OLLAMA_URL = "http://localhost:11434"
DEFAULT_MODEL = "qwen2.5:7b"  # Balthasar's preferred lightweight model
SKILLS_DIR = os.path.expanduser("~/AI/MAGI/skills")
# ------------------------------------------------

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("BalthasarAgent")

@app.route('/health', methods=['GET'])
def health():
    return jsonify({
        "status": "online", 
        "service": "balthasar",
        "role": "Diplomat & Pragmatist"
    })

@app.route('/api/chat', methods=['POST'])
def chat():
    """
    Proxy chat requests to Ollama (For Night Talk Voting).
    """
    data = request.json
    prompt = data.get("prompt", "")
    model = data.get("model", DEFAULT_MODEL)
    options = data.get("options", {})
    
    # Forward to Ollama
    try:
        payload = {
            "model": model,
            "prompt": prompt,
            "stream": False,
            "options": options
        }
        resp = requests.post(f"{OLLAMA_URL}/api/generate", json=payload)
        if resp.status_code == 200:
            return jsonify({"response": resp.json().get("response", "")})
        else:
            return jsonify({"response": f"Error: Ollama {resp.status_code}"}), 500
    except Exception as e:
        return jsonify({"response": f"Error: {str(e)}"}), 500

@app.route('/api/skills/sync', methods=['POST'])
def sync_skills():
    """
    Receives a ZIP file of skills from Casper and extracts them.
    (Knowledge Transfer Protocol)
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
    logger.info(f"🍏 Balthasar Agent v2 Listening on Port {PORT}")
    app.run(host='0.0.0.0', port=PORT)
