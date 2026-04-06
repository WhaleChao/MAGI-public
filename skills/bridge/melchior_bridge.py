import requests
import logging
import base64
import os
import sys
import re
from api.model_config import TEXT_PRIMARY_MODEL
from skills.bridge.http_pool import get_session as _get_session
from skills.bridge import melchior_client
from skills.bridge.inference_gateway import InferenceGateway
_MAGI_ROOT = os.path.abspath(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

# Configuration
# MELCHIOR_HOST should be the Tailscale IP of the Windows machine
try:
    from api.routing.node_registry import get_node_ip as _get_node_ip
    MELCHIOR_HOST = os.environ.get("MELCHIOR_HOST") or _get_node_ip("melchior") or "100.116.54.16"
except Exception:
    MELCHIOR_HOST = os.environ.get("MELCHIOR_HOST", "100.116.54.16")
MELCHIOR_PORT = os.environ.get("MELCHIOR_PORT", "5002")
MELCHIOR_URL = f"http://{MELCHIOR_HOST}:{MELCHIOR_PORT}/api/generate"
VISION_MODEL = TEXT_PRIMARY_MODEL  # text-model fallback when vision-specific route is unavailable

# Local fallback (oMLX)
try:
    from api.routing.service_registry import get_service_url as _get_svc_url
    LOCAL_OLLAMA_URL = _get_svc_url("omlx_inference") + "/v1/chat/completions"
except Exception:
    LOCAL_OLLAMA_URL = "http://127.0.0.1:8080/v1/chat/completions"
LOCAL_VISION_MODEL = TEXT_PRIMARY_MODEL
REMOTE_TEXT_MODEL = TEXT_PRIMARY_MODEL

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("MelchiorBridge")

def generate_text(prompt, model=REMOTE_TEXT_MODEL):
    """
    Generate text using best available local model.
    Falls back through local model chain.
    """
    logger.info(f"🧠 [Deep] Sending prompt via Melchior client ({model})...")
    # Local-first routing is enforced inside melchior_client.
    result = melchior_client.chat(prompt, model=model, timeout=120)
    if result.get("success"):
        return result.get("response", "").strip()

    logger.error(f"❌ Melchior text generation failed: {result.get('error')}")
    return None


def encode_image(image_path):
    """Encodes an image to base64."""
    if not os.path.exists(image_path):
        logger.error(f"❌ Image not found: {image_path}")
        return None
        
    with open(image_path, "rb") as image_file:
        return base64.b64encode(image_file.read()).decode('utf-8')


def analyze_image_local(image_path, prompt="Describe this image"):
    """
    Local vision analysis chain:
    Primary: GLM-OCR on vision server (port 8082) — best for Chinese OCR
    Fallback: Gemma 4 on text server (port 8080)
    """
    base64_image = encode_image(image_path)
    if not base64_image:
        return "Error: Image not found."

    p_low = (prompt or "").lower()
    is_ocr = bool(re.search(r"(ocr|辨識|文字|讀取|transcribe)", p_low, re.IGNORECASE))

    # ── Primary: GLM-OCR on vision server (port 8082) ──
    # GLM-OCR bf16 is specialized for Chinese document OCR, far superior
    # to quantized Gemma 4 for reading text from screenshots.
    try:
        _chat_omlx = getattr(melchior_client, "_chat_omlx", None)
        _vision_avail = getattr(melchior_client, "_omlx_vision_available", None)
        if callable(_chat_omlx) and callable(_vision_avail) and _vision_avail():
            _default_vm = os.environ.get("MAGI_OMLX_VISION_MODEL", "")
            omlx_model = (
                getattr(melchior_client, "OMLX_OCR_MODEL", _default_vm)
                if is_ocr
                else getattr(melchior_client, "OMLX_VISION_MODEL", _default_vm)
            )
            vision_base = getattr(melchior_client, "OMLX_VISION_BASE", "http://127.0.0.1:8082")
            vision_circuit = getattr(melchior_client, "_OMLX_VISION_CIRCUIT", None)
            vision_lock = getattr(melchior_client, "_OMLX_VISION_LOCK", None)
            logger.info(f"💾 [GLM-OCR] Trying vision model: {omlx_model} on {vision_base}...")
            r = _chat_omlx(
                prompt=prompt, model=omlx_model, timeout=60,
                temperature=0.3, max_tokens=2048, images=[base64_image],
                base_url=vision_base, circuit=vision_circuit, lock=vision_lock,
            )
            if r.get("success") and r.get("response"):
                logger.info(f"✅ GLM-OCR Vision Response from {omlx_model}.")
                return f"[oMLX/{omlx_model}] {r['response'].strip()}"
            logger.debug("GLM-OCR vision returned empty, falling back to Gemma 4...")
    except Exception as e:
        logger.debug(f"GLM-OCR vision failed: {e}, falling back to Gemma 4...")

    # ── Fallback: Gemma 4 on text server (port 8080) ──
    # Note: Gemma 4 4-bit has weak Chinese OCR but can describe image structure.
    logger.info(f"💾 [Gemma4] Trying fallback vision on text server: {TEXT_PRIMARY_MODEL}...")
    payload = {
        "model": TEXT_PRIMARY_MODEL,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}},
            ],
        }],
        "stream": False,
        "max_tokens": 2048,
        "temperature": 0.3,
    }
    try:
        response = _get_session().post(LOCAL_OLLAMA_URL, json=payload, timeout=120)
        if response.status_code == 200:
            choices = response.json().get("choices") or []
            description = (choices[0].get("message", {}).get("content", "") if choices else "").strip()
            if description:
                logger.info(f"✅ Gemma4 fallback vision response.")
                return f"[Gemma4/{TEXT_PRIMARY_MODEL}] {description}"
        logger.warning(f"⚠️ Gemma4 returned empty or error ({response.status_code})")
    except Exception as e:
        logger.warning(f"⚠️ Gemma4 vision failed: {e}")

    return "Local Vision Error: all vision models failed"


def analyze_image(image_path, prompt="Describe this image"):
    """
    Sends an image for analysis using local vision chain.
    Primary: GLM-OCR on vision server (port 8082, best for Chinese OCR)
    Fallback: Gemma 4 on text server (port 8080)
    """
    logger.info(f"👁️ Vision request: {image_path}")
    p = str(prompt or "").strip() or "Describe this image in detail"
    p_low = p.lower()
    is_ocr = bool(re.search(r"(ocr|辨識|文字|讀取|transcribe)", p_low, re.IGNORECASE))
    is_captcha = bool(re.search(r"(captcha|驗證碼|digits|characters)", p_low, re.IGNORECASE))
    task_type = "captcha" if is_captcha else "vision"
    force_local = str(os.environ.get("MAGI_VISION_FORCE_LOCAL", "1") or "1").strip().lower() in {"1", "true", "yes", "on"}
    _default_ocr = os.environ.get("MAGI_OMLX_OCR_MODEL", os.environ.get("MAGI_OMLX_VISION_MODEL", ""))
    model_hint = (
        os.environ.get("MAGI_VISION_OCR_MODEL", _default_ocr)
        if is_ocr
        else os.environ.get("MAGI_VISION_MODEL", _default_ocr)
    )
    try:
        timeout_sec = int(os.environ.get("MAGI_VISION_TIMEOUT_SEC", "90") or "90")
    except Exception:
        timeout_sec = 90

    try:
        gw = InferenceGateway()
        r = gw.vision(
            image_path=image_path,
            prompt=p,
            timeout=max(8, timeout_sec),
            task_type=task_type,
            force_local=force_local,
            model=model_hint,
        )
        if r.get("success"):
            return str(r.get("analysis") or r.get("response") or "").strip()
        logger.error(f"❌ Gateway vision failed: {r.get('error')}. Trying local fallback...")
    except Exception as e:
        logger.error(f"❌ Gateway vision exception: {e}. Trying local fallback...")

    return analyze_image_local(image_path, p)


def melchior_search(query, num_results=5):
    """
    Sends a search request to Melchior's new !search command.
    This uses Melchior's Iron Dome filtered search.
    
    Args:
        query: Search query string
        num_results: Number of results to return (default: 5)
    
    Returns:
        dict: Search results with sources and content
    """
    search_url = f"http://{MELCHIOR_HOST}:{MELCHIOR_PORT}/api/search"
    logger.info(f"🔍 Sending search to Melchior: '{query}'")
    
    try:
        response = _get_session().post(
            search_url,
            json={"query": query, "num_results": num_results},
            timeout=30
        )
        
        if response.status_code == 200:
            results = response.json()
            logger.info(f"✅ Melchior returned {len(results.get('results', []))} results")
            return results
        else:
            logger.error(f"❌ Melchior Search Error: {response.status_code}")
            return {"error": f"Melchior Error: {response.status_code}", "results": []}
            
    except requests.exceptions.ConnectionError:
        logger.error("❌ Failed to connect to Melchior for search")
        return {"error": "Melchior is offline", "results": []}
    except Exception as e:
        logger.error(f"❌ Search Error: {e}")
        return {"error": str(e), "results": []}


def _generate_image_openai(prompt: str, output_path: str = None) -> dict:
    """
    Fallback: generate image via OpenAI Images API (DALL-E 3 / gpt-image-1).
    Requires OPENAI_API_KEY in environment.
    """
    import base64
    import json as _json
    import urllib.request as _urllib

    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        return {"success": False, "error": "OPENAI_API_KEY not set — add it to MAGI/.env to enable image generation"}

    model = os.environ.get("MAGI_IMAGE_MODEL", "dall-e-3")
    size = os.environ.get("MAGI_IMAGE_SIZE", "1024x1024")
    quality = os.environ.get("MAGI_IMAGE_QUALITY", "standard")

    body = _json.dumps({"model": model, "prompt": prompt, "n": 1, "size": size,
                        **({"quality": quality} if model != "dall-e-2" else {})}).encode("utf-8")
    req = _urllib.Request(
        "https://api.openai.com/v1/images/generations",
        method="POST",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        data=body,
    )
    try:
        with _urllib.urlopen(req, timeout=120) as resp:
            data = _json.loads(resp.read().decode("utf-8"))
        item = (data.get("data") or [{}])[0]
        url = item.get("url") or ""
        b64 = item.get("b64_json") or ""

        final_path = output_path or os.path.join(
            f"{_MAGI_ROOT}/static/images", f"gen_{os.urandom(4).hex()}.png"
        )
        os.makedirs(os.path.dirname(final_path), exist_ok=True)

        if b64:
            with open(final_path, "wb") as f:
                f.write(base64.b64decode(b64))
        elif url:
            with _urllib.urlopen(url, timeout=60) as img_resp:
                with open(final_path, "wb") as f:
                    f.write(img_resp.read())
        else:
            return {"success": False, "error": "OpenAI returned no image data"}

        logger.info(f"✅ Image saved via OpenAI ({model}) → {final_path}")
        return {"success": True, "path": final_path, "model": model, "provider": "openai"}
    except Exception as e:
        logger.error(f"❌ OpenAI image generation failed: {e}")
        return {"success": False, "error": f"openai_image_failed: {e}"}


SD_NEGATIVE_PROMPT = (
    "deformed, ugly, blurry, low quality, bad anatomy, disfigured, "
    "poorly drawn, extra limbs, mutated, watermark, text, signature, "
    "out of frame, cropped, worst quality, low resolution"
)


def generate_image(prompt: str, output_path: str = None) -> dict:
    """
    Generate an image from a text prompt.
    Route 1: Melchior Stable Diffusion API (MELCHIOR_HOST:MELCHIOR_PORT)
    Route 2 (fallback): OpenAI Images API (DALL-E 3) via OPENAI_API_KEY
    """
    sd_url = f"http://{MELCHIOR_HOST}:{MELCHIOR_PORT}/api/generate_image"
    logger.info(f"🎨 Image generation request: '{prompt[:80]}'")

    # Enhance prompt with quality tags for better SD output
    enhanced_prompt = f"{prompt}, masterpiece, best quality, highly detailed, photorealistic, 8k"

    melchior_tried = False
    try:
        response = _get_session().post(sd_url, json={
            "prompt": enhanced_prompt,
            "negative_prompt": SD_NEGATIVE_PROMPT,
            "steps": 30,
            "width": 768,
            "height": 768,
        }, timeout=180)
        melchior_tried = True
        if response.status_code == 200:
            result = response.json()
            if result.get("success"):
                if result.get("image_base64"):
                    import base64
                    img_data = base64.b64decode(result["image_base64"])
                    final_path = output_path or os.path.join(
                        f"{_MAGI_ROOT}/static/images", f"gen_{os.urandom(4).hex()}.png"
                    )
                    os.makedirs(os.path.dirname(final_path), exist_ok=True)
                    with open(final_path, "wb") as f:
                        f.write(img_data)
                    logger.info(f"✅ Image saved from Melchior Base64 → {final_path}")
                    return {"success": True, "path": final_path, "model": result.get("model"), "provider": "melchior"}
                elif result.get("url"):
                    image_url = f"http://{MELCHIOR_HOST}:{MELCHIOR_PORT}{result['url']}"
                    img_resp = _get_session().get(image_url, timeout=30)
                    if img_resp.status_code == 200:
                        final_path = output_path or os.path.join(
                            f"{_MAGI_ROOT}/static/images", os.path.basename(result["url"])
                        )
                        os.makedirs(os.path.dirname(final_path), exist_ok=True)
                        with open(final_path, "wb") as f:
                            f.write(img_resp.content)
                        logger.info(f"✅ Image downloaded from Melchior → {final_path}")
                        return {"success": True, "path": final_path, "model": result.get("model"), "provider": "melchior"}
                return {"success": True, "path": result.get("path", ""), "provider": "melchior",
                        "message": "Image on Melchior filesystem"}
        logger.warning(f"⚠️ Melchior SD returned HTTP {response.status_code}, falling back to OpenAI")
    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
        logger.info("ℹ️ Melchior image endpoint unreachable, falling back to OpenAI Images API")
    except Exception as e:
        logger.warning(f"⚠️ Melchior image error: {e}, falling back to OpenAI")

    # Fallback: OpenAI DALL-E / gpt-image
    return _generate_image_openai(prompt, output_path)


def check_health():
    """Check if Melchior is online."""
    result = melchior_client.check_health()
    if result.get("online"):
        return True, f"Online ({result.get('mode', 'unknown')})"
    return False, result.get("mode", "offline")

if __name__ == "__main__":
    if len(sys.argv) > 1:
        img = sys.argv[1]
        print(analyze_image(img))
    else:
        print("Usage: python melchior_bridge.py <image_path>")


def sync_skills(zip_path):
    """
    Uploads a ZIP file of skills to Melchior to keep it in sync.
    Endpoint: POST /api/skills/sync
    """
    if not os.path.exists(zip_path):
        return {"success": False, "error": "ZIP file not found"}

    url = f"http://{MELCHIOR_HOST}:{MELCHIOR_PORT}/api/skills/sync"
    logger.info(f"📤 Syncing skills to Melchior: {zip_path}...")

    try:
        with open(zip_path, 'rb') as f:
            files = {'file': (os.path.basename(zip_path), f, 'application/zip')}
            response = _get_session().post(url, files=files, timeout=300) # 5 min timeout for large uploads

        if response.status_code == 200:
            logger.info("✅ Skills Synced to Melchior Successfully.")
            return response.json()
        else:
            logger.error(f"❌ Skill Sync Failed: {response.text}")
            return {"success": False, "error": f"HTTP {response.status_code}: {response.text}"}

    except Exception as e:
        logger.error(f"❌ Skill Sync Error: {e}")
        return {"success": False, "error": str(e)}
