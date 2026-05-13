# EPUB Bridge for MAGI
# Provides EPUB reading and chapter extraction

import os
import logging
from ebooklib import epub
from ebooklib import ITEM_DOCUMENT

logger = logging.getLogger("EPUBBridge")


def extract_chapters(epub_path: str) -> list:
    """
    Extract chapter content from an EPUB file.
    
    Args:
        epub_path: Path to the EPUB file
    
    Returns:
        List of dicts with chapter title and content
    """
    try:
        logger.info(f"📚 Reading EPUB: {epub_path}")
        book = epub.read_epub(epub_path)
        
        chapters = []
        
        for item in book.get_items():
            if item.get_type() == ITEM_DOCUMENT:
                # Get raw HTML content
                content = item.get_body_content()
                if content:
                    # Simple HTML to text conversion
                    import re
                    text = content.decode('utf-8', errors='ignore')
                    # Remove HTML tags
                    text = re.sub(r'<[^>]+>', ' ', text)
                    # Clean up whitespace
                    text = re.sub(r'\s+', ' ', text).strip()
                    
                    if len(text) > 50:  # Skip very short items
                        chapters.append({
                            "title": item.get_name() or f"Chapter {len(chapters) + 1}",
                            "content": text,
                            "length": len(text)
                        })
        
        logger.info(f"✅ Extracted {len(chapters)} chapters")
        return chapters
        
    except Exception as e:
        logger.error(f"❌ EPUB extraction error: {e}")
        return []


def get_epub_info(epub_path: str) -> dict:
    """
    Get metadata about an EPUB file.
    
    Returns:
        Dictionary with title, author, language, etc.
    """
    try:
        book = epub.read_epub(epub_path)
        
        title = book.get_metadata('DC', 'title')
        author = book.get_metadata('DC', 'creator')
        language = book.get_metadata('DC', 'language')
        
        return {
            "title": title[0][0] if title else "Unknown",
            "author": author[0][0] if author else "Unknown",
            "language": language[0][0] if language else "Unknown",
            "chapters": len([i for i in book.get_items() if i.get_type() == ITEM_DOCUMENT])
        }
    except Exception as e:
        return {"error": str(e)}


def summarize_epub(epub_path: str, max_chapters: int = 0) -> str:
    """
    Extract and summarize an EPUB file using Casper.

    Args:
        epub_path: Path to the EPUB file
        max_chapters: Maximum chapters (0 = env var or unlimited)

    Returns:
        Summary of the EPUB content
    """
    from skills.bridge.grounded_ai import chat_casper
    from skills.documents.vector_pipeline import ingest_text_to_vector_memory

    try:
        # Get book info
        info = get_epub_info(epub_path)

        if "error" in info:
            return f"[EPUB 讀取失敗: {info['error']}]"

        # Extract chapters
        chapters = extract_chapters(epub_path)

        if not chapters:
            return "[EPUB 內容提取失敗或檔案為空]"

        # ── Limits from env (0 = unlimited) ──
        env_max_ch = int(os.environ.get("MAGI_EPUB_MAX_CHAPTERS", "0") or "0")
        env_max_chars = int(os.environ.get("MAGI_EPUB_SUMMARY_MAX_CHARS", "0") or "0")
        env_excerpt = int(os.environ.get("MAGI_EPUB_CHAPTER_EXCERPT", "0") or "0")

        if max_chapters <= 0:
            max_chapters = env_max_ch if env_max_ch > 0 else len(chapters)
        max_chars = env_max_chars if env_max_chars > 0 else 999_999_999
        excerpt_limit = env_excerpt if env_excerpt > 0 else 999_999_999

        # ── Vector ingest: full text, no truncation ──
        try:
            full_text = "\n\n".join(
                f"## {ch['title']}\n{ch['content']}" for ch in chapters
            )
            chunk_chars = int(os.environ.get("MAGI_FILE_VECTOR_CHUNK_CHARS", "1200") or "1200")
            auto_max = max(20, (len(full_text) // max(1, chunk_chars)) + 10)
            hard_max = int(os.environ.get("MAGI_FILE_VECTOR_MAX_CHUNKS_HARD", "99999") or "99999")
            vec_max = min(hard_max, auto_max)
            import threading as _th
            _th.Thread(
                target=lambda: ingest_text_to_vector_memory(
                    kind="epub", primary=epub_path,
                    title=f"{info.get('title', '')} - {info.get('author', '')}",
                    text=full_text, chunk_chars=chunk_chars,
                    overlap=int(os.environ.get("MAGI_FILE_VECTOR_OVERLAP", "120") or "120"),
                    max_chunks_total=vec_max,
                ),
                daemon=True, name="epub-vector-ingest",
            ).start()
            logger.info("📚 EPUB vector ingest started (%d chars, max %d chunks)", len(full_text), vec_max)
        except Exception as e:
            logger.warning("EPUB vector ingest skipped: %s", e)

        # Build content for summarization
        content_parts = [
            f"書名: {info['title']}",
            f"作者: {info['author']}",
            f"語言: {info['language']}",
            f"章節數: {info['chapters']}",
            "",
            "--- 內容節錄 ---"
        ]

        total_chars = 0

        for i, chapter in enumerate(chapters[:max_chapters]):
            if total_chars > max_chars:
                content_parts.append("\n[... 內容過長，已截斷 ...]")
                break

            excerpt = chapter['content'][:excerpt_limit]
            content_parts.append(f"\n**{chapter['title']}**\n{excerpt}")
            total_chars += len(excerpt)

        full_content = "\n".join(content_parts)
        
        # Generate summary via Casper
        prompt = f"""請摘要以下電子書的內容，用繁體中文說明：

{full_content}

請提供：
1. 書籍主旨/類型
2. 主要內容概述
3. 推薦給什麼樣的讀者"""

        logger.info("🧠 Sending to Casper for summarization...")
        summary = chat_casper(prompt)
        
        return f"📚 **EPUB 摘要**\n\n📖 {info['title']} - {info['author']}\n\n{summary}"
        
    except Exception as e:
        logger.error(f"❌ EPUB summarization error: {e}")
        return f"[EPUB 摘要失敗: {str(e)}]"
