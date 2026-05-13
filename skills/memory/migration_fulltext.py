import mysql.connector
from skills.memory.setup_rag_db import DB_CONFIG

def add_fulltext_index():
    try:
        conn = mysql.connector.connect(**DB_CONFIG)
        cursor = conn.cursor()
        print("Adding FULLTEXT index to documents.content...")
        # Check if index exists implies checking information_schema, skipping for brevity, try/catch
        try:
            cursor.execute("ALTER TABLE documents ADD FULLTEXT(content)")
            conn.commit()
            print("✅ FULLTEXT index added.")
        except mysql.connector.Error as err:
            if "Duplicate key name" in str(err):
                print("ℹ️ FULLTEXT index already exists.")
            else:
                print(f"❌ Error adding index: {err}")
    except Exception as e:
        print(f"❌ Connection Error: {e}")
    finally:
        if 'conn' in locals() and conn.is_connected():
            conn.close()

if __name__ == "__main__":
    add_fulltext_index()
