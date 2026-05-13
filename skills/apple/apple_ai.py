"""
APPLE AI TOOLS (蘋果智能工具)
=============================
Provides Python wrappers for Apple's on-device AI capabilities via Shortcuts.
Includes: Speech-to-Text (STT), PDF Text Extraction, OCR.
"""

import subprocess
import sys
import json
import tempfile
import os

# =============================================================================
# Speech-to-Text (語音辨識)
# =============================================================================

def speech_to_text(duration_seconds: int = 10) -> dict:
    """
    Record audio and transcribe using Apple's on-device speech recognition.
    
    Requires Shortcut: "MAGI 語音辨識"
    
    Args:
        duration_seconds: Recording duration (max 30s recommended)
        
    Returns:
        {"success": bool, "text": str, "error": str}
    """
    try:
        result = subprocess.run(
            ['shortcuts', 'run', 'MAGI 語音辨識', '-i', str(duration_seconds)],
            capture_output=True,
            text=True,
            timeout=duration_seconds + 10
        )
        
        if result.returncode == 0 and result.stdout.strip():
            return {"success": True, "text": result.stdout.strip(), "error": ""}
        else:
            return {"success": False, "text": "", "error": result.stderr or "No transcription result"}
            
    except subprocess.TimeoutExpired:
        return {"success": False, "text": "", "error": "Recording timeout"}
    except Exception as e:
        return {"success": False, "text": "", "error": str(e)}


# =============================================================================
# PDF Text Extraction (PDF 文字擷取)
# =============================================================================

def extract_pdf_text(pdf_path: str) -> dict:
    """
    Extract text from a PDF file using Apple's PDFKit.
    
    Requires Shortcut: "MAGI 讀取 PDF"
    
    Args:
        pdf_path: Absolute path to the PDF file
        
    Returns:
        {"success": bool, "text": str, "pages": int, "error": str}
    """
    if not os.path.exists(pdf_path):
        return {"success": False, "text": "", "pages": 0, "error": f"File not found: {pdf_path}"}
    
    try:
        result = subprocess.run(
            ['shortcuts', 'run', 'MAGI 讀取 PDF', '-i', pdf_path],
            capture_output=True,
            text=True,
            timeout=60
        )
        
        if result.returncode == 0 and result.stdout.strip():
            text = result.stdout.strip()
            # Estimate pages (rough: 3000 chars per page)
            pages = max(1, len(text) // 3000)
            return {"success": True, "text": text, "pages": pages, "error": ""}
        else:
            return {"success": False, "text": "", "pages": 0, "error": result.stderr or "Failed to extract PDF text"}
            
    except subprocess.TimeoutExpired:
        return {"success": False, "text": "", "pages": 0, "error": "PDF processing timeout"}
    except Exception as e:
        return {"success": False, "text": "", "pages": 0, "error": str(e)}


# =============================================================================
# OCR (光學字元辨識)
# =============================================================================

def ocr_image(image_path: str) -> dict:
    """
    Extract text from an image using Apple's Live Text (Vision framework).
    
    Requires Shortcut: "MAGI OCR 掃描"
    
    Args:
        image_path: Absolute path to the image file (PNG, JPG, HEIC)
        
    Returns:
        {"success": bool, "text": str, "error": str}
    """
    if not os.path.exists(image_path):
        return {"success": False, "text": "", "error": f"File not found: {image_path}"}
    
    try:
        result = subprocess.run(
            ['shortcuts', 'run', 'MAGI OCR 掃描', '-i', image_path],
            capture_output=True,
            text=True,
            timeout=30
        )
        
        if result.returncode == 0 and result.stdout.strip():
            return {"success": True, "text": result.stdout.strip(), "error": ""}
        else:
            return {"success": False, "text": "", "error": result.stderr or "No text detected in image"}
            
    except subprocess.TimeoutExpired:
        return {"success": False, "text": "", "error": "OCR timeout"}
    except Exception as e:
        return {"success": False, "text": "", "error": str(e)}


def ocr_screenshot() -> dict:
    """
    Take a screenshot and OCR it immediately.
    
    Requires Shortcut: "MAGI 螢幕掃描"
    
    Returns:
        {"success": bool, "text": str, "error": str}
    """
    try:
        result = subprocess.run(
            ['shortcuts', 'run', 'MAGI 螢幕掃描'],
            capture_output=True,
            text=True,
            timeout=30
        )
        
        if result.returncode == 0:
            return {"success": True, "text": result.stdout.strip(), "error": ""}
        else:
            return {"success": False, "text": "", "error": result.stderr or "Screenshot OCR failed"}
            
    except Exception as e:
        return {"success": False, "text": "", "error": str(e)}


# =============================================================================
# Utility: Check if Shortcuts exist
# =============================================================================

REQUIRED_SHORTCUTS = [
    "MAGI 語音辨識",
    "MAGI 讀取 PDF",
    "MAGI OCR 掃描",
    "MAGI 螢幕掃描",
    # Apple Intelligence / extended flows (optional but recommended)
    "MAGI 摘要",
    "MAGI 音檔轉文字",
    # GoodNotes Sync
    "MAGI GoodNotes 建立資料夾",
    "MAGI GoodNotes 匯入文件",
    "MAGI GoodNotes 搜尋",
]

def check_shortcuts() -> dict:
    """Check if all required Apple Shortcuts are installed."""
    try:
        result = subprocess.run(['shortcuts', 'list'], capture_output=True, text=True)
        installed = result.stdout.splitlines()
        
        status = {}
        for shortcut in REQUIRED_SHORTCUTS:
            status[shortcut] = shortcut in installed
        
        all_ready = all(status.values())
        return {"ready": all_ready, "shortcuts": status}
    except Exception:
        return {"ready": False, "shortcuts": {}, "error": "Cannot list shortcuts"}


# =============================================================================
# CLI Interface
# =============================================================================

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage:")
        print("  python apple_ai.py check          # Check if shortcuts are installed")
        print("  python apple_ai.py stt [seconds]  # Speech-to-text")
        print("  python apple_ai.py pdf <path>     # Extract PDF text")
        print("  python apple_ai.py ocr <path>     # OCR image")
        print("  python apple_ai.py screen         # OCR screenshot")
        sys.exit(1)
    
    cmd = sys.argv[1].lower()
    
    if cmd == "check":
        result = check_shortcuts()
        print(json.dumps(result, indent=2, ensure_ascii=False))
        
    elif cmd == "stt":
        duration = int(sys.argv[2]) if len(sys.argv) > 2 else 10
        result = speech_to_text(duration)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        
    elif cmd == "pdf":
        if len(sys.argv) < 3:
            print("Error: Please provide PDF path")
            sys.exit(1)
        result = extract_pdf_text(sys.argv[2])
        print(json.dumps(result, indent=2, ensure_ascii=False))
        
    elif cmd == "ocr":
        if len(sys.argv) < 3:
            print("Error: Please provide image path")
            sys.exit(1)
        result = ocr_image(sys.argv[2])
        print(json.dumps(result, indent=2, ensure_ascii=False))
        
    elif cmd == "screen":
        result = ocr_screenshot()
        print(json.dumps(result, indent=2, ensure_ascii=False))
        
    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)
