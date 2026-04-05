import logging

import mysql.connector
import os
import sys
import json
import argparse
from datetime import datetime

# --- Load .env for subprocess/cron credential access ---
try:
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv()
except Exception:
    logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 13, exc_info=True)


# DATABASE CONFIG (Keeper - Dell) — credentials from environment
DB_CONFIG = {
    'user': os.environ.get('DB_USER', ''),
    'password': os.environ.get('DB_PASSWORD', ''),
    'host': os.environ.get('DB_HOST', '127.0.0.1'),
    'database': os.environ.get('MAGI_REMOTE_DB_NAME', 'law_firm_data'),
}

def store_crawler_data(title, summary, url, source="Casper Crawler"):
    try:
        conn = mysql.connector.connect(**DB_CONFIG)
        cursor = conn.cursor()
        
        # TARGET TABLE: legal_news matches requested schema best
        # Fields: title, snippet (as summary), url, source, crawled_at
        sql = """
            INSERT INTO legal_news (title, snippet, url, source, crawled_at) 
            VALUES (%s, %s, %s, %s, NOW())
        """
        cursor.execute(sql, (title, summary, url, source))
        conn.commit()
        
        return {"success": True, "message": "Crawler data stored.", "id": cursor.lastrowid}
    except Exception as e:
        return {"success": False, "error": str(e)}
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close()
            conn.close()

if __name__ == "__main__":
    if len(sys.argv) < 4:
        print(json.dumps({"error": "Usage: python store_crawler.py <title> <summary> <url> [source]"}))
        sys.exit(1)
        
    title = sys.argv[1]
    summary = sys.argv[2]
    url = sys.argv[3]
    source = sys.argv[4] if len(sys.argv) > 4 else "Casper Crawler"
    
    print(json.dumps(store_crawler_data(title, summary, url, source), ensure_ascii=False))
