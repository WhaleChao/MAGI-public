import logging
import os
import mysql.connector
import sys

# --- Load .env for subprocess/cron credential access ---
try:
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv()
except Exception:
    logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 9, exc_info=True)


CONFIG = {
    'user': os.environ.get("DB_USER", "casper_service"),
    'password': os.environ.get("DB_PASSWORD", ""),
    'host': os.environ.get("DB_HOST", "127.0.0.1"),
    'database': 'magi_brain',
    'raise_on_warnings': True
}

def check_connection():
    try:
        print(f"Connecting to Keeper at {CONFIG['host']}...")
        cnx = mysql.connector.connect(**CONFIG)
        print("✅ Connection successful!")
        
        cursor = cnx.cursor()
        cursor.execute("SHOW TABLES;")
        print("Tables in magi_brain:")
        for (table,) in cursor:
            print(f"- {table}")
            
        cursor.close()
        cnx.close()
    except mysql.connector.Error as err:
        print(f"❌ Connection failed: {err}")
        sys.exit(1)

if __name__ == "__main__":
    check_connection()
