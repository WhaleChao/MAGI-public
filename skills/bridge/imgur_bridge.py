"""
IMGUR BRIDGE MODULE
===================
Handles anonymous image uploading to Imgur.
Returns permanent HTTPS URLs for LINE integration.
"""

import requests
import logging
import os
import base64

from skills.bridge.http_pool import get_session as _get_session

# Configuration
IMGUR_CLIENT_ID = os.environ.get("IMGUR_CLIENT_ID", "d3101526084ac92") # Verified Working ID
IMGUR_API_URL = "https://api.imgur.com/3/image"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ImgurBridge")

def upload_image(image_path: str) -> dict:
    """
    Uploads an image to Imgur anonymously.
    
    Args:
        image_path: Absolute path to the image file.
        
    Returns:
        dict: {"success": bool, "link": str, "error": str}
    """
    if not os.path.exists(image_path):
        return {"success": False, "error": f"File not found: {image_path}"}
        
    try:
        logger.info(f"📤 Uploading to Imgur: {image_path}")
        
        headers = {
            "Authorization": f"Client-ID {IMGUR_CLIENT_ID}"
        }
        
        with open(image_path, "rb") as file:
            files = {"image": file}
            response = _get_session().post(IMGUR_API_URL, headers=headers, files=files, timeout=60)
        
        if response.status_code == 200:
            data = response.json()
            if data.get("success"):
                link = data["data"]["link"]
                logger.info(f"✅ Imgur Upload Success: {link}")
                return {"success": True, "link": link, "error": None}
            else:
                error_msg = f"Imgur API Error: {data.get('status')} - {data.get('data', {}).get('error')}"
                logger.error(f"❌ {error_msg}")
                return {"success": False, "error": error_msg}
        else:
            error_msg = f"HTTP Error {response.status_code}: {response.text}"
            logger.error(f"❌ {error_msg}")
            return {"success": False, "error": error_msg}
            
    except Exception as e:
        logger.error(f"❌ Upload Exception: {e}")
        return {"success": False, "error": str(e)}

if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        print(upload_image(sys.argv[1]))
    else:
        print("Usage: python imgur_bridge.py <image_path>")
