---
name: apple
description: Apple ecosystem integration including Calendar/Reminders/Notes, plus Apple on-device (Quartz/Vision) and Apple Intelligence via Shortcuts for summarization and file-based speech-to-text when available.
license: MIT
compatibility: Requires macOS with AppleScript support
metadata:
  author: MAGI-Federation
  version: "1.1"
  sage: casper
---

# Apple Ecosystem Skill

Integration with Apple apps on macOS.

## Capabilities

- **Calendar**: Read and create calendar events
- **Reminders**: Manage reminders and tasks
- **Notes**: Access Apple Notes
- **PDF 判讀（on-device）**: Quartz PDFDocument 抽文字
- **圖片 OCR（on-device）**: Vision VNRecognizeTextRequest
- **摘要（Apple Intelligence）**: 透過捷徑 `MAGI 摘要`（需先建立）
- **音檔轉文字（檔案逐字稿）**: 透過捷徑 `MAGI 音檔轉文字`（需先建立）

## Usage

```python
from skills.apple.calendar_bridge import list_events, create_event
from skills.apple.reminders_bridge import get_reminders
from skills.apple.apple_intelligence import extract_pdf_text_quartz, ocr_image_vision, summarize_text_apple_intelligence, transcribe_audio

# List today's events
events = list_events("2026-02-08")

# Create event
create_event("Meeting with Client", "2026-02-09T10:00:00", duration=60)

# Get reminders
tasks = get_reminders()

# PDF 取字（Quartz）
pdf = extract_pdf_text_quartz("/path/to/file.pdf", max_pages=3)

# OCR（Vision）
ocr = ocr_image_vision("/path/to/image.png")

# Apple Intelligence 摘要（需建立捷徑：MAGI 摘要）
summary = summarize_text_apple_intelligence("請幫我摘要這段文字…")

# 音檔轉文字（需建立捷徑：MAGI 音檔轉文字）
stt = transcribe_audio("/path/to/audio.aiff", engine="apple_intelligence")
```

## Smoke Test

```bash
/Users/ai/Desktop/MAGI/venv/bin/python3 /Users/ai/Desktop/MAGI/scripts/tests/apple_intelligence_smoke_test.py
```

## Files

- `calendar_bridge.py` - Apple Calendar integration
- `reminders_bridge.py` - Reminders integration
- `notes_bridge.py` - Notes integration
- `apple_intelligence.py` - Apple on-device + Apple Intelligence (Shortcuts) best-effort bridge
