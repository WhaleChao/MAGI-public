"""
MAGI Memory Consolidation - 夜間記憶歸類
=====================================
Runs nightly to:
1. Read OpenClaw session logs from the day
2. Use CASPER to categorize and summarize conversations
3. Store consolidated memories in the vector database (Keeper)

Designed to be called by nightly_council.py
"""

import os
import json
import glob
import logging
import requests
from datetime import datetime, timedelta

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("MemoryConsolidation")

# Configuration
OPENCLAW_SESSIONS_DIR = "/Users/ai/.openclaw/agents/main/sessions"
try:
    from api.routing.service_registry import get_service_url as _get_svc_url
    _tools_default = _get_svc_url("tools_api")
    _omlx_chat_url = _get_svc_url("omlx_inference") + "/v1/chat/completions"
except Exception:
    _tools_default = "http://localhost:5003"
    _omlx_chat_url = "http://127.0.0.1:8080/v1/chat/completions"
MAGI_API = os.environ.get("MAGI_API", _tools_default)
MEMORY_CATEGORIES = [
    "user_preferences",   # 用戶偏好
    "task_learned",       # 學到的任務方法
    "important_facts",    # 重要事實
    "context_notes",      # 脈絡筆記
    "decisions_made",     # 做過的決定
]


def get_today_sessions():
    """
    Get all session files modified today.
    """
    sessions = []
    today = datetime.now().date()
    
    pattern = os.path.join(OPENCLAW_SESSIONS_DIR, "*.jsonl")
    for filepath in glob.glob(pattern):
        try:
            mtime = datetime.fromtimestamp(os.path.getmtime(filepath))
            # Get sessions from last 24 hours
            if mtime.date() >= today - timedelta(days=1):
                sessions.append(filepath)
        except Exception as e:
            logger.warning(f"Error checking {filepath}: {e}")
    
    return sessions


def extract_messages_from_session(filepath):
    """
    Extract user and assistant messages from a session file.
    """
    messages = []
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            for line in f:
                try:
                    entry = json.loads(line.strip())
                    # Extract relevant message content
                    if entry.get("type") == "message":
                        role = entry.get("role", "")
                        content = entry.get("content", "")
                        if role in ["user", "assistant"] and content:
                            messages.append({
                                "role": role,
                                "content": content[:500]  # Truncate for processing
                            })
                except json.JSONDecodeError:
                    continue
    except Exception as e:
        logger.error(f"Error reading {filepath}: {e}")
    
    return messages


def summarize_conversations(messages):
    """
    Use CASPER to summarize and categorize conversations.
    Returns categorized memories.
    """
    if not messages:
        return []
    
    # Build conversation summary
    conversation_text = ""
    for msg in messages[:50]:  # Limit to last 50 messages
        role = "用戶" if msg["role"] == "user" else "助理"
        conversation_text += f"{role}: {msg['content']}\n"
    
    if len(conversation_text) < 100:
        return []  # Too short to analyze
    
    # Ask CASPER to categorize
    prompt = f"""請分析以下對話，並提取值得記住的重點（用繁體中文）：

{conversation_text[:3000]}

請輸出 JSON 格式：
{{
  "memories": [
    {{"category": "類別", "content": "記憶內容", "importance": "high/medium/low"}}
  ]
}}

類別可以是: user_preferences(用戶偏好), task_learned(學到的方法), important_facts(重要事實), decisions_made(做過的決定)

只提取真正重要、值得長期記住的資訊。如果沒有值得記住的，回傳空陣列。"""

    try:
        # Using oMLX TAIDE model for summarization (OpenAI-compatible format)
        response = requests.post(
            _omlx_chat_url,
            json={
                "model": os.environ.get("MAGI_TEXT_PRIMARY_MODEL", ""),
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.3,
                "max_tokens": 1024,
                "stream": False,
            },
            timeout=120,
        )

        if response.status_code == 200:
            choices = response.json().get("choices") or []
            result = (choices[0].get("message", {}).get("content", "") if choices else "").strip()
            # Try to extract JSON from response
            try:
                # Find JSON in response
                start = result.find("{")
                end = result.rfind("}") + 1
                if start >= 0 and end > start:
                    json_str = result[start:end]
                    data = json.loads(json_str)
                    return data.get("memories", [])
            except json.JSONDecodeError:
                logger.warning("Failed to parse CASPER response as JSON")
                return []
    except Exception as e:
        logger.error(f"Failed to call CASPER: {e}")
    
    return []


def store_memories(memories):
    """
    Store categorized memories in Keeper's vector database.
    """
    stored = 0
    for mem in memories:
        try:
            # Call MAGI remember endpoint
            response = requests.post(
                f"{MAGI_API}/remember",
                json={
                    "content": f"[{mem['category']}] {mem['content']}",
                    "source": f"nightly_consolidation_{datetime.now().strftime('%Y%m%d')}",
                    "metadata": {
                        "category": mem.get("category", "general"),
                        "importance": mem.get("importance", "medium"),
                        "timestamp": datetime.now().isoformat()
                    }
                },
                timeout=10
            )
            if response.status_code == 200:
                stored += 1
                logger.info(f"Stored memory: [{mem['category']}] {mem['content'][:50]}...")
        except Exception as e:
            logger.error(f"Failed to store memory: {e}")
    
    return stored


def run_consolidation():
    """
    Main consolidation routine.
    Returns a summary report.
    """
    logger.info("🧠 Starting nightly memory consolidation...")
    
    # 1. Get today's sessions
    sessions = get_today_sessions()
    logger.info(f"Found {len(sessions)} session files from today")
    
    if not sessions:
        return "無新對話可歸類"
    
    # 2. Extract messages
    all_messages = []
    for session_file in sessions:
        messages = extract_messages_from_session(session_file)
        all_messages.extend(messages)
    
    logger.info(f"Extracted {len(all_messages)} messages")
    
    if len(all_messages) < 5:
        return "對話量太少，跳過歸類"
    
    # 3. Summarize and categorize
    memories = summarize_conversations(all_messages)
    logger.info(f"Identified {len(memories)} memories to store")
    
    if not memories:
        return "無重要記憶需歸類"
    
    # 4. Store in vector DB
    stored = store_memories(memories)
    
    # Build report
    report = f"""
**🧠 記憶歸類報告**
- 分析對話數: {len(all_messages)}
- 識別記憶: {len(memories)}
- 儲存成功: {stored}

**歸類記憶:**
"""
    for mem in memories:
        report += f"- [{mem.get('category', '一般')}] {mem['content'][:60]}...\n"
    
    return report


if __name__ == "__main__":
    report = run_consolidation()
    print(report)
