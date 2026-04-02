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
    'host': os.environ.get('DB_HOST', '100.121.61.74'),
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

def query_clients(keyword):
    try:
        conn = mysql.connector.connect(**DB_CONFIG)
        cursor = conn.cursor(dictionary=True)
        
        query = "SELECT * FROM clients WHERE (name LIKE %s OR contact_person LIKE %s) LIMIT 10"
        param = f"%{keyword}%"
        cursor.execute(query, (param, param))
        results = cursor.fetchall()
        
        # Datetime serialization fix
        for r in results:
            for k, v in r.items():
                if isinstance(v, datetime):
                    r[k] = v.isoformat()
                    
        return {"success": True, "data": results}
    except Exception as e:
        return {"success": False, "error": str(e)}
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close()
            conn.close()

def add_client(code, name, contact, phone, address):
    try:
        conn = mysql.connector.connect(**DB_CONFIG)
        cursor = conn.cursor()
        
        sql = "INSERT INTO clients (client_code, name, contact_person, phone, address, status) VALUES (%s, %s, %s, %s, %s, 'Active')"
        val = (code, name, contact, phone, address)
        cursor.execute(sql, val)
        conn.commit()
        
        log_audit("ADD_CLIENT", {"code": code, "name": name})
        return {"success": True, "message": f"Client {name} added."}
    except Exception as e:
        return {"success": False, "error": str(e)}
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close()
            conn.close()

def update_client(client_id, field, value):
    try:
        conn = mysql.connector.connect(**DB_CONFIG)
        cursor = conn.cursor()
        
        # Whitelist fields to prevent SQL injection via field name
        valid_fields = ['name', 'contact_person', 'phone', 'address', 'email', 'notes']
        if field not in valid_fields:
            return {"success": False, "error": "Invalid field"}

        sql = f"UPDATE clients SET {field} = %s WHERE id = %s"
        cursor.execute(sql, (value, client_id))
        conn.commit()
        
        log_audit("UPDATE_CLIENT", {"id": client_id, "field": field, "new_value": value})
        return {"success": True, "message": f"Client {client_id} updated."}
    except Exception as e:
        return {"success": False, "error": str(e)}
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close()
            conn.close()

def soft_delete_client(client_id):
    try:
        conn = mysql.connector.connect(**DB_CONFIG)
        cursor = conn.cursor()
        
        # SOFT DELETE ONLY
        sql = "UPDATE clients SET status = 'Inactive' WHERE id = %s"
        cursor.execute(sql, (client_id,))
        conn.commit()
        
        log_audit("SOFT_DELETE_CLIENT", {"id": client_id})
        return {"success": True, "message": f"Client {client_id} marked as Inactive."}
    except Exception as e:
        return {"success": False, "error": str(e)}
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close()
            conn.close()

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest='command')

    # Query
    q_parser = subparsers.add_parser('query')
    q_parser.add_argument('keyword', type=str)

    # Add
    a_parser = subparsers.add_parser('add')
    a_parser.add_argument('--code', required=True)
    a_parser.add_argument('--name', required=True)
    a_parser.add_argument('--contact', default="")
    a_parser.add_argument('--phone', default="")
    a_parser.add_argument('--address', default="")

    # Update
    u_parser = subparsers.add_parser('update')
    u_parser.add_argument('id', type=int)
    u_parser.add_argument('field', type=str)
    u_parser.add_argument('value', type=str)

    # Delete (Soft)
    d_parser = subparsers.add_parser('delete')
    d_parser.add_argument('id', type=int)

    args = parser.parse_args()

    if args.command == 'query':
        print(json.dumps(query_clients(args.keyword), ensure_ascii=False))
    elif args.command == 'add':
        print(json.dumps(add_client(args.code, args.name, args.contact, args.phone, args.address), ensure_ascii=False))
    elif args.command == 'update':
        print(json.dumps(update_client(args.id, args.field, args.value), ensure_ascii=False))
    elif args.command == 'delete':
        print(json.dumps(soft_delete_client(args.id), ensure_ascii=False))
    else:
        print(json.dumps({"error": "Unknown command"}))
