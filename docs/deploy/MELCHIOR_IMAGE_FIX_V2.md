# Melchior Server Fix V2 (Simplified)

如果前一版本仍然報錯，請嘗試這個簡化版。
此版本移除了 `override_settings`，避免因參數格式問題導致的錯誤。

```python
@app.route('/api/generate_image', methods=['POST'])
def generate_image():
    """Generate image using local Stable Diffusion (Simplified Version)."""
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
        
        # Prepare payload for SD WebUI (No override settings)
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
        
        print(f"🎨 Generating image: {prompt[:50]}...")
        
        # Call SD WebUI API
        sd_url = "http://127.0.0.1:7860/sdapi/v1/txt2img"
        try:
            response = requests.post(sd_url, json=payload, timeout=120)
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
                filepath = os.path.join("static", "images", filename)
                os.makedirs(os.path.dirname(filepath), exist_ok=True)
                
                with open(filepath, "wb") as f:
                    f.write(base64.b64decode(image_data))
                    
                path_abs = os.path.abspath(filepath)
                print(f"✅ Image saved to: {path_abs}")

                return jsonify({
                    "success": True, 
                    "path": path_abs,
                    "url": f"/static/images/{filename}",
                    "model": "current_loaded_model"
                })
            else:
                return jsonify({"success": False, "error": "No images in SD response", "details": str(r)[:200]}), 500
        else:
            return jsonify({"success": False, "error": f"SD API HTTP {response.status_code}", "body": response.text[:200]}), 500

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"success": False, "error": f"Image generation exception: {str(e)}"}), 500
```
