import os
import requests
import base64
import logging
import json
import re

logger = logging.getLogger("osc-orchestrator-vision")

def extract_events_from_pdf(pdf_path: str, timeout_sec: int = 60) -> list:
    """
    Extracts events (hearings, deadlines) from a PDF using GPT-4o / Claude Vision API.
    Returns a list of todo dicts compatible with insert_case_todos.
    """
    if not pdf_path or not os.path.exists(pdf_path):
        return []
        
    try:
        import fitz
    except ImportError:
        logger.error("fitz (PyMuPDF) not installed.")
        return []

    try:
        doc = fitz.open(pdf_path)
        if doc.needs_pass:
            try:
                doc.authenticate("3800")
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 29, exc_info=True)
                
        # Only analyze the first 2 pages to save tokens and time
        scan_depth = min(2, doc.page_count)
        if scan_depth == 0:
            return []
            
        images_content = []
        for i in range(scan_depth):
            page = doc[i]
            pix = page.get_pixmap(dpi=150)
            png_bytes = pix.tobytes("png")
            b64 = base64.b64encode(png_bytes).decode("utf-8")
            images_content.append({
                "type": "image_url",
                "image_url": {
                    "url": f"data:image/png;base64,{b64}"
                }
            })
            
        model = os.environ.get("MAGI_OMLX_VISION_MODEL", os.environ.get("MAGI_VISION_MODEL", "GLM-OCR-bf16"))
        _vision_base = os.environ.get("MAGI_OMLX_VISION_URL", "http://127.0.0.1:8082").rstrip("/")
        url = f"{_vision_base}/v1/chat/completions"
        
        prompt = '''請分析這幾張台灣法院/機關文件圖片（通常是開庭通知、裁定、補正函等）。
請萃取「必須記錄在行事曆上」的待辦事項。通常包含：
1. 「開庭」：提取日期(YYYY-MM-DD)、時間(HH:MM)、與法庭/股別地點。
2. 「補正/期限」：提取具體的繳報、補正或陳述意見的期限日期(YYYY-MM-DD)。

請根據上述原則，回傳一個 JSON Array，每一個元素都是一個事件({} )。若無任何待辦，回傳空陣列 []。
每一個元素的欄位：
- type (字串): 填入 "開庭" 或 "補正" 或 "繳費" 或 "陳述意見" 或 "待辦"。
- date (字串): 格式 "YYYY-MM-DD"。如果找不到明確日期，填入 null。
- time (字串): 格式 "HH:MM"。如果是全天或是沒有特別指定時間（例如期限的最後一天），填入 null。
- description (字串): 一句簡短說明。例如 "第4法庭 (信股)" 或是 "7日內補正委任狀"。

請嚴格只回傳一個合法的 JSON Array，不要有 markdown 符號（例如 ```json）或其他贅字。
範例寫法：
[
  {"type": "開庭", "date": "2025-03-12", "time": "14:30", "description": "第24法庭 (仁股)"},
  {"type": "補正", "date": "2025-02-28", "time": null, "description": "收到後7日內補正裁判費及委任狀"}
]
'''
        
        messages = [
            {
                "role": "user",
                "content": [{"type": "text", "text": prompt}] + images_content
            }
        ]
        
        payload = {
            "model": model,
            "messages": messages,
            "temperature": 0.1,
        }
        
        r = requests.post(url, json=payload, timeout=timeout_sec)
        if r.status_code == 200:
            content = r.json().get("choices", [{}])[0].get("message", {}).get("content", "").strip()
            # Clean up markdown if model incorrectly adds it
            content = re.sub(r"```json\s*", "", content)
            content = re.sub(r"```\s*", "", content)
            
            try:
                events = json.loads(content)
                if isinstance(events, list):
                    return events
            except json.JSONDecodeError as je:
                logger.error(f"Failed to parse Vision JSON: {je} from content: {content}")
        else:
            logger.warning(f"Vision API returned {r.status_code}: {r.text}")
            
    except Exception as e:
        logger.error(f"Vision API extraction failed: {e}")
        
    return []
