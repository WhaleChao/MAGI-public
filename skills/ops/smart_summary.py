# -*- coding: utf-8 -*-
"""
Smart Summary Skill (智能摘要)
Based on ClawHub community pattern: content summarization
Iron Dome Audit: ✅ SAFE — Text processing only

Provides: URL content summarization, text summarization, key point extraction
"""

import logging
import re

logger = logging.getLogger("SmartSummary")


def summarize_text(text, max_length=500):
    """
    Extract key points from text using simple heuristics.
    For LLM-powered summary, delegates to Casper.
    """
    if not text or len(text) < 50:
        return "⚠️ 文字太短，無需摘要。"
    
    # Simple extractive summary: pick sentences with key indicators
    sentences = re.split(r'[。！？\.\!\?]', text)
    sentences = [s.strip() for s in sentences if len(s.strip()) > 10]
    
    if not sentences:
        return text[:max_length]
    
    # Score sentences by position and keywords
    key_indicators = ['重要', '關鍵', '必須', '注意', '結論', '總結', '因此',
                      'important', 'key', 'must', 'conclusion', 'therefore',
                      '第一', '第二', '第三', '首先', '其次', '最後']
    
    scored = []
    for i, s in enumerate(sentences):
        score = 0
        # First and last sentences get bonus
        if i == 0: score += 3
        if i == len(sentences) - 1: score += 2
        # Keyword bonus
        for kw in key_indicators:
            if kw in s:
                score += 2
        # Length bonus (not too short, not too long)
        if 20 < len(s) < 200:
            score += 1
        scored.append((score, s))
    
    # Sort by score, take top sentences
    scored.sort(reverse=True)
    top_sentences = [s for _, s in scored[:5]]
    
    summary = "。\n".join(top_sentences)
    if len(summary) > max_length:
        summary = summary[:max_length] + "..."
    
    return summary


def extract_key_points(text):
    """
    Extract bullet points from text.
    """
    if not text:
        return "⚠️ 無內容可分析。"
    
    # Find existing bullet points or numbered items
    patterns = [
        r'[•·⚫▪]\s*(.+)',      # Bullet points
        r'\d+[\.、\)]\s*(.+)',    # Numbered lists
        r'[-–—]\s*(.+)',          # Dash lists
        r'[★☆✓✔]\s*(.+)',       # Special markers
    ]
    
    points = []
    for pattern in patterns:
        matches = re.findall(pattern, text)
        for m in matches:
            clean = m.strip()
            if len(clean) > 5 and clean not in points:
                points.append(clean)
    
    if points:
        formatted = "\n".join([f"• {p}" for p in points[:15]])
        return f"📌 **重點摘錄** ({len(points)} 點)\n\n{formatted}"
    
    # Fallback: use summary
    return f"📝 **摘要**\n\n{summarize_text(text)}"


def summarize_url(url):
    """
    Fetch URL content and summarize it.
    Uses browser skill for dynamic content.
    """
    try:
        from skills.browser.browser_control import browse_url
        content = browse_url(url)
        if "❌" in content or "🛡️" in content:
            return content
        
        # Extract just the text part
        parts = content.split("\n\n", 1)
        if len(parts) > 1:
            text = parts[1]
        else:
            text = content
        
        summary = summarize_text(text, max_length=800)
        key_points = extract_key_points(text)
        
        return f"🔗 **網頁摘要**: `{url}`\n\n{key_points}\n\n---\n{summary}"
    except Exception as e:
        return f"❌ 網頁摘要失敗: {e}"


def summarize_to_docx(text, *, title="", prefix="summary"):
    """
    將文字摘要並輸出為 docx 表格（段落｜摘要｜原文節錄）。
    Returns dict: {"success": True, "path": ..., "filename": ..., "url": ...}
    """
    if not text or len(text) < 50:
        return {"success": False, "error": "text too short"}

    try:
        from skills.ops.export_docx import export_summary_docx
    except Exception as e:
        return {"success": False, "error": f"export_docx not available: {e}"}

    # Split into paragraphs for section-by-section summary
    paragraphs = re.split(r'\n{2,}', (text or "").strip())
    paragraphs = [p.strip() for p in paragraphs if p.strip() and len(p.strip()) > 20]

    if not paragraphs:
        return {"success": False, "error": "no meaningful paragraphs found"}

    sections = []
    for i, para in enumerate(paragraphs):
        summary = summarize_text(para, max_length=200)
        excerpt = para[:300] + ("..." if len(para) > 300 else "")
        sections.append({
            "heading": f"段落 {i + 1}",
            "summary": summary,
            "excerpt": excerpt,
        })

    return export_summary_docx(sections, title=title, prefix=prefix)


if __name__ == "__main__":
    test = """
    人工智慧（AI）正在快速改變我們的生活方式。第一，它提升了醫療診斷的準確率。
    第二，自動駕駛技術正在成熟。第三，教育領域也受益匪淺。
    然而，我們必須注意AI的倫理問題。重要的是，我們需要建立適當的監管框架。
    結論是，AI將繼續發展，但人類需要保持控制力。
    """
    print(extract_key_points(test))
