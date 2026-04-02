# Melchior Server Fix (Image Generation)

這段程式碼是用來修復 Melchior 上 `server.py` 的 `/api/generate_image` 函數。
目前的錯誤 `'str' object has no attribute 'get'` 顯示程式碼中某個地方把字串誤當成字典操作了。

請在 Melchior 上替換該函數：

```python
@app.route('/api/generate_image', methods=['POST'])
def generate_image():
    """Generate image using local Stable Diffusion provided by user."""
    try:
        data = request.get_json()
        if not data:
            return jsonify({"success": False, "error": "No JSON data provided"}), 400
            
        prompt = data.get('prompt')
        if not prompt:
            return jsonify({"success": False, "error": "No prompt provided"}), 400
            
        # Get parameters with defaults
        negative_prompt = data.get('negative_prompt', "")
        steps = int(data.get('steps', 20))
        width = int(data.get('width', 512))
        height = int(data.get('height', 512))
        cfg_scale = float(data.get('cfg_scale', 7.0))
        model_name = data.get('model', 'realisticVisionV51.safetensors')
        
        # Prepare payload for SD WebUI
        payload = {
            "prompt": prompt,
            "negative_prompt": negative_prompt,
            "steps": steps,
            "width": width,
            "height": height,
            "cfg_scale": cfg_scale,
            "sampler_name": "Euler a",
            "batch_size": 1,
            "n_iter": 1,
            # Critical: override_settings must be a dict
            "override_settings": {
                "sd_model_checkpoint": model_name
            },
            "override_settings_restore_afterwards": True
        }
        
        print(f"🎨 Generating image: {prompt[:50]}... (Model: {model_name})")
        
        # Call SD WebUI API
        # Ensure URL is correct (default 7860)
        sd_url = "http://127.0.0.1:7860/sdapi/v1/txt2img"
        response = requests.post(sd_url, json=payload, timeout=120)
        
        if response.status_code == 200:
            r = response.json()
            
            # Debug: check type
            if isinstance(r, str):
                print(f"❌ SD API returned string instead of dict: {r[:100]}")
                return jsonify({"success": False, "error": "SD API response format error (got string)"}), 500
                
            if 'images' in r:
                image_data = r['images'][0]
                
                # Save locally
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                filename = f"gen_{timestamp}.png"
                filepath = os.path.join("static", "images", filename)
                os.makedirs(os.path.dirname(filepath), exist_ok=True)
                
                with open(filepath, "wb") as f:
                    f.write(base64.b64decode(image_data))
                    
                return jsonify({
                    "success": True, 
                    "path": os.path.abspath(filepath), # Return absolute path for bridge
                    "url": f"/static/images/{filename}",
                    "image_base64": image_data, # Optional: return base64 if needed immediately
                    "model": model_name
                })
            else:
                return jsonify({"success": False, "error": "No images in SD response", "details": r}), 500
        else:
            return jsonify({"success": False, "error": f"SD API HTTP {response.status_code}", "body": response.text}), 500

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"success": False, "error": f"Image generation failed: {str(e)}"}), 500
```

### 修正重點：
1. **型別檢查**：確保 `request.get_json()` 和 `response.json()` 的結果
2. **Override Settings**：正確傳遞 `override_settings` 字典
3. **錯誤處理**：加入 `traceback` 印出詳細錯誤堆疊
4. **絕對路徑**：回傳 `abspath` 方便 Casper 的 Bridge 處理
