# Melchior Server Fix (Windows Encoding & 404 Fix)

Melchior 報錯 `'cp950' codec can't encode character`，是因為 Windows 主控台不支援 Emoji 顯示。
此外，`/health` 端點似乎也遺失了。

請將 `K:\MAGI\melchior_server.py` 替換為以下內容 (移除了 Emoji，並補上 health check)：

```python
import os
import sys
import json
import requests
import base64
from datetime import datetime
from flask import Flask, request, jsonify

app = Flask(__name__)

# Config
SD_URL = "http://127.0.0.1:7860/sdapi/v1/txt2img"
PORT = 5002

@app.route('/health', methods=['GET'])
def health():
    return jsonify({"status": "online", "node": "melchior"})

@app.route('/api/generate_image', methods=['POST'])
def generate_image():
    """Generate image using local Stable Diffusion."""
    try:
        data = request.get_json()
        if not data:
            return jsonify({"success": False, "error": "No JSON data"}), 400
            
        prompt = data.get('prompt')
        if not prompt:
            return jsonify({"success": False, "error": "No prompt"}), 400
            
        # Defaults
        negative_prompt = data.get('negative_prompt', "")
        steps = int(data.get('steps', 20))
        width = int(data.get('width', 512))
        height = int(data.get('height', 512))
        cfg_scale = float(data.get('cfg_scale', 7.0))
        
        # Payload
        payload = {
            "prompt": prompt,
            "negative_prompt": negative_prompt,
            "steps": steps,
            "width": width,
            "height": height,
            "cfg_scale": cfg_scale,
            "sampler_name": "Euler a",
            "batch_size": 1,
            "n_iter": 1
        }
        
        # Log without Emoji for Windows CP950 compatibility
        print(f"[Melchior] Generating: {prompt[:50]}...")
        
        try:
            response = requests.post(SD_URL, json=payload, timeout=120)
        except Exception as conn_err:
             return jsonify({"success": False, "error": f"SD Connection Error: {str(conn_err)}"}), 502

        if response.status_code == 200:
            try:
                r = response.json()
            except:
                return jsonify({"success": False, "error": "SD API response is not JSON"}), 500
            
            if isinstance(r, dict) and 'images' in r:
                image_data = r['images'][0]
                
                # Save locally
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                filename = f"gen_{timestamp}.png"
                # Save to absolute path K:\MAGI\static\images\
                base_dir = os.path.dirname(os.path.abspath(__file__))
                # If script is in K:\MAGI, then static is K:\MAGI\static
                # If script is K:\MAGI\api\..., adjust accordingly. 
                # Safer: assume current working directory is root
                static_dir = os.path.join(os.getcwd(), "static", "images")
                os.makedirs(static_dir, exist_ok=True)
                
                filepath = os.path.join(static_dir, filename)
                
                with open(filepath, "wb") as f:
                    f.write(base64.b64decode(image_data))
                    
                path_abs = os.path.abspath(filepath)
                print(f"[Melchior] Saved to: {path_abs}")

                return jsonify({
                    "success": True, 
                    "path": path_abs,
                    "url": f"/static/images/{filename}",
                    "model": "current"
                })
            else:
                return jsonify({"success": False, "error": "No images in SD response", "details": str(r)[:200]}), 500
        else:
            return jsonify({"success": False, "error": f"SD API HTTP {response.status_code}", "body": response.text[:200]}), 500

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"success": False, "error": f"Exception: {str(e)}"}), 500

if __name__ == '__main__':
    print(f"[Melchior] Starting on port {PORT}...")
    app.run(host='0.0.0.0', port=PORT)
```
