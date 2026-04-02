# -*- coding: utf-8 -*-
"""
Cortex Sync Skill (皮質同步)
Iron Dome Audit: ✅ SAFE — Read-only from source DB, write to internal DB

Bridges Source DB (law_firm_data) -> Vector DB (magi_brain)
"""

import mysql.connector
import json
import os
_MAGI_ROOT = os.path.abspath(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
import logging
from skills.memory.mem_bridge import remember

# --- Load .env for subprocess/cron credential access ---
try:
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv()
except Exception:
    logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 20, exc_info=True)

logger = logging.getLogger("CortexSync")

# STATE FILE to track last synced IDs
STATE_FILE = f"{_MAGI_ROOT}/cortex_sync_state.json"

# DB CONFIG (Source: law_firm_data)
SOURCE_DB_CONFIG = {
    'user': os.environ.get("DB_USER", "casper_service"),
    'password': os.environ.get("DB_PASSWORD", ""),
    'host': os.environ.get("DB_HOST", "100.121.61.74"),
    'database': 'law_firm_data',
}

class CortexSync:
    def __init__(self):
        self.state = self._load_state()

    def _load_state(self):
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE, 'r') as f:
                    return json.load(f)
            except Exception:
                return {}
        return {}

    def _save_state(self):
        with open(STATE_FILE, 'w') as f:
            json.dump(self.state, f, indent=2)

    def get_source_connection(self):
        return mysql.connector.connect(**SOURCE_DB_CONFIG)

    def sync_legal_news(self, limit=10):
        """Sync new legal news to memory."""
        last_id = self.state.get('legal_news_last_id', 0)
        added = 0
        
        try:
            conn = self.get_source_connection()
            cursor = conn.cursor(dictionary=True)
            
            cursor.execute("""
                SELECT id, title, snippet, url, published_date, source 
                FROM legal_news 
                WHERE id > %s 
                ORDER BY id ASC 
                LIMIT %s
            """, (last_id, limit))
            
            rows = cursor.fetchall()
            
            for row in rows:
                content = f"法律新聞: {row['title']}\n摘要: {row['snippet']}\n來源: {row['source']} ({row['published_date']})\n連結: {row['url']}"
                
                # Call Memory Bridge (Embed + Store)
                remember(content, source="legal_crawler_news")
                
                last_id = row['id']
                added += 1
                
            self.state['legal_news_last_id'] = last_id
            self._save_state()
            
        except Exception as e:
            logger.error(f"Sync Legal News Error: {e}")
            return f"❌ News Sync Failed: {e}"
        finally:
            if 'conn' in locals() and conn.is_connected():
                cursor.close()
                conn.close()
                
        return added

    def sync_judgments(self, limit=5):
        """Sync new judgments (summary only) to memory."""
        last_id = self.state.get('judgments_last_id', 0)
        added = 0
        
        try:
            conn = self.get_source_connection()
            cursor = conn.cursor(dictionary=True)
            
            cursor.execute("""
                SELECT id, jid, case_number, court_name, summary, judgment_date 
                FROM court_judgments 
                WHERE id > %s 
                ORDER BY id ASC 
                LIMIT %s
            """, (last_id, limit))
            
            rows = cursor.fetchall()
            
            for row in rows:
                content = f"判決書: {row['court_name']} {row['case_number']}\n日期: {row['judgment_date']}\n摘要: {row['summary']}"
                
                # Call Memory Bridge
                remember(content, source="legal_crawler_judgment")
                
                last_id = row['id']
                added += 1
                
            self.state['judgments_last_id'] = last_id
            self._save_state()
            
        except Exception as e:
            logger.error(f"Sync Judgments Error: {e}")
            return f"❌ Judgments Sync Failed: {e}"
        finally:
            if 'conn' in locals() and conn.is_connected():
                cursor.close()
                conn.close()
                
        return added

    def run_sync(self):
        """Run full sync cycle."""
        logger.info("🧠 Starting Cortex Sync...")
        
        n_added = self.sync_legal_news()
        j_added = self.sync_judgments()
        
        msg = f"🧠 Cortex Sync Complete:\n- News: {n_added} items\n- Judgments: {j_added} items"
        logger.info(msg)
        return msg

if __name__ == "__main__":
    syncer = CortexSync()
    print(syncer.run_sync())
