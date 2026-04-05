import logging

import mysql.connector
import os
import sys
import json
import argparse
from datetime import datetime, timedelta

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
    'host': os.environ.get('OSC_DB_HOST', os.environ.get('MAGI_REMOTE_DB_HOST', '127.0.0.1')),
    'database': os.environ.get('MAGI_REMOTE_DB_NAME', 'law_firm_data'),
}

def log_audit(action, details):
    """Log actions to magi_brain.audit_log (Cross-Database)"""
    try:
        conn = mysql.connector.connect(**DB_CONFIG)
        cursor = conn.cursor()
        # Switch to magi_brain for audit log
        cursor.execute("CREATE DATABASE IF NOT EXISTS magi_brain")
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS magi_brain.audit_log (
                id INT AUTO_INCREMENT PRIMARY KEY,
                action VARCHAR(50),
                details TEXT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cursor.execute("INSERT INTO magi_brain.audit_log (action, details) VALUES (%s, %s)", (action, json.dumps(details)))
        conn.commit()
    except Exception as e:
        print(f"⚠️ Audit Log Failed: {e}", file=sys.stderr)
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close()
            conn.close()

def book_meeting(title, start_time, duration_mins=60, client_name=None, location="事務所"):
    try:
        conn = mysql.connector.connect(**DB_CONFIG)
        cursor = conn.cursor()
        
        # Calculate End Time? Schema might just use duration or have end_time. 
        # Checking previous schema view: `start_time`, `duration`.
        
        sql = """
            INSERT INTO meetings (title, start_time, duration, client_name, location, status, created_at)
            VALUES (%s, %s, %s, %s, %s, 'scheduled', NOW())
        """
        # Note: 'title' might not exist in schema, schema had `meeting_type` and `client_name`.
        # Let's map 'title' to 'meeting_type' or 'notes' if title column is missing.
        # Based on Step 1272 output: `case_number`, `case_id`, `client_name`, `client_id`, `meeting_type`, `start_time`, `duration`, `location`, `notes`.
        # I will use `meeting_type` for the title/type and `notes` for details.
        
        # Confirmed Schema: type, client_name, datetime are MANDATORY.
        # created_at -> created_date (default current_timestamp)
        
        actual_sql = """
            INSERT INTO meetings (type, client_name, datetime, duration, location, status)
            VALUES (%s, %s, %s, %s, %s, 'scheduled')
        """
        
        # Default client_name if None
        if not client_name:
            client_name = "Internal"
            
        val = (title, client_name, start_time, duration_mins, location)
        cursor.execute(actual_sql, val)
        conn.commit()
        
        meeting_id = cursor.lastrowid
        log_audit("BOOK_MEETING", {"id": meeting_id, "title": title, "start": start_time})
        
        return {"success": True, "message": f"Meeting booked.", "id": meeting_id}
    except Exception as e:
        return {"success": False, "error": str(e)}
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close()
            conn.close()

def list_meetings(date_str=None):
    try:
        conn = mysql.connector.connect(**DB_CONFIG)
        cursor = conn.cursor(dictionary=True)
        
        if date_str:
            sql = "SELECT * FROM calendar_events WHERE DATE(start_date) = %s ORDER BY start_date"
            params = (date_str,)
        else:
            # Default to today onwards
            sql = "SELECT * FROM calendar_events WHERE start_date >= CURRENT_DATE ORDER BY start_date LIMIT 10"
            params = ()
            
        cursor.execute(sql, params)
        results = cursor.fetchall()
        
        # Map columns to expected format for Orchestrator
        mapped_results = []
        for r in results:
            # Check if start_date is datetime object
            dt_val = r.get('start_date')
            if isinstance(dt_val, datetime):
                dt_str = dt_val.isoformat()
            else:
                dt_str = str(dt_val)

            # Map to standard keys used by Orchestrator
            # Orchestrator expects: datetime, type, client_name, location
            mapped_item = {
                "datetime": dt_str,
                "type": r.get('title', 'Event'),
                "client_name": r.get('case_number', ''), # Map case_number to client_name slot
                "location": r.get('location', ''),
                "description": r.get('description', ''),
                "original_id": r.get('id')
            }
            mapped_results.append(mapped_item)
                     
        return {"success": True, "data": mapped_results}
    except Exception as e:
        return {"success": False, "error": str(e)}
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close()
            conn.close()

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest='command')

    # Book
    b_parser = subparsers.add_parser('book')
    b_parser.add_argument('--title', required=True, help="Meeting Type/Title")
    b_parser.add_argument('--start', required=True, help="YYYY-MM-DD HH:MM:SS")
    b_parser.add_argument('--duration', type=int, default=60)
    b_parser.add_argument('--client', default=None)
    b_parser.add_argument('--location', default="事務所")

    # List
    l_parser = subparsers.add_parser('list')
    l_parser.add_argument('--date', help="YYYY-MM-DD")

    args = parser.parse_args()

    if args.command == 'book':
        print(json.dumps(book_meeting(args.title, args.start, args.duration, args.client, args.location), ensure_ascii=False))
    elif args.command == 'list':
        print(json.dumps(list_meetings(args.date), ensure_ascii=False))
    else:
        print(json.dumps({"error": "Unknown command"}))
